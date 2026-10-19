"""Render measured story timelines with path-based text, artwork and marker motion."""
from pathlib import Path
import bisect
import hashlib
import json
import math
import subprocess
import wave
import numpy as np
from PIL import Image, ImageColor, ImageFilter
from watermark_assets import watermark_position
from motion_profile import distance_fraction
import illustration_layers


ENTER_SECONDS, EXIT_SECONDS = .45, .5
STAY_SECONDS = 1.  # shorter pen-up gaps keep the hand in frame
MAX_TILT = 7  # degrees of wrist turn
SHADOW_OPACITY = .16
SHADOW_DIRECTION = np.array([.555, .832])  # shadows fall down and to the right (light from the upper left)
PEN_SHADOW_SLOPE, PEN_SHADOW_OPACITY = .2, .22  # a bare pen's shadow drifts this far per pixel of barrel from the nib


def ease(fraction):
    fraction=float(np.clip(fraction,0,1))
    return fraction*fraction*(3-2*fraction)


def lifted_move(origin,destination,fraction,height=10.):
    """Pen-up move: brief dwell at both ends, min-jerk glide and a small arc lift. Returns (point, lift)."""
    moving=float(np.clip((fraction-.15)/.75,0,1))
    lift=height*math.sin(math.pi*moving)
    return origin+distance_fraction(moving,'minjerk')*(destination-origin)+[0,-lift],lift


def asset_tip(metadata,progress,starts=None):
    """Pen tip on an asset at normalized progress, plus how far the pen is lifted off the paper."""
    segments=metadata['segments']
    starts=starts if starts is not None else [segment['start'] for segment in segments]
    index=bisect.bisect_right(starts,progress)-1
    if index<0:
        return np.array(segments[0]['points'][0],dtype=float),0.
    segment=segments[index]
    points=np.array(segment['points'],dtype=float)
    if progress>segment['end']:
        if index+1==len(segments):
            return points[-1],0.
        following=segments[index+1]
        fraction=(progress-segment['end'])/max(1e-7,following['start']-segment['end'])
        return lifted_move(points[-1],np.array(following['points'][0],dtype=float),fraction)
    fraction=(progress-segment['start'])/max(1e-7,segment['end']-segment['start'])
    if segment['kind'] in ('travel', 'pause'):
        return lifted_move(points[0],points[-1],fraction)
    distances=np.array(segment['lengths'])
    target=distance_fraction(fraction,segment.get('profile','linear'))*distances[-1]
    return np.array([np.interp(target,distances,points[:,0]),np.interp(target,distances,points[:,1])]),0.


def point_on_asset(metadata,progress):
    return asset_tip(metadata,progress)[0]


def bezier(origin,control,destination,fraction):
    return (1-fraction)**2*origin+2*(1-fraction)*fraction*control+fraction**2*destination


class Renderer:
    def __init__(self, job_directory):
        self.root=Path(job_directory).resolve()
        self.work=self.root/'work'
        self.output=self.root/'outputs'
        self.timeline=json.loads((self.work/'timeline.json').read_text())
        self.width=self.timeline['canvas']['width']
        self.height=self.timeline['canvas']['height']
        self.fps=self.timeline['canvas']['fps']
        self.paper=ImageColor.getrgb(self.timeline['paper'])+(255,)
        self.starts=[page['start'] for page in self.timeline['pages']]
        self.artwork={name:Image.open(self.work/'assets'/f'{name}.png').convert('RGBA') for name in self.timeline['assets']}
        self.pixels={name:np.array(image) for name,image in self.artwork.items()}
        self.maps={name:np.load(self.work/'assets'/f'{name}.npz') for name in self.timeline['assets']}
        self.layers={name:illustration_layers.load(self.work/'assets',name) for name in self.timeline['assets'] if (self.work/'assets'/f'{name}-line.png').exists()}
        self.cover=Image.open(self.output/'cover.png').convert('RGBA')
        hand=Image.open(self.timeline['hand']['source']).convert('RGBA')
        scale=self.timeline['hand']['scale']
        self.hand=hand.resize((round(hand.width*scale),round(hand.height*scale)),Image.Resampling.LANCZOS)
        self.nib=np.array(self.timeline['hand']['nib'])*scale
        self.watermark = Image.open(self.timeline['watermark']['source']).convert('RGBA') if self.timeline.get('watermark') else None

    def layer(self,event,time):
        name=event['asset']
        if event.get('caption') or time>=event['end']:
            return self.artwork[name]
        progress=float(np.clip((time-event['start'])/(event['end']-event['start']),0,1))
        if name in self.layers:
            return Image.fromarray(illustration_layers.compose(self.layers[name],progress))
        maps=self.maps[name]
        pixels=self.pixels[name].copy()
        if 'ink' in maps:
            ink_fraction=np.clip((progress-maps['ink'])*250,0,1)
            color_fraction=np.clip((progress-maps['color'])*250,0,1)
            fraction=maps['ink_weight']*ink_fraction+(1-maps['ink_weight'])*color_fraction
        else:
            fraction=np.clip((progress-maps['reveal'])*250,0,1)
        pixels[:,:,3]=np.rint(pixels[:,:,3]*fraction).astype(np.uint8)
        return Image.fromarray(pixels)

    def event_state(self,event,progress):
        cache=self.__dict__.setdefault('segment_starts',{})
        metadata=self.timeline['assets'][event['asset']]
        if event['asset'] not in cache:
            cache[event['asset']]=[segment['start'] for segment in metadata['segments']]
        point,lift=asset_tip(metadata,progress,cache[event['asset']])
        return np.array([event['x'],event['y']])+point,lift

    def event_tip(self,event,progress):
        return self.event_state(event,progress)[0]

    def offstage(self,tip,fraction,leaving):
        """Arc between the tip and a rest point below the frame, bowing up and out like a withdrawing arm."""
        drop=self.height+self.nib[1]+self.hand.width*math.sin(math.radians(MAX_TILT))+40-tip[1]
        away=tip+np.array([.55*drop,drop])
        chord=away-tip
        control=(tip+away)/2+.22*np.array([chord[1],-chord[0]])
        moving=float(distance_fraction(fraction,'minjerk'))
        along=moving if leaving else 1-moving
        return bezier(tip,control,away,along),22*min(1.,3*along)

    def pen_events(self):
        """Every main drawing event of the film in time order with its page: one physical pen throughout."""
        if 'pen_event_list' not in self.__dict__:
            events=[(event,index) for index,page in enumerate(self.timeline['pages']) for event in page.get('events',[]) if not event['caption']]
            self.pen_event_list=sorted(events,key=lambda item:item[0]['start'])
            self.pen_event_starts=[event['start'] for event,_ in self.pen_event_list]
        return self.pen_event_list

    def pen_state(self,time):
        """(tip, lift, page) of the pen, or None while the hand is out of frame."""
        events=self.pen_events()
        index=bisect.bisect_right(self.pen_event_starts,time)-1
        previous=events[index] if index>=0 else None
        following=events[index+1] if index+1<len(events) else None
        if previous and time<=previous[0]['end']:
            event=previous[0]
            return (*self.event_state(event,(time-event['start'])/(event['end']-event['start'])),previous[1])
        leave,enter=EXIT_SECONDS,ENTER_SECONDS
        if previous and following:
            gap=following[0]['start']-previous[0]['end']
            origin,target=self.event_tip(previous[0],1),self.event_tip(following[0],0)
            if gap<=STAY_SECONDS and previous[1]==following[1]:
                point,lift=lifted_move(origin,target,(time-previous[0]['end'])/gap,min(18.,8+.02*np.linalg.norm(target-origin)))
                return point,lift,following[1]
            # Across a page turn the hand always leaves and returns; squeeze both arcs into short gaps.
            squeeze=min(1.,gap/(leave+enter))
            leave,enter=leave*squeeze,enter*squeeze
        elif following and not previous:
            enter=float(np.clip(following[0]['start']-self.timeline['cover_seconds'],.15,enter))
        if previous and time<previous[0]['end']+leave:
            return (*self.offstage(self.event_tip(previous[0],1),(time-previous[0]['end'])/leave,True),previous[1])
        if following and following[0]['start']-time<enter:
            return (*self.offstage(self.event_tip(following[0],0),1-(following[0]['start']-time)/enter,False),following[1])
        return None

    def pen(self,page,time):
        state=self.pen_state(time)
        return None if state is None or state[2]!=page['index'] else state[0]

    def sampled_tip(self,time):
        """Quarter-frame cached (tip, page) samples for smoothing wrist rotation and camera follow."""
        cache=self.__dict__.setdefault('tip_cache',{})
        key=round(time*self.fps*4)
        if key not in cache:
            if len(cache)>8192:
                cache.clear()
            state=self.pen_state(key/(self.fps*4))
            cache[key]=None if state is None else (state[0],state[2])
        return cache[key]

    def hand_pose(self,angle):
        """Hand, soft shadow, nib and cut-edge points rotated about the nib; cached per whole degree."""
        cache=self.__dict__.setdefault('hand_poses',{})
        if angle not in cache:
            hand=self.hand.rotate(angle,Image.Resampling.BICUBIC,expand=True) if angle else self.hand
            radians=math.radians(angle)
            turn=np.array([[math.cos(radians),-math.sin(radians)],[math.sin(radians),math.cos(radians)]])
            nib=(self.nib-np.array(self.hand.size)/2)@turn+np.array(hand.size)/2
            alpha=np.array(self.hand.getchannel('A'))
            ys,xs=np.nonzero(alpha>128)
            border=(ys==0)|(xs==0)|(ys==alpha.shape[0]-1)|(xs==alpha.shape[1]-1)
            cut=(np.c_[xs[border],ys[border]][::6]-self.nib)@turn
            radius=max(2,round(self.height*.006))
            pad=3*radius
            if self.timeline.get('hand',{}).get('kind')=='pen':
                shadow=self.pen_shadow(hand,nib,radius,pad)
            else:
                shadow=Image.new('RGBA',(hand.width+2*pad,hand.height+2*pad),(24,22,20,0))
                mask=Image.new('L',shadow.size)
                mask.paste(hand.getchannel('A'),(pad,pad))
                shadow.putalpha(mask.filter(ImageFilter.GaussianBlur(radius)).point(lambda value:round(value*SHADOW_OPACITY)))
            cache[angle]=(hand,shadow,nib,pad,cut)
        return cache[angle]

    def pen_shadow(self,pen,nib,radius,pad):
        """A held pen touches the paper only at its nib: the shadow starts there and falls further off as the barrel rises."""
        ys,xs=np.nonzero(np.array(pen.getchannel('A'))>128)
        axis=np.array([xs.mean(),ys.mean()])-nib
        axis/=np.linalg.norm(axis)
        reach=PEN_SHADOW_SLOPE*float(np.max((np.c_[xs,ys]-nib)@axis))
        extra=np.ceil(reach*SHADOW_DIRECTION).astype(int)
        mask=Image.new('L',(pen.width+2*pad+int(extra[0]),pen.height+2*pad+int(extra[1])))
        mask.paste(pen.getchannel('A'),(pad,pad))
        # Shear about the nib: each point moves along the light by its distance up the barrel (inverse map for PIL).
        origin=nib+pad
        inverse=np.linalg.inv(np.eye(2)+PEN_SHADOW_SLOPE*np.outer(SHADOW_DIRECTION,axis))
        shift=origin-inverse@origin
        mask=mask.transform(mask.size,Image.Transform.AFFINE,(*inverse[0],shift[0],*inverse[1],shift[1]),Image.Resampling.BILINEAR)
        shadow=Image.new('RGBA',mask.size,(24,22,20,0))
        shadow.putalpha(mask.filter(ImageFilter.GaussianBlur(radius)).point(lambda value:round(value*PEN_SHADOW_OPACITY)))
        return shadow

    def hand_angle(self,time,point):
        """Small wrist turn following the smoothed horizontal pen velocity, never exposing the arm's cut edge."""
        # Gaussian-weighted slope of x(t): quick pen jumps blend in gradually instead of flipping the wrist.
        offsets=np.arange(-15,16)/self.fps
        samples=[(offset,tip[0][0]) for offset,tip in ((offset,self.sampled_tip(time+offset)) for offset in offsets) if tip is not None]
        tilt=0.
        if len(samples)>=6:
            offset,x=np.array(samples).T
            weight=np.exp(-.5*(offset/.25)**2)
            offset=offset-np.average(offset,weights=weight)
            velocity=np.sum(weight*offset*(x-np.average(x,weights=weight)))/np.sum(weight*offset**2)
            tilt=MAX_TILT*math.tanh(velocity/(.6*self.width))
        angle=-int(round(tilt))
        for candidate in range(angle,0,-1 if angle>0 else 1):
            cut=point+self.hand_pose(candidate)[4]
            if not ((cut[:,0]>=0)&(cut[:,0]<self.width)&(cut[:,1]>=0)&(cut[:,1]<self.height)).any():
                return candidate
        return 0

    def draw_hand(self,frame,time,page=None):
        """Draw the pen if it is in frame (and, when a page is given, only if it belongs to that page)."""
        state=self.pen_state(time)
        if state is None or (page is not None and state[2]!=page):
            return
        point,lift,_=state
        hand,shadow,nib,pad,_=self.hand_pose(self.hand_angle(time,point))
        corner=np.rint(point-nib).astype(int)
        # The shadow stays on the paper while the pen lifts, so it separates slightly from the hand; a bare pen's meets its nib.
        resting=0. if self.timeline.get('hand',{}).get('kind')=='pen' else self.height*np.array([.008,.012])
        offset=np.rint(resting+lift*np.array([.5,1.4])).astype(int)
        frame.alpha_composite(shadow,tuple(corner-pad+offset))
        frame.alpha_composite(hand,tuple(corner))

    def draw_captions(self,frame,index,time):
        for event in sorted(self.timeline['pages'][index]['events'],key=lambda event:(event['z'],event['start'])):
            if event['caption'] and event['start']<=time<event['hide']:
                frame.alpha_composite(self.layer(event,time),(event['x'],event['y']))

    def page_frame(self,index,time,include_hand=True,include_captions=True):
        page=self.timeline['pages'][index]
        frame=Image.new('RGBA',(self.width,self.height),self.paper)
        events=sorted(page['events'],key=lambda event:(event['z'],event['start']))
        for event in events:
            if event['caption']:
                continue
            if event['start']<=time<event['hide']:
                frame.alpha_composite(self.layer(event,time),(event['x'],event['y']))
        if include_hand:
            self.draw_hand(frame,time,index)
        if include_captions:
            self.draw_captions(frame,index,time)
        return frame

    def camera_keys(self,index):
        cache=self.__dict__.setdefault('camera_cache',{})
        if index not in cache:
            page=self.timeline['pages'][index]
            beats={beat['id']:beat['start']-page['start'] for beat in page['beats']}
            keys=[]
            for key in page.get('camera') or []:
                at=key.get('at',0)
                keys.append({'at':beats[at[5:]] if isinstance(at,str) else float(at),'zoom':float(key.get('zoom',1)),
                             'center':np.array(key.get('center',[self.width/2,self.height/2]),dtype=float),
                             'seconds':float(key.get('seconds',.8)),'ease':key.get('ease','inout'),'follow':bool(key.get('follow'))})
            cache[index]=sorted(keys,key=lambda key:key['at'])
        return cache[index]

    def clamp_view(self,center,zoom):
        if zoom<1:
            return center
        half=np.array([self.width,self.height])/(2*zoom)
        return np.clip(center,half,np.array([self.width,self.height])-half)

    def camera(self,index,time):
        """Camera centre and zoom; a key at 0 s frames the page from its start, later keys glide in."""
        page=self.timeline['pages'][index]
        local=time-page['start']
        center,zoom=np.array([self.width/2,self.height/2]),1.
        for key in self.camera_keys(index):
            if local<key['at']:
                break
            target=key['center']
            if key['follow']:
                tips=[tip[0] for tip in (self.sampled_tip(time+.15*step) for step in range(-4,3))
                      if tip is not None and tip[1]==index and 0<=tip[0][1]<=self.height]
                target=target+.5*(np.mean(tips,axis=0)-target) if tips else target
            fraction=1. if key['seconds']<=0 or key['at']<=0 else float(np.clip((local-key['at'])/key['seconds'],0,1))
            weight=fraction if key['ease']=='linear' else float(distance_fraction(fraction,'minjerk'))
            center,zoom=center+weight*(target-center),zoom+weight*(key['zoom']-zoom)
        return self.clamp_view(center,zoom),zoom

    def apply_camera(self,frame,center,zoom):
        if abs(zoom-1)<1e-4:
            return frame
        size=np.array([self.width,self.height])
        box=tuple(np.clip(np.r_[center-size/(2*zoom),center+size/(2*zoom)],0,np.r_[size,size]))
        return frame.convert('RGB').resize((self.width,self.height),Image.Resampling.BICUBIC,box=box).convert('RGBA')

    def view(self,index,time,include_captions=True):
        if not self.timeline['pages'][index].get('camera') and include_captions and not self.foreign_hand(index,time):
            return self.page_frame(index,time)
        frame=self.apply_camera(self.page_frame(index,time,include_captions=False),*self.camera(index,time))
        if include_captions:
            if self.foreign_hand(index,time):
                self.draw_hand(frame,time)
            self.draw_captions(frame,index,time)
        return frame

    def foreign_hand(self,index,time):
        state=self.pen_state(time) if self.timeline['pages'][index].get('events') else None
        return state is not None and state[2]!=index

    def transition(self,index):
        """Window of the move from page index-1 into page index; pages without a transition keep the legacy slide."""
        pages=self.timeline['pages']
        if not 0<index<len(pages):
            return None
        boundary=pages[index-1]['end']
        spec=pages[index].get('transition')
        if not spec:
            return {'type':'legacy','start':boundary-.24,'end':boundary}
        if spec['type']=='cut':
            return None
        seconds=float(spec.get('seconds',.6))
        drawn=[event['start'] for event in pages[index]['events'] if not event['caption']]
        # No ink is laid while the board moves; a leaving or arriving hand rides along with its page.
        room_in=max(0.,min(drawn)-boundary) if drawn else seconds
        room_out=max(0.,boundary-pages[index-1]['last_drawing_end'])
        incoming=min(seconds/2,room_in)
        outgoing=min(seconds-incoming,room_out)
        incoming=min(seconds-outgoing,room_in)
        if outgoing+incoming<2/self.fps:
            return None
        return {'type':spec['type'],'start':boundary-outgoing,'end':boundary+incoming}

    def pan(self,index,window,time,moving):
        """Glide across one continuous board: the next page waits to the right of the current one."""
        offset=round(self.width*1.06)
        start_center,start_zoom=self.camera(index-1,window['start'])
        end_center,end_zoom=self.camera(index,window['end'])
        center=start_center+moving*(end_center+[offset,0]-start_center)
        zoom=(start_zoom+moving*(end_zoom-start_zoom))*(1-.14*math.sin(math.pi*moving))
        board=Image.new('RGB',(offset+self.width,self.height),self.paper[:3])
        board.paste(self.page_frame(index-1,time,include_captions=False).convert('RGB'),(0,0))
        board.paste(self.page_frame(index,time,include_captions=False).convert('RGB'),(offset,0))
        left,top=center-np.array([self.width,self.height])/(2*zoom)
        return board.transform((self.width,self.height),Image.Transform.AFFINE,(1/zoom,0,left,0,1/zoom,top),
                               Image.Resampling.BILINEAR,fillcolor=self.paper[:3]).convert('RGBA')

    def blend(self,index,window,time):
        fraction=(time-window['start'])/(window['end']-window['start'])
        if window['type']=='legacy':
            frame=self.page_frame(index-1,time)
            canvas=Image.new('RGBA',(self.width,self.height),self.paper)
            canvas.alpha_composite(frame,(-round(self.width*ease(fraction)),0))
            self.draw_hand(canvas,time,index)
            return canvas
        moving=float(distance_fraction(fraction,'minjerk'))
        if window['type']=='pan':
            frame=self.pan(index,window,time,moving)
        else:
            old,new=self.view(index-1,time,False),self.view(index,time,False)
            if window['type']=='fade':
                frame=Image.blend(old,new,moving)
            else:
                frame=Image.new('RGBA',(self.width,self.height),self.paper)
                shift=round(self.width*moving)
                frame.alpha_composite(old,(-shift,0))
                frame.alpha_composite(new,(self.width-shift,0))
        self.draw_captions(frame,index if time>=self.timeline['pages'][index-1]['end'] else index-1,time)
        return frame

    def scene(self,index,time):
        for target in (index+1,index):
            window=self.transition(target)
            if window and window['start']<=time<window['end']:
                return self.blend(target,window,time)
        return self.view(index,time)

    def frame(self,time):
        if time<self.timeline['cover_seconds']:
            frame=self.cover.copy()
            if time>.5:
                canvas=Image.new('RGBA',(self.width,self.height),self.paper)
                frame=Image.blend(frame,canvas,(time-.5)/.5)
            return frame.convert('RGB')
        index=min(len(self.starts)-1,max(0,bisect.bisect_right(self.starts,time)-1))
        page=self.timeline['pages'][index]
        frame=self.scene(index,time)
        remaining=page['end']-time
        fade = self.timeline.get('ending', {}).get('fade_seconds', 0)
        if index == len(self.starts) - 1 and fade > 0 and remaining < fade:
            frame = Image.blend(frame, Image.new('RGBA', frame.size, self.paper), ease(1 - remaining / fade))
        if getattr(self, 'watermark', None) is not None and time >= self.timeline['watermark']['start_seconds']:
            frame.alpha_composite(self.watermark, watermark_position(self.timeline['watermark'], self.width, self.height, time))
        return frame.convert('RGB')


def mix_audio(root,timeline):
    root=Path(root)/'work'
    with wave.open(str(root/'narration.wav'),'rb') as stream:
        rate=stream.getframerate()
        samples=np.frombuffer(stream.readframes(stream.getnframes()),dtype='<i2')
    policy = timeline.get('audio_policy', {'background_music': False, 'layers': []})
    normalized_path=root/'normalized-narration.wav'
    # 70 Hz high-pass removes rumble/DC; two-pass linear loudnorm applies one measured gain instead of
    # dynamic pumping. Duplicating mono into two full-level channels adds 3 LU to stereo loudness.
    target='highpass=f=70,loudnorm=I=-20:TP=-2:LRA=7'
    analysis=subprocess.run(['ffmpeg','-hide_banner','-nostats','-i',str(root/'narration.wav'),'-af',target+':print_format=json','-f','null','-'],
                            capture_output=True,text=True,check=True).stderr
    measured=json.loads(analysis[analysis.rindex('{'):analysis.rindex('}')+1])
    second=target+(':measured_I={input_i}:measured_TP={input_tp}:measured_LRA={input_lra}:measured_thresh={input_thresh}'
                   ':offset={target_offset}:linear=true:print_format=json').format(**measured)
    applied=subprocess.run(['ffmpeg','-y','-hide_banner','-nostats','-i',str(root/'narration.wav'),'-af',second,
                            '-ar',str(rate),'-ac','1','-c:a','pcm_s16le',str(normalized_path)],capture_output=True,text=True,check=True).stderr
    result=json.loads(applied[applied.rindex('{'):applied.rindex('}')+1])
    layers = policy.get('layers', [])
    if not layers:
        # No implicit soundtrack and no buffered amix/limiter tail on narration-only output.
        subprocess.run(['ffmpeg','-y','-v','error','-i',str(normalized_path),'-af','pan=stereo|c0=c0|c1=c0',
                        '-c:a','pcm_s16le',str(root/'mix.wav')],check=True)
    else:
        from narration_master import sha256
        command=['ffmpeg','-y','-v','error','-i',str(normalized_path)]
        # Keep the same full-level dual-mono narration with or without effects.
        # Implicit mono-to-stereo negotiation otherwise attenuates it by 3 dB.
        filters=['[0:a]pan=stereo|c0=c0|c1=c0[narration]']
        for index, layer in enumerate(layers, 1):
            if sha256(layer['source']) != layer['sha256']:
                raise ValueError('Audio layer changed after build')
            command.extend(['-i',layer['source']])
            filters.append(f"[{index}:a]aformat=channel_layouts=stereo,volume={layer['gain']},adelay={round(layer['start']*1000)}:all=1[layer{index}]")
        inputs='[narration]'+''.join(f'[layer{index}]' for index in range(1,len(layers)+1))
        filters.append(inputs+f'amix=inputs={len(layers)+1}:duration=first:normalize=0,alimiter=limit=0.89:level=false:latency=true[out]')
        command.extend(['-filter_complex',';'.join(filters),'-map','[out]','-ar',str(rate),'-ac','2',str(root/'mix.wav')])
        subprocess.run(command,check=True)
    (root/'audio-mix-report.json').write_text(json.dumps({'background_music':policy.get('background_music',False),
        'layers':layers,'narration_only':not layers,'narration_filter':target,'loudnorm_first_pass':measured,
        'loudnorm_mode':result['normalization_type'],'delivery_notice':policy.get('delivery_notice',
        '默认未添加背景音乐；可自行合成，或在发布平台选择配乐。')},ensure_ascii=False,indent=2))
    with wave.open(str(root/'mix.wav'),'rb') as mixed_stream:
        mixed_seconds=mixed_stream.getnframes()/mixed_stream.getframerate()
    if abs(mixed_seconds-len(samples)/rate)>.01:
        raise ValueError('Audio mixing changed narration duration; refusing to encode a truncated track')


def aigc_arguments(produce_id):
    """Implicit AI-generated-content label (GB 45438 style keys); platforms may strip it, so delivery notes still ask for an explicit declaration."""
    label={'Label':'1','ContentProducer':'sweety-handdraw-video','ProduceID':produce_id,'ReservedCode1':'','ContentPropagator':'','PropagateID':'','ReservedCode2':''}
    return ['-metadata','AIGC='+json.dumps(label),'-movflags','+faststart+use_metadata_tags']


def encode(renderer,path,start=0.,duration=None):
    duration=renderer.timeline['duration']-start if duration is None else duration
    if start < 0 or duration <= 0 or start+duration > renderer.timeline['duration']+.001:
        raise ValueError('Preview interval must stay within the narration timeline')
    temporary_path=path.with_name(path.stem+'.rendering'+path.suffix)
    command=['ffmpeg','-y','-v','error','-f','rawvideo','-pix_fmt','rgb24','-s',f'{renderer.width}x{renderer.height}','-r',str(renderer.fps),'-i','-','-ss',str(start),'-i',str(renderer.work/'mix.wav'),'-c:v','libx264','-preset','medium','-crf','18','-pix_fmt','yuv420p','-c:a','aac','-b:a','192k','-t',str(duration),'-map_metadata','-1',*aigc_arguments(hashlib.sha256((renderer.root/'job.json').read_bytes()).hexdigest()[:16]),str(temporary_path)]
    process=subprocess.Popen(command,stdin=subprocess.PIPE)
    for index in range(math.ceil(duration*renderer.fps)):
        process.stdin.write(renderer.frame(start+index/renderer.fps).tobytes())
        if index%(15*renderer.fps)==0:
            print('RENDER',index/renderer.fps,'/',round(duration,2),flush=True)
    process.stdin.close()
    if process.wait()!=0:
        raise RuntimeError('ffmpeg failed to encode the video')
    temporary_path.replace(path)



def export_storyboard(renderer):
    review=renderer.work/'review'
    review.mkdir(parents=True,exist_ok=True)
    rows=math.ceil((len(renderer.timeline['pages'])+1)/2)
    sheet=Image.new('RGB',(1280,360*rows),renderer.paper[:3])
    frames=[renderer.cover.convert('RGB')]
    for page in renderer.timeline['pages']:
        frame=renderer.page_frame(page['index'],page['speech_end']-.15,False).convert('RGB')
        frame.save(review/f"page-{page['index']:02d}.jpg",quality=94)
        frames.append(frame)
    for index,frame in enumerate(frames):
        frame.thumbnail((640,360))
        sheet.paste(frame,((index%2)*640,(index//2)*360))
    sheet.save(review/'storyboard-contact.jpg',quality=95)


def render_job(job_directory,start=0.,duration=None,preview=False):
    from video_verifier import build_fingerprint, file_hash, verify_assets
    from art_direction import verify_art_direction
    renderer=Renderer(job_directory)
    verify_art_direction(renderer.root,json.loads((renderer.root/'job.json').read_text()))
    verify_assets(renderer.root,renderer.timeline)
    from narration_master import verify_compiled_master
    verify_compiled_master(renderer.root, renderer.timeline)
    from timeline_contract import validate_timeline,validate_cover
    validate_timeline(renderer.timeline)
    validate_cover(renderer)
    export_storyboard(renderer)
    mix_audio(renderer.root,renderer.timeline)
    path=renderer.work/'review'/'preview.mp4' if preview else renderer.output/'film.mp4'
    if not preview and (start or duration is not None):
        raise ValueError('Use --preview for partial renders; delivery film must include the complete timeline')
    encode(renderer,path,start,duration)
    from subtitle_contract import validate_subtitle_delivery
    validate_subtitle_delivery(path)
    if not preview:
        record={'video_sha256':file_hash(path),'build_fingerprint':build_fingerprint(renderer.root)}
        (renderer.work/'render-record.json').write_text(json.dumps(record,indent=2))
    print(str(path))
    return path

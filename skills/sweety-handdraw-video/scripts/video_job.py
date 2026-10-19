"""Compile content-designed pages against measured narration, not fixed scene lengths."""
from pathlib import Path
import copy
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import wave
import numpy as np
from PIL import Image
import stroke_assets
from hand_assets import prepare_hand
from art_direction import verify_art_direction
from caption_assets import build_caption, resolve_cues
from watermark_assets import prepare_watermark
from narration_master import load_master
from audio_contract import resolve_audio_layers, validate_ending
from fonts import check_coverage, select as select_font
from storyboard_lint import anchor_beat, check_build, group_beats
from timeline_contract import validate_page_motion

PACKAGE = Path(__file__).resolve().parents[1]
SAMPLE_RATE = 48000


def load_manifest(job_directory):
    job_directory = Path(job_directory).resolve()
    manifest = json.loads((job_directory / 'job.json').read_text())
    if manifest.get('version') != 1 or not manifest.get('pages'):
        raise ValueError('job.json requires version=1 and at least one page')
    return job_directory, manifest


def resolve_input(job_directory, value):
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (job_directory / path).resolve()


def find_font(manifest):
    """Handwriting font: job.json font.path/index, else the first covering font of the platform chain (see fonts.py)."""
    return select_font('handwriting', '', manifest.get('font'))


def fix_commands(report):
    """One actionable line per missing item; image generation is a host capability scripts cannot detect."""
    mac = sys.platform == 'darwin'
    fixes = []
    if not all(report['tools'].values()):
        fixes.append('FFmpeg: ' + ('brew install ffmpeg' if mac else 'sudo apt install ffmpeg  (dnf install ffmpeg-free / pacman -S ffmpeg)') + '; or bash scripts/setup.sh')
    if 'font_error' in report:
        fixes.append('Fonts: ' + ('install Hannotate SC in Font Book (or set font.path in job.json)' if mac else
                     'sudo apt install fonts-noto-cjk fontconfig  (dnf install google-noto-sans-cjk-fonts / pacman -S noto-fonts-cjk)')
                     + '; run python3 scripts/fonts.py JOB to see every font tried')
    if report['missing_inputs']:
        fixes.append('Inputs: create or fix these job paths: ' + ', '.join(str(value) for value in report['missing_inputs']))
    if 'art_direction_error' in report:
        fixes.append('Art direction: record and bind the brief/prompt/review for every image (references/style-and-drawing.md)')
    if not report['narration_files_available']:
        fixes.append('Narration: start the local VoiceStudio service (http://127.0.0.1:3900) then python3 scripts/voice_studio.py JOB; '
                     'or set voxcpm.model_path and run python3 scripts/voxcpm_voice.py JOB with the MLX Python; '
                     'or, only with the user\'s own key, volcengine_tts.py / gemini_tts.py; '
                     'or supply narration.source + alignment (references/continuous-narration.md)')
    fixes.append('Images: Codex has built-in image generation; Claude Code needs an image-generation tool/MCP or user-provided images. '
                 'Scripts cannot detect this; without it, stop instead of producing a text-only film.')
    return fixes


def inspect_environment(job_directory):
    root, manifest = load_manifest(job_directory)
    report = {'python': sys.version.split()[0], 'tools': {name: shutil.which(name) for name in ('ffmpeg', 'ffprobe')}, 'missing_inputs': []}
    try:
        report['fonts'] = check_coverage(copy.deepcopy(manifest))
        report['font'] = (report['fonts']['handwriting']['path'], report['fonts']['handwriting']['index'])
    except (ValueError, FileNotFoundError) as error:
        report['font_error'] = str(error)
    inputs = [manifest.get('cover', {}).get('source')]
    audio = [manifest['narration'].get(key) for key in ('source', 'alignment')] if manifest.get('narration') else []
    for page in manifest['pages']:
        for beat in page['beats']:
            if not manifest.get('narration'):
                audio.append(beat.get('audio'))
            inputs.extend(action.get('source') for action in beat.get('actions',[]) if action['type'] == 'illustration')
    for page in manifest['pages']:
        for group in page.get('visual_groups', []):
            inputs.extend(action.get('source') for action in group['actions'] if action['type']=='illustration')
    if manifest.get('hand', {}).get('source'):
        inputs.append(manifest['hand']['source'])
    for value in inputs + audio:
        if not value or not resolve_input(root, value).is_file():
            report['missing_inputs'].append(value)
    visuals_missing = any(not value or not resolve_input(root, value).is_file() for value in inputs)
    try:
        report['art_direction']=verify_art_direction(root,manifest)
    except (ValueError,FileNotFoundError,KeyError) as error:
        report['art_direction_error']=str(error)
    if not manifest.get('runtime_fixture'):
        from voice_backends import voice_backends_report
        backends = voice_backends_report(manifest.get('voice_studio', {}).get('url', 'http://127.0.0.1:3900'))
        model = manifest.get('voxcpm', {}).get('model_path')
        if model and (Path(model).expanduser()/'config.json').is_file():
            backends['local_voxcpm2'].update(model=str(Path(model).expanduser()), available=bool(backends['local_voxcpm2']['python']))
        report['voice_backends'] = backends
    report['narration_files_available'] = all(beat.get('audio') and resolve_input(root,beat['audio']).is_file()
        for page in manifest['pages'] for beat in page['beats'])
    if manifest.get('narration'):
        try:
            load_master(root, manifest, root/'work/audio/doctor-master.wav')
            report['narration_files_available'] = True
        except (ValueError, FileNotFoundError, KeyError) as error:
            report['narration_error'] = str(error)
            report['narration_files_available'] = False
    elif not manifest.get('runtime_fixture'):
        report['narration_error'] = 'Production requires a complete narration master and alignment'
        report['narration_files_available'] = False
    renderable = all(report['tools'].values()) and not visuals_missing and 'font_error' not in report and 'art_direction_error' not in report
    report['ready'] = renderable and not report['missing_inputs'] and report['narration_files_available']
    report['capabilities'] = {
        'render': renderable,
        'generate_narration': bool(report.get('voice_backends') and (report['voice_backends']['local_voxcpm2']['available'] or report['voice_backends']['voice_studio']['reachable']
                                                                     or any(report['voice_backends']['cloud_optional'].values()))),
        'narration_ready': report['narration_files_available'],
        'generate_images': 'host-dependent: not detectable by scripts'}
    report['fixes'] = fix_commands(report)
    return report


def save_wave(path, samples):
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, SAMPLE_RATE, 0, 'NONE', 'not compressed'))
        stream.writeframes(samples.astype('<i2').tobytes())


def read_narration(source, destination, pace, trim):
    if not .5 <= pace <= 2:
        raise ValueError('audio.pace must be within 0.5..2.0')
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-i', str(source), '-af', ('anull' if pace == 1 else f'atempo={pace}'),
                    '-ar', str(SAMPLE_RATE), '-ac', '1', '-c:a', 'pcm_s16le', str(destination)], check=True)
    with wave.open(str(destination), 'rb') as stream:
        samples = np.frombuffer(stream.readframes(stream.getnframes()), dtype='<i2')
    block = round(.02 * SAMPLE_RATE)
    blocks = np.pad(samples.astype(float) / 32768, (0, (-len(samples)) % block)).reshape(-1, block)
    rms = np.sqrt(np.mean(blocks**2, axis=1))
    active = np.flatnonzero(rms > max(.001, rms.max() * .02))
    if not len(active):
        raise ValueError(f'Narration has no audible samples: {source}')
    first = max(0, round((active[0] * .02 - .08) * SAMPLE_RATE)) if trim else 0
    last = min(len(samples), round(((active[-1] + 1) * .02 + .10) * SAMPLE_RATE)) if trim else len(samples)
    return samples[first:last], {'source_seconds': len(samples) / SAMPLE_RATE, 'trim_start_seconds': first / SAMPLE_RATE,
                                 'trim_end_seconds': last / SAMPLE_RATE, 'used_seconds': (last - first) / SAMPLE_RATE}


def subtitle_time(seconds):
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3600000)
    minutes, milliseconds = divmod(milliseconds, 60000)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}'


def drawn_seconds(metadata, seconds):
    """How long an asset actually draws: pictures and stroke-order text stop at video pace, others use their window."""
    if metadata.get('type') == 'illustration':
        return float(metadata['seconds'])
    if metadata.get('handwriting', {}).get('mode') == 'stroke_order':
        return float(metadata['handwriting']['seconds'])
    return seconds


class FilmBuilder:
    def __init__(self, job_directory):
        self.root, self.manifest = load_manifest(job_directory)
        self.art_direction = verify_art_direction(self.root, self.manifest)
        self.work = self.root / 'work'
        self.output = self.root / 'outputs'
        for directory in (self.work / 'assets', self.work / 'audio', self.work / 'review', self.output):
            directory.mkdir(parents=True, exist_ok=True)
        for stale in (self.work / 'assets').glob('element-*'):
            stale.unlink()  # assets are renumbered every build; a leftover layer file would attach to whatever now has its name
        canvas = self.manifest.get('canvas', {})
        if not canvas.get('width') or not canvas.get('height'):
            from aspect_ratio import resolve_canvas, DEFAULT_RATIO
            resolved = resolve_canvas(canvas.get('aspect_ratio', DEFAULT_RATIO), canvas.get('width'), canvas.get('height'), canvas.get('fps', 30))
            self.width, self.height, self.fps = resolved['width'], resolved['height'], resolved['fps']
            self.aspect_ratio = resolved['aspect_ratio']
        else:
            self.width = int(canvas['width'])
            self.height = int(canvas['height'])
            self.fps = int(canvas.get('fps', 30))
            self.aspect_ratio = canvas.get('aspect_ratio')
        if self.width < 320 or self.height < 240 or self.width % 2 or self.height % 2 or not 1 <= self.fps <= 60:
            raise ValueError('Canvas requires even width/height >=320x240 and fps within 1..60')
        self.fonts = check_coverage(self.manifest)  # every rendered character must have a glyph; pins role fonts
        self.font_path, self.font_index = find_font(self.manifest)
        stroke_assets.configure_assets(self.work / 'assets', self.font_path, self.font_index, self.fps)
        self.assets = {}
        self.next_asset = 0
        self.seen_beat_ids = set()

    @staticmethod
    def action_weight(action):
        weight = float(action.get('weight', 3 if action['type'] == 'illustration' else 1))
        if not math.isfinite(weight) or weight <= 0:
            raise ValueError('Action weight must be finite and positive')
        return weight

    @staticmethod
    def minimum_seconds(action):
        # Readers need about 0.3 s per hanzi to see the stroke order; Latin and punctuation write faster.
        if action['type'] != 'text':
            return 0.
        return sum(.3 if '\u3400' <= character <= '\u9fff' else .12 for character in str(action['text']))

    @staticmethod
    def travel_gap(first, second, minimum):
        # Lift-and-move time grows with distance so the pen never teleports between elements.
        anchors = [np.array([float(spec['x']) + float(spec.get('width', spec.get('size', 60))) / 2,
                             float(spec['y']) + float(spec.get('height', spec.get('size', 60))) / 2]) for spec in (first, second)]
        return float(np.clip(np.linalg.norm(anchors[1] - anchors[0]) / 1600, max(minimum, .12), .45))

    def create_asset(self, action, seconds=None):
        self.next_asset += 1
        name = f'element-{self.next_asset:04d}'
        kind = action['type']
        if kind == 'text':
            text = str(action['text'])
            if not text.strip() or '\n' in text:
                raise ValueError('Each text action must contain one nonempty line')
            metadata = stroke_assets.build_text(name, text, int(action.get('size', 60)), action.get('color', '#302C26'), seconds=seconds,
                                                stroke_order=bool(action.get('stroke_order', True)))
        elif kind == 'marker':
            metadata = stroke_assets.build_brush(name, int(action['width']), int(action['height']),
                action.get('color', '#EAC958'), action.get('style', 'highlight'), action.get('line_width'), seconds=seconds)
        elif kind == 'illustration':
            source = resolve_input(self.root, action['source'])
            optional = lambda key: resolve_input(self.root, action[key]) if action.get(key) else None
            drawing = {**self.manifest.get('drawing', {}), **action.get('drawing', {})}
            if drawing.get('mode') == 'reveal' and self.art_direction.get('style', {}).get('drawing_mode') == 'draw':
                raise ValueError(f'{action["source"]}: this film is drawn; one picture cannot switch to reveal. Simplify or regenerate it so it can be drawn')
            metadata = stroke_assets.build_illustration(name, source, int(action['width']), int(action['height']), action.get('crop'), action.get('parts'),
                optional('outline_mask'), seconds=seconds, region_map_path=optional('region_map'), part_regions=action.get('part_regions'),
                lineart_exception=action.get('lineart_exception'), drawing=drawing)
        else:
            raise ValueError(f'Unsupported action type: {kind}')
        self.assets[name] = metadata
        x, y = int(action['x']), int(action['y'])
        if not (0 <= x and 0 <= y and x + metadata['width'] <= self.width and y + metadata['height'] <= self.height):
            raise ValueError(f'Element is outside canvas: {action}')
        return {'asset': name, 'x': x, 'y': y, 'weight': self.action_weight(action),
                'z': int(action.get('z', 10 if kind == 'marker' and action.get('style', 'highlight') == 'highlight' else 30)),
                'role': kind}

    def caption_events(self, cues):
        options = self.manifest.get('captions', {})
        events = []
        for cue in cues:
            self.next_asset += 1
            name = f'element-{self.next_asset:04d}'
            metadata = build_caption(self.work / 'assets', name, cue['text'], options, self.width, self.height)
            self.assets[name] = metadata
            x = (self.width - metadata['width']) // 2
            y = self.height - metadata['height'] - int(options.get('bottom_margin', round(self.height * .04)))
            if min(x, y) < 0 or y + metadata['height'] > self.height:
                raise ValueError('Caption lies outside the canvas')
            events.append({'asset': name, 'x': x, 'y': y, 'role': 'caption', 'caption': True, 'z': 91,
                           'start': cue['start'], 'end': cue['end'], 'hide': cue['end']})
        return events

    def build(self):
        options = self.manifest.get('audio', {})
        for name in ('sentence_pause_seconds','page_pause_seconds','end_hold_seconds'):
            value=float(options.get(name,0))
            if not math.isfinite(value) or value<0:
                raise ValueError('Audio pauses must be finite and nonnegative: '+name)
        cover_seconds = float(self.manifest.get('cover', {}).get('seconds', 1.0))
        if cover_seconds != 1.0:
            raise ValueError('Cover must last exactly 1 second: 0.5 seconds still, then 0.5 seconds fade')
        pieces = [np.zeros(round(cover_seconds * SAMPLE_RATE), dtype=np.int16)]
        master_samples, master_beats, master_report = None, {}, None
        if self.manifest.get('narration') or not self.manifest.get('runtime_fixture'):
            master_samples, master_beats, master_report = load_master(self.root, self.manifest, self.work/'audio/master.wav')
        cursor = len(pieces[0]) / SAMPLE_RATE
        pages, cues, audio_report = [], [], []
        for page_index, page_spec in enumerate(self.manifest['pages']):
            if not page_spec.get('beats'):
                raise ValueError('Each page requires narration beats')
            page = {'index': page_index, 'start': cursor, 'beats': [], 'events': [],
                    'camera': page_spec.get('camera'), 'transition': page_spec.get('transition')}
            for beat_index, beat_spec in enumerate(page_spec['beats']):
                beat_id = beat_spec.get('id', f'p{page_index:02d}-b{beat_index:02d}')
                if not re.fullmatch(r'[A-Za-z0-9_-]+', beat_id) or beat_id in self.seen_beat_ids:
                    raise ValueError('Beat IDs must be unique safe filenames')
                self.seen_beat_ids.add(beat_id)
                if master_report:
                    aligned = master_beats[beat_id]
                    cursor = cover_seconds + aligned['start']
                    duration = aligned['end'] - aligned['start']
                    measurements = {'source_seconds': master_report['duration'], 'master_start': aligned['start'],
                                    'master_end': aligned['end'], 'used_seconds': duration}
                else:
                    samples, measurements = read_narration(resolve_input(self.root, beat_spec['audio']),
                        self.work / 'audio' / f'{beat_id}.wav', float(options.get('pace', 1.0)), bool(options.get('trim_silence', True)))
                    duration = len(samples) / SAMPLE_RATE
                beat = {'id': beat_id, 'start': cursor, 'end': cursor + duration, 'duration': duration, 'text': beat_spec['text']}
                if not str(beat['text']).strip():
                    raise ValueError('Narration text cannot be empty')
                if not beat_spec.get('actions') and not page_spec.get('visual_groups'):
                    raise ValueError('Every beat needs actions or a page visual group')
                cue_source = aligned if master_report else beat_spec
                if cue_source.get('caption_cues'):
                    beat['caption_cues'] = cue_source['caption_cues']
                beat_cues = resolve_cues(beat)
                page['events'].extend(self.caption_events(beat_cues))
                page['beats'].append(beat)
                cues.extend(beat_cues)
                audio_report.append({'id': beat_id, 'source': master_report['source'] if master_report else str(resolve_input(self.root, beat_spec['audio'])), **measurements})
                if not master_report:
                    pieces.append(samples)
                    pause_samples = np.zeros(round(float(options.get('sentence_pause_seconds', .18)) * SAMPLE_RATE), dtype=np.int16)
                    pieces.append(pause_samples)
                    cursor += duration + len(pause_samples) / SAMPLE_RATE
            ids = [beat['id'] for beat in page['beats']]
            blocks = [(beat['start'] + .54, beat['end'] - max(.5, beat['duration'] * .13), .07, spec['actions'], beat['id'])
                      for spec, beat in zip(page_spec['beats'], page['beats']) if spec.get('actions')]
            for group in page_spec.get('visual_groups', []):
                covered = group_beats(ids, group)
                lead, hold = float(group.get('lead', .54)), float(group.get('hold', .6))
                if not math.isfinite(lead + hold) or lead < 0 or hold < .45:
                    raise ValueError('Visual group needs a nonnegative lead and at least 0.45s reading hold')
                if not group.get('actions'):
                    raise ValueError('Visual group has no actions')
                # A group keeps drawing across sentences; an action with from_beat starts when its sentence starts.
                start, end, current = page['beats'][ids.index(covered[0])]['start'] + lead, page['beats'][ids.index(covered[-1])]['end'] - hold, []
                for spec in group['actions']:
                    anchor = page['beats'][ids.index(anchor_beat(covered, ids, spec))]['start']
                    if current and spec.get('from_beat') is not None:
                        if anchor - .08 <= start:
                            raise ValueError('from_beat must name a sentence after the earlier actions of its visual group have started')
                        blocks.append((start, anchor - .08, .08, current, f'visual group {covered[0]}..{covered[-1]}'))
                        start, current = anchor, []
                    start = max(start, anchor)
                    current.append(spec)
                blocks.append((start, end, .08, current, f'visual group {covered[0]}..{covered[-1]}'))
            blocks.sort(key=lambda block: block[0])
            for (_, previous_end, _, _, previous), (start, _, _, _, label) in zip(blocks, blocks[1:]):
                if start < previous_end:
                    raise ValueError(f'{label} starts drawing while {previous} is still drawing (one pen); '
                                     'put both in one visual group and use "from_beat" to start an action with its sentence')
            for start, end, gap, specs, label in blocks:
                gaps = [self.travel_gap(first, second, gap) for first, second in zip(specs, specs[1:])] + [0.]
                floors = [self.minimum_seconds(spec) for spec in specs]
                available = end - start - sum(gaps)
                if available < .15 * len(specs):
                    raise ValueError(f'{label}: too many drawing actions for the measured narration; simplify or extend the window across beats with a visual group')
                if available - sum(floors) < .15 * len(specs):
                    # Readable writing time is preferred, not guaranteed: shrink it and say so.
                    print(f'WARNING {label}: text needs {sum(floors):.2f}s to show stroke order but only {available:.2f}s exist; '
                          'shorten the text or widen the window', file=sys.stderr)
                    floors = [floor * (available - .15 * len(specs)) / sum(floors) for floor in floors]
                weights = [self.action_weight(spec) for spec in specs]
                for index, (spec, floor, travel) in enumerate(zip(specs, floors, gaps)):
                    # Each action may take its weighted share of the time still left. Assets are built after scheduling,
                    # draw at video pace and hand unused time back: later actions may use it, the rest holds the picture.
                    left = end - start - sum(gaps[index:]) - sum(floors[index:])
                    seconds = floor + max(0., left) * weights[index] / sum(weights[index:])
                    action = self.create_asset(spec, seconds)
                    seconds = min(seconds, drawn_seconds(self.assets[action['asset']], seconds))
                    action.update(start=start, end=start + seconds, caption=False, visual=spec.get('visual'))
                    if spec['type'] == 'illustration':
                        action['image'] = [spec['source'], spec.get('crop')]
                    page['events'].append(action)
                    start += seconds + travel
            validate_page_motion(page, {'width': self.width, 'height': self.height}, first=page_index == 0)
            page['speech_end'] = page['beats'][-1]['end']
            pause = float(options.get('page_pause_seconds', .24)) if page_index < len(self.manifest['pages']) - 1 else float(options.get('end_hold_seconds', .45))
            if pause < 0:
                raise ValueError('Audio pauses must be nonnegative')
            if master_report:
                if page_index + 1 < len(self.manifest['pages']):
                    next_id = self.manifest['pages'][page_index + 1]['beats'][0]['id']
                    cursor = cover_seconds + master_beats[next_id]['start']
                else:
                    cursor = cover_seconds + len(master_samples) / SAMPLE_RATE + pause
            else:
                pause_samples = np.zeros(round(pause * SAMPLE_RATE), dtype=np.int16)
                pieces.append(pause_samples)
                cursor += len(pause_samples) / SAMPLE_RATE
            page['end'] = cursor
            for event in page['events']:
                if not event['caption']:
                    event['hide'] = cursor
            page['last_drawing_end'] = max(event['end'] for event in page['events'] if not event['caption'])
            pages.append(page)
        ending = self.manifest.get('ending', {})
        fade = float(ending.get('fade_seconds', 0))
        narration_end = cover_seconds + len(master_samples) / SAMPLE_RATE if master_report else pages[-1]['speech_end']
        hold = pages[-1]['end'] - narration_end
        if not math.isfinite(fade) or not 0 <= fade <= hold:
            raise ValueError('Ending fade must fit entirely after narration')
        caption_hold = float(ending.get('caption_hold_seconds', 0))
        if not math.isfinite(caption_hold) or not 0 <= caption_hold <= hold - fade + .001:
            raise ValueError('Final subtitle hold must end before the closing fade')
        if caption_hold or master_report:
            captions = [event for event in pages[-1]['events'] if event['caption']]
            final_caption_end = narration_end + caption_hold
            captions[-1]['end'] = final_caption_end
            captions[-1]['hide'] = final_caption_end
            cues[-1]['end'] = final_caption_end
        if master_report:
            pieces = [pieces[0], master_samples, np.zeros(round(float(options.get('end_hold_seconds', .45)) * SAMPLE_RATE), dtype=np.int16)]
        ending_review = validate_ending(self.manifest, master_samples, SAMPLE_RATE) if master_report and not self.manifest.get('runtime_fixture') else None
        narration = np.concatenate(pieces)
        save_wave(self.work / 'narration.wav', narration)
        # Check cue timing on the clean narration now, not after a render: a late cue costs only a re-alignment here.
        from align import caption_sync
        sync = caption_sync(narration, cues)
        if sync['status'] == 'FAIL' and not self.manifest.get('runtime_fixture'):
            raise ValueError('Captions start away from the speech; fix the alignment before rendering: ' + json.dumps(sync['outside_tolerance'], ensure_ascii=False))
        hand = self.manifest.get('hand', {})
        timeline = {'title': self.manifest['title'], 'source_notes': self.manifest.get('source_notes', []),
                    'canvas': {'width': self.width, 'height': self.height, 'fps': self.fps, **({'aspect_ratio': self.aspect_ratio} if getattr(self, 'aspect_ratio', None) else {})},
                    'paper': self.manifest.get('paper', '#FBF9F1'), 'cover_seconds': cover_seconds, 'ending': {'fade_seconds': fade, 'caption_hold_seconds': caption_hold},
                    'narration_master': master_report, 'caption_sync': sync,
                    'captions': {'presentation': 'burned', 'owner': 'caption_events'},
                    'audio_policy': resolve_audio_layers(self.root, self.manifest), 'ending_review': ending_review,
                    'duration': len(narration) / SAMPLE_RATE, 'pages': pages, 'cues': cues, 'audio_report': audio_report,
                    'assets': self.assets, 'font': {'path': self.font_path, 'index': self.font_index},
                    'hand': prepare_hand(hand, self.root, self.work, self.height, self.font_path, self.font_index),
                    'watermark': prepare_watermark(self.manifest.get('watermark'), self.work, self.width, self.height),
                    'art_direction': self.art_direction,
                    'manifest_sha256': hashlib.sha256((self.root / 'job.json').read_bytes()).hexdigest()}
        from subtitle_contract import caption_clearance, validate_subtitles
        validate_subtitles(timeline)
        caption_clearance(timeline, self.work / 'assets')
        timeline['storyboard'] = check_build(self.root, self.manifest, timeline)
        cover = Image.open(resolve_input(self.root, self.manifest['cover']['source'])).convert('RGB')
        if abs(cover.width / cover.height - self.width / self.height) > .025:
            raise ValueError('Cover aspect ratio differs from canvas; generate or intentionally crop an appropriate cover')
        cover.resize((self.width, self.height), Image.Resampling.LANCZOS).save(self.output / 'cover.png')
        (self.work / 'timeline.json').write_text(json.dumps(timeline, ensure_ascii=False, indent=2))
        editing = self.output / 'editing'
        editing.mkdir(exist_ok=True)
        legacy = self.output / 'captions.srt'
        if legacy.exists():
            legacy.replace(editing / 'previous-captions.srt')
        (editing / 'transcript.srt').write_text('\n'.join(f"{index + 1}\n{subtitle_time(cue['start'])} --> {subtitle_time(cue['end'])}\n{cue['text']}\n" for index, cue in enumerate(cues)))
        (self.output / 'script.txt').write_text(self.manifest['title'] + '\n\n' + '\n\n'.join(cue['text'] for cue in cues))
        print(json.dumps({'duration_seconds': timeline['duration'], 'pages': len(pages), 'beats': len(cues), 'drawn_assets': len(self.assets),
                          'storyboard': timeline['storyboard']['stats'], 'warnings': timeline['storyboard']['warnings']}, ensure_ascii=False))
        return timeline

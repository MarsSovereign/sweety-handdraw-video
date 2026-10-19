from pathlib import Path
import json
import math
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage
from scipy.spatial import cKDTree
import hashlib
import handwriting
import illustration_layers
import line_drawing
import line_topology
import lineart_gate
import paint_motion
from motion_profile import time_fraction
import stroke_paths as stroke_utils

ASSET_DIRECTORY = None
FONT_PATH = None
FONT_INDEX = 0
FPS = 30


def configure_assets(directory, font_path, font_index=0, fps=30):
    global ASSET_DIRECTORY, FONT_PATH, FONT_INDEX, FPS
    ASSET_DIRECTORY = Path(directory)
    ASSET_DIRECTORY.mkdir(parents=True, exist_ok=True)
    FONT_PATH = str(font_path)
    FONT_INDEX = int(font_index)
    FPS = int(fps)


def lengths(points):
    return stroke_utils.calculate_lengths(points)


def ordered_paths(mask):
    skeleton = stroke_utils.thin_ink(mask)
    paths = [np.array(path) for path in stroke_utils.trace_strokes(skeleton)]
    covered = np.zeros(skeleton.shape, dtype=bool)
    for path in paths:
        coordinates = np.rint(path).astype(int)
        covered[coordinates[:, 1], coordinates[:, 0]] = True
    # Follow only adjacent leftover pixels. A branch starts a new physical stroke.
    pending = set(zip(*np.where(skeleton & ~covered)))
    while pending:
        point = min(pending)
        chain = []
        while point in pending:
            pending.remove(point)
            y, x = point
            chain.append([x, y])
            neighbors = sorted((y+dy, x+dx) for dy in (-1,0,1) for dx in (-1,0,1)
                               if (dy or dx) and (y+dy, x+dx) in pending)
            if not neighbors:
                break
            point = neighbors[0]
        if len(chain) == 1:
            chain.append([chain[0][0]+.01, chain[0][1]])
        paths.append(np.array(chain, dtype=float))
    if not paths:
        ys, xs = np.where(mask)
        if not len(xs):
            return []
        paths = [np.array([[xs[0],ys[0]],[xs[0]+.01,ys[0]]],dtype=float)]
    covered[:] = False
    for path in paths:
        coordinates = np.rint(path).astype(int)
        covered[coordinates[:,1],coordinates[:,0]] = True
    # Keep broad stroke tips within nib distance without connecting across blank paper.
    while True:
        distance, nearest = ndimage.distance_transform_edt(~covered, return_indices=True)
        distance[~mask] = 0
        y, x = np.unravel_index(np.argmax(distance), mask.shape)
        if distance[y,x] <= 7:
            break
        anchor = np.array([nearest[1,y,x],nearest[0,y,x]],dtype=float)
        extension = np.linspace(anchor,[x,y],max(2,math.ceil(distance[y,x])+1))
        coordinates = np.rint(extension).astype(int)
        if not mask[coordinates[:,1],coordinates[:,0]].all():
            extension = np.array([[x,y],[x+.01,y]],dtype=float)
            coordinates = np.rint(extension).astype(int)
        paths.append(extension)
        covered[coordinates[:,1],coordinates[:,0]] = True
    oriented = [path if tuple(path[0][::-1]) <= tuple(path[-1][::-1]) else path[::-1] for path in paths]
    return sorted(oriented,key=lambda path:(round(path[:,1].min()/16),path[:,0].min()))


def time_paths(paths, shape, start=0., duration=1.):
    reveal = np.full(shape, np.inf, dtype=np.float32)
    segments = []
    weights = [max(1., lengths(path)[-1]) for path in paths]
    travel_weights = [max(3., np.linalg.norm(paths[i + 1][0] - paths[i][-1]) * .22) for i in range(len(paths) - 1)]
    total = sum(weights) + sum(travel_weights)
    cursor = start
    for index, (path, weight) in enumerate(zip(paths, weights)):
        path_length = lengths(path)
        interval = duration * weight / max(1, total)
        for point, distance in zip(path, path_length):
            x, y = np.rint(point).astype(int)
            if 0 <= y < shape[0] and 0 <= x < shape[1]:
                reveal[y, x] = min(reveal[y, x], cursor + interval * distance / max(.001, path_length[-1]))
        segments.append({'kind':'ink','start':cursor,'end':cursor + interval,'points':path.tolist(),'lengths':path_length.tolist()})
        cursor += interval
        if index + 1 < len(paths):
            transit = duration * travel_weights[index] / max(1, total)
            segments.append({'kind':'travel','start':cursor,'end':cursor + transit,'points':[path[-1].tolist(),paths[index + 1][0].tolist()]})
            cursor += transit
    if np.isfinite(reveal).any():
        _, nearest = ndimage.distance_transform_edt(~np.isfinite(reveal), return_indices=True)
        reveal = reveal[nearest[0], nearest[1]]
    return reveal, segments


def save_asset(name, pixels, reveal, segments, extra=None):
    Image.fromarray(pixels).save(ASSET_DIRECTORY / f'{name}.png')
    np.savez_compressed(ASSET_DIRECTORY / f'{name}.npz', reveal=reveal.astype(np.float32))
    metadata = {'name':name,'width':pixels.shape[1],'height':pixels.shape[0],'segments':segments,**(extra or {})}
    (ASSET_DIRECTORY / f'{name}.json').write_text(json.dumps(metadata,ensure_ascii=False))
    return metadata


def measure_text(text, font, margin=5):
    """Reserve actual glyph bearings and descenders, not the nominal font size."""
    placements, cursor = [], 0.
    for character in text:
        placements.append((character, round(cursor), font.getbbox(character)))
        cursor += font.getlength(character)
    left = min([0] + [x + bounds[0] for _, x, bounds in placements])
    top = min([0] + [bounds[1] for _, _, bounds in placements])
    right = max([math.ceil(cursor)] + [x + bounds[2] for _, x, bounds in placements])
    bottom = max([0] + [bounds[3] for _, _, bounds in placements])
    return {'width': right-left+2*margin, 'height': bottom-top+2*margin,
            'origin': [margin-left, margin-top], 'placements': placements}


def build_text(name, text, size, color='#302C26', seconds=None, stroke_order=True):
    font = ImageFont.truetype(FONT_PATH, size, index=FONT_INDEX)
    line = handwriting.build_line(text, font, size, color, seconds, FPS) if stroke_order else None
    if line is not None:
        pixels, reveal, owners, segments, characters, origin, report = line
        metadata = save_asset(name,pixels,reveal,segments,{'type':'handwritten_text','text':text,'font_size':size,
                              'text_origin':origin,'safe_margin':5,'characters':characters,'handwriting':report})
        np.savez_compressed(ASSET_DIRECTORY / f'{name}.npz', reveal=reveal, owners=owners)
        return metadata
    # Whole-line font fallback keeps one consistent look when any ideograph lacks stroke data.
    missing = [character for character in text if handwriting.is_hanzi(character) and handwriting.character_data(character) is None]
    report = {'mode':'font','reason':'missing stroke data: '+''.join(dict.fromkeys(missing)) if missing else 'stroke order disabled'}
    layout = measure_text(text, font)
    width, height = layout['width'], layout['height']
    layer = Image.new('RGBA',(width,height))
    glyphs = []
    for character, offset, bounds in layout['placements']:
        glyph = Image.new('L',(max(1,bounds[2]-bounds[0]),max(1,bounds[3]-bounds[1])))
        glyph_draw = ImageDraw.Draw(glyph)
        glyph_draw.text((-bounds[0],-bounds[1]),character,font=font,fill=255)
        mask = np.array(glyph) > 30
        paths = [handwriting.orient(path) for path in ordered_paths(mask)]
        left = layout['origin'][0]+offset+bounds[0]
        top = layout['origin'][1]+bounds[1]
        glyphs.append((character, left, top, np.array(glyph), paths))
        ink = Image.new('RGBA',glyph.size,color)
        ink.putalpha(glyph)
        layer.alpha_composite(ink,(left,top))
    pixels = np.array(layer)
    reveal = np.full((height, width), np.inf, dtype=np.float32)
    owners = np.full((height, width), -1, dtype=np.int16)
    segments, character_intervals = [], []
    weights = [max(4., sum(lengths(path)[-1] for path in paths)) for _, _, _, _, paths in glyphs]
    cursor = 0.
    for index, ((character, left, top, glyph, paths), weight) in enumerate(zip(glyphs, weights)):
        interval = weight / sum(weights)
        if paths:
            local, strokes = time_paths(paths, glyph.shape, cursor, interval * .94)
            glyph_height, glyph_width = glyph.shape
            mask = glyph > 0
            patch = reveal[top:top+glyph_height, left:left+glyph_width]
            patch[mask] = local[mask]
            owners[top:top+glyph_height, left:left+glyph_width][mask] = index
            for stroke in strokes:
                stroke['points'] = (np.array(stroke['points']) + [left, top]).tolist()
                stroke['character_index'] = index
            segments.extend(strokes)
        character_intervals.append({'index':index,'character':character,'start':cursor,'end':cursor+interval})
        cursor += interval
    metadata = save_asset(name,pixels,reveal,segments,{'type':'handwritten_text','text':text,'font_size':size,
                          'text_origin':layout['origin'],'safe_margin':5,'characters':character_intervals,'handwriting':report})
    np.savez_compressed(ASSET_DIRECTORY / f'{name}.npz', reveal=reveal, owners=owners)
    return metadata


def build_brush(name, width, height, color, style='highlight', line_width=None, seconds=None):
    if style == 'highlight':
        radius = min(20, height / 2.4)
        xs = np.linspace(radius,width - radius,max(5,math.ceil(width / 3)))
        amplitude = max(0,height / 2 - radius)
        ys = height / 2 + amplitude * np.sin((xs - radius) / 17 * math.pi)
        points = np.c_[xs,ys]
    elif style == 'underline':
        radius = (line_width or 5) / 2
        xs = np.linspace(4,width - 4,max(3,math.ceil(width / 2)))
        points = np.c_[xs,height / 2 + 2 * np.sin(xs / max(1,width) * math.pi * 1.4)]
    elif style == 'circle':
        radius = (line_width or 5) / 2
        theta = np.linspace(-math.pi / 3,5 * math.pi / 3,max(50,round((width + height) / 2)))
        points = np.c_[width / 2 + (width / 2 - radius - 3) * np.cos(theta),height / 2 + (height / 2 - radius - 3) * np.sin(theta)]
    elif style == 'box':
        radius = (line_width or 4) / 2
        corners = np.array([[5,5],[width-6,4],[width-5,height-6],[4,height-5],[5,5]],dtype=float)
        points = np.vstack([np.linspace(a,b,max(2,round(np.linalg.norm(b-a)/2))) for a,b in zip(corners[:-1],corners[1:])])
    else:
        raise ValueError(style)
    rgba = np.zeros((height,width,4),dtype=np.uint8)
    rgba[:,:,:3] = tuple(int(color[index:index+2],16) for index in (1,3,5))
    reveal = np.full((height,width),np.inf,dtype=np.float32)
    distances = lengths(points)
    yy,xx = np.ogrid[:height,:width]
    for point,distance in zip(points,distances):
        footprint = (xx-point[0])**2 + (yy-point[1])**2 <= radius**2
        reveal[footprint] = np.minimum(reveal[footprint],distance/max(.01,distances[-1]))
    rgba[:,:,3] = np.isfinite(reveal).astype(np.uint8)*255
    segments=[{'kind':'brush','start':0.,'end':1.,'points':points.tolist(),'lengths':distances.tolist(),'radius':radius}]
    return save_asset(name,rgba,reveal,segments,{'type':'marker_'+style})


def paper_color(rgb):
    """Median colour of the image border when it is light, near-neutral paper (cream, warm white); else white."""
    ring = max(2, round(.02 * min(rgb.shape[:2])))
    border = np.concatenate([rgb[:ring].reshape(-1, 3), rgb[-ring:].reshape(-1, 3), rgb[:, :ring].reshape(-1, 3), rgb[:, -ring:].reshape(-1, 3)])
    paper = np.median(border, axis=0)
    return paper if paper.min() >= 200 and np.ptp(paper) <= 30 else np.full(3, 255.)


def prepare_artwork(source_path, max_width, max_height, crop=None, outline_mask_path=None):
    """Crop, remove the paper background, fit the box, and find the drawable outline (shared with region marks)."""
    artwork=Image.open(source_path).convert('RGBA')
    source_size = artwork.size
    # Generated art often sits on cream or warm-white paper: key against the paper measured on the whole source,
    # not pure white, or the paper itself becomes a region the marker has to colour.
    paper = paper_color(np.array(artwork)[:, :, :3].astype(float)) if artwork.getextrema()[3] == (255, 255) else None
    if crop is not None:
        left, top, right, bottom = crop
        if not (0 <= left < right <= artwork.width and 0 <= top < bottom <= artwork.height):
            raise ValueError('Artwork crop must stay inside the source image')
        artwork = artwork.crop(tuple(crop))
    if paper is not None:
        rgb = np.array(artwork)[:, :, :3]
        alpha = np.rint(np.clip(((paper - 2) - rgb).max(axis=2) / 18, 0, 1) * 255).astype(np.uint8)
        artwork = Image.fromarray(np.dstack([rgb, alpha]))
    scale = min(max_width/artwork.width, max_height/artwork.height)
    artwork = artwork.resize((max(1,round(artwork.width*scale)),max(1,round(artwork.height*scale))),Image.Resampling.LANCZOS)
    pixels=np.array(artwork)
    maximum=pixels[:,:,:3].max(axis=2)
    alpha=pixels[:,:,3]
    # Faint isolated paper grain left by the white key is not drawable content.
    labels, count = ndimage.label(alpha > 0, structure=np.ones((3, 3)))
    noise = (np.bincount(labels.ravel()) < 20) & (np.r_[255, ndimage.maximum(alpha, labels, np.arange(1, count + 1))] < 64)
    alpha[noise[labels]] = 0
    red,green,blue=[pixels[:,:,channel].astype(float) for channel in range(3)]
    cool_ink=(blue>=red+4)&(blue>=green-4)&(red<218)
    dark=((maximum<125)|cool_ink)&(alpha>30)
    outline=dark&(ndimage.distance_transform_edt(dark)<5.2)
    support=(maximum<165)&(alpha>30)
    if outline_mask_path is not None:
        explicit = Image.open(outline_mask_path).convert('L')
        if explicit.size != source_size:
            raise ValueError('Outline mask must match source image dimensions before crop')
        if crop is not None:
            explicit = explicit.crop(tuple(crop))
        explicit = explicit.resize(artwork.size,Image.Resampling.NEAREST)
        outline = (np.array(explicit)>127)&(alpha>0)
    if not (alpha>0).any():
        raise ValueError('Illustration is empty after background removal')
    return {'artwork':artwork,'pixels':pixels,'alpha':alpha,'outline':outline,'support':support}


def polygon_owners(parts, artwork, alpha):
    owners = np.full(alpha.shape, -1, dtype=np.int16)
    names = []
    hsv = None
    for index, part in enumerate(parts):
        if not part.get('id') or part['id'] in names:
            raise ValueError('Every semantic part needs a unique id')
        names.append(part['id'])
        region = Image.new('L', artwork.size)
        polygon = np.asarray(part['polygon'], dtype=float)
        if len(polygon)<3 or not ((polygon>=0)&(polygon<=1)).all():
            raise ValueError('Part polygons must have >=3 normalized vertices')
        ImageDraw.Draw(region).polygon([tuple(point) for point in polygon * [artwork.width-1,artwork.height-1]],fill=255)
        mask = (np.array(region)>0)&(alpha>0)&(owners<0)
        # Preserve held objects and other occluding shapes for their own semantic part.
        for excluded_polygon in part.get('exclude_polygons', []):
            excluded_polygon = np.asarray(excluded_polygon, dtype=float)
            if len(excluded_polygon)<3 or not ((excluded_polygon>=0)&(excluded_polygon<=1)).all():
                raise ValueError('Excluded polygons must have >=3 normalized vertices')
            excluded_region = Image.new('L', artwork.size)
            ImageDraw.Draw(excluded_region).polygon([tuple(point) for point in excluded_polygon * [artwork.width-1,artwork.height-1]],fill=255)
            mask &= np.array(excluded_region)==0
        # A background prop visible between curls belongs to the prop, not the head polygon.
        for exclusion in part.get('exclude_hsv', []):
            if len(exclusion)!=4 or not (0<=exclusion[0]<=exclusion[1]<=360 and 0<=exclusion[2]<=1 and 0<=exclusion[3]<=1):
                raise ValueError('exclude_hsv requires [hue_min, hue_max, saturation_min, value_min]')
            hsv=np.asarray(artwork.convert('RGB').convert('HSV'),dtype=float) if hsv is None else hsv
            hue=hsv[:,:,0]*360/255
            mask &= ~((hue>=exclusion[0])&(hue<=exclusion[1])&(hsv[:,:,1]>=exclusion[2]*255)&(hsv[:,:,2]>=exclusion[3]*255))
        if not mask.any():
            raise ValueError(f'Empty semantic part: {part["id"]}')
        owners[mask] = index
    # A model's polygon cannot trace the edge and anti-alias halo of a line it cuts through: faint pixels, and any
    # within a few px of a part, join the nearest part. Solid content farther from every part still has to be declared.
    stray = (alpha>0)&(owners<0)
    if stray.any() and (owners>=0).any():
        distance, nearest = ndimage.distance_transform_edt(owners<0, return_indices=True)
        edge = stray&((distance<=4)|(alpha<64))
        owners[edge] = owners[nearest[0], nearest[1]][edge]
    if ((alpha>0)&(owners<0)).any():
        raise ValueError('Semantic parts do not cover every visible pixel; inspect and repair masks')
    return names, owners


def region_owners(region_map_path, part_regions, alpha, source_path):
    """Set-of-Mark ownership: the model maps numbered regions (region_marks.py) to ordered semantic parts."""
    if not region_map_path:
        raise ValueError('part_regions requires the region_map written by region_marks.py')
    data = np.load(region_map_path)
    if 'source_sha256' in data.files and str(data['source_sha256']) != hashlib.sha256(Path(source_path).read_bytes()).hexdigest():
        raise ValueError('Region map belongs to another image; rerun region_marks.py and relabel')
    labels = data['labels']
    if labels.shape != alpha.shape:
        labels = np.array(Image.fromarray(labels.astype(np.int32)).resize((alpha.shape[1], alpha.shape[0]), Image.Resampling.NEAREST))
    present = set(np.unique(labels[alpha > 0]).tolist()) - {0}
    names, owners, seen = [], np.full(alpha.shape, -1, np.int16), {}
    for index, part in enumerate(part_regions):
        if not part.get('id') or part['id'] in names:
            raise ValueError('Every semantic part needs a unique id')
        names.append(part['id'])
        numbers = [int(number) for number in part.get('regions', [])]
        if not numbers:
            raise ValueError(f'Empty semantic part: {part["id"]}')
        for number in numbers:
            if number in seen:
                raise ValueError(f'Region {number} is assigned to both {seen[number]} and {part["id"]}')
            if number not in present:
                raise ValueError(f'Region {number} of {part["id"]} is not a visible region of this region map')
            seen[number] = part['id']
        owners[np.isin(labels, numbers)] = index
    missing = sorted(present - set(seen))
    if missing:
        raise ValueError('Visible regions are not assigned to any part: ' + ', '.join(map(str, missing)) + '; add them to part_regions')
    stray = (alpha > 0) & (owners < 0)
    if stray.any():
        _, nearest = ndimage.distance_transform_edt(owners < 0, return_indices=True)
        owners[stray] = owners[nearest[0], nearest[1]][stray]
    return names, owners, labels


def source_ink_layer(pixels, paint, clean, zone):
    """Unmix source ink from the paint beneath, preserving the original antialiased edge."""
    _, core = ndimage.distance_transform_edt(~clean, return_indices=True)
    rgb = pixels[:, :, :3].astype(np.float32)
    ink_rgb, under, painted = rgb[core[0], core[1]], paint[:, :, :3].astype(np.float32), paint[:, :, 3] > 0
    toward = ink_rgb - under
    share = np.clip(((rgb - under) * toward).sum(2) / np.maximum((toward ** 2).sum(2), 1.), 0, 1)
    alpha = np.where(painted, share, pixels[:, :, 3] / 255.) * zone
    return np.where(painted[..., None], ink_rgb, rgb), (np.rint(alpha * 255) / 255).astype(np.float32)


def source_ink(pixels, paint, clean, zone, strokes):
    """Bind each source pixel to the closest actual nib sample and that stroke's part."""
    rgb, alpha = source_ink_layer(pixels, paint, clean, zone)
    ys, xs = np.nonzero(alpha > 0)
    points = np.vstack([stroke['points'] for stroke in strokes])
    times = np.concatenate([stroke['times'] for stroke in strokes])
    parts = np.concatenate([np.full(len(stroke['points']), stroke['part'], np.int16) for stroke in strokes])
    _, closest = cKDTree(points).query(np.c_[xs, ys])
    ink_time = np.full(alpha.shape, np.inf, np.float32)
    ink_owner = np.full(alpha.shape, -1, np.int16)
    ink_time[ys, xs], ink_owner[ys, xs] = times[closest], parts[closest]
    return rgb, alpha, ink_time, ink_owner


def build_reveal(name, source_path, artwork, pixels, alpha, names, owners, seconds, speed, ownership):
    """Reveal mode for styles without ink outlines (watercolor, gouache, paper cut, pixel...): part by part the hand
    sweeps a broad soft brush and the finished picture shows only where it has passed. No line layer, no shading pass."""
    shape = alpha.shape
    _, nearest = ndimage.distance_transform_edt(owners < 0, return_indices=True)
    owners_everywhere = owners[nearest[0], nearest[1]]
    visible, rgb = alpha > 0, pixels[:, :, :3].astype(float)
    # One region per part: the brush follows the part, not the palette of a painted surface.
    regions = paint_motion.color_regions(rgb, visible, owners_everywhere, len(names), np.ones(shape, np.int32))
    for region in regions:
        region.update(tilt=.35, soft=.6, brush=2.5)
    ys, xs = np.nonzero(visible)
    tip = np.array([xs.mean(), ys.min()], dtype=float)
    fill = paint_motion.requirements(regions, rgb, len(names), tip, speed, FPS, shade=False)
    paced = max(fill['minimum'], fill['required'] / line_drawing.VIDEO_LAPSE)
    total = min(float(seconds), paced) if seconds else float(np.clip(paced, 1, 30))
    segments, fill_parts, _, _, _, counts = paint_motion.schedule(regions, rgb, len(names), tip, total, 0., total, speed, FPS, shade=False)
    first, cover, full, _, _ = paint_motion.render(shape, regions, [], owners_everywhere)
    ending = float(max([segment['end'] for segment in segments] + [full[np.isfinite(full)].max(initial=0.)]))
    if ending > 1:
        first, full = first / ending, full / ending
        for segment in segments:
            segment['start'], segment['end'] = segment['start'] / ending, segment['end'] / ending
        fill_parts = [[value / ending for value in part] for part in fill_parts]
    never = np.full(shape, np.inf, np.float32)
    maps = {'ink': never, 'ink_owner': np.full(shape, -1, np.int16), 'color': first.astype(np.float32),
            'color_cover': cover, 'color_full': full.astype(np.float32), 'shade': never, 'shade_ramp': np.float32(1e-3)}
    empty = np.zeros(shape + (4,), np.uint8)
    final = illustration_layers.prepare(maps, empty, pixels, pixels)['final']
    owners_final = np.where(final[:, :, 3] > 0, owners_everywhere, -1).astype(np.int16)
    for suffix, layer in (('line', empty), ('paint', pixels), ('shade', pixels)):
        Image.fromarray(layer).save(ASSET_DIRECTORY/f'{name}-{suffix}.png')
    Image.fromarray(final).save(ASSET_DIRECTORY/f'{name}.png')
    np.savez_compressed(ASSET_DIRECTORY/f'{name}.npz', owners=owners_final, **maps)
    part_reports = [{'id': part_id, 'index': index, 'outline_start': 0., 'outline_end': 0.,
                     'fill_start': fill_parts[index][0], 'fill_end': fill_parts[index][1]} for index, part_id in enumerate(names)]
    segments.sort(key=lambda segment: segment['start'])
    drawability = {'line_length_px': 0, 'stroke_count': 0, 'drawn_stroke_count': 0, 'fill_area_px': int(visible.sum()),
                   'required_seconds': round(float(fill['required']), 2), 'minimum_seconds': round(float(fill['minimum']), 2), 'lineart_gate': None}
    metadata = {'name': name, 'type': 'illustration', 'engine': 'draw-v2', 'mode': 'reveal', 'source': str(source_path), 'width': artwork.width,
                'height': artwork.height, 'seconds': total, 'segments': segments, 'parts': part_reports, 'layers': ['paint'],
                'lines': {}, 'painting': counts, 'drawability': drawability, 'ownership': ownership, 'lineart_exception': None,
                'time_compression': round(max(1., ending), 3),
                'idle_seconds': round(max(0., total - max([segment['end'] for segment in segments] + [0.]) * total), 2)}
    (ASSET_DIRECTORY/f'{name}.json').write_text(json.dumps(metadata))
    return metadata


def build_illustration(name, source_path, max_width, max_height, crop=None, parts=None, outline_mask_path=None, seconds=None,
                       region_map_path=None, part_regions=None, lineart_exception=None, drawing=None):
    """Draw, don't reveal: whole strokes inked part by part, then marker color and soft shading, as layers."""
    drawing = dict(drawing or {})
    speed = float(drawing.get('speed', 1.))
    if not (math.isfinite(speed) and .25 <= speed <= 4):
        raise ValueError('drawing.speed must be within 0.25..4')
    mode = drawing.get('mode', 'draw')
    if mode not in ('draw', 'reveal'):
        raise ValueError('drawing.mode must be "draw" or "reveal"')
    art = prepare_artwork(source_path, max_width, max_height, crop, None if mode == 'reveal' else outline_mask_path)
    artwork, pixels, alpha, outline = art['artwork'], art['pixels'], art['alpha'], art['outline']
    shape, labels = alpha.shape, None
    if part_regions:
        names, owners, labels = region_owners(region_map_path, part_regions, alpha, source_path)
        declared = part_regions
    elif parts:
        names, owners = polygon_owners(parts, artwork, alpha)
        declared = parts
    else:
        raise ValueError('Illustrations require model-designed semantic parts: part_regions (region_marks.py) or ordered normalized polygons')
    if mode == 'reveal':
        return build_reveal(name, source_path, artwork, pixels, alpha, names, owners, seconds, speed, 'regions' if part_regions else 'polygons')
    for index, part_id in enumerate(names):
        if not (outline & (owners == index)).any():
            raise ValueError('Part has no drawable outline; prepare a contour layer/mask before generation: '+part_id)
    strokes, clean, skeleton, topology = line_topology.extract_strokes(outline, art['support'])
    gate = lineart_gate.evaluate(outline, strokes, clean, skeleton)
    if not gate['passed'] and not lineart_exception:
        raise ValueError(lineart_gate.failure_message(gate))
    _, nearest = ndimage.distance_transform_edt(owners < 0, return_indices=True)
    owners_everywhere = owners[nearest[0], nearest[1]]
    # The soft grey edge of a line (anti-aliasing, compression ringing) belongs to the line, not to marker paint:
    # neutral pixels next to a line are refilled from the nearest real paint (paper on line-only art).
    neutral = np.ptp(pixels[:, :, :3].astype(int), axis=2) <= 24
    line_zone = ndimage.binary_dilation(clean, iterations=2) | (ndimage.binary_dilation(clean, iterations=5) & neutral)
    shade_pixels = paint_motion.inpaint_lines(pixels, line_zone)
    shade_pixels[:, :, 3] = np.where(shade_pixels[:, :, 3] >= 96, shade_pixels[:, :, 3], 0)
    _, source_line_alpha = source_ink_layer(pixels, shade_pixels, clean, line_zone)
    topology['edge_visits'] = line_topology.cover_ink_edges(strokes, source_line_alpha > 0, support=line_zone)
    line_drawing.classify(strokes, owners, alpha, pixels, names, {part['id'] for part in declared if part.get('key')})
    painted = shade_pixels[:, :, 3] > 0
    shade_rgb = shade_pixels[:, :, :3].astype(float)
    regions = paint_motion.color_regions(shade_rgb, painted, owners_everywhere, len(names), labels)
    ys, xs = np.nonzero(alpha > 0)
    tip = np.array([xs.mean(), ys.min()], dtype=float)
    lines = line_drawing.requirements(strokes, len(names), tip, speed, FPS)
    fill = paint_motion.requirements(regions, shade_rgb, len(names), tip, speed, FPS) if regions else {'required': 0., 'moderate': 0., 'minimum': 0.}
    shaded = bool(regions) and bool(paint_motion.shaded_parts(regions, shade_rgb, len(names)))
    ramp = paint_motion.SHADE_RAMP if shaded else 0.
    paced = max(lines['all_at_cap'] + fill['minimum'] + ramp, (lines['required'] + fill['required'] + ramp) / line_drawing.VIDEO_LAPSE)
    paced /= 1. - 1. / illustration_layers.SHARP
    paced += min(1.5, .136 * paced) if drawing.get('sketch') else 0.
    total = min(float(seconds), paced) if seconds else float(np.clip(paced, 1, 30))
    sketches = line_drawing.sketch_strokes(owners, alpha, len(names)) if drawing.get('sketch') else []
    top_tip = np.asarray(tip, float)
    ceiling = float(seconds) if seconds else 30.
    # Lay the picture out again when the schedule overruns a window that still has room: the fill minimum
    # was first measured from the picture top, but the marker actually starts where the lines end, so give
    # the picture its measured minimum instead of compressing it. A window truly too small still
    # compresses the whole asset uniformly below.
    for attempt in range(3):
        tip = top_tip.copy()
        segments, cursor = [], 0.
        if sketches:
            each = [max(2. / FPS, min(.4, stroke['weights'][-1] / (line_drawing.line_cap(speed) * 1.6))) for stroke in sketches]
            factor = min(1., min(1.5, .12 * total) / (sum(each) + .1 * len(each)))
            for index, (stroke, length) in enumerate(zip(sketches, each)):
                length = max(2. / FPS, length * factor)
                if index:
                    lift = line_drawing.lift_seconds(np.linalg.norm(stroke['points'][0] - sketches[index - 1]['points'][-1]), FPS, factor)
                    segments.append({'kind': 'travel', 'start': cursor / total, 'end': (cursor + lift) / total,
                                     'points': [sketches[index - 1]['points'][-1].tolist(), stroke['points'][0].tolist()]})
                    cursor += lift
                stroke['times'] = (cursor + length * time_fraction(stroke['weights'] / max(1e-6, stroke['weights'][-1]), 'minjerk')) / total
                segments.append({'kind': 'sketch', 'start': cursor / total, 'end': (cursor + length) / total, 'profile': 'minjerk',
                                 'points': stroke['points'].tolist(), 'lengths': stroke['weights'].tolist()})
                cursor += length
            tip = sketches[-1]['points'][-1]
        # The source ink settles locally after the nib passes. Reserve that edge ramp
        # instead of accelerating the whole picture by 0.4% at the last frame.
        available = max(.001, total - cursor - ramp - total / illustration_layers.SHARP)
        # Water-fill the time: silhouette + key lines, a basic fill, every line at the cap, a moderate fill, then human pace.
        line_budget, fill_budget, remaining = 0., 0., available
        steps = [(lines['minimum'], 0.), (0., fill['minimum']), (lines['all_at_cap'] - lines['minimum'], 0.),
                 (0., fill['moderate'] - fill['minimum']), (lines['required'] - lines['all_at_cap'], fill['required'] - fill['moderate'])]
        for line_step, fill_step in steps:
            line_step, fill_step = max(0., line_step), max(0., fill_step)
            share = min(1., remaining / max(1e-9, line_step + fill_step))
            line_budget, fill_budget, remaining = line_budget + line_step * share, fill_budget + fill_step * share, remaining - (line_step + fill_step) * share
        if remaining > 1e-6:
            stretch = min(1.6, available / max(1e-6, line_budget + fill_budget))
            line_budget, fill_budget = line_budget * stretch, fill_budget * stretch
        ordered, line_segments, line_parts, line_end, line_counts = line_drawing.schedule(
            strokes, len(names), tip, line_budget, cursor, total, speed, FPS)
        segments += line_segments
        line_rgb, line_alpha, ink, ink_owner = source_ink(pixels, shade_pixels, clean, line_zone, strokes)
        tip = ordered[-1]['points'][-1] if ordered else tip
        # The marker minimum must start where the lines end, not at the picture top: the storyboard gate
        # reads minimum_seconds, and the fill below begins its travel at this tip.
        fill_minimum = paint_motion.requirements(regions, shade_rgb, len(names), tip, speed, FPS, shade=False)['minimum'] if regions else 0.
        minimum_total = (lines['all_at_cap'] + fill_minimum + ramp) / (1. - 1. / illustration_layers.SHARP)
        if attempt < 2 and total < minimum_total - 1e-6 and total < ceiling - 1e-6:
            total = min(ceiling, minimum_total)
            continue
        fill_parts, bands, fill_counts = [[line_end, line_end] for _ in names], [], {}
        if regions:
            fill_segments, fill_parts, bands, _, tip, fill_counts = paint_motion.schedule(
                regions, shade_rgb, len(names), tip, fill_budget, line_end * total, total, speed, FPS)
            segments += fill_segments
        first, cover, full, flat, shade = paint_motion.render(shape, regions, bands, owners_everywhere)
        maps = {'ink': ink, 'ink_owner': ink_owner, 'color': first, 'color_cover': cover, 'color_full': full,
                'shade': shade, 'shade_ramp': np.float32(max(ramp, 1e-3) / total)}
        # Two-frame floors and lift minimums can overrun an impossible budget: compress the whole asset uniformly.
        finite = lambda values: values[np.isfinite(values)]
        ending = float(max([segment['end'] for segment in segments] + [line_end, finite(ink + 1 / illustration_layers.SHARP).max(initial=0.), finite(full).max(initial=0.),
                            finite(shade).max(initial=0.) + float(maps['shade_ramp'])] + ([line_end + .5 / total] if sketches else [])))
        if ending <= 1. or total >= ceiling - 1e-9:
            break
        total = min(ceiling, total * ending)
    paint_layer = np.dstack([np.rint(flat), shade_pixels[:, :, 3]]).astype(np.uint8)
    line_layer = np.dstack([np.rint(line_rgb), np.rint(line_alpha * 255)]).astype(np.uint8)
    sketch_layer = None
    if sketches:
        sketch_rgb, sketch_alpha, sketch_time, _ = line_drawing.render(shape, sketches)
        sketch_layer = np.dstack([np.rint(sketch_rgb), np.rint(sketch_alpha * 120)]).astype(np.uint8)
        maps.update(sketch=sketch_time, sketch_fade=np.array([line_end, .5 / total], np.float32))
    if ending > 1:
        for key in ('ink', 'color', 'color_full', 'shade', 'shade_ramp', 'sketch', 'sketch_fade'):
            if key in maps:
                maps[key] = (maps[key] / ending).astype(np.float32)
        for segment in segments:
            segment['start'], segment['end'] = segment['start'] / ending, segment['end'] / ending
        line_parts = [[value / ending for value in part] for part in line_parts]
        fill_parts = [[value / ending for value in part] for part in fill_parts]
    final = illustration_layers.prepare(maps, line_layer, paint_layer, shade_pixels, sketch_layer)['final']
    owners_final = np.where(final[:, :, 3] > 0, owners_everywhere, -1).astype(np.int16)
    for suffix, layer in (('line', line_layer), ('paint', paint_layer), ('shade', shade_pixels), ('sketch', sketch_layer)):
        if layer is not None:
            Image.fromarray(layer).save(ASSET_DIRECTORY/f'{name}-{suffix}.png')
    Image.fromarray(final).save(ASSET_DIRECTORY/f'{name}.png')
    np.savez_compressed(ASSET_DIRECTORY/f'{name}.npz', owners=owners_final, **maps)
    part_reports = [{'id': part_id, 'index': index, 'outline_start': line_parts[index][0], 'outline_end': line_parts[index][1],
                     'fill_start': fill_parts[index][0], 'fill_end': fill_parts[index][1]} for index, part_id in enumerate(names)]
    segments.sort(key=lambda segment: segment['start'])
    drawability = {'line_length_px': round(sum(stroke['length'] for stroke in strokes)), 'stroke_count': len(strokes),
                   'drawn_stroke_count': line_counts['drawn_stroke_count'],
                   'fill_area_px': int(painted.sum()), 'required_seconds': round(lines['required'] + fill['required'] + ramp, 2),
                   'minimum_seconds': round(lines['all_at_cap'] + fill_minimum + ramp, 2), 'lineart_gate': gate}
    metadata = {'name': name, 'type': 'illustration', 'engine': 'draw-v2', 'source': str(source_path), 'width': artwork.width,
                'height': artwork.height, 'seconds': total, 'segments': segments, 'parts': part_reports,
                'layers': ['paint', 'shade'] + (['sketch'] if sketches else []) + ['line'],
                'lines': {**topology, **line_counts}, 'painting': fill_counts, 'drawability': drawability,
                'ownership': 'regions' if part_regions else 'polygons', 'lineart_exception': lineart_exception, 'time_compression': round(max(1., ending), 3),
                'idle_seconds': round(max(0., total - max([segment['end'] for segment in segments] + [0.]) * total), 2)}
    (ASSET_DIRECTORY/f'{name}.json').write_text(json.dumps(metadata))
    return metadata

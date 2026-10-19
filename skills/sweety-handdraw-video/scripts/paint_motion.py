"""Color like a person with a marker: per part, per color region, one continuous zigzag along the
region's principal axis (rounded turns, no lift inside a region), then a few broad soft shading strokes."""
import math
import numpy as np
from scipy import ndimage
from line_drawing import lift_seconds, weighted_lengths

FILL_SPEED = 700.     # px/s of marker travel for a person coloring at 1080p
SHADE_SECONDS = .35   # per shaded part, at human pace
SHADE_RAMP = .3       # seconds over which a broad soft stroke blends flat color into the source
SCALES = (1., 1.5, 2., 3.)
FILL_FACTOR = 6.      # coloring cap: a broad marker has no detail to follow, so time-lapsed coloring reads as human at 6x
FILL_FRAME_PX = 120.  # a broad marker may sweep this far per frame: its own width keeps the swept color continuous
PART_PAUSE = .12      # short rest when the marker moves to the next semantic part


def inpaint_lines(pixels, line_zone):
    """Replace ink (and its anti-aliased halo) with the nearest paint so lines live only in the line layer."""
    if not line_zone.any() or line_zone.all():
        return pixels.copy()
    _, nearest = ndimage.distance_transform_edt(line_zone, return_indices=True)
    return pixels[nearest[0], nearest[1]]


def palette(colors, count=3, rounds=8, merge=28.):
    """Small k-means; clusters whose colors are perceptually close are merged (noise is not a region)."""
    luminance = colors @ [.299, .587, .114]
    order = np.argsort(luminance)
    centers = np.array([colors[order[int(q * (len(colors) - 1))]] for q in np.linspace(.1, .9, count)])
    for _ in range(rounds):
        labels = np.argmin(((colors[:, None] - centers[None]) ** 2).sum(2), 1)
        centers = np.array([colors[labels == k].mean(0) if (labels == k).any() else centers[k] for k in range(count)])
    target = list(range(count))
    for first in range(count):
        for second in range(first + 1, count):
            if np.linalg.norm(centers[first] - centers[second]) < merge:
                target = [target[first] if value == target[second] else value for value in target]
    return np.array(target)[labels]


def region_pixels(region, image):
    (left, top), (height, width) = region['offset'], region['mask'].shape
    return image[top:top + height, left:left + width][region['mask']]


def color_regions(rgb, visible, owners, part_count, labels=None):
    """Regions to paint: the model's region map when given, else palette regions per part.
    Specks are absorbed by their nearest region instead of becoming separate passes."""
    regions = []
    for part in range(part_count):
        mask = visible & (owners == part)
        area = int(mask.sum())
        if area < 60:  # a few specks are not a region to colour; the line layer carries this part
            continue
        cells = np.argwhere(mask)
        (top, left), (bottom, right) = cells.min(0), cells.max(0) + 1
        crop = (slice(top, bottom), slice(left, right))
        local = mask[crop]
        found = np.zeros(local.shape, np.int32)
        if labels is not None:
            found[local] = labels[crop][local]
        elif area >= 120:
            clusters = np.zeros(local.shape, np.int32)
            clusters[local] = palette(rgb[crop][local].astype(float)) + 1
            for cluster in np.unique(clusters[local]):
                pieces, count = ndimage.label(ndimage.binary_opening(clusters == cluster, iterations=2))
                sizes = np.bincount(pieces.ravel())
                for piece in range(1, count + 1):
                    if sizes[piece] >= max(400, .03 * area):
                        found[pieces == piece] = found.max() + 1
        sizes = np.bincount(found[local], minlength=found.max() + 1)
        large = [value for value in range(1, len(sizes)) if sizes[value] >= max(60, .01 * area)]
        found[~np.isin(found, large)] = 0
        if not large:
            found[local], large = 1, [1]
        _, nearest = ndimage.distance_transform_edt(found == 0, return_indices=True)
        found = np.where(local, found[nearest[0], nearest[1]], 0)
        for value in large:
            region = {'part': part, 'offset': np.array([left, top]), 'mask': found == value}
            region.update(area=int(region['mask'].sum()), flat=np.median(region_pixels(region, rgb), axis=0))
            regions.append(region)
    regions.sort(key=lambda region: (region['part'], region['offset'][1], region['offset'][0], -region['area']))
    return regions


def axes(points):
    center = points.mean(0)
    axis = np.linalg.eigh(np.cov(points.T))[1][:, -1] if len(points) > 3 else np.array([1., 0.])
    axis = axis if (axis[0] > 0 if abs(axis[0]) >= abs(axis[1]) else axis[1] > 0) else -axis
    across = np.array([-axis[1], axis[0]])
    return center, axis, (across if (across[1] > 0 if abs(across[1]) >= abs(across[0]) else across[0] > 0) else -across)


def zigzag(region, radius):
    """Continuous boustrophedon cells: rows along the principal axis, stepped across it with rounded
    turns; a row that no longer overlaps the previous one starts a new cell (the only lifts in a region)."""
    ys, xs = np.nonzero(region['mask'])
    points = np.c_[xs, ys].astype(float) + region['offset']
    center, axis, across = axes(points)
    if region.get('tilt'):
        # A brush sweeping a painting runs on a slant, so the revealed edge is not a scanline.
        cosine, sine = math.cos(region['tilt']), math.sin(region['tilt'])
        axis = np.array([cosine * axis[0] - sine * axis[1], sine * axis[0] + cosine * axis[1]])
        across = np.array([-axis[1], axis[0]])
    along_values, across_values = (points - center) @ axis, (points - center) @ across
    spacing = 1.25 * radius
    rows = []
    for offset in np.arange(across_values.min() + .45 * radius, across_values.max() + .45 * radius, spacing):
        band = np.sort(along_values[np.abs(across_values - offset) <= .65 * spacing])
        if len(band):
            runs = np.split(band, np.where(np.diff(band) > 2 * radius)[0] + 1)
            rows.append([(offset, run[0] - .3 * radius, run[-1] + .3 * radius) for run in runs])
    unused = {(row, k) for row, runs in enumerate(rows) for k in range(len(runs))}
    cells, step, bulge = [], max(1.5, radius / 4), .35 * spacing
    position = lambda along, offset: center + along * axis + offset * across
    while unused:
        row, k = min(unused)
        chain = [(row, k)]
        unused.discard((row, k))
        while row + 1 < len(rows):
            low, high = rows[row][k][1:]
            overlaps = [(min(high, b) - max(low, a), j) for j, (_, a, b) in enumerate(rows[row + 1])
                        if (row + 1, j) in unused and min(high, b) > max(low, a)]
            if not overlaps:
                break
            row, k = row + 1, max(overlaps)[1]
            chain.append((row, k))
            unused.discard((row, k))
        polyline, previous = [], None
        for index, (row, k) in enumerate(chain):
            offset, a, b = rows[row][k]
            if index % 2:
                a, b = b, a
            if previous is not None:
                end, end_offset, direction = previous
                turn = max(end, a) + bulge if direction > 0 else min(end, a) - bulge
                u = np.linspace(0, 1, 10)[1:-1, None]
                start, middle, finish = position(end, end_offset), position(turn, (end_offset + offset) / 2), position(a, offset)
                polyline.append((1 - u) ** 2 * start + 2 * u * (1 - u) * middle + u * u * finish)
            along = np.linspace(a, b, max(2, int(abs(b - a) / step) + 1))
            polyline.append(position(along[:, None], offset))
            previous = (b, offset, 1 if b >= a else -1)
        cells.append(np.vstack(polyline))
    return cells or [np.repeat(center[None], 2, axis=0)]  # a sliver too thin for a row still gets one dab


def plan(regions, scale):
    """Every region, however small, gets its own pass of the marker: nothing settles in without a brush."""
    for region in regions:
        region['radius'] = float(np.clip(math.sqrt(region['area']) / 7, 7, 24) * scale * region.get('brush', 1.))
        region['cells'] = zigzag(region, region['radius'])


def shaded_parts(regions, rgb, part_count):
    """Parts whose source differs visibly from flat color get a short shading pass."""
    result = []
    for part in range(part_count):
        mine = [region for region in regions if region['part'] == part]
        if mine and np.average([np.abs(region_pixels(region, rgb) - region['flat']).mean() for region in mine],
                               weights=[region['area'] for region in mine]) > 6:
            result.append(part)
    return result


def timeline(regions, shading, part_count, tip, pace, lifts=1., start=0., total=1., fps=30, record=False):
    """Lay out every marker pass and shading band in order; returns (segments, parts, bands, end seconds, tip)."""
    tip = np.asarray(tip, float)
    segments, cursor, parts, bands = [], start, [], []
    shade_time = SHADE_SECONDS * min(1., FILL_SPEED * 1.5 / pace)

    def travel(destination):
        nonlocal cursor
        lift = lift_seconds(np.linalg.norm(destination - tip), fps, lifts)
        segments.append({'kind': 'travel', 'start': cursor / total, 'end': (cursor + lift) / total,
                         'points': [tip.tolist(), destination.tolist()]})
        cursor += lift

    for part in range(part_count):
        part_start = cursor
        for region in [region for region in regions if region['part'] == part]:
            passes = []
            for cell in region['cells']:
                travel(cell[0])
                weights = weighted_lengths(cell)
                seconds = max(2. / fps, weights[-1] / pace)
                segments.append({'kind': 'color', 'start': cursor / total, 'end': (cursor + seconds) / total,
                                 'points': cell.tolist(), 'lengths': weights.tolist(), 'radius': region['radius'] + 2.5})
                passes.append((cell, (cursor + seconds * weights / max(1e-6, weights[-1])) / total))
                cursor += seconds
                tip = cell[-1]
            if record:
                region.update(passes=passes, end=cursor / total, window=3 * region['radius'] / pace / total)
        if part in shading:
            mine = np.vstack([np.c_[np.nonzero(r['mask'])[::-1]].astype(float) + r['offset'] for r in regions if r['part'] == part])
            center, axis, across = axes(mine)
            along, side = (mine - center) @ axis, (mine - center) @ across
            count = int(np.clip(round(np.ptp(side) / 90), 1, 4))
            edges = np.linspace(side.min(), side.max(), count + 1)
            band = {'part': part, 'center': center, 'axis': axis, 'across': across, 'edges': edges,
                    'along': (along.min(), along.max()), 'times': []}
            for index in range(count):
                points = center + np.linspace(along.min(), along.max(), 24)[:, None] * axis + (edges[index] + edges[index + 1]) / 2 * across
                travel(points[0])
                seconds = shade_time / count
                segments.append({'kind': 'shade', 'start': cursor / total, 'end': (cursor + seconds) / total, 'points': points.tolist(),
                                 'lengths': weighted_lengths(points).tolist(), 'radius': float(np.ptp(edges) / count / 2 + 4)})
                band['times'].append((cursor / total, (cursor + seconds) / total))
                cursor += seconds
                tip = points[-1]
            bands.append(band)
        parts.append([part_start / total, cursor / total])
        if part_start < cursor and part + 1 < part_count and any(r['part'] == part + 1 for r in regions):
            pause = PART_PAUSE * min(1., FILL_SPEED / max(pace, 1e-6))
            segments.append({'kind': 'pause', 'start': cursor / total, 'end': (cursor + pause) / total,
                             'points': [tip.tolist(), tip.tolist()]})
            cursor += pause
    return segments, parts, bands, cursor, tip


def fill_cap(speed, fps):
    return min(FILL_SPEED * FILL_FACTOR, FILL_FRAME_PX * fps) * speed


def requirements(regions, rgb, part_count, tip, speed=1., fps=30, shade=True):
    """Seconds for: base marker at human pace with shading; a 1.5x marker at the cap with shading;
    the broadest marker at the cap with short lifts and no shading."""
    shading = shaded_parts(regions, rgb, part_count) if shade else []
    result = {}
    cap = fill_cap(speed, fps)
    for key, scale, pace, shade, lifts in (('required', SCALES[0], FILL_SPEED * speed, shading, 1.), ('moderate', SCALES[1], cap, shading, 1.),
                                           ('minimum', SCALES[-1], cap, [], .5)):
        plan(regions, scale)
        result[key] = timeline(regions, shade, part_count, tip, pace, lifts, fps=fps)[3]
    return result


def schedule(regions, rgb, part_count, tip, budget, start, total, speed=1., fps=30, shade=True):
    """Pick marker size, pace and lift length so the fill fits the budget: bigger marker first, then pace
    up to the cap; shading is dropped before the cap is exceeded. Returns segments, per-part [start, end]
    fractions, shading bands, end fraction, pen tip and counts."""
    shading = shaded_parts(regions, rgb, part_count) if shade else []
    base, cap = FILL_SPEED * speed, fill_cap(speed, fps)
    seconds = lambda pace, lifts=1., shade=None: timeline(regions, shading if shade is None else shade, part_count, tip, pace, lifts, fps=fps)[3]
    for scale in SCALES:
        plan(regions, scale)
        if seconds(cap) <= budget:
            break
    if seconds(cap) > budget:
        shading = []
    lifts = next((value for value in (1., .7, .5) if seconds(cap, value) <= budget), .5)
    low, high = base / 1.6, cap * 8
    if seconds(low, lifts) <= budget:
        high = low
    for _ in range(40):
        if high - low < 1:
            break
        middle = (low + high) / 2
        low, high = (middle, high) if seconds(middle, lifts) > budget else (low, middle)
    segments, parts, bands, end, tip = timeline(regions, shading, part_count, tip, high, lifts, start, total, fps, record=True)
    counts = {'fill_regions': len(regions), 'fill_passes': sum(len(r['cells']) for r in regions),
              'marker_radius_scale': scale, 'fill_speed_px_s': round(high, 1), 'fill_over_cap': bool(high > cap * 1.001),
              'shaded_parts': len(shading)}
    return segments, parts, bands, end / total, tip, counts


def render(shape, regions, bands, owners):
    """Marker reveal maps: first touch time and its coverage (soft, grainy edge), full coverage time,
    flat colors, and per-pixel shading time (flat -> source) for shaded parts."""
    first = np.full(shape, np.inf, np.float32)
    cover = np.zeros(shape, np.float32)
    full = np.full(shape, np.inf, np.float32)
    flat = np.zeros(shape + (3,), np.float32)
    grain = (.8 + .2 * np.random.default_rng(7).random(shape)).astype(np.float32)
    for region in regions:
        (left, top), (height, width) = region['offset'], region['mask'].shape
        mask, radius = region['mask'], region['radius']
        edge = max(4., region.get('soft', 0.) * radius)
        box = (slice(top, top + height), slice(left, left + width))
        flat[box][mask] = region['flat']
        first_local, cover_local, full_local, grain_local = first[box], cover[box], full[box], grain[box]
        for cell, times in region['passes']:
            for (x, y), when in zip(cell - region['offset'], times):
                x0, x1 = int(max(0, x - radius - 3)), int(min(width, x + radius + 4))
                y0, y1 = int(max(0, y - radius - 3)), int(min(height, y + radius + 4))
                if x1 <= x0 or y1 <= y0:
                    continue
                ys, xs = np.ogrid[y0:y1, x0:x1]
                window = (slice(y0, y1), slice(x0, x1))
                coverage = np.clip((radius + 2 - np.hypot(xs - x, ys - y)) / edge, 0, 1) * mask[window]
                touched = (coverage > .1) & ~np.isfinite(first_local[window])
                first_local[window][touched] = when
                passing = (coverage > 0) & (when - first_local[window] <= region['window'])
                cover_local[window][passing] = np.maximum(cover_local[window], coverage * grain_local[window])[passing]
                solid = (coverage >= .85) & ~np.isfinite(full_local[window])
                full_local[window][solid] = when
        missing = mask & ~np.isfinite(first_local)
        if missing.any():
            _, nearest = ndimage.distance_transform_edt(~(np.isfinite(first_local) & mask), return_indices=True)
            first_local[missing] = first_local[nearest[0], nearest[1]][missing]
            cover_local[missing] = 1
        late = mask & ~np.isfinite(full_local)
        full_local[late] = np.maximum(first_local[late], region['end'])
    shade = full.copy()
    for band in bands:
        ys, xs = np.nonzero(owners == band['part'])
        relative = np.c_[xs, ys].astype(float) - band['center']
        side, along = relative @ band['across'], relative @ band['axis']
        index = np.clip(np.searchsorted(band['edges'], side) - 1, 0, len(band['times']) - 1)
        low, high = band['along']
        fraction = np.clip((along - low) / max(1., high - low), 0, 1) * .8
        starts, ends = np.array(band['times']).T
        shade[ys, xs] = starts[index] + fraction * (ends[index] - starts[index])
    return first, cover, full, flat, shade

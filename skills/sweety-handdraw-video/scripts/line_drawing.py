"""Order, time and render whole line strokes like a right-handed person: per part the silhouette first,
then long interior structure, then details and short marks. Every mark is drawn by the nib: a picture that cannot
be drawn in its window is simplified upstream, never faded in."""
import math
import re
import numpy as np
from scipy import ndimage
from motion_profile import PEAK, time_fraction
from line_topology import turning
from stroke_paths import calculate_lengths

LINE_SPEED = 420.     # px/s along curvature-weighted length: a person inking a confident contour at 1080p
SPEED_FACTOR = 4.     # calibrated cap: speed-drawing videos run 4-8x life and still read as human when the order is coherent
FRAME_PX = 80.        # most an inking nib may move in one frame and still read as continuous motion
TRAVEL_PX = 120.      # peak per-frame move of a lifted pen: a quick flick between strokes, never a teleport
DETAIL_BOOST = 1.6    # details and short marks may run this much faster than the main strokes under pressure
VIDEO_LAPSE = 3.      # pictures are drawn at about 3x real pace, then hold; never slowed down to fill a window
PART_PAUSE = .18     # thinking pause between semantic parts at human pace (scaled by lapse)
REVERSE_COST = 250.  # px of extra travel a right-hander accepts rather than draw a line upward or leftward
HURRIED = REVERSE_COST / SPEED_FACTOR  # a hand at the speed cap cares less about direction
KEY_PART = re.compile(r'head|face|eye|mouth|nose|hand|finger|paw', re.I)
SILHOUETTE, KEY, STRUCTURE, DETAIL, TEXTURE = range(5)


def hashed(value):
    return (math.sin(value * 12.9898 + 4.1414) * 43758.5453) % 1.


def weighted_lengths(points):
    """Arc length inflated on tight turns so the nib slows down on curves and corners (turn measured over
    +-6 px so skeleton pixel jitter does not count as curvature)."""
    steps = np.linalg.norm(np.diff(points, axis=0), axis=1)
    if not len(steps):
        return np.zeros(1)
    turn = np.radians(turning(points, 6))[1:] / 6
    return np.r_[0., np.cumsum(steps * (1 + 1.5 * np.clip(turn / (np.pi / 8), 0, 1)))]


def line_cap(speed):
    """Mean nib speed cap: SPEED_FACTOR x human pace (each stroke is further held inside FRAME_PX per frame)."""
    return LINE_SPEED * SPEED_FACTOR * speed


def lift_seconds(distance, fps=30, scale=1., lapse=1.):
    """Pen-up time: 80-150 ms at human pace, time-lapsed with the ink down to 2 frames, never so short that the
    renderer's lifted move (75% of it in min-jerk motion, peak 1.875x mean) exceeds TRAVEL_PX per frame."""
    return max(2. / fps, (.08 + .07 * min(1., distance / 300.)) * scale / lapse, 1.875 * distance / (.75 * TRAVEL_PX * fps))


def stroke_profile(stroke, lapse):
    """Min-jerk at human pace; when time-lapsed, long strokes cruise between eased ends (lower peak speed)."""
    return 'plateau' if lapse > 1.5 and stroke['length'] >= 60 else 'minjerk'


def classify(strokes, owners, alpha, pixels, part_ids, key_parts=()):
    """Assign each whole stroke to the part owning most of it, and a priority tier."""
    height, width = alpha.shape
    owned = owners >= 0
    _, nearest = ndimage.distance_transform_edt(~owned, return_indices=True)
    owner_map = owners[nearest[0], nearest[1]]
    exterior = ndimage.distance_transform_edt(ndimage.binary_fill_holes(alpha > 30))
    rgb = pixels[:, :, :3].astype(float)
    luminance = np.where(alpha > 30, rgb @ [.299, .587, .114], 255.)
    ink_reference = np.percentile(luminance[alpha > 30], 2) if (alpha > 30).any() else 0.
    offsets = np.array([(dx, dy) for dy in range(-2, 3) for dx in range(-2, 3)])
    for index, stroke in enumerate(strokes):
        cells = np.clip(np.rint(stroke['points']).astype(int), 0, [width - 1, height - 1])
        stroke['part'] = int(np.bincount(owner_map[cells[:, 1], cells[:, 0]]).argmax())
        window = np.clip(cells[:, None, :] + offsets[None], 0, [width - 1, height - 1])
        values = luminance[window[..., 1], window[..., 0]]
        darkest = np.argmin(values, axis=1)
        chosen = window[np.arange(len(cells)), darkest]
        stroke['color'] = ndimage.uniform_filter1d(rgb[chosen[:, 1], chosen[:, 0]], 9, axis=0, mode='nearest')
        tone = np.clip((250 - values.min(1)) / max(20., 250 - ink_reference), 0, 1)
        stroke['tone'] = float(tone.mean())
        silhouette = (exterior[cells[:, 1], cells[:, 0]] <= 4).mean() >= .5
        key = part_ids[stroke['part']] in key_parts or bool(KEY_PART.search(part_ids[stroke['part']]))
        if stroke['micro'] or stroke['tone'] < .3 or (stroke['length'] < 25 and stroke['tone'] < .5 and not key):
            stroke['tier'] = TEXTURE
        elif silhouette and stroke['length'] >= 20:
            stroke['tier'] = SILHOUETTE
        elif key:
            stroke['tier'] = KEY
        else:
            stroke['tier'] = STRUCTURE if stroke['length'] >= 40 else DETAIL
        stroke['weights'] = weighted_lengths(stroke['points'])
        stroke['index'] = index
    return strokes


def orient(stroke, tip, reverse=REVERSE_COST):
    """Right-hander prior as a point index order: loops start at the top going counter-clockwise,
    lines steeper than ~30 degrees run down, flatter ones run right; tiny marks start nearer the pen."""
    points = stroke['points']
    count = len(points)
    forward, backward = np.arange(count), np.arange(count)[::-1]
    if stroke['closed']:
        top = int(np.argmin(points[:, 1] * 1000 + points[:, 0]))
        ring = np.r_[np.arange(top, count), np.arange(0, top + 1)]
        loop = points[ring]
        area = np.sum(loop[:-1, 0] * loop[1:, 1] - loop[1:, 0] * loop[:-1, 1])
        return (ring[::-1] if area > 0 else ring), None
    delta = points[-1] - points[0]
    if np.linalg.norm(delta) < 8:
        near = np.linalg.norm(points[0] - tip) <= np.linalg.norm(points[-1] - tip)
        return (forward if near else backward), None
    if abs(delta[1]) >= .6 * abs(delta[0]):
        return (forward if delta[1] >= 0 else backward), reverse
    return (forward if delta[0] >= 0 else backward), reverse


def apply_order(stroke, index):
    if not np.array_equal(index, np.arange(len(stroke['points']))):
        for key in ('points', 'radius', 'color'):
            stroke[key] = stroke[key][index]
        stroke['weights'] = weighted_lengths(stroke['points'])


def order(strokes, part_count, tip, reverse=REVERSE_COST):
    """Within each semantic part and priority tier, scan top to bottom then left to right.
    Travel distance may choose a stroke's direction, never move a lower stroke ahead of an upper one."""
    ordered = []
    tip = np.asarray(tip, float)
    for part in range(part_count):
        for tier in (SILHOUETTE, KEY, STRUCTURE, DETAIL, TEXTURE):
            pool = sorted((stroke for stroke in strokes if stroke['part'] == part and stroke['tier'] == tier),
                          key=lambda stroke: (float(stroke['points'][:, 1].min()), float(stroke['points'][:, 0].min()), stroke['index']))
            for stroke in pool:
                index, penalty = orient(stroke, tip, reverse)
                points = stroke['points']
                if penalty is not None and np.linalg.norm(points[index[-1]] - tip) + penalty < np.linalg.norm(points[index[0]] - tip):
                    index = index[::-1]
                apply_order(stroke, index)
                ordered.append(stroke)
                tip = stroke['points'][-1]
    return ordered, tip


def duration(stroke, speed, fps, lapse=1.):
    """(seconds, profile): at least 2 frames, and the profile's peak speed stays inside FRAME_PX per frame."""
    profile = stroke_profile(stroke, lapse)
    return max(2. / fps, stroke['weights'][-1] / min(speed, FRAME_PX * fps / PEAK[profile])), profile


def line_seconds(ordered, speed, fps, detail_speed=None, lapse=1., part_count=None):
    total, previous, last_part = 0., None, None
    for stroke in ordered:
        rate = detail_speed if detail_speed and stroke['tier'] >= DETAIL else speed
        total += duration(stroke, rate, fps, lapse)[0]
        if previous is not None:
            total += lift_seconds(np.linalg.norm(stroke['points'][0] - previous), fps, 1.1, lapse)
        if last_part is not None and stroke['part'] != last_part:
            total += PART_PAUSE / max(lapse, 1e-6)
        previous, last_part = stroke['points'][-1], stroke['part']
    return total


def requirements(strokes, part_count, tip, speed, fps):
    """Seconds for: every stroke at human pace; every stroke at the cap; silhouette + key features at the cap."""
    every = [dict(stroke) for stroke in strokes]
    essential = [dict(stroke) for stroke in strokes if stroke['tier'] in (SILHOUETTE, KEY)]
    cap = line_cap(speed)
    at_cap = lambda kept, detail: line_seconds(order(kept, part_count, tip, HURRIED)[0], cap, fps, detail, SPEED_FACTOR)
    return {'required': line_seconds(order(every, part_count, tip)[0], LINE_SPEED * speed, fps),
            'all_at_cap': at_cap(every, cap * DETAIL_BOOST), 'minimum': at_cap(essential, None)}


def schedule(strokes, part_count, tip, budget, start, total, speed=1., fps=30):
    """Fit every stroke into budget seconds (faster up to the cap, never fewer strokes). Returns ordered strokes with
    times (fractions of total), segments, per-part [start, end] fractions and counts."""
    base, cap = LINE_SPEED * speed, line_cap(speed)
    budget += 1e-6  # a window at exactly the minimum sums the same terms; float noise must not read as haste past the cap
    # A hurried hand takes the nearer end more often: soften the direction prior only for haste beyond video pace
    # (a time-lapse of a calm hand keeps its order and directions); at the cap it is the order the minimum assumed.
    reverse = REVERSE_COST / float(np.clip(line_seconds(order(strokes, part_count, tip)[0], base, fps) / VIDEO_LAPSE / max(budget, 1e-6), 1, SPEED_FACTOR))
    ordered, _ = order(strokes, part_count, tip, reverse)
    seconds = lambda pace, detail=None: line_seconds(ordered, pace, fps, detail, max(1., pace / base))
    if seconds(cap, cap * DETAIL_BOOST) > budget:
        ordered, _ = order(strokes, part_count, tip, HURRIED)
    low, high = base, cap
    if seconds(base) <= budget:
        speed_used = max(base / 1.6, base * seconds(base) / max(budget, 1e-6))
    else:
        if seconds(cap, cap * DETAIL_BOOST) > budget:
            high = cap * 64
        for _ in range(48):
            middle = (low + high) / 2
            pressure = min(1., (middle - base) / max(1e-6, cap - base))
            if seconds(middle, middle * (1 + (DETAIL_BOOST - 1) * pressure)) > budget:
                low = middle
            else:
                high = middle
        speed_used = high
    pressure = min(1., max(0., (speed_used - base) / max(1e-6, cap - base)))
    detail_speed = speed_used * (1 + (DETAIL_BOOST - 1) * pressure)
    lapse = max(1., speed_used / base)
    segments, cursor, previous, parts = [], start, None, []
    for part in range(part_count):
        part_start = cursor
        mine = [stroke for stroke in ordered if stroke['part'] == part]
        for stroke in mine:
            if previous is not None:
                lift = lift_seconds(np.linalg.norm(stroke['points'][0] - previous), fps, .9 + .2 * hashed(stroke['index']), lapse)
                segments.append({'kind': 'travel', 'start': cursor / total, 'end': (cursor + lift) / total,
                                 'points': [previous.tolist(), stroke['points'][0].tolist()]})
                cursor += lift
            seconds, profile = duration(stroke, detail_speed if stroke['tier'] >= DETAIL else speed_used, fps, lapse)
            fraction = time_fraction(stroke['weights'] / max(1e-6, stroke['weights'][-1]), profile)
            stroke['times'] = (cursor + seconds * fraction) / total
            segments.append({'kind': 'ink', 'start': cursor / total, 'end': (cursor + seconds) / total, 'profile': profile,
                             'points': stroke['points'].tolist(), 'lengths': stroke['weights'].tolist()})
            cursor += seconds
            previous = stroke['points'][-1]
        parts.append([part_start / total, cursor / total])
        # Humans pause briefly when switching semantic parts (not between strokes of the same part).
        if mine and part + 1 < part_count and any(s['part'] == part + 1 for s in ordered):
            pause = PART_PAUSE / max(lapse, 1e-6)
            segments.append({'kind': 'pause', 'start': cursor / total, 'end': (cursor + pause) / total,
                             'points': [previous.tolist(), previous.tolist()] if previous is not None else None})
            cursor += pause
    end = cursor / total
    counts = {'drawn_stroke_count': len(ordered), 'line_speed_px_s': round(speed_used, 1), 'speed_over_cap': bool(speed_used > cap * 1.001),
              'drawn_fast_count': sum(s['tier'] >= DETAIL and pressure > 0 for s in ordered),
              'part_pause_seconds': PART_PAUSE}
    return ordered, segments, parts, end, counts


def taper(stroke):
    distances = calculate_lengths(stroke['points'])
    reach = max(1., min(.15 * distances[-1], 18.))
    envelope = np.clip(np.minimum(distances, distances[-1] - distances) / reach, 0, 1)
    return .3 + .7 * envelope * envelope * (3 - 2 * envelope)


def stamp(stroke, shape, alpha, color, records, times, opacity):
    radius = stroke['radius'] * taper(stroke)
    for k, ((x, y), r) in enumerate(zip(stroke['points'], radius)):
        left, right = int(max(0, x - r - 2)), int(min(shape[1], x + r + 3))
        top, bottom = int(max(0, y - r - 2)), int(min(shape[0], y + r + 3))
        if right <= left or bottom <= top:
            continue
        ys, xs = np.ogrid[top:bottom, left:right]
        coverage = np.clip(r + .5 - np.hypot(xs - x, ys - y), 0, 1) * opacity[k]
        window = (slice(top, bottom), slice(left, right))
        stronger = coverage > alpha[window]
        color[window][stronger] = stroke['color'][k]
        np.maximum(alpha[window], coverage, out=alpha[window])
        for threshold, time, owner in records:
            earlier = (coverage > threshold) & (times[k] < time[window])
            time[window][earlier] = times[k]
            owner[window][earlier] = stroke['part']


def render(shape, strokes):
    """Draw the line layer: variable width from the source ink, pressure taper, slight opacity variation.
    Each pixel's reveal time is when the nib passes it."""
    alpha = np.zeros(shape, np.float32)
    color = np.zeros(shape + (3,), np.float32)
    # Solid hits (coverage > .3) decide time/owner; faint anti-alias edges fall back to their first touch.
    records = [(threshold, np.full(shape, np.inf, np.float32), np.full(shape, -1, np.int16)) for threshold in (.3, 0.)]
    for stroke in strokes:
        count = len(stroke['points'])
        opacity = (.9 + .1 * hashed(stroke['index'] + 7)) * (1 + .05 * np.sin(np.linspace(0, 3, count) + stroke['index']))
        stamp(stroke, shape, alpha, color, records, stroke['times'], np.clip(opacity, 0, 1))
    (_, hit, owner), (_, touch, touch_owner) = records
    only = ~np.isfinite(hit) & np.isfinite(touch)
    hit[only], owner[only] = touch[only], touch_owner[only]
    return color, alpha, hit, owner


def simplify(points, epsilon):
    """Ramer-Douglas-Peucker polyline simplification."""
    if len(points) < 3:
        return points
    direction = points[-1] - points[0]
    norm = np.linalg.norm(direction)
    offsets = points - points[0]
    distances = np.abs(direction[0] * offsets[:, 1] - direction[1] * offsets[:, 0]) / norm if norm > 1e-9 else np.linalg.norm(offsets, axis=1)
    k = int(np.argmax(distances))
    if distances[k] <= epsilon:
        return points[[0, -1]]
    return np.vstack([simplify(points[:k + 1], epsilon)[:-1], simplify(points[k:], epsilon)])


def sketch_strokes(owners, alpha, part_count):
    """Light construction pass: each part's silhouette simplified to a few straight-ish segments with a small wobble."""
    from line_topology import graph, resample, trace
    from stroke_paths import thin_ink
    strokes = []
    for part in range(part_count):
        labels, count = ndimage.label(ndimage.binary_opening((owners == part) & (alpha > 30), iterations=2))
        sizes = np.bincount(labels.ravel())
        sizes[0] = 0
        if not count or sizes.max() < 400:
            continue
        shape = ndimage.binary_fill_holes(labels == sizes.argmax())
        paths = trace(graph(thin_ink(shape & ~ndimage.binary_erosion(shape))))
        if not paths:
            continue
        rough = simplify(max(paths, key=len), max(3., .05 * math.sqrt(sizes.max())))
        wobble = np.array([[hashed(part * 31 + k) - .5, hashed(part * 17 + k + 5) - .5] for k in range(len(rough))]) * 4
        points = resample(rough + wobble)
        strokes.append({'points': points, 'part': part, 'radius': np.full(len(points), .8),
                        'color': np.full((len(points), 3), 120.), 'index': part, 'tier': SILHOUETTE,
                        'weights': weighted_lengths(points)})
    return strokes

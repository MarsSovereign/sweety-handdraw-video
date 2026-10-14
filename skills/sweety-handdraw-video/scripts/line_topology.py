"""Whole-image line topology: clean the outline once, skeletonize it once, and join pieces through
junctions by tangent continuity so a stroke follows the line a person would draw in one motion."""
import math
import numpy as np
from scipy import ndimage
from scipy.spatial import cKDTree
from stroke_paths import thin_ink, calculate_lengths

STEPS = [(-1, 0), (0, 1), (1, 0), (0, -1), (-1, -1), (-1, 1), (1, 1), (1, -1)]
MICRO = 15.          # px; shorter pieces are absorbed by a neighbour or treated as texture
MAX_STROKE = 420.    # px; longer runs are split where a hand would re-grip


def clean_outline(outline, hole=12, speck=20):
    """Fill pinholes inside ink and drop isolated specks before skeletonizing."""
    gaps, _ = ndimage.label(~outline)
    small = np.bincount(gaps.ravel()) <= hole
    small[0] = False
    filled = outline | small[gaps]
    blobs, _ = ndimage.label(filled, structure=np.ones((3, 3)))
    keep = np.bincount(blobs.ravel()) >= speck
    keep[0] = False
    return keep[blobs]


def graph(skeleton):
    ys, xs = np.nonzero(skeleton)
    pixels = set(zip(ys.tolist(), xs.tolist()))
    return {(y, x): [(y + dy, x + dx) for dy, dx in STEPS if (y + dy, x + dx) in pixels
                     and not (dy and dx and ((y, x + dx) in pixels or (y + dy, x) in pixels))]
            for y, x in pixels}


def trace(adjacency):
    """Split the skeleton graph into chains between junctions/endpoints (x, y order)."""
    used, paths = set(), []

    def walk(start, step):
        chain, previous, current = [start, step], start, step
        used.add(frozenset((start, step)))
        while len(adjacency[current]) == 2 and current != start:
            following = [n for n in adjacency[current] if n != previous]
            if not following or frozenset((current, following[0])) in used:
                break
            previous, current = current, following[0]
            used.add(frozenset((previous, current)))
            chain.append(current)
        return np.array([(x, y) for y, x in chain], float)

    for node in sorted(adjacency, key=lambda p: (len(adjacency[p]) == 2, p)):
        if not adjacency[node]:
            paths.append(np.array([[node[1], node[0]], [node[1] + .5, node[0]]], float))
        for step in adjacency[node]:
            if frozenset((node, step)) not in used:
                paths.append(walk(node, step))
    return paths


def prune_spurs(skeleton, limit=6., rounds=2):
    skeleton = skeleton.copy()
    for _ in range(rounds):
        adjacency = graph(skeleton)
        for path in trace(adjacency):
            ends = [(int(path[0][1]), int(path[0][0])), (int(path[-1][1]), int(path[-1][0]))]
            degrees = [len(adjacency.get(end, ())) for end in ends]
            if calculate_lengths(path)[-1] < limit and min(degrees) <= 1 and max(degrees) >= 3:
                junction = ends[int(np.argmax(degrees))]
                for x, y in path.astype(int):
                    if (y, x) != junction:
                        skeleton[y, x] = False
    return skeleton


def end_direction(path, at_start, reach=6):
    k = min(reach, len(path) - 1)
    vector = path[k] - path[0] if at_start else path[-1 - k] - path[-1]
    norm = np.linalg.norm(vector)
    return vector / norm if norm > 1e-6 else vector


def supported(a, b, support_distance):
    samples = np.linspace(a, b, max(3, int(np.linalg.norm(b - a) * 2) + 1))
    values = support_distance[np.rint(samples[:, 1]).astype(int), np.rint(samples[:, 0]).astype(int)]
    return (values <= 1.5).mean() >= .8


def join(paths, support_distance, gap=10., turn=35.):
    """Pair path ends whose tangents continue (<= turn degrees); bridge small gaps only over ink."""
    joins = 0
    while len(paths) > 1:
        ends = [(index, start, path[0] if start else path[-1]) for index, path in enumerate(paths) for start in (True, False)]
        proposals = []
        for first, second in cKDTree([end[2] for end in ends]).query_pairs(gap):
            (i, start_i, point_i), (j, start_j, point_j) = ends[first], ends[second]
            if i == j:
                continue
            cosine = -np.dot(end_direction(paths[i], start_i), end_direction(paths[j], start_j))
            angle = math.degrees(math.acos(float(np.clip(cosine, -1, 1))))
            distance = float(np.linalg.norm(point_i - point_j))
            if angle <= turn and (distance <= 1.5 or supported(point_i, point_j, support_distance)):
                proposals.append((angle + 4 * distance, i, start_i, j, start_j, distance))
        used, joined = set(), []
        for _, i, start_i, j, start_j, distance in sorted(proposals):
            if i in used or j in used:
                continue
            used |= {i, j}
            head = paths[i][::-1] if start_i else paths[i]
            tail = paths[j] if start_j else paths[j][::-1]
            bridge = np.linspace(head[-1], tail[0], max(2, int(math.ceil(distance)) + 1))[1:-1]
            joined.append(np.vstack([head, bridge, tail[1:] if distance <= 1.5 else tail]))
        if not joined:
            break
        joins += len(joined)
        paths = [path for index, path in enumerate(paths) if index not in used] + joined
    return paths, joins


def resample(path, step=1.):
    lengths = calculate_lengths(path)
    if lengths[-1] < step:
        return path[[0, -1]].astype(float)
    samples = np.linspace(0, lengths[-1], max(2, int(lengths[-1] / step) + 1))
    return np.c_[np.interp(samples, lengths, path[:, 0]), np.interp(samples, lengths, path[:, 1])]


def turning(points, window=4):
    index = np.arange(len(points))
    before = points - points[np.clip(index - window, 0, len(points) - 1)]
    after = points[np.clip(index + window, 0, len(points) - 1)] - points
    norms = np.linalg.norm(before, axis=1) * np.linalg.norm(after, axis=1)
    cosine = np.where(norms > 1e-9, (before * after).sum(1) / np.maximum(norms, 1e-9), 1.)
    return np.degrees(np.arccos(np.clip(cosine, -1, 1)))


def split_for_hand(points, corner=120., maximum=MAX_STROKE):
    """Lift only at hairpin corners and re-grip on very long runs; ordinary corners are drawn through
    (the nib slows there via the curvature weighting)."""
    if len(points) < 30:
        return [points]
    angles = turning(points)
    peaks = [k for k in range(15, len(points) - 15) if angles[k] >= corner and angles[k] == angles[k - 3:k + 4].max()]
    pieces, last = [], 0
    for k in peaks:
        if k - last >= 15:
            pieces.append(points[last:k + 1])
            last = k
    pieces.append(points[last:])
    result = []
    while pieces:
        piece = pieces.pop(0)
        if len(piece) <= maximum:
            result.append(piece)
            continue
        third = len(piece) // 3
        middle = third + int(np.argmax(turning(piece)[third:2 * third]))
        pieces[:0] = [piece[:middle + 1], piece[middle:]]
    return result


def extract_strokes(outline, support=None):
    """Return whole strokes [{'points','radius','closed','micro'}] for an outline mask, and statistics."""
    clean = clean_outline(outline)
    if not clean.any():
        clean = outline.copy()
    skeleton = prune_spurs(thin_ink(clean))
    support_mask = clean if support is None else (support | clean)
    support_distance = ndimage.distance_transform_edt(~support_mask)
    raw = trace(graph(skeleton))
    paths, joins = join(raw, support_distance)
    paths = [resample(path) for path in paths]
    lengths = [calculate_lengths(path)[-1] for path in paths]
    shortest = min(MICRO, .04 * max(outline.shape))
    long_paths = [path for path, length in zip(paths, lengths) if length >= shortest]
    tree = cKDTree(np.vstack(long_paths)) if long_paths else None
    strokes, absorbed = [], 0
    half_width = ndimage.distance_transform_edt(clean)
    for path, length in zip(paths, lengths):
        micro = length < shortest
        if micro and tree is not None and tree.query(path)[0].max() <= 3:
            absorbed += 1
            continue
        if len(path) > 9:
            path = np.r_[path[:1], ndimage.uniform_filter1d(path, 5, axis=0, mode='nearest')[1:-1], path[-1:]]
        closed = len(path) > 8 and np.linalg.norm(path[0] - path[-1]) <= 1.5
        for piece in ([path] if micro else split_for_hand(path)):
            cells = np.clip(np.rint(piece).astype(int), 0, [clean.shape[1] - 1, clean.shape[0] - 1])
            radius = ndimage.uniform_filter1d(np.clip(half_width[cells[:, 1], cells[:, 0]] - .6, .6, 5.), 7, mode='nearest')
            strokes.append({'points': piece, 'radius': radius, 'closed': bool(closed and len(piece) == len(path)),
                            'micro': micro, 'length': float(calculate_lengths(piece)[-1])})
    statistics = {'outline_px': int(outline.sum()), 'holes_filled_px': int((clean & ~outline).sum()),
                  'specks_removed_px': int((outline & ~clean).sum()), 'skeleton_px': int(skeleton.sum()),
                  'raw_pieces': len(raw), 'junction_joins': joins, 'micro_absorbed': absorbed}
    return strokes, clean, skeleton, statistics

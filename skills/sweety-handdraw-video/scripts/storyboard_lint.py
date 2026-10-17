#!/usr/bin/env python3
"""Storyboard gate: visual cadence, reuse, variety, visual count and drawing time come from the real narration."""
import argparse
import hashlib
import json
import math
import statistics
import sys
from pathlib import Path
from line_drawing import VIDEO_LAPSE

COVER = 1.0  # video time = narration time + the fixed one-second cover
PICTURES = ('illustration', 'diagram')
KINDS = PICTURES + ('text_group',)
LEAD, HOLD, GAP = .54, .6, .08
# The minimum structure that makes a declared shot type true; the director still judges the content.
SHOT_TYPES = {
    'scene_with_background': ('an illustration with a declared background', lambda visuals: any(visual.get('kind') == 'illustration' and visual.get('background') for visual in visuals)),
    'comparison': ('two pictures (illustration/diagram) side by side', lambda visuals: sum(visual.get('kind') in PICTURES for visual in visuals) >= 2),
    'process_sequence': ('at least two visuals as steps', lambda visuals: len(visuals) >= 2),
    'closeup': ('an illustration', lambda visuals: any(visual.get('kind') == 'illustration' for visual in visuals)),
    'key_points': ('a text_group', lambda visuals: any(visual.get('kind') == 'text_group' for visual in visuals)),
    'overview_recap': ('at least two visuals', lambda visuals: len(visuals) >= 2),
    'character_action': ('an illustration featuring a declared character', lambda visuals: any(visual.get('kind') == 'illustration' and visual.get('characters') for visual in visuals)),
}


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load(root, manifest):
    reference = manifest.get('storyboard')
    return json.loads((Path(root) / reference).read_text()) if reference else None


def max_hold(board):
    value = float(board.get('max_hold_seconds', 8))
    if not 5 <= value <= 12:
        raise ValueError('storyboard max_hold_seconds must be within 5..12')
    return value


def group_beats(beat_ids, group):
    """Beat ids covered by a page visual group; start/end may be page-local indices or beat ids."""
    def index(value):
        if isinstance(value, str) and value in beat_ids:
            return beat_ids.index(value)
        if isinstance(value, int) and 0 <= value < len(beat_ids):
            return value
        raise ValueError(f'Visual group beat {value!r} is not a beat id or index on this page')
    first, last = index(group['start_beat']), index(group.get('end_beat', group['start_beat']))
    if first > last:
        raise ValueError('Visual group beats must form an ordered range inside this page')
    return beat_ids[first:last + 1]


STROKES_PER_SECOND = 3.5  # measured draw-v2 pace at the cap: contour strokes, pen lifts and marker color included
MAX_DRAWING_SHARE = .7    # the pen is busy at most this share of the narration; the finished pictures hold the rest


def tier(seconds, hold):
    strokes = max(3, int(seconds * STROKES_PER_SECOND))
    if seconds < 3:
        return f'label or tiny icon: at most ~{strokes} strokes'
    if seconds < 6:
        return f'simple icon/silhouette: at most ~{strokes} strokes, flat fills'
    if seconds <= hold:
        return f'one subject: at most ~{strokes} strokes, flat fills, no texture lines'
    return (f'at most ~{strokes} strokes: a scene with background (declare background; each semantic part paces the drawing) '
            'or split into two visuals')


def spans(board, beats):
    """[(shot, start, end)] in video time; a shot lasts until the next shot's first beat."""
    shots = board.get('shots') or []
    if not shots or not all(shot.get('beats') for shot in shots):
        raise ValueError('Storyboard needs shots, each covering at least one beat')
    if [beat_id for shot in shots for beat_id in shot['beats']] != [beat['id'] for beat in beats]:
        raise ValueError('Storyboard shots must list every narration beat id exactly once, in narration order')
    times = {beat['id']: beat for beat in beats}
    ends = [times[shot['beats'][0]]['start'] for shot in shots[1:]] + [beats[-1]['end']]
    return [(shot, times[shot['beats'][0]]['start'], end) for shot, end in zip(shots, ends)]


def plan(board, beats):
    """Checks that need only the storyboard and beat times; returns the per-visual drawing budget."""
    hold, errors, warnings = max_hold(board), [], []
    timed = spans(board, beats)
    times = {beat['id']: beat for beat in beats}
    characters = {character.get('id') for character in board.get('characters', [])}
    seen, rows = set(), []
    for shot, start, end in timed:
        label, visuals = shot.get('id', shot['beats'][0]), shot.get('visuals', [])
        for visual in visuals:
            if visual.get('kind') not in KINDS or not visual.get('id') or visual['id'] in seen or not str(visual.get('subject', '')).strip():
                errors.append(f'{label}: every visual needs a unique id, a subject and kind in {KINDS}')
            seen.add(visual.get('id'))
            if set(visual.get('characters', [])) - characters:
                errors.append(f'{label}/{visual.get("id")}: undeclared character; add it with a locked reference image')
        if shot.get('type') not in SHOT_TYPES:
            errors.append(f'{label}: type must be one of {sorted(SHOT_TYPES)}')
        elif not SHOT_TYPES[shot['type']][1](visuals):
            errors.append(f'{label}: a {shot["type"]} shot needs {SHOT_TYPES[shot["type"]][0]}')
        if not str(shot.get('purpose', '')).strip():
            errors.append(f'{label}: state what this shot shows beyond the narration (purpose)')
        # Estimate: one visual group per shot, drawn in order and split by weight like build().
        weights = [float(visual.get('weight', 3 if visual.get('kind') == 'illustration' else 1)) for visual in visuals]
        cursor = start + LEAD
        window = times[shot['beats'][-1]]['end'] - HOLD - cursor - GAP * max(0, len(visuals) - 1)
        for visual, weight in zip(visuals, weights):
            seconds = window * weight / sum(weights)
            rows.append({'shot': label, 'type': shot.get('type'), 'visual': visual.get('id'), 'kind': visual.get('kind'), 'subject': visual.get('subject'),
                         'start': round(cursor, 2), 'seconds': round(seconds, 2), 'scene': bool(visual.get('background')),
                         'complexity': tier(seconds * MAX_DRAWING_SHARE, hold) if visual.get('kind') in PICTURES else 'short text lines'})
            if seconds < .5:
                errors.append(f'{label}/{visual.get("id")}: only {seconds:.1f}s to draw; give the shot more beats or fewer visuals')
            cursor += seconds + GAP
    narration = timed[-1][2] - timed[0][1]
    visuals = [visual for shot, _, _ in timed for visual in shot.get('visuals', [])]
    pictures = sum(visual.get('kind') in PICTURES for visual in visuals)
    required = math.ceil(narration / hold)
    shares, text_only = {}, 0.
    for shot, start, end in timed:
        shares[shot.get('type')] = shares.get(shot.get('type'), 0) + (end - start) / narration
        if not any(visual.get('kind') in PICTURES for visual in shot.get('visuals', [])):
            text_only += (end - start) / narration
    sparse = sorted(((end - start) / max(1, len(shot.get('visuals', []))), shot.get('id')) for shot, start, end in timed)[::-1][:3]
    if len(visuals) < required:
        errors.append(f'{len(visuals)} planned visuals for {narration:.1f}s of narration; at least {required} are needed '
                      f'(a new visual every {hold:g}s). Add visuals to the sparsest shots: '
                      + ', '.join(f'{identity} ({seconds:.1f}s per visual)' for seconds, identity in sparse))
    if pictures < math.ceil(.8 * required) and not board.get('text_only_reason'):
        errors.append(f'{pictures} illustrations/diagrams planned; at least {math.ceil(.8 * required)} are needed. '
                      'If this host cannot generate images, stop and tell the user instead of shipping text-only visuals')
    if text_only > .2 and not board.get('text_only_reason'):
        errors.append(f'Text-only shots fill {text_only:.0%} of the runtime (limit 20%); add illustrations/diagrams or declare text_only_reason')
    if narration > 45 and len(shares) < 3:
        errors.append(f'{narration:.0f}s film uses {len(shares)} shot type(s); use at least 3 of {sorted(SHOT_TYPES)}')
    dominant = max(shares, key=shares.get)
    if narration > 45 and shares[dominant] > .6:
        errors.append(f'Shot type {dominant} fills {shares[dominant]:.0%} of the runtime (limit 60%); vary composition')
    # Planned cadence estimate: scene visuals are assumed to be paced by their parts (build checks the real parts).
    points = [row['start'] + step * hold for row in rows for step in range(1 + (int(row['seconds'] // hold) if row['scene'] else 0))]
    stretch = longest(points, timed[0][1], timed[-1][2])
    if stretch[0] > hold:
        warnings.append(f'Estimated {stretch[0]:.1f}s with nothing new between {stretch[1]:.1f}s and {stretch[2]:.1f}s; split that shot or add a visual')
    stats = {'narration_seconds': round(narration, 2), 'max_hold_seconds': hold, 'required_visuals': required,
             'planned_visuals': len(visuals), 'planned_pictures': pictures, 'text_only_share': round(text_only, 3),
             'shot_type_shares': {key: round(value, 3) for key, value in shares.items()}}
    return {'errors': errors, 'warnings': warnings, 'stats': stats, 'visuals': rows}


def longest(points, start, end):
    points = sorted([start, end] + [point for point in points if start < point < end])
    return max((right - left, left, right) for left, right in zip(points, points[1:]))


def page_actions(manifest):
    """(page_index, beat ids where the action may draw, action) for every action in job.json."""
    for page_index, page in enumerate(manifest['pages']):
        ids = [beat.get('id', f'p{page_index:02d}-b{index:02d}') for index, beat in enumerate(page['beats'])]
        for beat_id, beat in zip(ids, page['beats']):
            for action in beat.get('actions', []):
                yield page_index, [beat_id], action
        for group in page.get('visual_groups', []):
            covered = group_beats(ids, group)
            for action in group['actions']:
                yield page_index, covered[covered.index(anchor_beat(covered, ids, action)):], action


def anchor_beat(covered, beat_ids, action):
    """An action with from_beat starts with that sentence; it must lie inside its group."""
    if action.get('from_beat') is None:
        return covered[0]
    beat_id = group_beats(beat_ids, {'start_beat': action['from_beat']})[0]
    if beat_id not in covered:
        raise ValueError(f'from_beat {action["from_beat"]!r} lies outside its visual group')
    return beat_id


def usage(root, manifest, board):
    """Every planned visual is drawn in its own shot; every illustration is planned; characters cite locked references."""
    root, errors = Path(root), []
    visuals = {visual['id']: (shot, visual) for shot in board['shots'] for visual in shot.get('visuals', []) if visual.get('id')}
    placed = {}
    for page_index, beat_ids, action in page_actions(manifest):
        identity = action.get('visual')
        if identity is None:
            if action['type'] == 'illustration':
                errors.append(f'page {page_index}: illustration {action["source"]} is not in the storyboard; plan it and tag the action with "visual"')
            continue
        if identity not in visuals:
            errors.append(f'page {page_index}: action visual {identity!r} is not planned in the storyboard')
            continue
        shot = visuals[identity][0]
        if not set(beat_ids) & set(shot['beats']):
            errors.append(f'{identity} is planned for shot {shot.get("id")} but drawn during beats {beat_ids}')
        placed.setdefault(identity, []).append(action)
    for identity, (shot, visual) in visuals.items():
        types = [action['type'] for action in placed.get(identity, [])]
        need = {'illustration': 'illustration' in types, 'text_group': 'text' in types,
                'diagram': 'marker' in types and len(types) >= 2}.get(visual.get('kind'), True)
        if not need:
            errors.append(f'{shot.get("id")}/{identity}: planned {visual.get("kind")} "{visual.get("subject")}" is missing from job.json '
                          '(diagram = marker + another action); planned visuals cannot be skipped')
        for action in placed.get(identity, []):
            if action['type'] == 'illustration' and not (root / action['source']).is_file():
                errors.append(f'{identity}: image is missing: {action["source"]}')
    references = {}
    for character in board.get('characters', []):
        path = root / character.get('reference', '')
        if not path.is_file() or character.get('sha256') != sha256(path):
            errors.append(f'character {character.get("id")}: reference image must exist and match its locked sha256')
        references[character.get('id')] = path.resolve()
    if any(visual.get('characters') for _, visual in visuals.values()):
        plans = {}
        if manifest.get('drawing_plan'):
            plans = {item['id']: item for item in json.loads((root / manifest['drawing_plan']).read_text())['illustrations']}
        for identity, (_, visual) in visuals.items():
            for action in placed.get(identity, []):
                cited = {(root / path).resolve() for path in plans.get(action.get('drawing_plan_id'), {}).get('references', [])}
                missing = [name for name in visual.get('characters', []) if references.get(name) not in cited]
                if action['type'] == 'illustration' and missing:
                    errors.append(f'{identity}: its drawing plan must cite the locked reference image of {missing} in "references"')
    return errors


def timing(board, timeline):
    """Actual cadence and main-illustration reuse from the built timeline."""
    hold, errors = max_hold(board), []
    visuals = {visual['id']: visual for shot in board['shots'] for visual in shot.get('visuals', [])}
    shot_of = {beat_id: shot for shot in board['shots'] for beat_id in shot['beats']}
    beats = [beat for page in timeline['pages'] for beat in page['beats']]
    points = []
    for page in timeline['pages']:
        for event in page['events']:
            if event['caption']:
                continue
            points.append(event['start'])
            if visuals.get(event.get('visual'), {}).get('background'):
                parts = timeline['assets'][event['asset']].get('parts', [])
                points += [event['start'] + part['outline_start'] * (event['end'] - event['start']) for part in parts if 'outline_start' in part]
    start, end = beats[0]['start'], beats[-1]['end']
    ordered = sorted([start, end] + [point for point in points if start < point < end])
    for left, right in zip(ordered, ordered[1:]):
        if right - left > hold:
            inside = [beat['id'] for beat in beats if beat['start'] < right and beat['end'] > left]
            shots = sorted({shot_of[beat_id].get('id') for beat_id in inside if beat_id in shot_of})
            errors.append(f'Nothing new is drawn for {right - left:.1f}s ({left:.1f}-{right:.1f}s, shots {shots}); limit {hold:g}s. '
                          'Add or split a visual there, or make it a scene whose parts pace the drawing')
    stretch = longest(points, start, end)
    run, previous = 0, None
    for page in timeline['pages']:
        illustrations = sorted((event for event in page['events'] if event.get('image')), key=lambda event: event['start'])
        for beat in page['beats']:
            started = [event for event in illustrations if event['start'] < beat['end']]
            current = json.dumps(started[-1]['image']) if started else None
            run = run + 1 if current and current == previous else int(bool(current))
            previous = current
            shot = shot_of.get(beat['id'], {})
            if run == 3 and not str(shot.get('callback', '')).strip():
                source, crop = json.loads(current)
                errors.append(f'{source}{" crop " + str(crop) if crop else ""} stays the main visual for more than 2 consecutive beats (through {beat["id"]}, shot {shot.get("id")}); '
                              'start a new illustration or mark the shot "callback" with a reason')
    holds = []
    for page in timeline['pages']:
        primary = sorted((event for event in page['events'] if not event['caption']), key=lambda event: event['start'])
        for index, event in enumerate(primary):
            if event.get('image'):
                following = primary[index + 1]['start'] if index + 1 < len(primary) else page.get('end', event['end'])
                holds.append(max(0., following - event['end']))
    stats = {'longest_static_seconds': round(stretch[0], 2), 'longest_static_range': [round(stretch[1], 2), round(stretch[2], 2)]}
    if holds:
        # How long a finished picture stays readable before the next element: the first Codex film held
        # only 0.56s on average, validated films hold 1.1-1.7s. Warn when the pen leaves too little time.
        stats['finished_hold_median_seconds'] = round(statistics.median(holds), 2)
        stats['finished_hold_min_seconds'] = round(min(holds), 2)
    return errors, stats


def drawing_time(timeline):
    """Engine drawability vs the scheduled window: too short to draw is an error, too short for detail a warning."""
    errors, warnings = [], []
    for page in timeline['pages']:
        for event in page['events']:
            measured = timeline['assets'][event['asset']].get('drawability') if not event['caption'] else None
            if not measured:
                continue
            window, label = event['end'] - event['start'], event.get('visual') or event['asset']
            if window < measured.get('minimum_seconds', 0) - .02:  # the reported minimum is rounded to 0.01 s
                strokes = f' ({measured["stroke_count"]} strokes)' if measured.get('stroke_count') else ''
                errors.append(f'{label}: {window:.1f}s drawing window but the image needs at least {measured["minimum_seconds"]:.1f}s{strokes}; '
                              'simplify the image or extend its window across beats with a page visual group')
            elif window < max(measured.get('minimum_seconds', 0), measured.get('required_seconds', 0) / VIDEO_LAPSE) - .05:
                warnings.append(f'{label}: {window:.1f}s window is shorter than video pace; the pen hurries above it')
    return errors, warnings


def narration_seconds(timeline):
    return max(1e-6, timeline['pages'][-1]['speech_end'] - timeline['pages'][0]['beats'][0]['start'])


def pen_busy_share(timeline):
    """Share of the narration during which the pen is on the page at all (overlaps merged): what the finished
    pictures cannot use for holding. It counts lines, colour, reveal, writing and pen-up moves alike."""
    spans = sorted((event['start'], event['end']) for page in timeline['pages'] for event in page['events'] if not event['caption'])
    covered, reach = 0., -math.inf
    for start, end in spans:
        covered += max(0., end - max(start, reach))
        reach = max(reach, end)
    return covered / narration_seconds(timeline)


def pen_time(timeline):
    """What the pen spends the narration on, as shares: line (ink and sketch strokes of drawn pictures), colour
    (their marker and shading), reveal (pictures brushed in from the finished image), handwriting, marker, pen_up."""
    shares = dict.fromkeys(('line', 'colour', 'reveal', 'handwriting', 'marker', 'pen_up'), 0.)
    for page in timeline['pages']:
        for event in page['events']:
            if event['caption']:
                continue
            asset = timeline['assets'][event['asset']]
            for segment in asset.get('segments', []):
                kind = segment['kind']
                if kind == 'travel':
                    key = 'pen_up'
                elif asset['type'] == 'illustration':
                    key = 'reveal' if asset.get('mode') == 'reveal' else 'line' if kind in ('ink', 'sketch') else 'colour'
                else:
                    key = 'handwriting' if asset['type'] == 'handwritten_text' else 'marker'
                shares[key] += (segment['end'] - segment['start']) * (event['end'] - event['start'])
    return {key: round(value / narration_seconds(timeline), 3) for key, value in shares.items()}


def check_build(root, manifest, timeline):
    """Called by build(): hard failure on errors, warnings kept in the timeline."""
    board = load(root, manifest)
    if board is None and not manifest.get('runtime_fixture'):
        raise ValueError('Production requires a storyboard: set job.json "storyboard" (see story-design.md) and run storyboard_lint.py')
    errors, warnings = drawing_time(timeline)
    report = {'errors': errors, 'warnings': warnings, 'stats': {}}
    if board is not None:
        beats = [beat for page in timeline['pages'] for beat in page['beats']]
        planned = plan(board, beats)
        late, cadence = timing(board, timeline)
        share, spent = pen_busy_share(timeline), pen_time(timeline)
        if share > MAX_DRAWING_SHARE:
            late.append(f'The pen is busy {share:.0%} of the narration (limit {MAX_DRAWING_SHARE:.0%}; ' +
                        ', '.join(f'{key} {value:.0%}' for key, value in spent.items() if value) + '), so finished pictures barely hold: '
                        'give pictures less to draw, draw fewer per page, or let one board accumulate across beats instead of redrawing')
        median_hold = cadence.get('finished_hold_median_seconds')
        if median_hold is not None and median_hold < 1.:
            warnings = warnings + [f'A finished picture holds only {median_hold:.2f}s before the next element (median; validated films hold about 1.1-1.7s): '
                                   'give pictures less to draw or let one board accumulate across beats instead of redrawing']
        report = {'errors': planned['errors'] + usage(root, manifest, board) + late + errors,
                  'warnings': warnings, 'stats': {**planned['stats'], **cadence, 'pen_busy_share': round(share, 3), 'pen_time_share': spent}}
    if report['errors']:
        raise ValueError('Storyboard lint failed:\n- ' + '\n- '.join(report['errors']))
    return report


def alignment_beats(root, manifest):
    if not manifest.get('narration', {}).get('alignment'):
        raise ValueError('The storyboard budget needs narration.alignment: lock the complete narration first')
    alignment = json.loads((Path(root) / manifest['narration']['alignment']).read_text())
    return [{'id': beat['id'], 'start': COVER + float(beat['start']), 'end': COVER + float(beat['end'])} for beat in alignment['beats']]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('job', type=Path)
    parser.add_argument('--budget', action='store_true', help='Before image generation: plan checks and drawing seconds per planned visual')
    arguments = parser.parse_args()
    root = arguments.job.resolve()
    manifest = json.loads((root / 'job.json').read_text())
    board = load(root, manifest)
    if board is None:
        raise ValueError('job.json has no "storyboard"')
    report = plan(board, alignment_beats(root, manifest))
    if not arguments.budget:
        report['errors'] += usage(root, manifest, board)
        report['note'] = 'Actual cadence, reuse and drawing-time checks run in build'
    report['status'] = 'fail' if report['errors'] else 'pass'
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 1 if report['errors'] else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, FileNotFoundError, KeyError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        sys.exit(1)

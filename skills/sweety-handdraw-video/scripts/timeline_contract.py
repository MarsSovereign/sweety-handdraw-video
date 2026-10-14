"""Shared pre-render checks for timing, arm coverage, page camera/transitions and the fixed cover behavior."""
import math
import numpy as np
from PIL import Image


TRANSITIONS = ('slide', 'pan', 'cut', 'fade')
CAMERA_FIELDS = {'at', 'center', 'zoom', 'ease', 'seconds', 'follow'}


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_page_motion(page, canvas, first=False):
    """Camera keyframes and the transition into this page (pages[].camera / pages[].transition)."""
    keys, transition = page.get('camera'), page.get('transition')
    if keys is not None:
        if not isinstance(keys, list):
            raise ValueError('Page camera must be a list of keyframes')
        beats = {beat['id'] for beat in page.get('beats', [])}
        for key in keys:
            if not isinstance(key, dict) or set(key) - CAMERA_FIELDS:
                raise ValueError('Camera keyframes accept only at/center/zoom/ease/seconds/follow')
            at = key.get('at', 0)
            if isinstance(at, str):
                if not at.startswith('beat:') or at[5:] not in beats:
                    raise ValueError('Camera "at" must be "beat:<id>" of a beat on the same page: ' + at)
            elif not number(at) or not 0 <= at < page['end'] - page['start']:
                raise ValueError('Camera "at" must be seconds from the page start, inside the page')
            center = key.get('center', [canvas['width'] / 2, canvas['height'] / 2])
            if not (isinstance(center, list) and len(center) == 2 and all(map(number, center))
                    and 0 <= center[0] <= canvas['width'] and 0 <= center[1] <= canvas['height']):
                raise ValueError('Camera center must be [x, y] inside the canvas')
            if not number(key.get('zoom', 1)) or not 1 <= key.get('zoom', 1) <= 1.3:
                raise ValueError('Camera zoom must stay within 1.0..1.3')
            if key.get('ease', 'inout') not in ('inout', 'linear') or not isinstance(key.get('follow', False), bool):
                raise ValueError('Camera ease is inout/linear and follow is true/false')
            if not number(key.get('seconds', .8)) or not 0 <= key.get('seconds', .8) <= 3:
                raise ValueError('Camera move seconds must be within 0..3')
    if transition is not None:
        if first:
            raise ValueError('pages[].transition describes the move into a page; the first page has none')
        if not isinstance(transition, dict) or transition.get('type') not in TRANSITIONS or set(transition) - {'type', 'seconds'}:
            raise ValueError('Transition must be {"type": slide|pan|cut|fade, "seconds": 0.3..1.0}')
        seconds = transition.get('seconds', .6)
        if transition['type'] != 'cut' and (not number(seconds) or not .3 <= seconds <= 1):
            raise ValueError('Transition seconds must be within 0.3..1.0')
    return True


def validate_timeline(timeline):
    if timeline['cover_seconds']!=1:
        raise ValueError('Cover must last exactly one second')
    if timeline['hand'].get('kind', 'hand') != 'pen' and timeline['hand'].get('arm_bottom_at_highest_tip',0)<timeline['canvas']['height']:
        raise ValueError('The forearm cut edge can become visible in-frame')
    for page in timeline['pages']:
        events=sorted((event for event in page['events'] if not event['caption']),key=lambda event:event['start'])
        if any(left['end']>right['start'] for left,right in zip(events,events[1:])):
            raise ValueError('Main drawing groups overlap; only one physical pen is allowed')
        if page['last_drawing_end']>page['speech_end']-.45:
            raise ValueError('Page drawing has no completion hold before narration ends')
        validate_page_motion(page, timeline['canvas'], page is timeline['pages'][0])
    if timeline.get('cues') or timeline.get('assets'):
        from subtitle_contract import validate_subtitles
        validate_subtitles(timeline)
    return True


def validate_cover(renderer):
    original=np.array(renderer.cover.convert('RGB'))
    for moment in (0.,.25,.49,.5):
        if not np.array_equal(np.array(renderer.frame(moment)),original):
            raise ValueError('Cover moves or fades during its first half second')
    for moment in (.6,.75,.9):
        expected=np.array(Image.blend(renderer.cover,Image.new('RGBA',renderer.cover.size,renderer.paper),(moment-.5)/.5).convert('RGB'))
        if not np.array_equal(np.array(renderer.frame(moment)),expected):
            raise ValueError('Cover does not fade smoothly to paper in its last half second')
    return True

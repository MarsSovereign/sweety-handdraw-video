#!/usr/bin/env python3
"""Measure beat ranges and caption cues from narration audio; timestamps are never hand-written.

Caption cues come from character timestamps when available (forced aligner, ASR, or cloud TTS
subtitles), otherwise from a deterministic pause split: N pieces use the N-1 most prominent pauses.
"""
import argparse
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re
import unicodedata
import numpy as np

SAMPLE_RATE = 48000
PUNCTUATION = set('，。！？；：、“”‘’（）《》〈〉【】—…·,.!?;:"\'()[]<>-~ \n\t')
SENTENCE_END = '。！？!?…'
CAPTION_BREAKS = '。！？!?…；;，,、：:'
MAX_CAPTION_CHARS = 24
MIN_PAUSE_SECONDS = .08
SYNC_TOLERANCE_SECONDS = .3
SYNC_GAP_SECONDS = .2
# Clause breaks in fluent narration can be 30-50 ms; shifting cues by 0.4 s still fails nearly every cue.
SYNC_PAUSE_SECONDS = .03
TRANSCRIBE = r'''
import json, sys, mlx_whisper
result = mlx_whisper.transcribe(sys.argv[1], path_or_hf_repo=sys.argv[2], language='zh', word_timestamps=True)
try:
    from opencc import OpenCC
    convert = OpenCC('t2s').convert
except ImportError:
    convert = lambda text: text
words = [{'text': convert(word['word']).strip(), 'start': word['start'], 'end': word['end']}
         for segment in result['segments'] for word in segment.get('words', [])]
print(json.dumps({'text': convert(result['text']), 'words': words}, ensure_ascii=False))
'''
FORCE_ALIGN = r'''
import json, sys
from mlx_audio.stt.utils import load_model
result = load_model(sys.argv[2]).generate(audio=sys.argv[1], text=sys.argv[3], language='Chinese')
print(json.dumps({'words': [{'text': item.text, 'start': item.start_time, 'end': item.end_time} for item in result.items]}, ensure_ascii=False))
'''


CHINESE_DIGITS = dict(zip('零〇一二两三四五六七八九', (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
CHINESE_UNITS = {'十': 10, '百': 100, '千': 1000, '万': 10 ** 4, '亿': 10 ** 8}
NUMBER_DIGITS = '0-9零〇一二两三四五六七八九'
NUMBER_PATTERN = '[' + NUMBER_DIGITS + '十百千万亿]+(?:[.点][' + NUMBER_DIGITS + '十百千万亿]+)*'


def spoken(text):
    # A decimal point carries a value; sentence punctuation does not.
    return ''.join(character for index, character in enumerate(text)
                   if character not in PUNCTUATION or (character == '.' and 0 < index < len(text) - 1
                       and text[index - 1].isdigit() and text[index + 1].isdigit()))


def chinese_number(text):
    """Parse exact Chinese/Arabic magnitude notation without float rounding or range guessing."""
    normalized = ''.join(str(CHINESE_DIGITS.get(character, character)) for character in text).replace('点', '.')
    # Adjacent spoken nonzero digits can be an approximation (两三万, 四五十), not 23/45.
    if re.search('[一二两三四五六七八九]{2}', text) and len(text) < 4:
        return text

    def value(token):
        for unit in ('亿', '万'):
            if unit in token:
                if token.count(unit) != 1:
                    raise ValueError('Repeated magnitude')
                left, right = token.split(unit)
                return value(left or '1') * CHINESE_UNITS[unit] + (value(right) if right else 0)
        total, previous, cursor = Decimal(0), 10000, 0
        for match in re.finditer(r'([0-9]+(?:\.[0-9]+)?)?([十百千])', token):
            if match.start() != cursor or CHINESE_UNITS[match[2]] >= previous:
                raise ValueError('Invalid small-unit order')
            previous = CHINESE_UNITS[match[2]]
            total += Decimal(match[1] or '1') * previous
            cursor = match.end()
        return total + (Decimal(token[cursor:]) if token[cursor:] else 0)

    try:
        result = format(value(normalized), 'f')
        return result.rstrip('0').rstrip('.') if '.' in result else result
    except (InvalidOperation, ValueError):
        return text


def numbers(text):
    """Normalize comparison text only; retain different magnitudes, fractions and signs."""
    text = unicodedata.normalize('NFKC', text)
    text = re.sub(r'(?<=\d),(?=\d{3}(?:\D|$))', '', text)
    text = re.sub('百分之(' + NUMBER_PATTERN + ')', lambda match: chinese_number(match[1]) + '%', text)
    text = re.sub(NUMBER_PATTERN, lambda match: chinese_number(match[0]), text)
    return re.sub(r'-(?=\d)', '负', text)


def cer(expected, actual):
    expected, actual = spoken(numbers(expected)), spoken(numbers(actual))
    row = np.arange(len(actual) + 1)
    for index, character in enumerate(expected, 1):
        previous, row = row, np.empty_like(row)
        row[0] = index
        for column, other in enumerate(actual, 1):
            row[column] = min(previous[column] + 1, row[column - 1] + 1, previous[column - 1] + (character != other))
    return float(row[-1] / max(1, len(expected)))


def covers(expected, actual, max_cer=.15):
    """ASR must cover the whole text: low CER and matched characters near both ends (numbers compared by value)."""
    raw_actual = actual
    expected, actual = spoken(numbers(expected)), spoken(numbers(actual))
    blocks = [block for block in SequenceMatcher(None, expected, actual, autojunk=False).get_matching_blocks() if block.size]
    error = cer(expected, actual)
    ok = bool(blocks) and error <= max_cer and blocks[0].a <= 2 and blocks[-1].a + blocks[-1].size >= len(expected) - 2
    return {'cer': round(error, 3), 'status': 'pass' if ok else 'fail', 'asr_text': raw_actual, 'comparison_text': actual}


def char_times(text, words):
    """Map every spoken character of text to (start, end) seconds through ASR/aligner/TTS word times."""
    recognized = []
    for word in words:
        characters = spoken(word['text'])
        step = (word['end'] - word['start']) / max(1, len(characters))
        recognized += [(character, word['start'] + index * step, word['start'] + (index + 1) * step) for index, character in enumerate(characters)]
    expected = spoken(text)
    times = [None] * len(expected)
    matcher = SequenceMatcher(None, expected, ''.join(item[0] for item in recognized), autojunk=False)
    for tag, a0, a1, b0, b1 in matcher.get_opcodes():
        if tag == 'equal' or (tag == 'replace' and a1 - a0 == b1 - b0):
            for offset in range(a1 - a0):
                times[a0 + offset] = recognized[b0 + offset][1:]
    known = [index for index, value in enumerate(times) if value]
    if not known:
        raise ValueError('Timestamps do not match the narration text')
    for index, value in enumerate(times):
        if value is None:  # interpolate unmatched characters between their matched neighbours
            left = max((k for k in known if k < index), default=None)
            right = min((k for k in known if k > index), default=None)
            start = times[left][1] if left is not None else times[right][0]
            end = times[right][0] if right is not None else times[left][1]
            times[index] = (start, max(start, end))
    return times, round(len(known) / len(expected), 3)


def activity(samples, rate=SAMPLE_RATE):
    hop, width = round(.01 * rate), round(.02 * rate)
    samples = np.asarray(samples, dtype=np.float64)
    if len(samples) < width:
        return np.zeros(0, dtype=bool)
    count = 1 + (len(samples) - width) // hop
    levels = 20 * np.log10(np.sqrt(np.mean(samples[np.arange(width)[None] + hop * np.arange(count)[:, None]] ** 2, axis=1)) + 1e-9)
    threshold = max(np.percentile(levels, 95) - 35, np.percentile(levels, 10) + 6)
    return levels > threshold


def speech_span(samples, rate=SAMPLE_RATE):
    active = np.flatnonzero(activity(samples, rate))
    if not len(active):
        raise ValueError('No speech found')
    return active[0] * .01, active[-1] * .01 + .02


def pauses(samples, rate=SAMPLE_RATE, minimum=MIN_PAUSE_SECONDS):
    """Interior quiet runs as (start, end) seconds, between the first and last speech frames."""
    active = activity(samples, rate)
    found, start = [], None
    first, last = np.flatnonzero(active)[[0, -1]]
    for index in range(first, last + 1):
        if not active[index] and start is None:
            start = index
        elif active[index] and start is not None:
            if (index - start) * .01 >= minimum:
                found.append((start * .01 + .02, index * .01))
            start = None
    return found


def onsets(samples, rate=SAMPLE_RATE, minimum=.05):
    first, _ = speech_span(samples, rate)
    return [first] + [end for _, end in pauses(samples, rate, minimum)]


def pause_split(samples, weights, rate=SAMPLE_RATE):
    """Ranges for consecutive text pieces: the N-1 longest pauses, unless that gives implausible rates."""
    start, end = speech_span(samples, rate)
    if len(weights) == 1:
        return [(start, end)], 'single'
    found = pauses(samples, rate)
    def ranges(chosen):
        edges = [start] + [value for pause in chosen for value in pause] + [end]
        return [(edges[2 * index], edges[2 * index + 1]) for index in range(len(weights))]
    def plausible(result):
        return all(weight < 3 or 1.5 <= weight / max(.01, right - left) <= 10 for weight, (left, right) in zip(weights, result))
    longest = sorted(sorted(found, key=lambda pause: pause[1] - pause[0])[-(len(weights) - 1):]) if len(found) >= len(weights) - 1 else []
    if longest and plausible(ranges(longest)):
        return ranges(longest), 'longest_pauses'
    chosen, total, cursor = [], sum(weights), start
    for share in np.cumsum(weights)[:-1] / total:
        expected = start + share * (end - start)
        window = max(.8, .25 * (end - start))
        nearby = [pause for pause in found if pause[0] > cursor and abs((pause[0] + pause[1]) / 2 - expected) <= window]
        pick = max(nearby, key=lambda pause: pause[1] - pause[0]) if nearby else (expected, expected)
        chosen.append(pick)
        cursor = pick[1]
    return ranges(chosen), 'pauses_near_text_proportion'


def caption_texts(text, limit=MAX_CAPTION_CHARS):
    clauses = re.findall('[^' + CAPTION_BREAKS + ']+[' + CAPTION_BREAKS + ']*|[' + CAPTION_BREAKS + ']+', text)
    pieces = []
    for clause in clauses:
        if pieces and (not spoken(clause) or len(pieces[-1]) + len(clause) <= limit):
            pieces[-1] += clause
        else:
            pieces.append(clause)
    return pieces


def ranges_from_times(texts, times):
    result, cursor = [], 0
    for text in texts:
        count = len(spoken(text))
        result.append((times[cursor][0], times[cursor + count - 1][1]) if count else (times[max(0, cursor - 1)][1],) * 2)
        cursor += count
    return result


def piece_ranges(samples, texts, words=None, rate=SAMPLE_RATE):
    """Ranges (seconds within samples) for consecutive text pieces, from timestamps or pauses."""
    if words:
        times, matched = char_times(''.join(texts), words)
        return ranges_from_times(texts, times), f'timestamps (matched {matched:.0%})'
    return pause_split(samples, [max(1, len(spoken(text))) for text in texts], rate)


def entry(beat_id, text, texts, ranges, method):
    """One alignment beat: absolute range plus caption cues relative to the beat start."""
    start, end = ranges[0][0], ranges[-1][1]
    cues = [{'text': piece, 'start': round(max(0., left - start), 3), 'end': round(min(end, right) - start, 3)} for piece, (left, right) in zip(texts, ranges)]
    for previous, following in zip(cues, cues[1:]):
        previous['end'] = min(previous['end'], following['start'])
    return {'id': beat_id, 'text': text, 'start': round(start, 3), 'end': round(end, 3), 'caption_cues': cues, 'cue_method': method}


def beat_entry(samples, beat_id, text, offset, words=None, rate=SAMPLE_RATE):
    texts = caption_texts(text)
    ranges, method = piece_ranges(samples, texts, words, rate)
    return entry(beat_id, text, texts, [(offset + left, offset + right) for left, right in ranges], method)


def caption_sync(samples, cues, rate=SAMPLE_RATE, tolerance=SYNC_TOLERANCE_SECONDS):
    """A cue after a caption gap (>= SYNC_GAP_SECONDS) must start within tolerance of a speech onset (pause
    end); a cue continuing fluent speech, where energy shows no word boundary, must start where speech is."""
    found, active = np.array(onsets(samples, rate, SYNC_PAUSE_SECONDS)), activity(samples, rate)
    offsets, outside, previous_end = [], [], None
    for cue in cues:
        offset = float(np.min(np.abs(found - cue['start'])))
        if previous_end is None or cue['start'] - previous_end >= SYNC_GAP_SECONDS:
            offsets.append(offset)
            ok = offset <= tolerance
        else:
            index = round(cue['start'] * 100)
            ok = bool(active[max(0, index - round(tolerance * 100)):index + round(tolerance * 100)].any())
        if not ok:
            outside.append({'text': cue['text'], 'start': cue['start'], 'nearest_onset_offset': round(offset, 3)})
        previous_end = cue['end']
    return {'status': 'FAIL' if outside else 'PASS', 'tolerance_seconds': tolerance, 'cues': len(cues), 'cues_after_gap': len(offsets),
            'max_offset_seconds': round(max(offsets, default=0.), 3), 'outside_tolerance': outside}


def transcribe(path, python=None, model=None, text=None):
    """Character timestamps from an installed forced aligner (with text) or local Whisper; None if missing."""
    from voice_backends import tools, run_python
    found = tools()
    python = python or found['mlx_python']
    if text and python and found['forced_aligner']:
        return run_python(python, FORCE_ALIGN, path, found['forced_aligner'], text)
    model = model or found['asr_model']
    if not python or not model:
        return None
    return run_python(python, TRANSCRIBE, path, model)


def align_master(source, beats, words, output, script_sha256):
    """Alignment for a whole user-provided master from ASR/aligner character times."""
    times, matched = char_times(''.join(beat['text'] for beat in beats), words)
    entries, cursor = [], 0
    for beat in beats:
        count, texts = len(spoken(beat['text'])), caption_texts(beat['text'])
        entries.append(entry(beat['id'], beat['text'], texts, ranges_from_times(texts, times[cursor:cursor + count]), 'whole_master_timestamps'))
        cursor += count
    for previous, following in zip(entries, entries[1:]):
        if previous['end'] > following['start']:
            raise ValueError('Character timestamps overlap between beats: ' + following['id'])
    alignment = {'version': 1, 'source_sha256': hashlib.sha256(Path(source).read_bytes()).hexdigest(), 'script_sha256': script_sha256,
                 'method': 'whole_master_character_timestamps', 'matched_characters': matched, 'beats': entries}
    Path(output).write_text(json.dumps(alignment, ensure_ascii=False, indent=2))
    return alignment


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('job', type=Path, help='Job whose narration.source is a finished user-provided master')
    parser.add_argument('--words', type=Path, help='JSON with {"words":[{"text","start","end"}]} from any aligner; default: local ASR')
    arguments = parser.parse_args()
    root = arguments.job.resolve()
    manifest = json.loads((root/'job.json').read_text())
    from narration_master import script_text
    settings = manifest['narration']
    source = root/settings['source']
    words = json.loads(arguments.words.read_text()) if arguments.words else transcribe(source)
    if not words:
        raise SystemExit('No local ASR/aligner installed; provide --words from an aligner or cloud TTS timestamps')
    beats = [beat for page in manifest['pages'] for beat in page['beats']]
    result = align_master(source, beats, words['words'], root/settings['alignment'], hashlib.sha256(script_text(manifest).encode()).hexdigest())
    print(json.dumps({'beats': len(result['beats']), 'matched_characters': result['matched_characters']}, ensure_ascii=False))

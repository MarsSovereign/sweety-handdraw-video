#!/usr/bin/env python3
"""Objective narration gate: stationary tones, pause depth, DC offset and clipping.

FAIL when a narrow line (30-4000 Hz) stands >= 8 dB above its +-25 Hz neighbourhood in >= 50% (and at
least 2) of the 2-second windows (hum, drone, sustained music note), when the quietest 5% of 20 ms frames are less
than 25 dB below the speech p95 level (pauses never become quiet: noise or music bed), or when DC
offset / clipping exceed the limits below. Cloning references and single generated chunks are short dense
speech that may hold less than 5% pause frames, so they use the quietest 2% instead (still > -24 dB on every known-bad
reference/film measured, < -29 dB on HTDemucs+DeepFilterNet3 cleaned ones). Signals never replace listening.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import numpy as np
from scipy import signal
from scipy.ndimage import median_filter

SAMPLE_RATE = 48000
TONE_BAND_HZ = (30, 4000)
TONE_EXCESS_DB = 8.
TONE_PERSISTENCE = .5
TONE_WINDOW_SECONDS = 2.
TONE_MIN_WINDOWS = 2  # one 2 s window of a held vowel shows harmonic lines; hum repeats in every window
MAX_PAUSE_DEPTH_DB = -25.
PAUSE_PERCENTILE = 5
SHORT_CLIP_PAUSE_PERCENTILE = 2
MAX_DC_OFFSET = .005
CLIP_LEVEL = .999
MAX_CLIPPED_FRACTION = 1e-4


def decode(path, start=None, duration=None):
    """First channel at unity gain (ffmpeg's -ac 1 adds +3 dB to duplicated stereo and fakes clipping)."""
    command = ['ffmpeg', '-v', 'error'] + (['-ss', str(start)] if start is not None else []) + ['-i', str(path)]
    command += (['-t', str(duration)] if duration is not None else []) + ['-map', 'a:0', '-af', 'pan=mono|c0=c0', '-ar', str(SAMPLE_RATE), '-f', 'f32le', '-']
    return np.frombuffer(subprocess.run(command, capture_output=True, check=True).stdout, '<f4').astype(np.float64)


def tonal_lines(x):
    size = int(TONE_WINDOW_SECONDS * SAMPLE_RATE)
    rows = []
    for start in range(0, len(x) - size + 1, size):
        window = x[start:start + size]
        if np.sqrt(np.mean(window ** 2)) < 1e-5:
            continue  # digital silence such as cover or tail padding
        frequencies, power = signal.welch(window, SAMPLE_RATE, nperseg=size // 2)
        level = 10 * np.log10(power + 1e-24)
        rows.append(level - median_filter(level, size=51, mode='nearest'))
    if not rows:
        return []
    band = (frequencies >= TONE_BAND_HZ[0]) & (frequencies <= TONE_BAND_HZ[1])
    hits = (np.array(rows)[:, band] > TONE_EXCESS_DB).sum(axis=0)
    persistence = hits / len(rows)
    lines = []
    # Adjacent FFT bins often tie on persistence. Rank the actual spectral peak next,
    # rather than letting NumPy's platform-dependent tie order name a neighbouring bin.
    excess = np.median(np.asarray(rows)[:, band], axis=0)
    for index in np.lexsort((frequencies[band], -excess, -persistence)):
        if persistence[index] < TONE_PERSISTENCE:
            break
        if hits[index] < TONE_MIN_WINDOWS:
            continue
        if all(abs(frequencies[band][index] - hz) > 4 for hz, _ in lines):
            lines.append((float(frequencies[band][index]), round(float(persistence[index]), 2)))
    return lines[:5]


def frame_levels(x, frame=.02, hop=.01):
    width, step = round(frame * SAMPLE_RATE), round(hop * SAMPLE_RATE)
    if len(x) < width:
        return np.zeros(0)
    count = 1 + (len(x) - width) // step
    return np.sqrt(np.mean(x[np.arange(width)[None] + step * np.arange(count)[:, None]] ** 2, axis=1))


def pause_depth(x, percentile=PAUSE_PERCENTILE):
    levels = frame_levels(x)
    levels = 20 * np.log10(levels[levels > 1e-6])
    return float(np.percentile(levels, percentile) - np.percentile(levels, 95)) if len(levels) else 0.


def measure(source, label=None, short_clip=False):
    x = np.asarray(source, dtype=np.float64) if isinstance(source, np.ndarray) else decode(source)
    if not len(x) or not np.isfinite(x).all() or not np.any(x):
        return {'source': label or str(source), 'status': 'FAIL', 'failures': ['empty, silent or non-finite audio']}
    clipped = float(np.mean(np.abs(x) >= CLIP_LEVEL))
    report = {'source': label or (None if isinstance(source, np.ndarray) else str(source)), 'duration_seconds': round(len(x) / SAMPLE_RATE, 3),
              'tonal_lines_hz_persistence': tonal_lines(x), 'pause_percentile': SHORT_CLIP_PAUSE_PERCENTILE if short_clip else PAUSE_PERCENTILE,
              'pause_depth_db': round(pause_depth(x, SHORT_CLIP_PAUSE_PERCENTILE if short_clip else PAUSE_PERCENTILE), 1),
              'dc_offset': round(float(np.mean(x)), 5), 'peak_dbfs': round(float(20 * np.log10(np.abs(x).max())), 2),
              'clipped_fraction': clipped}
    failures = []
    if report['tonal_lines_hz_persistence']:
        failures.append('stationary tone/hum/music line at ' + ', '.join(f'{hz:.0f} Hz' for hz, _ in report['tonal_lines_hz_persistence']))
    if report['pause_depth_db'] > MAX_PAUSE_DEPTH_DB:
        failures.append(f"pauses not quiet ({report['pause_depth_db']} dB > {MAX_PAUSE_DEPTH_DB} dB): noise or music bed")
    if abs(report['dc_offset']) > MAX_DC_OFFSET:
        failures.append(f"DC offset {report['dc_offset']}")
    if clipped > MAX_CLIPPED_FRACTION:
        failures.append(f'clipping in {clipped:.2%} of samples')
    return {**report, 'status': 'FAIL' if failures else 'PASS', 'failures': failures}


def require_clean(source, context, label=None, short_clip=False):
    report = measure(source, label, short_clip)
    if report['status'] != 'PASS':
        raise ValueError(f"{context}: audio gate FAIL ({'; '.join(report['failures'])}). "
                         '参考音频请先用 scripts/clean_reference.py 分离人声/降噪并重新导入；生成的旁白请重新生成该段。')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('files', nargs='+', type=Path)
    parser.add_argument('--short-clip', action='store_true', help='Cloning reference or single chunk: quietest 2%% pause statistic')
    arguments = parser.parse_args()
    results = [measure(path, short_clip=arguments.short_clip) for path in arguments.files]
    for result in results:
        print(json.dumps(result, ensure_ascii=False))
    sys.exit(0 if all(result['status'] == 'PASS' for result in results) else 1)

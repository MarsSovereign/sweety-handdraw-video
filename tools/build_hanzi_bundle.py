#!/usr/bin/env python3
"""Repack hanzi-writer-data (Make Me a Hanzi, Arphic PL) into the bundled GB2312 stroke file.

Usage: download https://registry.npmjs.org/hanzi-writer-data/-/hanzi-writer-data-2.0.1.tgz,
extract it, then run: python3 tools/build_hanzi_bundle.py <extracted>/package
"""
from pathlib import Path
import argparse
import json
import lzma
import re

VERSION = '2.0.1'
TARGET = Path(__file__).resolve().parents[1] / 'skills/sweety-handdraw-video/assets/hanzi'
NOTICE = ('Derived from hanzi-writer-data ' + VERSION + ' (https://github.com/chanind/hanzi-writer-data), '
          'which comes from Make Me a Hanzi (https://github.com/skishore/makemeahanzi) and the Arphic fonts '
          'AR PL KaitiM GB / AR PL UKai. Redistributed under the Arphic Public License (ARPHICPL.TXT, unaltered). '
          'Modified on 2026-10-14 by sweety-handdraw-video: kept only the 6763 GB2312 characters, dropped radStrokes, '
          'rounded coordinates to integers, rewrote stroke outlines as equivalent relative SVG paths and packed all '
          'characters into one xz-compressed JSON file. Glyph geometry and stroke order are otherwise unchanged.')


def gb2312():
    characters = []
    for row in range(16, 88):
        for cell in range(1, 95):
            try:
                characters.append(bytes([0xA0 + row, 0xA0 + cell]).decode('gb2312'))
            except UnicodeDecodeError:
                pass
    return characters


def relative(path):
    """Absolute M/L/Q/C/Z outline -> SVG relative path (every pair relative to the segment start)."""
    tokens = re.findall(r'[MLQCZ]|-?\d+(?:\.\d+)?', path)
    out, x, y, index, command = [], 0, 0, 0, None
    while index < len(tokens):
        if tokens[index].isalpha():
            command = tokens[index]
            index += 1
            if command == 'Z':
                out.append('z')
            continue
        count = {'M': 2, 'L': 2, 'Q': 4, 'C': 6}[command]
        values = [round(float(value)) for value in tokens[index:index + count]]
        index += count
        out.append(command.lower() + ' '.join(str(value - (y if position % 2 else x)) for position, value in enumerate(values)))
        x, y = values[-2], values[-1]
    return ''.join(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path, help='Extracted hanzi-writer-data package directory')
    arguments = parser.parse_args()
    characters = {}
    for character in gb2312():
        data = json.loads((arguments.source / f'{character}.json').read_text())
        characters[character] = [[relative(stroke) for stroke in data['strokes']],
                                 [[[round(x), round(y)] for x, y in median] for median in data['medians']]]
    document = {'format': 1, 'source': f'hanzi-writer-data@{VERSION}', 'license': 'ARPHICPL.TXT', 'notice': NOTICE, 'characters': characters}
    raw = json.dumps(document, ensure_ascii=False, separators=(',', ':')).encode()
    (TARGET / 'gb2312-strokes.json.xz').write_bytes(lzma.compress(raw, preset=9 | lzma.PRESET_EXTREME))
    (TARGET / 'ARPHICPL.TXT').write_bytes((arguments.source / 'ARPHICPL.TXT').read_bytes())
    print(json.dumps({'characters': len(characters), 'bytes': (TARGET / 'gb2312-strokes.json.xz').stat().st_size}))


if __name__ == '__main__':
    main()

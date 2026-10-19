#!/usr/bin/env python3
"""Check release entry points and build the archive from an immutable tag."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = 'threerocks/sweety-handdraw-video'


def validate_metadata(root=ROOT, tag=None):
    package = root / 'skills/sweety-handdraw-video'
    version = (package / 'VERSION').read_text().strip()
    if not re.fullmatch(r'\d+\.\d+(?:\.\d+)?', version):
        raise ValueError('VERSION must be a stable numeric version')
    expected_tag = 'v' + version
    if tag is not None and tag != expected_tag:
        raise ValueError(f'Tag {tag!r} does not match VERSION {expected_tag}')
    readme = (root / 'README.md').read_text()
    if f'当前版本：{expected_tag}（' not in readme:
        raise ValueError('README current version does not match VERSION')
    archive_name = f'sweety-handdraw-video-{expected_tag}.zip'
    download = f'https://github.com/{REPOSITORY}/releases/download/{expected_tag}/{archive_name}'
    install_line = next((line for line in readme.splitlines() if '需要固定本版时' in line), '')
    if f'[{expected_tag} 安装包]({download})' not in install_line:
        raise ValueError('README pinned installation link does not match VERSION')
    if f'当前版本：{expected_tag}。' not in (package / 'README.md').read_text():
        raise ValueError('Packaged README current version does not match VERSION')
    notes = root / f'docs/releases/{expected_tag}.md'
    if not notes.is_file() or not notes.read_text().strip():
        raise ValueError('Release notes are missing: ' + str(notes))
    return {'version': version, 'tag': expected_tag, 'archive': archive_name, 'notes': str(notes)}


def validate_archive(output, metadata):
    archive = output / metadata['archive']
    expected = hashlib.sha256(archive.read_bytes()).hexdigest() + '  ' + archive.name
    if (output / 'SHA256SUMS.txt').read_text().strip() != expected:
        raise ValueError('Archive checksum does not match SHA256SUMS.txt')
    manifest = json.loads((output / 'package-sha256.json').read_text())
    with zipfile.ZipFile(archive) as bundle:
        prefix = 'sweety-handdraw-video/'
        if set(bundle.namelist()) != {prefix + name for name in manifest}:
            raise ValueError('Archive file list does not match package-sha256.json')
        for name, checksum in manifest.items():
            if hashlib.sha256(bundle.read(prefix + name)).hexdigest() != checksum:
                raise ValueError('Archive content checksum mismatch: ' + name)
        if bundle.read(prefix + 'VERSION').decode().strip() != metadata['version']:
            raise ValueError('Archive VERSION does not match release tag')
    return len(manifest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('check', 'prepare'))
    parser.add_argument('--tag')
    parser.add_argument('--output', type=Path)
    options = parser.parse_args()
    metadata = validate_metadata(tag=options.tag)
    if options.command == 'prepare':
        if not options.tag or not options.output:
            parser.error('prepare requires --tag and --output')
        tagged_commit = subprocess.check_output(['git', 'rev-parse', '--verify', f'{options.tag}^{{commit}}'], cwd=ROOT, text=True).strip()
        head_commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        if tagged_commit != head_commit:
            raise ValueError('Build releases from the tagged commit, not another checkout')
        if subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=normal'], cwd=ROOT, text=True).strip():
            raise ValueError('Release packaging requires a clean working tree')
        subprocess.run([sys.executable, '-B', str(ROOT / 'tools/package.py'), '--output', str(options.output)], check=True)
        metadata['files_checked'] = validate_archive(options.output, metadata)
        metadata['commit'] = tagged_commit
    print(json.dumps(metadata, ensure_ascii=False))


if __name__ == '__main__':
    main()

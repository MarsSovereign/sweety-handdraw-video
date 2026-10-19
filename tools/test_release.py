"""Reject stale installation entry points and mislabeled release archives."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from release import validate_archive, validate_metadata


class ReleaseConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.package = self.root / 'skills/sweety-handdraw-video'
        self.package.mkdir(parents=True)
        (self.package / 'VERSION').write_text('1.4.1\n')
        (self.package / 'README.md').write_text('当前版本：v1.4.1。')
        (self.root / 'README.md').write_text('当前版本：v1.4.1（2026-10-19）\n需要固定本版时，使用 [v1.4.1 安装包](https://github.com/threerocks/sweety-handdraw-video/releases/download/v1.4.1/sweety-handdraw-video-v1.4.1.zip)。')
        (self.root / 'docs/releases').mkdir(parents=True)
        (self.root / 'docs/releases/v1.4.1.md').write_text('Release notes')

    def test_current_metadata_and_matching_tag_pass(self):
        self.assertEqual(validate_metadata(self.root, 'v1.4.1')['archive'], 'sweety-handdraw-video-v1.4.1.zip')

    def test_stale_version_or_download_is_rejected(self):
        for old_text, new_text in [('当前版本：v1.4.1', '当前版本：v1.4-dev'), ('download/v1.4.1/', 'download/v1.3/')]:
            with self.subTest(stale=old_text):
                readme = self.root / 'README.md'
                original = readme.read_text()
                readme.write_text(original.replace(old_text, new_text))
                with self.assertRaises(ValueError):
                    validate_metadata(self.root)
                readme.write_text(original)

    def test_mismatched_tag_or_packaged_version_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Tag'):
            validate_metadata(self.root, 'v1.4')
        (self.package / 'README.md').write_text('当前版本：v1.3。')
        with self.assertRaisesRegex(ValueError, 'Packaged README'):
            validate_metadata(self.root)

    def test_archive_content_is_checked_beyond_the_filename(self):
        metadata = validate_metadata(self.root)
        archive = self.root / metadata['archive']
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('sweety-handdraw-video/VERSION', '1.4.1\n')
        (self.root / 'package-sha256.json').write_text(json.dumps({'VERSION': hashlib.sha256(b'1.4.1\n').hexdigest()}))
        (self.root / 'SHA256SUMS.txt').write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + '  ' + archive.name)
        self.assertEqual(validate_archive(self.root, metadata), 1)
        (self.root / 'package-sha256.json').write_text(json.dumps({'VERSION': 'wrong'}))
        with self.assertRaisesRegex(ValueError, 'content checksum'):
            validate_archive(self.root, metadata)


if __name__ == '__main__':
    unittest.main()

import tempfile
import unittest
from pathlib import Path
from PIL import Image, ImageDraw
import stroke_assets
import video_verifier


class DrawingBudgetTests(unittest.TestCase):
    def picture(self, root, seconds):
        image = Image.new('RGBA', (300, 300))
        draw = ImageDraw.Draw(image)
        draw.rectangle([20, 20, 120, 120], fill='#ecb65e', outline='#202020', width=9)
        draw.rectangle([180, 180, 280, 280], fill='#7bbf6a', outline='#202020', width=9)
        image.save(root / 'source.png')
        return stroke_assets.build_illustration('budget', root / 'source.png', 300, 300,
            parts=[{'id': 'top', 'polygon': [[0, 0], [.5, 0], [.5, .5], [0, .5]]},
                   {'id': 'bottom', 'polygon': [[.5, .5], [1, .5], [1, 1], [.5, 1]]}], seconds=seconds)

    def test_a_picture_with_room_is_laid_out_at_its_measured_minimum(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stroke_assets.configure_assets(root, '/dev/null')
            metadata = self.picture(root, 40)
            # The fill minimum must start where the lines end, not at the picture top: with room in the
            # window the picture is never compressed below its own schedule, whichever layout order
            # the lines happen to end in.
            self.assertLessEqual(metadata['time_compression'], 1.001)
            self.assertEqual(metadata['drawability']['drawn_stroke_count'], metadata['drawability']['stroke_count'])
            self.assertGreater(metadata['drawability']['minimum_seconds'], .2)
            self.assertLessEqual(metadata['seconds'], 40)

    def test_a_window_too_small_still_compresses_instead_of_dropping_strokes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stroke_assets.configure_assets(root, '/dev/null')
            metadata = self.picture(root, .7)
            self.assertEqual(metadata['drawability']['drawn_stroke_count'], metadata['drawability']['stroke_count'])
            self.assertGreater(metadata['time_compression'], 1)


class EngineRecordTests(unittest.TestCase):
    def test_identity_reads_the_packaged_version_and_a_script_digest(self):
        identity = video_verifier.engine_identity()
        version = (Path(video_verifier.__file__).resolve().parent.parent / 'VERSION').read_text().strip()
        self.assertEqual(identity['version'], version)
        self.assertRegex(identity['scripts_sha256'], r'^[0-9a-f]{16}$')


if __name__ == '__main__':
    unittest.main()

"""Reading order survives a pen starting nearer a lower or right-hand stroke."""
import unittest
import numpy as np
from line_drawing import order, SILHOUETTE, STRUCTURE, DETAIL


def stroke(identity, x, y, tier=STRUCTURE):
    points = np.array([[x, y], [x + 20, y]], dtype=float)
    return {'index': identity, 'part': 0, 'tier': tier, 'points': points, 'closed': False,
            'radius': np.ones(2), 'color': np.zeros((2, 3))}


class ReadingOrderTests(unittest.TestCase):
    def test_top_left_wins_over_shorter_pen_travel(self):
        strokes = [stroke(0, 100, 100), stroke(1, 100, 20), stroke(2, 10, 20)]
        arranged, _ = order(strokes, 1, np.array([100, 100]))
        self.assertEqual([entry['index'] for entry in arranged], [2, 1, 0])

    def test_outline_precedes_structure_and_details_within_the_part(self):
        strokes = [stroke(0, 0, 0, DETAIL), stroke(1, 0, 20, STRUCTURE), stroke(2, 0, 40, SILHOUETTE)]
        arranged, _ = order(strokes, 1, np.array([0, 0]))
        self.assertEqual([entry['index'] for entry in arranged], [2, 1, 0])

    def test_marker_keeps_the_planned_top_first_sweep_even_when_tip_is_lower(self):
        from paint_motion import timeline
        region = {'part': 0, 'radius': 7, 'cells': [np.array([[10, 10], [80, 10]]), np.array([[10, 100], [80, 100]])]}
        segments = timeline([region], [], 1, np.array([80, 100]), pace=100)[0]
        sweeps = [segment for segment in segments if segment['kind'] == 'color']
        self.assertEqual(sweeps[0]['points'][0], [10, 10])
        self.assertEqual(sweeps[1]['points'][0], [10, 100])


if __name__ == '__main__':
    unittest.main()

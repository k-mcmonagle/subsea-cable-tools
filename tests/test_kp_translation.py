"""Parsing must never silently remove or reinterpret a delivered KP row."""

import unittest

from ..processing.kp_translation import mapping_flags, parse_kp, pasted_rows, sample_kps


class TranslationInputTests(unittest.TestCase):
    def test_kp_notation_and_non_finite(self):
        self.assertEqual(parse_kp('12+345'), 12.345)
        self.assertEqual(parse_kp('-0+500'), -0.5)
        self.assertEqual(parse_kp('-2.5'), -2.5)
        for text in ('nan', 'inf', '', '12+1000', '1,234', 'None'):
            with self.assertRaises(ValueError):
                parse_kp(text)

    def test_explicit_header_and_bad_rows_preserved(self):
        names, rows = pasted_rows('start,end,label\n1,2,"sand, clay"\nbad,3,rock\n4,,gap', True, True)
        self.assertEqual(names, ['start', 'end', 'label'])
        self.assertEqual(rows, [['1', '2', 'sand, clay'], ['bad', '3', 'rock'], ['4', '', 'gap']])
        self.assertEqual(pasted_rows('bad\n2')[1], [['bad'], ['2']])

    def test_point_labels_and_single_column_ranges(self):
        self.assertEqual(pasted_rows('1\t123\n2\t456')[1], [['1', '123'], ['2', '456']])
        self.assertEqual(pasted_rows('1\n2', True)[1], [['1', ''], ['2', '']])
        with self.assertRaises(ValueError):
            pasted_rows('KP', header=True)

    def test_automatic_headers_preserve_names(self):
        for heading in ['KP', 'KP (km)', 'Chainage', 'KP [km]']:
            details = {}
            names, rows = pasted_rows(heading + '\n1\n2', details=details)
            self.assertEqual(names, [heading])
            self.assertEqual(rows, [['1'], ['2']])
            self.assertTrue(details['header_detected'])
        for heading in ['Start KP,End KP,Group', 'KP From,KP To,Group', 'start_kp,end_kp,Group']:
            names, rows = pasted_rows(heading + '\n1,2,Sand', ranges=True)
            self.assertEqual(names, heading.split(','))
            self.assertEqual(rows, [['1', '2', 'Sand']])

    def test_header_override_and_bad_first_rows(self):
        self.assertEqual(pasted_rows('KP\n1', header=False)[1], [['KP'], ['1']])
        self.assertEqual(pasted_rows('Custom heading\n1', header=True)[1][0][0], '1')
        self.assertEqual(pasted_rows('bad,2\n1,2', ranges=True)[1], [['bad', '2'], ['1', '2']])
        self.assertEqual(pasted_rows('1,123\n2,456')[1], [['1', '123'], ['2', '456']])
        with self.assertRaises(ValueError):
            pasted_rows(',\n,', ranges=True)
        with self.assertRaises(ValueError):
            pasted_rows('KP')

    def test_samples_keep_exact_boundaries_and_vertices(self):
        self.assertEqual(sample_kps(2, 1, 500, [1.3]), [2, 1.5, 1.3, 1])
        self.assertEqual(sample_kps(1, 1, 50), [1])

    def test_interior_diagnostics_even_when_endpoints_match(self):
        flags = mapping_flags([0, 1, 2, 3], [0, 2, 1, 3], [0, 100, 0, 0], 25, 10)
        self.assertEqual(flags, ['offset_exceeded', 'non_monotone'])
        self.assertIn('reversed', mapping_flags([0, 1], [1, 0], [0, 0], 25, 10))
        self.assertEqual(mapping_flags([2, 1], [4, 3], [0, 0], 25, 10), [])

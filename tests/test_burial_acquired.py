"""Acquired burial revisions, support, time filtering and report consistency."""
import copy
import tempfile
import unittest
from pathlib import Path

from ..laydata import burial_data as d, burial_report as report
from ..laydata.burial_store import BurialDataStore

SPEC = {'mapping': {'time': 'time', 'kp': 'kp', 'depth': 'depth'}, 'definition': 'Depressor position',
        'timezone': 'UTC', 'crs': 'EPSG:4326'}
CONTENT = b'time,kp,depth\n2026-01-01T00:00:00Z,10,1\n2026-01-01T00:00:10Z,10.010,2\n'
RECIPE = {'route': {'fingerprint': 'design-A'}, 'kp_mode': 'supplied', 'supplied_kp_confirmed': True,
          'reversal_m': 5, 'time_gap_s': 300}


class AcquiredTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = BurialDataStore(str(Path(self.tmp.name) / 'operations.gpkg'))
        self.store.ensure()

    def imported(self, content=CONTENT, name='plough.csv'):
        identity, _ = self.store.ingest(content, name, name, SPEC)
        rows = self.store.observations(identity)
        processed = d.process(rows, RECIPE)
        revision = self.store.save_processing(identity, RECIPE, processed)
        return identity, revision, self.store.samples([revision])

    def test_utc_and_dst(self):
        self.assertEqual(d.epoch('2026-01-01T01:00:00+01:00'), d.epoch('2026-01-01T00:00:00Z'))
        self.assertEqual(d.epoch('2026-01-01 01:00', {'timezone': 'UTC+01:00'}), d.epoch('2026-01-01T00:00:00Z'))
        self.assertEqual(d.epoch('2,01:00:00', {'time_format': 'day,time', 'start_date': '2026-01-01'}),
                         d.epoch('2026-01-02T01:00:00Z'))

    def test_originals_and_reimport(self):
        identity, first = self.store.ingest(CONTENT, 'plough', 'one.csv', SPEC)
        again, second = self.store.ingest(CONTENT, 'plough', 'renamed.csv', SPEC)
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(identity, again)
        changed, _ = self.store.ingest(CONTENT.replace(b',2\n', b',3\n'), 'plough', 'one.csv', SPEC)
        self.assertEqual(self.store.imports(True)[0]['id'], changed)
        self.assertEqual(self.store.original(identity)[1], CONTENT)
        self.assertEqual(self.store.imports()[-1]['supersedes'], identity)

    def test_transaction_cancel(self):
        with self.assertRaises(InterruptedError):
            self.store.ingest(CONTENT, 'plough', 'one.csv', SPEC, cancelled=lambda: True)
        self.assertEqual(self.store.imports(), [])

    def test_station_support_and_unique_length(self):
        _, _, rows = self.imported()
        samples = d.stations(rows, {}, 1, 20)
        self.assertEqual(len(samples), 11)
        self.assertAlmostEqual(samples[5]['channels']['depth'], 1.5)
        selected = d.composite(samples + samples)
        stats = d.assess(selected, lambda kp: 1.5, (10, 10.02))
        self.assertAlmostEqual(stats['covered_km'], .010)
        self.assertAlmostEqual(stats['missing_km'], .010)
        self.assertAlmostEqual(stats['shortfall_km'], .005)

    def test_no_interpolation_across_gaps_or_bad_record(self):
        _, _, rows = self.imported()
        self.assertEqual(len(d.stations(rows, {}, 1, 5)), 2)
        middle = copy.deepcopy(rows[0])
        middle.update(kp=10.005, time=rows[0]['time'] + 5, valid=False)
        samples = d.stations([rows[0], middle, rows[1]], {}, 1, 20)
        self.assertEqual(len(samples), 2)

    def test_cutoff_before_resampling(self):
        _, _, rows = self.imported()
        cutoff = rows[1]['time']
        selected = d.stations(rows, {'time_end': cutoff}, 1)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]['channels']['depth'], 1)

    def test_overrides_and_definition(self):
        _, _, first = self.imported()
        _, _, later = self.imported(CONTENT.replace(b'2026-01-01', b'2026-01-02').replace(b',2\n', b',3\n'), 'later.csv')
        samples = d.stations(first + later, {}, 1)
        selected = d.composite(samples, [{'start_kp': 10, 'end_kp': 10.005, 'pass_id': first[0]['pass_id']}])
        self.assertEqual(selected[0]['pass_id'], first[0]['pass_id'])
        self.assertEqual(selected[-1]['pass_id'], later[0]['pass_id'])
        self.assertEqual(d.composite(samples, definition='Depth of Cover'), [])
        self.assertEqual(len(d.improvement(d.stations(first, {}), d.stations(later, {}))), 11)

    def test_reversal_preserved(self):
        content = CONTENT + b'2026-01-01T00:00:20Z,10,3\n'
        _, _, rows = self.imported(content)
        self.assertNotEqual(rows[1]['pass_id'], rows[2]['pass_id'])
        self.assertEqual(len(d.continuous_runs(rows)), 2)

    def test_corrections_are_derived(self):
        identity, _, rows = self.imported()
        recipe = dict(RECIPE, reason='Calibrated sensor offset', edits=[{'start_kp': 10, 'end_kp': 11, 'offset': .2}])
        fixed = d.process(self.store.observations(identity), recipe)
        self.assertAlmostEqual(fixed[0]['channels']['depth'], 1.2)
        self.assertEqual(self.store.observations(identity)[0]['channels']['depth'], 1)
        self.assertEqual(rows[0]['channels']['depth'], 1)

    def test_issue_survives_source_change(self):
        _, revision, rows = self.imported()
        samples = d.composite(d.stations(rows, {}))
        issue = self.store.save_issue('Daily', {'revisions': [revision]}, {'rows': samples})
        self.imported(CONTENT.replace(b',2\n', b',4\n'))
        self.assertEqual(self.store.issue(issue)['snapshot']['rows'], samples)

    def test_report_pages_and_gap_rendering(self):
        _, _, rows = self.imported()
        template = copy.deepcopy(report.DEFAULT_TEMPLATE)
        plots = report.series(rows, template)
        template['page_span'] = .005
        self.assertEqual(len(report.pages(plots, template)), 2)
        svg = report.svg_page(plots, template, (10, 10.01), subtitle='A < B')
        self.assertIn('A &lt; B', svg)
        self.assertIn('Burial Depth', svg)
        csv = report.listing_csv(d.stations(rows, {}, 1))
        self.assertIn('contributor_ids', csv)
        self.assertIn('2026-01-01T00:00:00Z', csv)

    def test_survey_without_time_and_explicit_join(self):
        spec = dict(SPEC, mapping={'kp': 'kp', 'depth': 'depth'})
        identity, _ = self.store.ingest(b'kp,depth\n10,1\n10.01,2\n', 'survey', 'survey.csv', spec)
        rows = d.process(self.store.observations(identity), RECIPE)
        self.assertEqual(len(d.stations(rows, {}, 1)), 11)
        a, b = copy.deepcopy(rows[0]), copy.deepcopy(rows[1])
        a.update(processing_id='one', pass_id='manual:joined')
        b.update(processing_id='two', pass_id='manual:joined')
        self.assertEqual(len(d.stations([a, b], {}, 1)), 11)
        b['pass_id'] = 'unjoined'
        self.assertEqual(len(d.stations([a, b], {}, 1)), 2)

    def test_target_transition_inside_coarse_interval(self):
        _, _, rows = self.imported()
        selected = d.composite(d.stations(rows, {}, 10))
        stats = d.assess(selected, lambda kp: 2 if kp < 10.004 else .5, (10, 10.02), [10.004])
        self.assertAlmostEqual(stats['shortfall_km'], .004)
        self.assertAlmostEqual(stats['met_km'], .006)
        self.assertEqual(stats['ranges'][-1]['kind'], 'missing evidence')
        line = report.target_series(selected, report.DEFAULT_TEMPLATE)[0]
        self.assertIn(10.004, line['x'])
        self.assertIn(.5, line['y'])

    def test_correction_scope_and_original_values(self):
        identity, _, rows = self.imported()
        recipe = dict(RECIPE, reason='One acquisition only', edits=[{'start_kp': 10, 'end_kp': 11,
                      'time_start': rows[1]['time'], 'offset': .5}])
        fixed = d.process(self.store.observations(identity), recipe)
        self.assertEqual(fixed[0]['channels']['depth'], 1)
        self.assertEqual(fixed[1]['channels']['depth'], 2.5)
        self.assertEqual(fixed[1]['original_channels']['depth'], 2)

    def test_daily_extent_and_channel_colours(self):
        _, _, rows = self.imported()
        for row in rows:
            row['channels'].update(pitch=1, roll=2)
            row['event'] = 'Start burial' if row is rows[0] else ''
        template = dict(report.DEFAULT_TEMPLATE, axis='time', page_span=24)
        plots = report.series(rows, template)
        self.assertNotEqual(next(s['color'] for s in plots if s['channel'] == 'pitch'),
                            next(s['color'] for s in plots if s['channel'] == 'roll'))
        start = rows[0]['time']
        snapshot = dict(query={'time_start': start, 'time_end': start + 86400}, plots=plots, template=template)
        self.assertEqual(report.pages(plots, template, report.view_extent(snapshot)), [(start, start + 86400)])
        self.assertIn('Start burial', report.svg_page(plots, template, report.view_extent(snapshot)))

    def test_composite_does_not_draw_through_another_pass(self):
        _, _, rows = self.imported()
        samples = d.stations(rows, {}, 1)
        retained = [r for r in samples if not 10.003 <= r['kp'] < 10.007]
        runs = d.continuous_runs(retained)
        self.assertEqual(len(runs), 2)

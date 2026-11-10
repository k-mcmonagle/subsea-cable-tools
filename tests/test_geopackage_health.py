"""Checked SQLite snapshots, index-only recovery, and saved-copy diagnostics."""
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import tempfile
import unittest

from .. import geopackage_health as health

REQUIRES_QGIS = False


@contextmanager
def database(path):
    with closing(sqlite3.connect(path)) as conn:
        with conn:
            yield conn


class GeoPackageHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "plans.gpkg"
        self.target = self.root / "recovered.gpkg"
        with database(self.source) as conn:
            conn.executescript("""
                CREATE TABLE bp_meta(key TEXT, value TEXT);
                INSERT INTO bp_meta VALUES('schema_version', '12');
                CREATE TABLE bp_plan(name TEXT, modified_utc TEXT);
                INSERT INTO bp_plan VALUES('Trial', '2026-01-03');
                CREATE TABLE bp_change_log(utc TEXT);
                INSERT INTO bp_change_log VALUES('2026-01-02');
                CREATE TABLE bp_cpt_data(value REAL);
                INSERT INTO bp_cpt_data VALUES(1.2);
            """)

    def test_external_wal_commits_are_in_standalone_copy(self):
        conn = sqlite3.connect(self.source)
        self.addCleanup(conn.close)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("INSERT INTO bp_cpt_data VALUES(3.4)")
        conn.commit()
        health.checked_snapshot(str(self.source), str(self.target))
        with database(self.target) as dest:
            self.assertEqual(dest.execute("SELECT COUNT(*) FROM bp_cpt_data").fetchone()[0], 2)
            self.assertEqual(dest.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        self.assertFalse(Path(str(self.target) + "-wal").exists())

    def test_existing_backup_is_never_overwritten(self):
        first = health.migration_snapshot(str(self.source), "migrate_v12")
        before = Path(first).read_bytes()
        with database(self.source) as conn:
            conn.execute("INSERT INTO bp_cpt_data VALUES(7)")
        second = health.migration_snapshot(str(self.source), "migrate_v12")
        self.assertNotEqual(first, second)
        self.assertEqual(Path(first).read_bytes(), before)
        with self.assertRaises(FileExistsError):
            health.checked_snapshot(str(self.source), first)
        self.assertEqual(Path(first).read_bytes(), before)

    def test_bad_source_and_timeout_leave_no_output(self):
        self.source.write_bytes(b"broken file")
        original = self.source.read_bytes()
        report = health.inspect_file(str(self.source))
        self.assertFalse(report.healthy)
        self.assertTrue(health.is_corruption(sqlite3.DatabaseError(report.error)))
        with self.assertRaises(sqlite3.Error):
            health.checked_snapshot(str(self.source), str(self.target))
        self.assertFalse(self.target.exists())
        self.assertEqual(self.source.read_bytes(), original)

    def test_missing_source_is_not_created(self):
        missing = str(self.root / "missing.gpkg")
        self.assertFalse(health.inspect_file(missing).healthy)
        self.assertFalse(Path(missing).exists())
        with self.assertRaises(sqlite3.Error):
            health.checked_snapshot(missing, str(self.target))
        self.assertFalse(self.target.exists())

    def test_logical_index_damage_repaired_only_in_new_copy(self):
        # Make an index whose declared column disagrees with its contents.
        with database(self.source) as conn:
            conn.executescript("""
                CREATE TABLE readings(a INTEGER, b INTEGER);
                INSERT INTO readings VALUES(1, 30), (2, 20), (3, 10);
                CREATE INDEX broken_index ON readings(a);
                PRAGMA writable_schema=ON;
                UPDATE sqlite_master SET sql='CREATE INDEX broken_index ON readings(b)'
                WHERE name='broken_index';
            """)
        original = self.source.read_bytes()
        self.assertFalse(health.inspect_file(str(self.source)).healthy)
        with self.assertRaises(sqlite3.Error):
            health.checked_snapshot(str(self.source), str(self.target))
        self.assertFalse(self.target.exists())
        health.checked_snapshot(str(self.source), str(self.target), rebuild_indexes=True)
        self.assertTrue(health.inspect_file(str(self.target)).healthy)
        self.assertEqual(self.source.read_bytes(), original)
        with database(self.target) as conn:
            self.assertEqual(conn.execute("SELECT a,b FROM readings NOT INDEXED ORDER BY a").fetchall(),
                             [(1, 30), (2, 20), (3, 10)])

    def test_summary_uses_saved_contents(self):
        report = health.inspect_file(str(self.source))
        self.assertTrue(report.healthy)
        self.assertEqual(report.schema_version, "12")
        self.assertEqual(report.plans, [("Trial", "2026-01-03")])
        self.assertEqual(report.latest_change, "2026-01-02")
        self.assertEqual(report.counts["bp_cpt_data"], 1)

    def test_snapshot_timeout_is_bounded(self):
        with self.assertRaises(TimeoutError):
            health.checked_snapshot(str(self.source), str(self.target), timeout=-1)
        self.assertFalse(self.target.exists())

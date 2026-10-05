# -*- coding: utf-8 -*-
"""Append-only operational burial revisions in a shared GeoPackage.

Connections are short-lived and owned by the calling thread. Imported originals,
processed observations, selections, templates and issues are retained. Existing
Explorer layers are left intact when this registry is added to their GeoPackage.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import os
import sqlite3
import uuid
import zlib
from contextlib import contextmanager
from datetime import datetime, timezone

from . import burial_data as data

TABLES = ('bd_meta', 'bd_import', 'bd_observation', 'bd_processing', 'bd_sample',
          'bd_selection', 'bd_template', 'bd_issue', 'bd_history')


def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def new_id():
    return uuid.uuid4().hex


class BurialDataStore:
    def __init__(self, path):
        self.path = os.path.abspath(path)

    @contextmanager
    def connection(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA foreign_keys=ON')
        try:
            with con:
                yield con
        finally:
            con.close()

    def ensure(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with self.connection() as db:
            app = db.execute('PRAGMA application_id').fetchone()[0]
            if app not in (0, 0x47504B47):
                raise ValueError('This file is not a GeoPackage')
            existing = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if app == 0 and existing and 'gpkg_contents' not in existing:
                raise ValueError('Choose a GeoPackage or a new empty file')
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA application_id=1196444487')
            if not db.execute('PRAGMA user_version').fetchone()[0]:
                db.execute('PRAGMA user_version=10300')
            db.execute('''CREATE TABLE IF NOT EXISTS gpkg_geometry_columns (
                table_name TEXT NOT NULL, column_name TEXT NOT NULL, geometry_type_name TEXT NOT NULL,
                srs_id INTEGER NOT NULL, z TINYINT NOT NULL, m TINYINT NOT NULL,
                CONSTRAINT pk_geom_cols PRIMARY KEY (table_name, column_name),
                CONSTRAINT uk_gc_table_name UNIQUE (table_name))''')
            db.execute('''CREATE TABLE IF NOT EXISTS gpkg_spatial_ref_sys
                (srs_name TEXT NOT NULL, srs_id INTEGER NOT NULL PRIMARY KEY,
                 organization TEXT NOT NULL, organization_coordsys_id INTEGER NOT NULL,
                 definition TEXT NOT NULL, description TEXT)''')
            db.executemany('INSERT OR IGNORE INTO gpkg_spatial_ref_sys VALUES (?,?,?,?,?,?)', [
                ('Undefined Cartesian', -1, 'NONE', -1, 'undefined', ''),
                ('Undefined geographic', 0, 'NONE', 0, 'undefined', ''),
                ('WGS 84', 4326, 'EPSG', 4326,
                 'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
                 'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433],AUTHORITY["EPSG","4326"]]', '')])
            db.execute('''CREATE TABLE IF NOT EXISTS gpkg_contents (table_name TEXT NOT NULL PRIMARY KEY,
                data_type TEXT NOT NULL, identifier TEXT UNIQUE, description TEXT DEFAULT '',
                last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                min_x DOUBLE,min_y DOUBLE,max_x DOUBLE,max_y DOUBLE,srs_id INTEGER)''')
            db.execute('CREATE TABLE IF NOT EXISTS bd_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            version = db.execute("SELECT value FROM bd_meta WHERE key='version'").fetchone()
            if version and version[0] != '1':
                raise ValueError('Unsupported burial operations schema; open with a compatible plugin version')
            db.execute("INSERT OR IGNORE INTO bd_meta VALUES ('version','1')")
            db.execute('''CREATE TABLE IF NOT EXISTS bd_import (
                id TEXT PRIMARY KEY, source_key TEXT NOT NULL, name TEXT NOT NULL, checksum TEXT NOT NULL,
                spec TEXT NOT NULL, original BLOB NOT NULL, created TEXT NOT NULL, author TEXT NOT NULL,
                supersedes TEXT, count INTEGER NOT NULL, UNIQUE(source_key, checksum, spec))''')
            db.execute('''CREATE TABLE IF NOT EXISTS bd_observation (
                fid INTEGER PRIMARY KEY, observation_id TEXT UNIQUE NOT NULL,
                import_id TEXT NOT NULL REFERENCES bd_import(id), source_row INTEGER NOT NULL,
                ISO_Time TEXT, KP REAL, Burial_Depth REAL, Pitch REAL, Roll REAL, Tension REAL,
                source_file TEXT, record_status TEXT, payload TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS bd_processing (
                id TEXT PRIMARY KEY, import_id TEXT NOT NULL REFERENCES bd_import(id),
                recipe TEXT NOT NULL, route TEXT NOT NULL, created TEXT NOT NULL, author TEXT NOT NULL,
                count INTEGER NOT NULL, supersedes TEXT)''')
            db.execute('''CREATE TABLE IF NOT EXISTS bd_sample (
                fid INTEGER PRIMARY KEY, processing_id TEXT NOT NULL REFERENCES bd_processing(id),
                observation_id TEXT NOT NULL, pass_id TEXT NOT NULL, ISO_Time TEXT, KP REAL,
                Burial_Depth REAL, Pitch REAL, Roll REAL, Tension REAL, source_file TEXT,
                record_status TEXT, payload TEXT NOT NULL)''')
            for table in ('bd_selection', 'bd_template'):
                db.execute(f'''CREATE TABLE IF NOT EXISTS {table} (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, created TEXT NOT NULL,
                    author TEXT NOT NULL, payload TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS bd_issue (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, created TEXT NOT NULL,
                author TEXT NOT NULL, manifest TEXT NOT NULL, snapshot BLOB NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS bd_history (
                fid INTEGER PRIMARY KEY, created TEXT NOT NULL, author TEXT NOT NULL,
                action TEXT NOT NULL, target TEXT NOT NULL, detail TEXT NOT NULL)''')
            # GDAL's optional count cache avoids a second temporary count connection
            # on Windows; maintain it transactionally with append-only records.
            db.execute('CREATE TABLE IF NOT EXISTS gpkg_ogr_contents (table_name TEXT NOT NULL PRIMARY KEY, feature_count INTEGER DEFAULT NULL)')
            for table in ('bd_observation', 'bd_sample'):
                db.execute(f'INSERT OR IGNORE INTO gpkg_ogr_contents VALUES (?, (SELECT COUNT(*) FROM {table}))', (table,))
                db.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_insert_count AFTER INSERT ON {table}
                    BEGIN UPDATE gpkg_ogr_contents SET feature_count=feature_count+1 WHERE table_name='{table}'; END""")
                db.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_delete_count AFTER DELETE ON {table}
                    BEGIN UPDATE gpkg_ogr_contents SET feature_count=feature_count-1 WHERE table_name='{table}'; END""")
            db.execute('CREATE INDEX IF NOT EXISTS bd_obs_import ON bd_observation(import_id)')
            db.execute('CREATE INDEX IF NOT EXISTS bd_sample_processing ON bd_sample(processing_id, KP)')
            db.execute('CREATE INDEX IF NOT EXISTS bd_sample_time ON bd_sample(ISO_Time)')
            db.execute("""CREATE VIEW IF NOT EXISTS bd_current_observations AS
                SELECT o.* FROM bd_observation o JOIN bd_import i ON i.id=o.import_id
                WHERE NOT EXISTS (SELECT 1 FROM bd_import n WHERE n.source_key=i.source_key AND n.rowid>i.rowid)""")
            db.execute("""CREATE VIEW IF NOT EXISTS bd_current_samples AS
                SELECT o.* FROM bd_sample o JOIN bd_processing p ON p.id=o.processing_id
                JOIN bd_import i ON i.id=p.import_id
                WHERE NOT EXISTS (SELECT 1 FROM bd_import n WHERE n.source_key=i.source_key AND n.rowid>i.rowid)
                AND NOT EXISTS (SELECT 1 FROM bd_processing n WHERE n.import_id=p.import_id AND n.rowid>p.rowid)""")
            for table in (*TABLES, 'bd_current_observations', 'bd_current_samples'):
                db.execute('INSERT OR IGNORE INTO gpkg_contents(table_name,data_type,identifier) VALUES (?, ?, ?)',
                           (table, 'attributes', table))

    @staticmethod
    def _log(db, action, target, detail):
        db.execute('INSERT INTO bd_history(created,author,action,target,detail) VALUES (?,?,?,?,?)',
                   (now(), getpass.getuser(), action, target, data.serialise(detail)))

    def ingest(self, content, source_key, name, spec, records=None, cancelled=None):
        if not source_key.strip():
            raise ValueError('A logical source name is required')
        encoded = data.serialise(spec)
        checksum = hashlib.sha256(content).hexdigest()
        with self.connection() as db:
            found = db.execute('SELECT id FROM bd_import WHERE source_key=? AND checksum=? AND spec=?',
                               (source_key, checksum, encoded)).fetchone()
            if found:
                return found['id'], False
        if records is None:
            _, records = data.read_csv(content, spec)
        observations = data.normalise(records, spec)
        if not observations:
            raise ValueError('No observations were found')
        identity = new_id()
        with self.connection() as db:
            previous = db.execute('SELECT id FROM bd_import WHERE source_key=? ORDER BY rowid DESC LIMIT 1',
                                  (source_key,)).fetchone()
            db.execute('INSERT INTO bd_import VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (identity, source_key, name, checksum, encoded, content, now(), getpass.getuser(),
                        previous[0] if previous else None, len(observations)))
            batch = []
            for index, row in enumerate(observations):
                if cancelled and cancelled():
                    raise InterruptedError('Import cancelled; nothing was written')
                row.update(observation_id=f'{identity}:{index}', import_id=identity,
                           definition=spec['definition'], source_file=name)
                c = row['channels']
                batch.append((row['observation_id'], identity, row['source_row'], data.iso(row['time']),
                              row['kp'], c.get('depth'), c.get('pitch'), c.get('roll'), c.get('tension'), name,
                              'flagged' if row['flags'] else 'active', data.serialise(row)))
            db.executemany('''INSERT INTO bd_observation(observation_id,import_id,source_row,ISO_Time,KP,
                           Burial_Depth,Pitch,Roll,Tension,source_file,record_status,payload)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''', batch)
            self._log(db, 'import', identity, {'checksum': checksum, 'source_key': source_key,
                                              'rows': len(observations), 'flagged': sum(bool(r['flags']) for r in observations)})
        return identity, True

    def imports(self, current_only=False):
        with self.connection() as db:
            rows = [dict(r) for r in db.execute('''SELECT id,source_key,name,checksum,spec,created,author,
                supersedes,count FROM bd_import ORDER BY rowid''')]
        latest = {r['source_key']: r['id'] for r in rows}
        for row in rows:
            row['current'] = latest[row['source_key']] == row['id']
            row['spec'] = json.loads(row['spec'])
        return [r for r in rows if r['current'] or not current_only]

    def observations(self, identity):
        with self.connection() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT payload FROM bd_observation WHERE import_id=? ORDER BY fid',
                                                       (identity,))]

    def save_processing(self, identity, recipe, rows, cancelled=None):
        revision = new_id()
        route = data.serialise(recipe['route'])
        with self.connection() as db:
            previous = db.execute('SELECT id FROM bd_processing WHERE import_id=? AND route=? ORDER BY rowid DESC LIMIT 1',
                                  (identity, route)).fetchone()
            db.execute('INSERT INTO bd_processing VALUES (?,?,?,?,?,?,?,?)',
                       (revision, identity, data.serialise(recipe), route, now(), getpass.getuser(), len(rows),
                        previous[0] if previous else None))
            batch = []
            for row in rows:
                if cancelled and cancelled():
                    raise InterruptedError('Processing cancelled; no revision saved')
                row = dict(row, processing_id=revision)
                c = row['channels']
                batch.append((revision, row['observation_id'], row['pass_id'], data.iso(row['time']), row['kp'],
                              c.get('depth'), c.get('pitch'), c.get('roll'), c.get('tension'), row['source_file'],
                              'active' if row['valid'] else 'inactive', data.serialise(row)))
            db.executemany('''INSERT INTO bd_sample(processing_id,observation_id,pass_id,ISO_Time,KP,
                           Burial_Depth,Pitch,Roll,Tension,source_file,record_status,payload)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''', batch)
            self._log(db, 'process', revision, recipe)
        return revision

    def revisions(self):
        with self.connection() as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM bd_processing ORDER BY rowid')]
        latest = {(r['import_id'], r['route']): r['id'] for r in rows}
        current = {r['id'] for r in self.imports(True)}
        for row in rows:
            row['current'] = latest[(row['import_id'], row['route'])] == row['id'] and row['import_id'] in current
            row['recipe'], row['route'] = json.loads(row['recipe']), json.loads(row['route'])
        return rows

    def samples(self, identities):
        if not identities:
            return []
        result = []
        with self.connection() as db:
            for identity in dict.fromkeys(identities):
                result.extend(json.loads(r[0]) for r in db.execute(
                    'SELECT payload FROM bd_sample WHERE processing_id=? ORDER BY fid', (identity,)))
        return result

    def save_named(self, kind, name, payload):
        if kind not in ('selection', 'template') or not name.strip():
            raise ValueError('A valid kind and name are required')
        identity = new_id()
        with self.connection() as db:
            db.execute(f'INSERT INTO bd_{kind} VALUES (?,?,?,?,?)',
                       (identity, name.strip(), now(), getpass.getuser(), data.serialise(payload)))
            self._log(db, kind, identity, {'name': name})
        return identity

    def named(self, kind):
        if kind not in ('selection', 'template'):
            raise ValueError('Invalid registry')
        with self.connection() as db:
            result = [dict(r) for r in db.execute(f'SELECT * FROM bd_{kind} ORDER BY rowid')]
        for row in result:
            row['payload'] = json.loads(row['payload'])
        return result

    def save_issue(self, name, manifest, snapshot, identity=None):
        identity = identity or new_id()
        blob = zlib.compress(data.serialise(snapshot).encode('utf-8'))
        with self.connection() as db:
            db.execute('INSERT INTO bd_issue VALUES (?,?,?,?,?,?)',
                       (identity, name, now(), getpass.getuser(), data.serialise(manifest), blob))
            self._log(db, 'issue', identity, manifest)
        return identity

    def issues(self):
        with self.connection() as db:
            return [dict(r) for r in db.execute('SELECT id,name,created,author,manifest FROM bd_issue ORDER BY rowid DESC')]

    def issue(self, identity):
        with self.connection() as db:
            row = db.execute('SELECT * FROM bd_issue WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise ValueError('Report issue not found')
        return dict(id=row['id'], name=row['name'], created=row['created'], manifest=json.loads(row['manifest']),
                    snapshot=json.loads(zlib.decompress(row['snapshot'])))

    def original(self, identity):
        with self.connection() as db:
            row = db.execute('SELECT name,original FROM bd_import WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise ValueError('Source revision not found')
        return row['name'], bytes(row['original'])

    def history(self):
        with self.connection() as db:
            return [dict(r) for r in db.execute('SELECT * FROM bd_history ORDER BY fid DESC')]

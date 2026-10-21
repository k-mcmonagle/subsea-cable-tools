# -*- coding: utf-8 -*-
"""Acquired burial data: the burial data GeoPackage (sqlite3 + numpy, no QGIS).

* The file is a valid GeoPackage in rollback-journal mode; reopening adds
  nothing twice; a newer schema is refused with a plain message.
* Datasets and import profiles (a profile that reads the same keeps its
  id; a changed one becomes the next version); every change is logged.
* Daily deliveries: the 10-minute midnight overlaps are found and the newer
  file wins by default; the same bytes again are refused with when and by
  whom; a re-delivered day under the same name replaces the original (kept,
  greyed) and its corrected values take effect; originals come back byte
  for byte and a tampered checksum is caught; an overlap rule changes only
  with a reason and keeps the old decision; a withdrawn file drops out and
  can be restored.
* A survey without a time column needs its acquisition date, which then
  stands in for time; chunked files read only the chunks a KP window
  touches; a reader on another thread sees the same records.
* End to end: plough log, survey and re-survey assessed latest-wins against
  a 1 m target find the hard ground, each from the right dataset.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
from datetime import datetime, timezone

import numpy as np

from ..burial.acquired import bins as B
from ..burial.acquired import overlap as O
from ..burial.acquired import profile as P
from ..burial.acquired import reader
from ..burial.acquired import roles as R
from ..burial.acquired import samples as S
from ..burial.acquired import store as store_module
from ..burial.acquired.store import AcquiredStore, AcquiredStoreError, DuplicateFileError, StoreReader

REQUIRES_QGIS = False


def _result(name: str, ok: bool, detail: str = "") -> bool:
    ok = bool(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def _check(name: str, checks: dict) -> bool:
    failed = [k for k, v in checks.items() if not v]
    return _result(name, not failed, ", ".join(failed))


def _path(name: str = "project_burial_data.gpkg") -> str:
    return os.path.join(tempfile.mkdtemp(prefix="ba_store_"), name)


def _raises(call, kind=AcquiredStoreError, text: str = "") -> bool:
    try:
        call()
    except kind as exc:
        return text in str(exc)
    return False


def _profile(content: bytes, name: str, crs: str = f"EPSG:{S.UTM_EPSG}") -> dict:
    table = reader.sniff(content, name)
    raw = reader.read_table(content, name, table, max_rows=5)
    prof = P.new_profile(name)
    prof["table"] = table
    prof["columns"] = P.suggest(raw.headers, raw.units)
    prof["crs"] = crs
    return prof


def _import(store, dataset_id, name, content, prof, acquired="", replaces=None, rule=None):
    _raw, data = reader.read(content, name, prof)
    prepared = store_module.prepare_file(name, content, data)
    existing = [f for f in store.file_times(dataset_id) if f.file_id != replaces]
    overlaps = O.find_overlaps(existing, O.FileTimes("new", 0, prepared.times))
    for o in overlaps:
        o.rule = rule or o.rule
    profile_id = store.save_profile(prof["name"] or "Profile", prof)
    return store.add_file(dataset_id, prepared, profile_id, acquired_utc=acquired, replaces_file_id=replaces,
                          overlaps=overlaps), overlaps


_RUN = None


def _run():
    global _RUN
    if _RUN is None:
        _RUN = S.plough_run(rate_hz=0.1)
    return _RUN


def test_create_and_reopen() -> bool:
    path = _path()
    store = AcquiredStore.create(path)
    reopened = AcquiredStore(path).open().meta().get("schema_version")
    store.close()
    conn = sqlite3.connect(path)
    try:
        app = conn.execute("PRAGMA application_id").fetchone()[0]
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        conn.execute("UPDATE ba_meta SET value = '99' WHERE key = 'schema_version'")
        conn.commit()
    finally:
        conn.close()
    newer = _raises(lambda: AcquiredStore(path).open(), text="newer version")
    other = _path("other.gpkg")
    sqlite3.connect(other).close()
    store_module.close_connection()
    return _check("store: a GeoPackage, rollback journal, schema guard", {
        "gpkg": app == 0x47504B47 and {"gpkg_contents", "gpkg_spatial_ref_sys", "gpkg_geometry_columns"} <= tables,
        "tables": set(store_module._TABLES) <= tables,
        "not wal": str(journal).lower() != "wal",
        "probe": store_module.is_data_store(path) and not store_module.is_data_store(other),
        "reopen": reopened == str(store_module.SCHEMA_VERSION),
        "newer refused": newer,
        "not a data file": _raises(lambda: AcquiredStore(other).open(), text="not a burial data file"),
    })


def test_datasets_and_profiles() -> bool:
    store = AcquiredStore.create(_path())
    try:
        dataset = store.create_dataset("Plough log - main lay", rpl_id="r1", rpl_label="S1 rev C", rpl_start_kp=0.0)
        survey = store.create_dataset("Survey PLS-01", kind=store_module.KIND_SURVEY)
        store.update_dataset(dataset, {"originator": "Survey contractor", "notes": "1 Hz"}, reason="details")
        row = store.get_dataset(dataset)
        prof = P.new_profile("Plough v1")
        prof["columns"] = [P.column("KP", R.KP), P.column("DOL", R.DOL)]
        first = store.save_profile("Plough v1", prof)
        same = store.save_profile("Plough v1", P.normalise(prof))
        prof["columns"][0]["unit"] = "m"
        second = store.save_profile("Plough v1", prof)
        latest = store.list_profiles()
        actions = [e["action"] for e in store.log()]
        return _check("store: datasets, profile versions, log", {
            "order": [d["dataset_id"] for d in store.list_datasets()] == [dataset, survey],
            "measure": row["measure"] == store_module.MEASURE_DOL and row["rpl_label"] == "S1 rev C",
            "updated": row["originator"] == "Survey contractor",
            "name needed": _raises(lambda: store.create_dataset("  "), text="name"),
            "kind": _raises(lambda: store.create_dataset("x", kind="bogus"), text="dataset type"),
            "not editable": _raises(lambda: store.update_dataset(dataset, {"author": "x"}), text="Cannot change"),
            "same profile same id": first == same,
            "new version": second != first and store.get_profile(second)["version"] == 2
            and latest == [{**latest[0], "profile_id": second}],
            "profile reads back": store.get_profile(first)["profile"]["columns"][0]["unit"] == "km",
            "logged": actions.count("create_dataset") == 2 and "update_dataset" in actions
            and actions.count("save_profile") == 2,
        })
    finally:
        store.close()


def test_daily_imports() -> bool:
    run = _run()
    days = S.daily_files(run)
    store = AcquiredStore.create(_path())
    try:
        dataset = store.create_dataset("Plough log")
        prof = _profile(days[0][1], days[0][0])
        ids, found = [], []
        for name, content in days:
            file_id, overlaps = _import(store, dataset, name, content, prof)
            ids.append(file_id)
            found.append(overlaps)
        files = store.list_files(dataset)
        kept = sum(f["rows_kept"] for f in files)
        effective = store.effective(dataset, [R.DOL])
        per_overlap = found[1][0].older_count if found[1] else -1
        duplicate = _raises(lambda: _import(store, dataset, days[0][0], days[0][1], prof), DuplicateFileError,
                            "already in this dataset")
        before = store.effective(dataset, [R.DOL])
        name, content = S.redelivered_day(run)
        new_day2, _o = _import(store, dataset, name, content, prof, replaces=ids[1])
        after = store.effective(dataset, [R.DOL])
        statuses = {f["file_id"]: f["status"] for f in store.list_files(dataset)}
        start2 = np.floor(run.time[0] / 86400.0) * 86400.0 + 86400.0
        window = (after[R.TIME] >= start2 + 10.5 * 3600) & (after[R.TIME] < start2 + 11.5 * 3600)
        old_window = (before[R.TIME] >= start2 + 10.5 * 3600) & (before[R.TIME] < start2 + 11.5 * 3600)
        shift = np.nanmean(after[R.DOL][window]) - np.nanmean(before[R.DOL][old_window])
        original_name, original = store.original(ids[0])
        rejected_old = store.rejected_rows(ids[1])
        rejected_new = store.rejected_rows(new_day2)
        return _check("store: daily files, overlaps, duplicates, re-delivery", {
            "three files": [f["seq"] for f in files] == [1, 2, 3],
            "overlaps found": [len(f) for f in found] == [0, 1, 1] and per_overlap == 60,
            "newer wins": effective["line"].size == kept - 2 * per_overlap,
            "file index": set(effective["file_index"].tolist()) == {0, 1, 2},
            "duplicate refused": duplicate,
            "replaced kept": statuses[ids[1]] == store_module.STATUS_REPLACED and statuses[new_day2] == "current"
            and len(statuses) == 4,
            "replacement overlaps": len(store.list_overlaps(dataset)) == 4,
            "correction in effect": abs(shift - 0.05) < 0.01,
            "original bytes": original == days[0][1] and original_name == days[0][0],
            "rejected rows": len(rejected_old) == 3 and rejected_new == [],
            "time kept": effective[R.TIME].size == effective["line"].size,
        })
    finally:
        store.close()


def test_rules_status_and_integrity() -> bool:
    run = _run()
    days = S.daily_files(run)
    store = AcquiredStore.create(_path())
    try:
        dataset = store.create_dataset("Plough log")
        prof = _profile(days[0][1], days[0][0])
        ids = [_import(store, dataset, name, content, prof)[0] for name, content in days]
        base = store.effective(dataset, [R.DOL])["line"].size
        first = store.list_overlaps(dataset)[0]
        no_reason = _raises(lambda: store.change_overlap(first["overlap_id"], O.RULE_EXISTING, "  "), text="why")
        changed = store.change_overlap(first["overlap_id"], O.RULE_BOTH, "two systems logged both")
        both = store.effective(dataset, [R.DOL])["line"].size
        stale = _raises(lambda: store.change_overlap(first["overlap_id"], O.RULE_NEWER, "again"), text="already")
        history = store.list_overlaps(dataset, include_superseded=True)
        store.set_file_status(ids[2], store_module.STATUS_WITHDRAWN, "wrong vessel")
        withdrawn = store.effective(dataset, [R.DOL])
        store.set_file_status(ids[2], store_module.STATUS_CURRENT, "right vessel after all")
        restored = store.effective(dataset, [R.DOL])["line"].size
        conn = sqlite3.connect(store.path)
        try:
            conn.execute("UPDATE ba_file SET sha256 = 'x' WHERE file_id = ?", (ids[0],))
            conn.commit()
        finally:
            conn.close()
        tampered = _raises(lambda: store.original(ids[0]), text="does not match")
        actions = [e["action"] for e in store.log(dataset)]
        return _check("store: overlap rules, file status, checksum", {
            "reason needed": no_reason,
            "keep both": both == base + first["older_count"],
            "old decision kept": len(history) == 3 and any(h["superseded_by"] == changed for h in history),
            "stale change refused": stale,
            "withdrawn drops out": 2 not in set(withdrawn["file_index"].tolist())
            and withdrawn["line"].size < both,
            "restored": restored == both,
            "tamper caught": tampered,
            "logged": actions.count("file_status") == 2 and actions.count("overlap") == 3,
        })
    finally:
        store.close()


def _utc(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


def test_survey_chunks_and_threads() -> bool:
    name, content = S.survey_listing(step_m=5.0)
    store = AcquiredStore.create(_path())
    previous = store_module.CHUNK_ROWS
    try:
        dataset = store.create_dataset("Survey PLS-01", kind=store_module.KIND_SURVEY)
        prof = _profile(content, name)
        no_date = _raises(lambda: _import(store, dataset, name, content, prof), text="date it was acquired")
        _import(store, dataset, name, content, prof, acquired=S.SURVEY_DATE + "T00:00:00Z")
        survey = store.effective(dataset, [R.DOL, R.EASTING])
        store_module.CHUNK_ROWS = 500
        run = _run()
        day_name, day = S.daily_files(run)[0]
        plough = store.create_dataset("Plough log")
        file_id, _o = _import(store, plough, day_name, day, _profile(day, day_name))
        chunks = store._read("SELECT COUNT(*) AS n FROM ba_chunk WHERE file_id = ?", (file_id,))[0]["n"]
        whole = store.read_columns(file_id, [R.KP])
        window = store.read_columns(file_id, [R.KP], kp_range=(1.0, 1.2))
        inside = int(((whole[R.KP] >= 1.0) & (whole[R.KP] <= 1.2)).sum())
        seen = {}

        def work():
            with StoreReader(store.path) as background:
                seen["n"] = background.effective(plough, [R.DOL])["line"].size

        thread = threading.Thread(target=work)
        thread.start()
        thread.join(30)
        return _check("store: survey date, chunks, background reader", {
            "date needed": no_date,
            "date stands in for time": np.all(survey[R.TIME] == _utc(S.SURVEY_DATE + "T00:00:00")),
            "survey rows": survey["line"].size == 12000 // 5 + 1 and np.isfinite(survey[R.EASTING]).all(),
            "chunked": chunks == -(-whole[R.KP].size // 500),
            "window reads fewer": 0 < inside <= window[R.KP].size < whole[R.KP].size,
            "thread": seen.get("n") == whole[R.KP].size,
        })
    finally:
        store_module.CHUNK_ROWS = previous
        store.close()


def test_end_to_end_assessment() -> bool:
    run = _run()
    store = AcquiredStore.create(_path())
    try:
        plough = store.create_dataset("Plough log")
        survey = store.create_dataset("Survey PLS-01", kind=store_module.KIND_SURVEY)
        resurvey = store.create_dataset("ROV re-survey", kind=store_module.KIND_SURVEY)
        days = S.daily_files(run)
        prof = _profile(days[0][1], days[0][0])
        for name, content in days:
            _import(store, plough, name, content, prof)
        name, content = S.survey_listing(step_m=1.0)
        _import(store, survey, name, content, _profile(content, name), acquired=S.SURVEY_DATE + "T00:00:00Z")
        name, content = S.rov_resurvey()
        _import(store, resurvey, name, content, _profile(content, name, crs=""))
        binsets = []
        for dataset in (plough, survey, resurvey):
            data = store.effective(dataset, [R.DOL])
            binsets.append(B.bin_samples(data[R.KP], data[R.DOL], data[R.TIME]))
        selection = B.select(binsets)
        target = B.target_at(selection.centre_km, [(0.0, 40.0, S.TARGET_DOL_M)])
        status = B.classify(selection, target)
        ranges = B.below_ranges(selection, status, target, join_gap_m=5.0)
        stats = B.summarise(selection, status)

        def source_at(kp):
            hit = [r for r in ranges if r["kp_from"] <= kp < r["kp_to"]]
            return hit[0]["sources"] if hit else None

        at = selection.index.tolist()
        return _check("store: plough, survey and re-survey, latest wins", {
            "re-survey governs its stretch": selection.source[at.index(3400)] == 2,
            "survey governs to 12 km": selection.source[at.index(9000)] == 1,
            "plough beyond": selection.source[at.index(15000)] == 0,
            "hard ground 3.2-3.55 from the re-survey": source_at(3.40) == [2],
            "hard ground 7.85-8.05 from the survey": source_at(7.95) == [1],
            "hard ground 14.3-14.42 from the plough": source_at(14.36) == [0],
            "good ground meets": source_at(10.0) is None and source_at(5.0) is None,
            "assessed over 20 km": stats["assessed_m"] > 20000.0 and stats["sources"] == [0, 1, 2],
        })
    finally:
        store.close()


def run_all() -> list:
    return [
        test_create_and_reopen(),
        test_datasets_and_profiles(),
        test_daily_imports(),
        test_rules_status_and_integrity(),
        test_survey_chunks_and_threads(),
        test_end_to_end_assessment(),
    ]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)

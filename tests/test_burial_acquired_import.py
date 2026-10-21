# -*- coding: utf-8 -*-
"""Acquired burial data: reviewing and importing deliveries, and the assessment (numpy, no QGIS).

* KpMap.map_array gives what map_kp gives, flags included (anchors, gaps,
  ends, shift, identity).
* Review writes nothing and reports rows, rejected rows, duplicates,
  same-name replacements and overlaps (with the files before it in the
  import); changing what is replaced works the overlaps out again and keeps
  chosen rules. Import writes the dataset, profile and files in one
  transaction: a cancel or a file changed on disk since the review leaves
  the data file as it was; files without a time column need their date.
* The assessment: latest wins over plough, survey and re-survey; plan
  sections get their own figures; a click names the dataset, file, line and
  time behind a bin; listings carry the record; a dataset on another RPL is
  moved onto the plan's KPs (here by a shift) and says so.
"""

from __future__ import annotations

import os
import tempfile

import numpy as np

from ..burial.acquired import assessment as A
from ..burial.acquired import bins as B
from ..burial.acquired import importer as I
from ..burial.acquired import overlap as O
from ..burial.acquired import profile as P
from ..burial.acquired import reader
from ..burial.acquired import roles as R
from ..burial.acquired import samples as S
from ..burial.acquired.store import AcquiredStore, AcquiredStoreError
from ..burial.kp_rereference import KpMap

REQUIRES_QGIS = False


def _result(name: str, ok: bool, detail: str = "") -> bool:
    ok = bool(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def _check(name: str, checks: dict) -> bool:
    failed = [k for k, v in checks.items() if not v]
    return _result(name, not failed, ", ".join(failed))


def _raises(call, kind=Exception, text: str = "") -> bool:
    try:
        call()
    except kind as exc:
        return text in str(exc)
    return False


def _write(folder: str, name: str, content: bytes) -> str:
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    with open(path, "wb") as handle:
        handle.write(content)
    return path


def _profile(content: bytes, name: str, crs: str = f"EPSG:{S.UTM_EPSG}") -> dict:
    table = reader.sniff(content, name)
    raw = reader.read_table(content, name, table, max_rows=5)
    prof = P.new_profile(name)
    prof["table"] = table
    prof["columns"] = P.suggest(raw.headers, raw.units)
    prof["crs"] = crs
    return prof


_RUN = None


def _run():
    global _RUN
    if _RUN is None:
        _RUN = S.plough_run(rate_hz=0.1)
    return _RUN


def test_map_array() -> bool:
    kp_map = KpMap([(0.0, 0.0), (1.0, 1.1), (2.0, 2.05), (5.0, 5.2)], gap_ranges=[(1.5, 1.8)])
    values = np.array([-0.5, 0.0, 0.3, 1.0, 1.6, 1.9, 3.0, 5.0, 6.0])
    mapped, extra, gap = kp_map.map_array(values)
    single = [kp_map.map_kp(v) for v in values]
    shift = KpMap.shift(0.25)
    s_mapped, s_extra, _g = shift.map_array(values)
    ident, i_extra, _g2 = KpMap.identity().map_array(values)
    return _check("KpMap.map_array agrees with map_kp", {
        "values": np.allclose(mapped, [v for v, _f in single]),
        "extrapolated": extra.tolist() == ["extrapolated" in f for _v, f in single],
        "gap": gap.tolist() == ["gap" in f for _v, f in single],
        "shift": np.allclose(s_mapped, values + 0.25) and not s_extra.any(),
        "identity": np.array_equal(ident, values) and not i_extra.any(),
    })


def test_review_and_import() -> bool:
    run = _run()
    days = S.daily_files(run)
    folder = tempfile.mkdtemp(prefix="ba_import_")
    paths = [_write(os.path.join(folder, "in"), name, content) for name, content in days]
    prof = _profile(days[0][1], days[0][0])
    store = AcquiredStore.create(os.path.join(folder, "job_burial_data.gpkg"))
    try:
        batch = I.review_files(None, None, paths, prof)
        counts = I.summary(batch)
        written_before = store.log()
        options = I.ImportOptions(prof, "Plough log v1", received_utc="2026-10-21T08:00:00Z")
        target = I.DatasetTarget(name="Plough log - main lay", rpl_id="rpl-1", rpl_label="S1 rev C", rpl_start_kp=0.0)
        result = I.import_batch(store.path, target, batch, options)
        dataset = store.get_dataset(result.dataset_id)
        files = store.list_files(result.dataset_id)
        first_overlaps = len(store.list_overlaps(result.dataset_id))

        # The re-delivery: day 1 again (same bytes) and day 2 re-sent under its name.
        name2, redelivered = S.redelivered_day(run)
        again = [paths[0], _write(os.path.join(folder, "resent"), name2, redelivered)]
        review2 = I.review_files(store, result.dataset_id, again, prof)
        replace_overlaps = sorted(o.older_name for o in review2[1].overlaps)
        review2[1].overlaps[0].rule = O.RULE_EXISTING
        kept_key = review2[1].overlaps[0].key()
        review2[1].replaces = None                       # keep both instead
        I.plan_overlaps(review2, store.file_times(result.dataset_id))
        both_overlaps = sorted(o.older_name for o in review2[1].overlaps)
        kept_rule = next(o.rule for o in review2[1].overlaps if o.key() == kept_key)
        review2[1].replaces = review2[1].same_name["file_id"]
        I.plan_overlaps(review2, store.file_times(result.dataset_id))

        files_before = len(store.list_files(result.dataset_id))
        cancelled = _raises(lambda: I.import_batch(store.path, I.DatasetTarget(dataset_id=result.dataset_id),
                                                   review2, options, cancelled=lambda: True),
                            InterruptedError, "nothing was written")
        with open(again[1], "ab") as handle:
            handle.write(b"\r\n")
        changed = _raises(lambda: I.import_batch(store.path, I.DatasetTarget(dataset_id=result.dataset_id),
                                                 review2, options), AcquiredStoreError, "changed on disk")
        untouched = len(store.list_files(result.dataset_id)) == files_before
        review3 = I.review_files(store, result.dataset_id, [again[1]], prof)
        second = I.import_batch(store.path, I.DatasetTarget(dataset_id=result.dataset_id), review3, options)
        statuses = sorted(f["status"] for f in store.list_files(result.dataset_id))

        name_s, survey = S.survey_listing(step_m=10.0)
        survey_path = _write(os.path.join(folder, "survey"), name_s, survey)
        survey_prof = _profile(survey, name_s)
        survey_batch = I.review_files(None, None, [survey_path], survey_prof)
        no_date = _raises(lambda: I.import_batch(store.path, I.DatasetTarget(name="Survey"), survey_batch,
                                                 I.ImportOptions(survey_prof, "Survey")), AcquiredStoreError,
                          "give the date")
        with_date = I.import_batch(store.path, I.DatasetTarget(name="Survey", kind="survey"), survey_batch,
                                   I.ImportOptions(survey_prof, "Survey", acquired_utc="2026-10-24T00:00:00Z"))
        return _check("review and import: counts, duplicates, replacement, overlaps, all or nothing", {
            "review writes nothing": written_before == [] and not os.path.exists(store.path + "-journal"),
            "review counts": counts["files"] == 3 and counts["rejected"] == 3 and counts["overlaps"] == 2
            and counts["duplicates"] == 0,
            "rejected sample": len(batch[1].rejected) == 3 and batch[1].rows_rejected == 3,
            "imported": len(result.imported) == 3 and result.rows == sum(b.rows_kept for b in batch),
            "dataset": dataset["name"] == "Plough log - main lay" and dataset["rpl_label"] == "S1 rev C"
            and dataset["profile_id"],
            "files": [f["seq"] for f in files] == [1, 2, 3] and first_overlaps == 2,
            "duplicate": review2[0].duplicate is not None and not review2[0].importable,
            "same name replaces": review2[1].replaces == files[1]["file_id"] and replace_overlaps == sorted(
                [days[0][0], days[2][0]]),
            "keep both overlaps all three": len(both_overlaps) == 3 and kept_rule == O.RULE_EXISTING,
            "cancel": cancelled,
            "changed on disk": changed and untouched,
            "replaced": len(second.imported) == 1 and statuses == ["current", "current", "current", "replaced"],
            "survey needs its date": no_date and len(with_date.imported) == 1,
        })
    finally:
        store.close()


def _campaign(folder: str):
    run = _run()
    store = AcquiredStore.create(os.path.join(folder, "camp_burial_data.gpkg"))
    days = S.daily_files(run)
    paths = [_write(os.path.join(folder, "plough"), n, c) for n, c in days]
    prof = _profile(days[0][1], days[0][0])
    plough = I.import_batch(store.path, I.DatasetTarget(name="Plough log"), I.review_files(None, None, paths, prof),
                            I.ImportOptions(prof, "Plough"))
    name, content = S.survey_listing(step_m=1.0)
    path = _write(os.path.join(folder, "survey"), name, content)
    prof_s = _profile(content, name)
    survey = I.import_batch(store.path, I.DatasetTarget(name="Survey PLS-01", kind="survey"),
                            I.review_files(None, None, [path], prof_s),
                            I.ImportOptions(prof_s, "Survey", acquired_utc=S.SURVEY_DATE + "T00:00:00Z"))
    name, content = S.rov_resurvey()
    path = _write(os.path.join(folder, "rov"), name, content)
    prof_r = _profile(content, name, crs="")
    resurvey = I.import_batch(store.path, I.DatasetTarget(name="ROV re-survey", kind="survey"),
                              I.review_files(None, None, [path], prof_r), I.ImportOptions(prof_r, "ROV"))
    return store, [plough.dataset_id, survey.dataset_id, resurvey.dataset_id]


def test_assessment() -> bool:
    folder = tempfile.mkdtemp(prefix="ba_assess_")
    store, ids = _campaign(folder)
    try:
        names = ["Plough log", "Survey PLS-01", "ROV re-survey"]
        specs = [A.DatasetSpec(i, n) for i, n in zip(ids, names)]
        sections = [{"section_id": "s1", "kind": "burial", "start_kp": 0.0, "end_kp": 5.0},
                    {"section_id": "s2", "kind": "burial", "start_kp": 14.0, "end_kp": 15.0}]
        request = A.Request(specs, target_runs=[(0.0, 40.0, S.TARGET_DOL_M)], scope_km=(0.0, 20.0),
                            channels=[R.WATER_DEPTH, "tension:tow"], sections=sections)
        result = A.build(store, request)
        info = A.provenance(result, 3.40)
        header, rows = A.listing_rows(result, 10.0, channels=[R.WATER_DEPTH])
        at_3400 = next(r for r in rows if r[0] <= 3.40 < r[1])
        shifted = A.build(store, A.Request([A.DatasetSpec(ids[1], "Survey PLS-01", KpMap.shift(0.5).to_dict(),
                                                          "KPs translated from S1 rev B")],
                                           target_runs=[(0.0, 40.0, 1.0)]))
        no_target = A.build(store, A.Request(specs[:1]))
        levels = set(result.status_runs[2].tolist())
        return _check("assessment: latest wins, sections, provenance, listing, translation", {
            "three series": [s.spec.name for s in result.series] == names and len(result.series[0].kp) > 10000,
            "channels": R.WATER_DEPTH in result.series[0].channels and "tension:tow" in result.series[0].channels,
            "provenance": info is not None and info["dataset"] == "ROV re-survey" and info["file"] == S.rov_resurvey()[0]
            and info["line"] > 1 and info["time"].startswith("2026-10-27") and info["status"] == B.STATUS_BELOW,
            "sections": [s["section_id"] for s in result.sections] == ["s1", "s2"]
            and result.sections[0]["ranges"] >= 1 and result.sections[1]["ranges"] >= 1,
            "listing": header[2].startswith("DOL (worst") and header[-1] == "Water depth (m)"
            and at_3400[6] == "ROV re-survey" and at_3400[4] == "Below target",
            "status levels": levels <= {B.STATUS_MEETS, B.STATUS_BELOW, B.STATUS_NO_DATA}
            and B.STATUS_BELOW in levels,
            "source runs": set(result.source_runs[2].tolist()) >= {0, 1, 2},
            "scope": abs(result.stats["scope_m"] - 20000.0) < 1e-6 and result.extent_km == (0.0, 20.0),
            "translated": abs(shifted.series[0].kp.min() - 0.5) < 1e-9
            and any("translated from S1 rev B" in n for n in shifted.notes),
            "no target said": any("no target depth" in n for n in no_target.notes)
            and set(no_target.status.tolist()) == {B.STATUS_NO_TARGET},
            "empty refused": _raises(lambda: A.build(store, A.Request([])), ValueError, "at least one"),
        })
    finally:
        store.close()


def test_kp_check_verdicts() -> bool:
    from ..burial.acquired import kp_check as K
    measured = np.linspace(0.0, 20.0, 400)
    dcc = np.full(measured.size, 1.5)
    agree = K.analyse(measured + 0.0002, measured, dcc, "S1 rev C")
    shifted = K.analyse(measured + 0.050, measured, dcc)
    grid = K.analyse(measured * 0.9996, measured, dcc)
    few = K.analyse(np.array([1.0]), np.array([1.0]), np.array([0.0]))
    return _check("KP check: agreement, a shifted start, grid-like scale", {
        "agree": agree.level == "ok" and agree.compared == 400 and "agree within 1 m" in agree.summary()
        and "on S1 rev C" in agree.summary(),
        "shift": shifted.level == "warn" and shifted.shift_m is not None and abs(shifted.shift_m - 50.0) < 0.01
        and "shifted by +50.0 m" in shifted.summary(),
        "grid": grid.level == "warn" and grid.scale_ppm is not None and abs(grid.scale_ppm + 400.0) < 1.0
        and grid.shift_m is None and "grid chainage" in grid.summary(),
        "dcc": abs(agree.dcc_median_m - 1.5) < 1e-9,
        "too few": few.compared == 0 and "Too few" in few.summary(),
    })


def run_all() -> list:
    return [
        test_map_array(),
        test_review_and_import(),
        test_assessment(),
        test_kp_check_verdicts(),
    ]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)

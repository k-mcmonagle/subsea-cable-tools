# -*- coding: utf-8 -*-
"""QGIS checks: the Import plan wizard against a real PlanModel.

Replace import with per-section tools and notes, overlay onto an existing
plan, Ctrl+Z undo of the import, and remembered value mappings. QSettings
are redirected to a temp INI so the tester's QGIS profile is untouched.
"""

from __future__ import annotations

import os
import tempfile

from ..burial import change_log, schema
from ..burial.plan_model import PlanModel
from ..burial.store import BurialStore
from .test_burial_task import _route

_PLAN = """Burial Plan Rev B,,,,
Section,KP From,KP To,Activity,Remarks
1,0.000,1.250,Plough,start of plan
2,1.250,1.600,Skip,cable crossing C-12
3,1.600,4.000,Plough,
4,4.000,4.800,Surface lay,rock outcrop
5,4.800,6.500,PLB (trencher),post-lay burial
6,6.500,7.000,Skip,
"""

_OVERLAY = """KP From,KP To,Activity
2.000,3.000,Skip
3.000,3.500,Plough
"""


def _result(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def _model(temp: str) -> PlanModel:
    store = BurialStore(os.path.join(temp, "plans.gpkg"))
    store.migrate()
    model = PlanModel(store)
    model.create_plan("Import", "plough")
    model.route, _da = _route()
    model.update_plan({"scope_start_kp": 0.0, "scope_end_kp": 7.0}, reason="scope")
    model.save_tools([{"tool_id": "t_pl", "name": "SMD Plough", "tool_type": "plough"},
                      {"tool_id": "t_tr", "name": "Q1400 Trencher", "tool_type": "trencher"}])
    return model


def _run_wizard(model, path, overlay=False):
    from ..burial.import_plan_wizard import ImportPlanWizard
    wizard = ImportPlanWizard(model, None, path=path)
    wizard.values.initializePage()
    if overlay:
        wizard.review.overlay_radio.setChecked(True)
    wizard.review.initializePage()
    if overlay:
        wizard.review.overlay_radio.setChecked(True)
    return wizard


def _burial(model):
    out = []
    for s in sorted(model.sections, key=lambda s: float(s["start_kp"])):
        if s.get("kind") == schema.SECTION_BURIAL:
            out.append((round(float(s["start_kp"]), 3), round(float(s["end_kp"]), 3),
                        s.get("tool_id") or ""))
    return out


def test_replace_import(temp: str) -> bool:
    path = os.path.join(temp, "plan.csv")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(_PLAN)
    model = _model(temp)
    wizard = _run_wizard(model, path)
    src = wizard.source
    roles_ok = src.header_index == 1 and src.roles[1:4] == ["start_kp", "end_kp", "action"]
    complete = wizard.review.isComplete()
    ok = wizard.commit()
    burial = _burial(model)
    ok = ok and roles_ok and complete
    ok = ok and burial == [(0.0, 1.25, "t_pl"), (1.6, 4.0, "t_pl"), (4.8, 6.5, "t_tr")]
    trencher = next(s for s in model.sections if s.get("tool_id") == "t_tr")
    ok = ok and trencher.get("method") == schema.METHOD_TRENCHER and trencher.get("notes") == "post-lay burial"
    skip = next(s for s in model.sections if s.get("kind") == schema.SECTION_SKIP
                and abs(float(s["start_kp"]) - 1.25) < 1e-6)
    ok = ok and skip.get("notes") == "cable crossing C-12"
    last = model.last_undoable_builder_change()
    ok = ok and last is not None and last.get("action") == change_log.ACTION_IMPORT
    # Ctrl+Z restores the empty plan in one step.
    ok = ok and model.undo_last_builder_edit() is not None and not model.events
    return _result("wizard: replace import with per-section tools/notes; Ctrl+Z undoes it",
                   bool(ok), f"roles={src.roles} burial={burial}")


def test_overlay_import(temp: str) -> bool:
    plan_path = os.path.join(temp, "plan2.csv")
    with open(plan_path, "w", encoding="utf-8") as handle:
        handle.write(_PLAN)
    overlay_path = os.path.join(temp, "overlay.csv")
    with open(overlay_path, "w", encoding="utf-8") as handle:
        handle.write(_OVERLAY)
    model = _model(os.path.join(temp, "overlay"))
    ok = _run_wizard(model, plan_path).commit()
    wizard = _run_wizard(model, overlay_path, overlay=True)
    ok = ok and wizard.review.mode() == "merge" and wizard.commit()
    burial = _burial(model)
    # 1.6-4.0 plough is cut by skip 2-3; 3-3.5 plough rejoins 3.5-4.0.
    ok = ok and [b[:2] for b in burial] == [(0.0, 1.25), (1.6, 2.0), (3.0, 4.0), (4.8, 6.5)]
    ok = ok and burial[0][2] == "t_pl" and burial[3][2] == "t_tr"
    # Remembered mapping: "Surface lay" was saved as skip on the first import.
    from ..burial.import_plan_wizard import _load_map
    ok = ok and _load_map("actions").get("surface lay") == "skip"
    return _result("wizard: overlay replaces only the covered KPs and keeps the rest", bool(ok), str(burial))


def _workbench_rpl(temp: str, protection=None):
    """A registered Workbench RPL along the test route (lon 0, lat 50 → 50.06)
    whose events carry the plan: PLDN, a crossing, PLUP / Start PLB, End PLB.
    With ``protection`` (one value per segment) the events are left blank
    and the plan is in the segments' ProtectionMethod instead."""
    from ..rpl_import.model import ImportedRpl, ImportPoint, ImportSegment
    from ..workbench.rpl_import_service import (CommitRequest, commit_import,
                                                make_wgs84_distance_area, reconcile_model,
                                                to_rpl_model)
    from ..workbench.store import WorkbenchStore
    store = WorkbenchStore(os.path.join(temp, "wb.gpkg"))
    store.migrate()
    events = ["BMH", "PL-DN", "Crossing C-12", "PLUP / Start PLB", "A/C", "End PLB", "RPL end"]
    remarks = ["", "Start of plough burial", "", "", "", "surface lay to end", ""]
    if protection is not None:
        events = ["BMH", "", "", "", "A/C", "", "RPL end"]
        remarks = [""] * len(events)
    doc = ImportedRpl(sheet="RPL")
    for i, (event, remark) in enumerate(zip(events, remarks)):
        doc.points.append(ImportPoint(seq=i, source_row=5 + i, pos_no=i + 1, event=event,
                                      remarks=remark, lat=50.0 + 0.01 * i, lon=0.0,
                                      dist_cum_km=1.112 * i))
    for i in range(len(events) - 1):
        doc.segments.append(ImportSegment(
            seq=i, source_row=6 + i,
            protection_method=protection[i] if protection is not None else ""))
    rpl_model, _ = to_rpl_model(doc, source_file="rpl.xlsx")
    reconcile_model(rpl_model, make_wgs84_distance_area())
    result = commit_import(store, rpl_model, CommitRequest(route_name="S01"))
    return store, result.rpl_id


def test_rpl_protection_import(temp: str) -> bool:
    from ..burial.import_plan_wizard import MODE_PROTECTION, SOURCE_RPL, ImportPlanWizard, _load_map
    folder = os.path.join(temp, "rpl_prot")
    os.makedirs(folder, exist_ok=True)
    model = _model(folder)
    protection = ["Surface laid", "Plough 1.0m", "Plough 1.0m", "PLB", "PLB", "Surface laid"]
    model.workbench_store, rpl_id = _workbench_rpl(folder, protection)
    wizard = ImportPlanWizard(model, None, source_kind=SOURCE_RPL)
    page = wizard.rpl
    page.initializePage()
    page.picker.reload(select=rpl_id)
    page.load()
    kp = [round(r.kp, 3) for r in page.rows]
    # No boundary events on this RPL, so the page picks the protection column itself.
    mode_ok = page.mode() == MODE_PROTECTION and page.protection_box.isVisibleTo(page)
    values_ok = [r.protection for r in page.rows] == protection + [""]
    wizard.review.initializePage()
    ok = wizard.commit()
    burial = _burial(model)
    ok = ok and mode_ok and values_ok
    ok = ok and burial == [(kp[1], kp[3], "t_pl"), (kp[3], kp[5], "t_tr")]
    notes = sorted(s.get("notes") or "" for s in model.sections
                   if s.get("kind") == schema.SECTION_BURIAL)
    ok = ok and notes == ["Protection: PLB", "Protection: Plough 1.0m"]
    ok = ok and _load_map("protectionValues").get("plough 1.0m") == "plough"
    return _result("wizard: plan from the RPL's protection method column (auto-picked)",
                   bool(ok), f"mode={page.mode()} kp={kp} burial={burial} notes={notes}")


def test_rpl_events_import(temp: str) -> bool:
    from ..burial.import_plan_wizard import SOURCE_RPL, ImportPlanWizard
    folder = os.path.join(temp, "rpl")
    os.makedirs(folder, exist_ok=True)
    model = _model(folder)
    model.update_plan({"scope_start_kp": 0.0, "scope_end_kp": 6.672}, reason="scope")
    model.workbench_store, rpl_id = _workbench_rpl(folder)
    wizard = ImportPlanWizard(model, None, source_kind=SOURCE_RPL)
    page = wizard.rpl
    page.initializePage()
    page.picker.reload(select=rpl_id)
    page.load()
    placed = [r.kp for r in page.rows]
    offsets_ok = all(r.offset_m is not None and r.offset_m < 1.0 for r in page.rows)
    tools_ok = page.tool_map()["plough"] == "t_pl" and page.tool_map()["trencher"] == "t_tr"
    crossing = page.walk.issues.get(2, [])
    wizard.review.initializePage()
    complete = wizard.review.isComplete()
    ok = wizard.commit()
    burial = _burial(model)
    kp = [round(k, 3) for k in placed]
    ok = ok and complete and offsets_ok and tools_ok and len(page.rows) == 7
    ok = ok and not page.swap.isChecked() and any("Crossing" in i.text for i in crossing)
    ok = ok and [(b[0], b[1]) for b in burial] == [(kp[1], kp[3]), (kp[3], kp[5])]
    ok = ok and [b[2] for b in burial] == ["t_pl", "t_tr"]
    ok = ok and model.undo_last_builder_edit() is not None and not model.events
    return _result("wizard: plan from RPL events placed by position, transition, tools, undo",
                   bool(ok), f"kp={kp} burial={burial} tools={page.tool_map()}")


def test_import_shares_one_depth_sampler(temp: str) -> bool:
    """Regression: every stamped event built its own DepthService, which
    clones the rasters and re-reads every contour — an 80-event import
    froze QGIS for minutes on a real bathymetry project."""
    from ..burial import plan_model as pm
    from ..burial import rpl_plan_import as rpi
    from ..burial.import_plan_wizard import SOURCE_RPL, ImportPlanWizard
    folder = os.path.join(temp, "sampler")
    os.makedirs(folder, exist_ok=True)
    model = _model(folder)
    built = []

    class _CountingService:
        def __init__(self, *_args, **_kwargs):
            built.append(1)

        def is_available(self):
            return True

        def sample(self, _lat, _lon):
            return -42.0

    rows = []
    for i in range(141):
        event = ("PLDN" if i % 7 == 1 else "PLUP" if i % 7 == 4 else "")
        rows.append(rpi.RplRow(seq=i, pos_no=i + 1, event=event, stated_kp=0.05 * i,
                               kp=0.05 * i, offset_m=0.0))
    original = pm.DepthService
    pm.DepthService = _CountingService
    try:
        model._stamp_service_cache = None
        wizard = ImportPlanWizard(model, None, source_kind=SOURCE_RPL)
        wizard.rpl.initializePage()
        wizard.rpl.set_rows(rows, "test")
        wizard.review.initializePage()
        built.clear()
        ok = wizard.commit()
    finally:
        pm.DepthService = original
    events = model.events
    ok = ok and len(events) == 40 and len(built) == 1
    ok = ok and all(e.get("depth_m") == -42.0 for e in events)
    return _result("import: one depth sampler for the whole batch (was one per event)",
                   bool(ok), f"events={len(events)} samplers built={len(built)}")


def run_all():
    from qgis.PyQt.QtCore import QSettings
    results = []
    old_format = QSettings.defaultFormat()
    # The GeoPackages stay open until the models are collected (Windows).
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, temp)
        os.makedirs(os.path.join(temp, "overlay"), exist_ok=True)
        try:
            for test in (test_replace_import, test_overlay_import, test_rpl_events_import,
                         test_import_shares_one_depth_sampler,
                         test_rpl_protection_import):
                try:
                    results.append(test(temp))
                except Exception as exc:  # report, keep going
                    import traceback
                    traceback.print_exc()
                    results.append(_result(test.__name__, False, repr(exc)))
        finally:
            QSettings.setDefaultFormat(old_format)
            import gc
            gc.collect()
    print(f"{sum(results)}/{len(results)} passed")
    return results


if __name__ == "__main__":  # pragma: no cover
    run_all()

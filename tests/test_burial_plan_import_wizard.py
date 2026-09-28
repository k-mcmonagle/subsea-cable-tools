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
            for test in (test_replace_import, test_overlay_import):
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

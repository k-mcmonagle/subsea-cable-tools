# -*- coding: utf-8 -*-
"""QGIS checks: multi-tool burial plans on a real PlanModel.

Plough → PLB → Plough via "Set tool for KP range" (tool transitions, no
skips), per-section labels / refs / exports, moving a transition as one
boundary, merging back (first tool kept) and Ctrl+Z.
"""

from __future__ import annotations

import os
import tempfile

from ..burial import io_csv, schema
from .test_burial_plan_import_wizard import _model


def _result(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def _burial(model):
    return [(round(float(s["start_kp"]), 3), round(float(s["end_kp"]), 3), s.get("tool_id") or "")
            for s in sorted(model.sections, key=lambda s: float(s["start_kp"]))
            if s.get("kind") == schema.SECTION_BURIAL]


def _labels(model):
    labels = model.event_labels()
    return [(round(float(e["kp"]), 3), labels[e["event_id"]])
            for e in model.events]


def _seed(model):
    """One plough section KP 1-6 (default tool = the plough)."""
    model.update_gen_params({"tool_id": "t_pl", "tool_config_id": ""}, stale=False)
    model.add_event(1.0, schema.EVENT_BURIAL_START)
    model.add_event(6.0, schema.EVENT_BURIAL_END)
    section = next(s for s in model.sections if s.get("kind") == schema.SECTION_BURIAL)
    model.update_section(section["section_id"], {"tool_id": "t_pl"})


def test_plough_plb_plough(temp: str) -> bool:
    model = _model(os.path.join(temp, "a"))
    _seed(model)
    section = next(s for s in model.sections if s.get("kind") == schema.SECTION_BURIAL)
    ok = model.set_tool_for_range(section["section_id"], 2.0, 3.0, "t_tr")
    burial = _burial(model)
    ok = ok and burial == [(1.0, 2.0, "t_pl"), (2.0, 3.0, "t_tr"), (3.0, 6.0, "t_pl")]
    ok = ok and not any(s.get("kind") == schema.SECTION_SKIP
                        and 1.0 < float(s["start_kp"]) < 6.0 for s in model.sections)
    labels = _labels(model)
    ok = ok and labels == [(1.0, "PLDN"), (2.0, "PLUP"), (2.0, "Start PLB"),
                           (3.0, "End PLB"), (3.0, "PLDN"), (6.0, "PLUP")]
    refs = schema.section_refs(model.sections, model.direction, model.label_method)
    codes = sorted(refs[s["section_id"]] for s in model.sections if s.get("kind") == schema.SECTION_BURIAL)
    ok = ok and codes == ["PB-01", "PS-01", "PS-02"]
    csv_text = model.export_events_csv()
    ok = ok and "Start PLB" in csv_text and "PLUP" in csv_text
    # Round trip: the events CSV re-imports (labels parse back to START/END).
    fmt, parsed = io_csv.detect_and_parse(csv_text)
    ok = ok and fmt == "events_csv" and len(parsed) == 6
    ok = ok and "Plough" in model_tools_text(model) and "Trencher" in model_tools_text(model)
    return _result("Plough → PLB → Plough via tool transitions: sections, labels, refs, CSV",
                   bool(ok), f"{burial} {labels} {codes}")


def model_tools_text(model):
    methods = {model.section_method(s) for s in model.sections
               if s.get("kind") == schema.SECTION_BURIAL}
    return ", ".join(schema.METHOD_LABELS.get(m, m) for m in sorted(methods))


def test_move_merge_undo(temp: str) -> bool:
    model = _model(os.path.join(temp, "b"))
    _seed(model)
    section = next(s for s in model.sections if s.get("kind") == schema.SECTION_BURIAL)
    model.set_tool_for_range(section["section_id"], 2.0, 3.0, "t_tr")
    start_plb = next(e for e in model.events if abs(float(e["kp"]) - 2.0) < 1e-9
                     and e["event_type"] == schema.EVENT_BURIAL_START)
    ok = model.move_event(start_plb["event_id"], 2.5, "")
    burial = _burial(model)
    ok = ok and burial == [(1.0, 2.5, "t_pl"), (2.5, 3.0, "t_tr"), (3.0, 6.0, "t_pl")]
    try:
        model.move_event(start_plb["event_id"], 3.5, "")
        crossed = True
    except ValueError:
        crossed = False
    ok = ok and not crossed
    first_two = [s["section_id"] for s in sorted(model.sections, key=lambda s: float(s["start_kp"]))
                 if s.get("kind") == schema.SECTION_BURIAL][:2]
    ok = ok and model.merge_sections(first_two, "")
    merged = _burial(model)
    ok = ok and merged == [(1.0, 3.0, "t_pl"), (3.0, 6.0, "t_pl")]
    ok = ok and model.undo_last_builder_edit() is not None
    ok = ok and _burial(model) == burial
    return _result("transition moves as one boundary, cannot cross another; merge keeps the "
                   "first tool; Ctrl+Z restores", bool(ok), f"{burial} → {merged}")


def test_default_tool_labels(temp: str) -> bool:
    model = _model(os.path.join(temp, "c"))
    model.save_tools([{"tool_id": "t_insp", "name": "ROV inspection", "tool_type": "inspection"}])
    model.update_gen_params({"tool_id": "t_tr", "tool_config_id": ""}, stale=False)
    model.add_event(1.0, schema.EVENT_BURIAL_START)
    model.add_event(2.0, schema.EVENT_BURIAL_END)
    labels = [label for _kp, label in _labels(model)]
    ok = model.label_method == schema.METHOD_TRENCHER and labels == ["Start PLB", "End PLB"]
    section = next(s for s in model.sections if s.get("kind") == schema.SECTION_BURIAL)
    model.update_section(section["section_id"], {"tool_id": "t_insp"})
    labels = [label for _kp, label in _labels(model)]
    ok = ok and labels == ["Start Inspection", "End Inspection"]
    return _result("sections without a tool follow the default tool's labels; Inspection tool",
                   bool(ok), str(labels))


def run_all():
    from qgis.PyQt.QtCore import QSettings
    results = []
    old_format = QSettings.defaultFormat()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, temp)
        for sub in ("a", "b", "c"):
            os.makedirs(os.path.join(temp, sub), exist_ok=True)
        try:
            for test in (test_plough_plb_plough, test_move_merge_undo, test_default_tool_labels):
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

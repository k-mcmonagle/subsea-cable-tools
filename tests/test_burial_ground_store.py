# -*- coding: utf-8 -*-
"""QGIS-side checks for the Ground Model: store round trip, model
mutators with change-log rollback, plan duplication, and construction of
the plot widget, tab and dialogs under a headless QgsApplication (the
Qt5/Qt6 surface the pure tests cannot reach).
"""

from __future__ import annotations

import os
import tempfile
import time

from qgis.core import QgsProject

from ..burial import change_log, ground_model, schema
from ..burial.plan_model import PlanModel
from ..burial.store import BurialStore

_COUNTER = [0]


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" - {detail}"
    print(msg)
    return ok


def _store() -> BurialStore:
    _COUNTER[0] += 1
    name = f"bp_ground_{os.getpid()}_{int(time.time() * 1000)}_{_COUNTER[0]}.gpkg"
    store = BurialStore(os.path.join(tempfile.gettempdir(), name),
                        QgsProject.instance().transformContext())
    store.migrate()
    return store


def _plan_row(name="Plan G"):
    return {"plan_id": schema.new_id(), "name": name, "description": "",
            "notes": "", "method": "plough", "rpl_id": "",
            "rpl_name": "Route", "rpl_revision": "Rev D",
            "rpl_gpkg_path": "", "rpl_fingerprint": "",
            "scope_start_kp": 0.0, "scope_end_kp": 10.0, "direction": 1,
            "target_burial_m": 1.5, "params_json": "{}"}


def _units(plan_id):
    return [
        {"unit_id": schema.new_id(), "plan_id": plan_id, "start_kp": 0.0,
         "end_kp": 4.0, "top_m": 0.0, "base_m": 1.0, "soil_class": "SAND",
         "description": "Dense sand", "src_start_kp": 0.0, "src_end_kp": 3.9,
         "src_rpl": "Rev B"},
        {"unit_id": schema.new_id(), "plan_id": plan_id, "start_kp": 0.0,
         "end_kp": 4.0, "top_m": 1.0, "base_m": None, "soil_class": "CLAY"},
        {"unit_id": schema.new_id(), "plan_id": plan_id, "start_kp": 4.0,
         "end_kp": 10.0, "top_m": 0.0, "base_m": 2.0, "top_end_m": 0.0,
         "base_end_m": 3.0, "soil_class": "ROCK"},
    ]


def test_store_round_trip() -> bool:
    store = _store()
    ok = all(store._table_exists(t) for t in
             (schema.TABLE_GROUND_UNIT, schema.TABLE_GROUND_CLASS))
    plan_id = store.save_plan(_plan_row())
    store.save_ground_units(plan_id, _units(plan_id))
    rows = store.list_ground_units(plan_id)
    ok = ok and len(rows) == 3
    open_unit = [r for r in rows if r.get("soil_class") == "CLAY"][0]
    ok = ok and open_unit.get("base_m") is None  # NULL survives the gpkg
    sloped = [r for r in rows if r.get("soil_class") == "ROCK"][0]
    ok = ok and abs(float(sloped.get("base_end_m")) - 3.0) < 1e-9
    ok = ok and abs(float(rows[0].get("src_end_kp") or 0) - 3.9) < 1e-9
    store.save_ground_classes([
        {"code": "SAND", "label": "Sand", "group": "sand", "color": "#f2d16b", "notes": ""},
        {"code": "CLAY", "label": "Clay", "group": "clay", "color": "#8fb4d9", "notes": ""},
    ])
    classes = store.list_ground_classes()
    ok = ok and [c["code"] for c in classes] == ["SAND", "CLAY"]
    store.save_ground_classes([{"code": "ROCK", "label": "Rock", "group": "rock",
                                "color": "#b06f8a", "notes": ""}])
    ok = ok and [c["code"] for c in store.list_ground_classes()] == ["ROCK"]
    # Per-plan replacement leaves other plans alone.
    other = store.save_plan(_plan_row("Other"))
    store.save_ground_units(other, _units(other)[:1])
    store.save_ground_units(plan_id, [])
    ok = ok and store.list_ground_units(plan_id) == [] \
        and len(store.list_ground_units(other)) == 1
    return _result("ground store round trip (units NULLs, classes, per-plan)", ok)


def test_model_save_rollback_duplicate() -> bool:
    store = _store()
    plan_id = store.save_plan(_plan_row())
    model = PlanModel(store)
    model.load_plan(plan_id)
    ok = model.ground_units == [] and model.ground_classes == []
    new_classes = ground_model.missing_classes(_units(plan_id), [])
    ok = ok and model.save_ground_units(
        _units(plan_id), action=change_log.ACTION_IMPORT_GROUND,
        reason="test import", new_classes=new_classes,
        params_updates={"ground_model": {"source_ref": "GM Rev B",
                                         "source_rpl": "Route Rev B"}})
    ok = ok and len(model.ground_units) == 3
    ok = ok and {c["code"] for c in model.ground_classes} == {"SAND", "CLAY", "ROCK"}
    ok = ok and model.ground_meta().get("source_ref") == "GM Rev B"
    ok = ok and model.plan.get("status") != schema.PLAN_STATUS_STALE
    entries = store.list_change_log(plan_id)
    import_entry = [e for e in entries if e.get("action") == change_log.ACTION_IMPORT_GROUND]
    ok = ok and len(import_entry) == 1
    # Edit: drop one unit, then roll the edit back.
    trimmed = [dict(u) for u in model.ground_units if u.get("soil_class") != "ROCK"]
    ok = ok and model.save_ground_units(trimmed, reason="drop rock")
    ok = ok and len(model.ground_units) == 2
    edit_entry = [e for e in store.list_change_log(plan_id)
                  if e.get("action") == change_log.ACTION_EDIT_GROUND][-1]
    ok = ok and model.rollback_to(edit_entry["change_id"])
    ok = ok and len(model.ground_units) == 3
    # Duplicate carries the ground model with fresh ids.
    copy_id = store.duplicate_plan(plan_id, "Copy")
    copied = store.list_ground_units(copy_id)
    ok = ok and len(copied) == 3
    ok = ok and not ({u["unit_id"] for u in copied}
                     & {u["unit_id"] for u in model.ground_units})
    return _result("model save + change log + rollback + duplicate", ok)


def test_widgets_construct() -> bool:
    from qgis.PyQt.QtWidgets import QApplication

    if QApplication.instance() is None:
        return _result("ground widgets construct", True, "skipped: no QApplication")
    from ..burial.ground_plot import GroundModelPlot
    from ..burial.tabs.ground_tab import GroundTab
    from ..burial.ground_dialogs import (ClassesDialog, GroundImportDialog,
                                         KpReferenceWidget, RereferenceDialog)

    store = _store()
    plan_id = store.save_plan(_plan_row())
    model = PlanModel(store)
    model.load_plan(plan_id)
    model.save_ground_units(_units(plan_id))

    class _Dock:
        iface = None
        canvas = None
        kps = []

        def workbench_store(self, *_a):
            return None

        def highlight_kp(self, kp):
            self.kps.append(kp)

        def goto_kp(self, kp):
            self.kps.append(kp)

        def goto_range(self, a, b):
            self.kps.append((a, b))

    dock = _Dock()
    plot = GroundModelPlot()
    plot.set_units(model.ground_units, model.ground_classes)
    plot.set_target_depth(1.5)
    plot.set_scope(0.0, 10.0)
    ok = plot.unit_at(2.0, 0.5)["soil_class"] == "SAND"
    ok = ok and plot.unit_at(6.0, 2.2)["soil_class"] == "ROCK"
    plot.show_kp(2.0, 0.5)
    ok = ok and "SAND" in plot._readout.textItem.toPlainText()

    tab = GroundTab(model, dock)
    ok = ok and tab.table.rowCount() == 3
    ok = ok and "3 unit(s)" in tab.status_label.text()
    # Table edit path: change an end KP, apply, check persisted.
    tab.table.item(0, 1).setText("3.5")
    ok = ok and tab._dirty and abs(tab._working[0]["end_kp"] - 3.5) < 1e-9
    tab._apply()
    ok = ok and not tab._dirty
    ok = ok and abs(float(store.list_ground_units(plan_id)[0]["end_kp"]) - 3.5) < 1e-9
    # Split at a KP creates a fourth unit.
    tab.table.selectRow(2)
    tab._split_at([2], 7.0)
    ok = ok and len(tab._working) == 4
    right = [u for u in tab._working if u["start_kp"] == 7.0][0]
    ok = ok and abs(right["top_m"] - 0.0) < 1e-9 and abs(right["base_m"] - 2.5) < 1e-9
    tab._revert()
    ok = ok and len(tab._working) == 3
    # Plot click → table selection + dock sync.
    tab._plot_unit_clicked(tab._working[1]["unit_id"])
    ok = ok and tab._selected_rows() == [1] and dock.kps
    # Dialogs build without a Workbench registry (geometry option disabled).
    ref = KpReferenceWidget(model, dock)
    ok = ok and not ref.radio_geometry.isEnabled()
    ref.radio_shift.setChecked(True)
    ref.shift_spin.setValue(0.25)
    shifted = ref.build_map()
    ok = ok and abs(shifted.map_kp(1.0)[0] - 1.25) < 1e-9
    import_dialog = GroundImportDialog(model, dock)
    ok = ok and not import_dialog.ok_button.isEnabled()
    rer = RereferenceDialog(model, dock, model.ground_units)
    rer.kp_ref.radio_anchors.setChecked(True)
    rer.kp_ref.anchor_edit.setPlainText("0,0\n10,10.2")
    rer._preview()
    ok = ok and rer.ok_button.isEnabled() and len(rer.units) == 3
    ok = ok and abs(rer.units[2]["end_kp"] - 10.2) < 1e-9
    classes_dialog = ClassesDialog(model)
    ok = ok and classes_dialog.table.rowCount() == 3
    for widget in (plot, tab, import_dialog, rer, classes_dialog, ref):
        widget.deleteLater()
    return _result("ground widgets construct + edit/split/dialog paths", ok)


def test_geometry_rereference_real_routes() -> bool:
    """Two WGS84 routes: Rev B straight east along 50 N for 30 km; Rev D
    identical except KP 10-14 detours 500 m north (a re-route ~0.25 km
    longer). Units quoted on Rev B must land on Rev D shifted after the
    detour, with the detour recorded as a gap."""
    from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsVectorLayer

    from ..burial.analysis_task import build_route_frame
    from ..burial import kp_rereference as kr
    from ..burial import rereference_qgis

    project = QgsProject.instance()
    # ~30 km east at lat 50: 1 deg lon ~ 71.7 km -> 0.4185 deg
    lon_per_km = 1.0 / 71.70
    straight = [QgsPointXY(0.0, 50.0), QgsPointXY(30.0 * lon_per_km, 50.0)]
    detour = [QgsPointXY(0.0, 50.0), QgsPointXY(10.0 * lon_per_km, 50.0),
              QgsPointXY(10.5 * lon_per_km, 50.0 + 0.5 / 111.2),
              QgsPointXY(13.5 * lon_per_km, 50.0 + 0.5 / 111.2),
              QgsPointXY(14.0 * lon_per_km, 50.0),
              QgsPointXY(30.0 * lon_per_km, 50.0)]

    def layer(points, name):
        vl = QgsVectorLayer("LineString?crs=EPSG:4326&field=SeqNo:integer", name, "memory")
        feat = QgsFeature(vl.fields())
        feat.setAttribute("SeqNo", 1)
        feat.setGeometry(QgsGeometry.fromPolylineXY(points))
        vl.dataProvider().addFeatures([feat])
        return vl

    src_route, _d = build_route_frame(layer(straight, "Rev B"), project)
    dst_route, _d = build_route_frame(layer(detour, "Rev D"), project)
    extra_km = dst_route.total_length_km - src_route.total_length_km
    ok = 0.3 < extra_km < 0.5
    kp_map = rereference_qgis.geometry_map(src_route, dst_route, step_km=0.05,
                                           offset_tol_m=25.0,
                                           source_label="Rev B", target_label="Rev D")
    d = kp_map.diagnostics
    ok = ok and d.method == kr.METHOD_GEOMETRY and len(kp_map.anchors) >= 2
    ok = ok and len(d.gap_ranges) == 1
    ok = ok and 9.5 <= d.gap_ranges[0][0] <= 10.1 and 13.9 <= d.gap_ranges[0][1] <= 14.5
    before, f_before = kp_map.map_kp(5.0)
    after, f_after = kp_map.map_kp(20.0)
    ok = ok and abs(before - 5.0) < 0.01 and not f_before
    ok = ok and abs((after - 20.0) - extra_km) < 0.02 and not f_after
    inside, f_inside = kp_map.map_kp(12.0)
    ok = ok and kr.FLAG_GAP in f_inside and 12.0 <= inside <= 12.0 + extra_km + 0.01
    units = [{"start_kp": 2.0, "end_kp": 8.0, "top_m": 0.0, "base_m": 1.0,
              "soil_class": "SAND"},
             {"start_kp": 16.0, "end_kp": 25.0, "top_m": 0.0, "base_m": 1.0,
              "soil_class": "CLAY"}]
    mapped, tally = ground_model.rereference_units(units, kp_map, source_label="Rev B")
    ok = ok and abs(mapped[0]["start_kp"] - 2.0) < 0.01
    ok = ok and abs(mapped[1]["start_kp"] - (16.0 + extra_km)) < 0.02
    ok = ok and mapped[1]["src_start_kp"] == 16.0 and mapped[1]["src_rpl"] == "Rev B"
    ok = ok and not tally
    return _result("geometry re-reference on real WGS84 routes", ok,
                   f"extra {extra_km:.3f} km; {d.summary()}")


def _memory_route():
    from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsVectorLayer

    from ..burial.analysis_task import build_route_frame

    lon_per_km = 1.0 / 71.70
    vl = QgsVectorLayer("LineString?crs=EPSG:4326&field=SeqNo:integer", "r", "memory")
    feat = QgsFeature(vl.fields())
    feat.setAttribute("SeqNo", 1)
    feat.setGeometry(QgsGeometry.fromPolylineXY(
        [QgsPointXY(0.0, 50.0), QgsPointXY(12.0 * lon_per_km, 50.0)]))
    vl.dataProvider().addFeatures([feat])
    route, _d = build_route_frame(vl, QgsProject.instance())
    return route


def test_overlay_layers() -> bool:
    """Ground-model and BAS overlays: written as route slices beside the
    route, ensured into the plan group with data-defined colours and the
    register's own fields, toggled from the tabs, rebuilt on plan
    selection, removed with the plan."""
    from ..burial import bas_model, map_layers
    from ..burial.tabs.bas_tab import BasTab
    from ..burial.tabs.ground_tab import GroundTab

    project = QgsProject.instance()
    store = _store()
    plan_id = store.save_plan(_plan_row("Overlay"))
    model = PlanModel(store)
    model.load_plan(plan_id)
    model.route = _memory_route()
    plan = model.plan
    cols = [{"key": "req_dol_m", "label": "Req DoL (m)", "kind": "number"},
            {"key": "soil", "label": "Soil", "kind": "text"}]
    bas_rows = [bas_model.decode_row({"start_kp": 0.0, "end_kp": 5.0,
                                      "values": {"req_dol_m": "1.5", "soil": "Sand"}}),
                bas_model.decode_row({"start_kp": 5.0, "end_kp": 40.0,   # past the route end
                                      "values": {"req_dol_m": "2", "soil": "Clay"}}),
                bas_model.decode_row({"start_kp": 50.0, "end_kp": 60.0})]  # off route
    ok = model.save_bas(bas_rows, columns=cols)
    ok = ok and model.save_ground_units(_units(plan_id),
                                        new_classes=ground_model.missing_classes(
                                            _units(plan_id), []))
    model.refresh_layers(immediate=True)
    base = (plan["name"], plan.get("rev_label") or "", plan_id)
    ground = map_layers.find_layer(project, store.gpkg_path,
                                   schema.ground_layer_name(*base))
    bas = map_layers.find_layer(project, store.gpkg_path,
                                schema.bas_layer_name(*base))
    ok = ok and ground is not None and bas is not None
    gfeats = list(ground.getFeatures())
    horizons = sorted({f["horizon"] for f in gfeats})
    ok = ok and horizons == ["seabed", "target"]
    seabed = sorted((f["start_kp"], f["end_kp"], f["soil_class"]) for f in gfeats
                    if f["horizon"] == "seabed")
    # scope 0-10: SAND 0-4 then ROCK 4-10 at the seabed
    ok = ok and [s[2] for s in seabed] == ["SAND", "ROCK"] and abs(seabed[1][1] - 10.0) < 1e-6
    target = [f for f in gfeats if f["horizon"] == "target"]
    # 1.5 m: CLAY 0-4 (open base), ROCK 4-10 (base 2->3 m)
    ok = ok and sorted(f["soil_class"] for f in target) == ["CLAY", "ROCK"]
    ok = ok and all(str(f["color"]).startswith("#") for f in gfeats)
    ok = ok and ground.renderer() is not None and len(ground.renderer().rootRule().children()) == 2
    bfeats = list(bas.getFeatures())
    ok = ok and len(bfeats) == 2   # the off-route row is skipped, the long one clipped
    names = set(bas.fields().names())
    ok = ok and {"req_dol_m", "soil", "src_rpl"} <= names
    by_start = {round(f["start_kp"], 3): f for f in bfeats}
    ok = ok and abs(float(by_start[0.0]["req_dol_m"]) - 1.5) < 1e-9 \
        and by_start[5.0]["soil"] == "Clay"
    ok = ok and abs(float(by_start[5.0]["end_kp"]) - 40.0) < 1e-6   # attribute keeps the row's KP
    ok = ok and not bas.geometryOptions() is None
    # Adding a column reshapes the layer (field map healed in place).
    bas_id = bas.id()
    ok = ok and model.save_bas_columns(cols + [{"key": "risk", "label": "Risk", "kind": "text"}])
    model._flush_layer_refresh()
    bas2 = map_layers.find_layer(project, store.gpkg_path, schema.bas_layer_name(*base))
    ok = ok and bas2 is not None and bas2.id() == bas_id and "risk" in set(bas2.fields().names())
    # Visibility toggles from the tabs.
    node = project.layerTreeRoot().findLayer(ground.id())
    ok = ok and node is not None
    map_layers.set_plan_layer_visibility(project, store.gpkg_path, plan,
                                         schema.ground_layer_name, False)
    ok = ok and not node.itemVisibilityChecked()
    map_layers.set_active_plan_layers(project, plan)
    ok = ok and node.itemVisibilityChecked()

    class _Dock:
        iface = None
        canvas = None

        def workbench_store(self, *_a):
            return None

        def highlight_kp(self, kp):
            pass

        def goto_kp(self, kp):
            pass

        def goto_range(self, a, b):
            pass

        def highlight_range(self, a, b):
            pass

        def highlight_ranges(self, r):
            pass

    dock = _Dock()
    gtab = GroundTab(model, dock)
    gtab.show_map.setChecked(False)
    ok = ok and not node.itemVisibilityChecked()
    gtab.show_map.setChecked(True)
    ok = ok and node.itemVisibilityChecked()
    btab = BasTab(model, dock)
    bnode = project.layerTreeRoot().findLayer(bas2.id())
    btab.show_map.setChecked(False)
    ok = ok and bnode is not None and not bnode.itemVisibilityChecked()
    btab.show_map.setChecked(True)
    # plan_layer_exists + removal
    ok = ok and map_layers.plan_layer_exists(project, store.gpkg_path, plan,
                                             schema.ground_layer_name)
    map_layers.remove_plan_layers(project, store.gpkg_path, plan)
    ok = ok and not map_layers.plan_layer_exists(project, store.gpkg_path, plan,
                                                 schema.ground_layer_name)
    ok = ok and not map_layers.plan_layer_exists(project, store.gpkg_path, plan,
                                                 schema.bas_layer_name)
    for widget in (gtab, btab):
        widget.deleteLater()
    return _result("ground + BAS map overlays (write/ensure/style/toggle/remove)", ok,
                   f"seabed={seabed}")


def run_all():
    return [
        test_overlay_layers(),
        test_store_round_trip(),
        test_model_save_rollback_duplicate(),
        test_widgets_construct(),
        test_geometry_rereference_real_routes(),
    ]


if __name__ == "__main__":
    import sys
    sys.exit(0 if all(run_all()) else 1)

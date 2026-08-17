# -*- coding: utf-8 -*-
"""Checks for the workbench system topology and the V3 adapter.

Builds a synthetic registered RPL inside a temp workbench GeoPackage, then
exercises: connected-system derivation + cached system ids, the BMH -> RPL ->
BU example, and the WorkbenchV3Adapter contract (route list, bathymetry
profile fallback to RPL depths, lay azimuth, assembly window slicing through
a stored fit, and KP-referenced result push).

Requires the QGIS API (run via tests/run_qgis_smoke_tests.py).
"""

from __future__ import annotations

import os
import tempfile

from qgis.PyQt.QtCore import QSettings
from qgis.core import QgsCoordinateReferenceSystem, QgsProject

from ..kp_range_utils import make_distance_area
from ..qgis_compat import WKB_LINESTRING, WKB_POINT
from ..workbench import assembly_model as am
from ..workbench import rpl_engine as eng
from ..workbench import schema
from ..workbench.assembly_manager_dock import ExtractReviewDialog
from ..workbench.configurable_table import ConfigurableTable
from ..workbench.rpl_engine import RplModel, RplPoint, RplSegment, SlackMode
from ..workbench.rpl_layer_io import model_rows_for_layers
from ..workbench.rpl_manager_dock import RplManagerPanel
from ..workbench.overview_panels import SegmentOverviewPanel, SystemOverviewPanel
from ..workbench.sld_widget import SldWidget
from ..workbench.store import WorkbenchStore
from ..workbench.system_topology import TopologyGraph, assign_system_ids
from ..workbench.v3_adapter import WorkbenchV3Adapter


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


def _graphics_text(item) -> str:
    if hasattr(item, "toPlainText"):
        return item.toPlainText()
    if hasattr(item, "text"):
        return item.text()
    return ""


def _da():
    return make_distance_area(
        QgsCoordinateReferenceSystem("EPSG:4326"),
        QgsProject.instance().transformContext(),
    )


def _temp_store() -> WorkbenchStore:
    folder = tempfile.mkdtemp(prefix="wb_adapter_test_")
    store = WorkbenchStore(os.path.join(folder, "workbench.gpkg"))
    store.ensure_created()
    return store


def _register_synthetic_rpl(store: WorkbenchStore, name: str = "Seg 1",
                            n_points: int = 11, slack_pct: float = 1.0) -> str:
    points = [
        RplPoint(seq=i, pos_no=i + 1,
                 event=("Start terminal" if i == 0 else
                        "End terminal" if i == n_points - 1 else ""),
                 lat=50.0 + 0.01 * i, lon=0.0,
                 depth_m=-100.0 - 10.0 * i)
        for i in range(n_points)
    ]
    segments = [RplSegment(seq=i, slack_pct=slack_pct, attrs={"CableType": "LW"})
                for i in range(n_points - 1)]
    model = RplModel(points=points, segments=segments)
    eng.recompute(model, _da(), slack_mode=SlackMode.HOLD_SLACK)

    rpl_id = schema.new_id()
    route_id = store.create_route(name)
    rows = model_rows_for_layers(model, rpl_id, "synthetic")
    points_layer = schema.rpl_points_layer_name(name)
    lines_layer = schema.rpl_lines_layer_name(name)
    store.write_spatial_layer(
        points_layer, schema.RPL_POINT_FIELDS, WKB_POINT, rows["points"])
    store.write_spatial_layer(
        lines_layer, schema.RPL_LINE_FIELDS, WKB_LINESTRING, rows["lines"])
    store.save_rpl({
        "rpl_id": rpl_id, "name": name, "kind": "planned",
        "points_layer": points_layer, "lines_layer": lines_layer,
        "slack_mode": "hold_slack", "depth_source_config": "",
        "route_id": route_id, "rev_label": "Rev 1",
    })
    return rpl_id


def test_systems_bmh_bu_example() -> bool:
    store = _temp_store()
    rpl1 = _register_synthetic_rpl(store, "Trunk")
    rpl2 = _register_synthetic_rpl(store, "Branch A")
    rpl3 = _register_synthetic_rpl(store, "Branch B")

    bmh = store.save_component({"kind": "node", "name": "BMH", "node_type": "bmh"}, ["A"])
    bu = store.save_component({"kind": "node", "name": "BU-1", "node_type": "bu"},
                              ["trunk_in", "branch_1", "branch_2"])
    graph = TopologyGraph.from_store(store)

    def port(subject_or_cid, label):
        cid = subject_or_cid
        rpl = store.get_rpl(subject_or_cid)
        component = (store.component_for_segment(rpl.get("route_id"))
                     if rpl else store.component_for_subject(subject_or_cid))
        if component is not None:
            cid = component["component_id"]
        return next(p["port_id"] for p in store.list_ports()
                    if p["component_id"] == cid and p["label"] == label)

    store.connect_ports(port(bmh, "A"), port(rpl1, "A"))
    store.connect_ports(port(rpl1, "B"), port(bu, "trunk_in"))
    store.connect_ports(port(bu, "branch_1"), port(rpl2, "A"))
    store.connect_ports(port(bu, "branch_2"), port(rpl3, "A"))

    graph = TopologyGraph.from_store(store)
    systems = graph.connected_systems()
    ok = len(systems) == 1 and len(systems[0]) == 5
    ok = ok and graph.validate() == []
    # two open ports remain: rpl2 B and rpl3 B
    ok = ok and len(graph.open_ports()) == 2

    assignment = assign_system_ids(store)
    ok = ok and len(set(assignment.values())) == 1
    components = store.list_components()
    ok = ok and all(c.get("system_id") for c in components)
    return _result("BMH -> trunk -> BU -> two branches system", ok,
                   f"systems={len(systems)}, open={len(graph.open_ports())}")


def test_manual_and_unassigned_system_membership() -> bool:
    store = _temp_store()
    system_id = store.create_system("North system")
    assigned_route = store.create_route("North trunk", system_id=system_id)
    unassigned_route = store.create_route("Future branch")
    node_id = store.save_component({
        "kind": "node", "name": "BU-1", "node_type": "bu",
        "system_id": system_id,
    }, ["Trunk", "Branch 1", "Branch 2"])

    assignment = assign_system_ids(store)
    assigned_component = store.component_for_segment(assigned_route) or {}
    unassigned_component = store.component_for_segment(unassigned_route) or {}
    ok = assignment.get(assigned_component.get("component_id")) == system_id
    ok = ok and assignment.get(node_id) == system_id
    ok = ok and assignment.get(unassigned_component.get("component_id")) == ""
    ok = ok and (store.get_route(assigned_route) or {}).get("system_id") == system_id
    ok = ok and not (store.get_route(unassigned_route) or {}).get("system_id")
    return _result("manual systems coexist with genuinely unassigned segments", ok)


def test_guided_overviews_construct() -> bool:
    store = _temp_store()
    rpl_id = _register_synthetic_rpl(store, "Guided segment")
    route_id = (store.get_rpl(rpl_id) or {}).get("route_id")
    system_id = store.create_system("Guided system")
    store.assign_route_to_system(route_id, system_id)
    assembly = am.Assembly(name="Guided load", items=[
        am.AssemblyItem(
            kind="section", name="Guided LW", cable_type="LW",
            length_m=12000.0, color_hex="#4477aa"),
    ])
    header, assembly_items = am.assembly_to_rows(assembly)
    store.save_assembly(header, assembly_items)
    store.add_makeup_assembly(route_id, assembly.assembly_id)

    system_panel = SystemOverviewPanel()
    segment_panel = SegmentOverviewPanel()
    system_panel.load_system(store, system_id)
    segment_panel.load_segment(store, route_id)
    ok = system_panel.title.text() == "Guided system"
    ok = ok and "cable segment" in system_panel.summary.text()
    ok = ok and [system_panel.views.tabText(i)
                 for i in range(system_panel.views.count())] == ["Table", "Schematic"]
    ok = ok and system_panel.table.rowCount() == 1
    ok = ok and system_panel.table.item(0, 0).text() == "Guided segment"
    ok = ok and system_panel.table.item(0, 2).text() == "1"
    ok = ok and system_panel.table.item(0, 6).text() == "LW"
    ok = ok and system_panel._schematic_args is not None
    system_panel.views.setCurrentIndex(1)
    ok = ok and system_panel._schematic_args is None
    ok = ok and not system_panel.schematic.scene().itemsBoundingRect().isEmpty()
    schematic_text = "\n".join(_graphics_text(item)
                                for item in system_panel.schematic.scene().items())
    ok = ok and "Start (A)" in schematic_text
    ok = ok and "End (B)" in schematic_text
    ok = ok and "LW" in schematic_text
    schematic_tooltips = "\n".join(
        item.toolTip() for item in system_panel.schematic.scene().items()
        if hasattr(item, "toolTip"))
    ok = ok and "Guided segment" in schematic_tooltips
    ok = ok and system_panel.schematic._home.toolTip().startswith("Home")
    ok = ok and segment_panel.schematic._home.toolTip().startswith("Home")
    ok = ok and segment_panel.title.text() == "Guided segment"
    ok = ok and [segment_panel.views.tabText(i)
                 for i in range(segment_panel.views.count())] == ["Table", "Schematic"]
    ok = ok and segment_panel.positions_table.rowCount() == 11
    ok = ok and segment_panel.sections_table.rowCount() == 1
    ok = ok and segment_panel.sections_table.item(0, 7).text() == "LW"
    ok = ok and segment_panel.makeup_table.rowCount() == 1
    ok = ok and segment_panel.makeup_table.item(0, 1).text() == "Guided load"
    ok = ok and "event-to-event RPL section" in segment_panel.endpoint_summary.text()
    ok = ok and "Start terminal" in segment_panel.endpoint_summary.text()
    ok = ok and "End terminal" in segment_panel.endpoint_summary.text()
    ok = ok and segment_panel._schematic_args is not None
    segment_panel.views.setCurrentIndex(1)
    ok = ok and segment_panel._schematic_args is None
    segment_text = "\n".join(_graphics_text(item)
                              for item in segment_panel.schematic.scene().items())
    ok = ok and "Guided load" in segment_text
    ok = ok and "LW" in segment_text and "km" in segment_text
    ok = ok and "Start terminal" not in segment_text
    system_panel.deleteLater()
    segment_panel.deleteLater()
    return _result("guided system/segment overviews and schematic construct", ok)


def test_node_line_assembly_sld_wraps() -> bool:
    items = []
    for index in range(9):
        items.append(am.AssemblyItem(
            kind="section", name=f"Section {index + 1}", cable_type="LW",
            length_m=1000.0, color_hex="#4477aa"))
        if index == 3:
            items.append(am.AssemblyItem(kind="body", name="BU-1", point_load_kN=2.0))
    widget = SldWidget()
    widget.resize(720, 320)
    widget.set_assembly(am.Assembly(name="Wrapped assembly", items=items))
    text = "\n".join(_graphics_text(item) for item in widget.scene().items())
    ok = "LW" in text and "1.000 km" in text and "BU-1" in text
    ok = ok and widget._home.toolTip().startswith("Home")
    ok = ok and widget._wrap_button.isChecked()
    ok = ok and widget._fit_all.toolTip().startswith("Fit the complete")
    widget.set_wrapped(True)
    y_rows = {round(point.y(), 3) for point in widget._positions.values()}
    ok = ok and widget._wrap_button.isChecked() and len(y_rows) > 1
    widget.mark_cable_dist(2500.0)
    ok = ok and widget._marker_item is not None
    widget.deleteLater()
    return _result("node-line assembly SLD supports wrapping", ok)


def test_configurable_table_columns_persist() -> bool:
    columns = [
        ("Position", "position", True),
        ("Event", "event", True),
        ("Imported client field", "attr:ClientField", False),
    ]
    first = ConfigurableTable("test_configurable_columns")
    first.configure_columns(columns, {"Essentials": {"position", "event"}})
    QSettings().remove(first._state_key)
    first.configure_columns(columns, {"Essentials": {"position", "event"}})
    first.setColumnHidden(2, False)
    first.setColumnWidth(1, 177)
    first._header_changed()

    second = ConfigurableTable("test_configurable_columns")
    second.configure_columns(columns, {"Essentials": {"position", "event"}})
    ok = second.field_key(2) == "attr:ClientField"
    ok = ok and not second.isColumnHidden(2)
    ok = ok and second.columnWidth(1) == 177
    QSettings().remove(first._state_key)
    first.deleteLater()
    second.deleteLater()
    return _result("configurable imported columns persist visibility and width", ok)


def test_assembly_review_uses_section_equipment_language() -> bool:
    model = RplModel(points=[
        RplPoint(0, 1, "Start", 50.0, 0.0),
        RplPoint(1, 2, "Joint JT-1", 50.01, 0.0),
        RplPoint(2, 3, "End", 50.02, 0.0),
    ], segments=[RplSegment(0, slack_pct=0.0), RplSegment(1, slack_pct=0.0)])
    for leg in model.segments:
        leg.attrs["CableType"] = "LW"
    eng.recompute(model, _da())
    _assembly, review = am.extract_from_rpl(
        model, am.EventClassifier.with_defaults(), name="Review")
    dialog = ExtractReviewDialog(model, review, "Review")
    classifications = dialog._classifications()
    ok = classifications.get(1) == "body"
    ok = ok and "equipment" in dialog.summary_label.text()
    ok = ok and dialog.grouping_combo.itemText(0).endswith("equipment)")
    dialog.deleteLater()
    return _result("assembly review distinguishes sections and equipment", ok)


def test_rpl_tables_populate_lazily() -> bool:
    panel = RplManagerPanel(None, embedded=True)
    model = RplModel(points=[
        RplPoint(0, 1, "Start", 50.0, 0.0),
        RplPoint(1, 2, "BU-1", 50.01, 0.0),
        RplPoint(2, 3, "End", 50.02, 0.0),
    ], segments=[RplSegment(0, slack_pct=0.0), RplSegment(1, slack_pct=0.0)])
    eng.recompute(model, _da())
    panel.model = model
    panel.tabs.setCurrentIndex(0)
    panel._refresh_tables()
    ok = panel.points_table.rowCount() == 3
    ok = ok and panel.sections_table.rowCount() == 0
    ok = ok and panel.segments_table.rowCount() == 0
    panel.tabs.setCurrentIndex(1)
    ok = ok and panel.sections_table.rowCount() == 2
    ok = ok and panel.segments_table.rowCount() == 0
    panel.tabs.setCurrentIndex(2)
    ok = ok and panel.segments_table.rowCount() == 2
    panel.deleteLater()
    return _result("RPL tables populate only when their tab is opened", ok)


def test_v3_adapter_contract() -> bool:
    store = _temp_store()
    rpl_id = _register_synthetic_rpl(store, "Route X", slack_pct=2.0)
    adapter = WorkbenchV3Adapter(store)

    refs = adapter.list_rpls()
    ok = len(refs) == 1 and refs[0].rpl_id == rpl_id and refs[0].length_km > 10.0

    # bathymetry: falls back to RPL ApproxDepth interpolation
    profile = adapter.bathymetry_profile(rpl_id, kp_km=5.0, back_km=1.0, fwd_km=1.0, step_m=100.0)
    ok = ok and len(profile) >= 15
    ok = ok and abs(profile[0][0] - (-1000.0)) < 1.0
    depths = [d for _, d in profile]
    ok = ok and all(-220.0 < d < -90.0 for d in depths)
    ok = ok and depths[0] > depths[-1]  # deepens along route

    azimuth = adapter.lay_azimuth_deg(rpl_id, 5.0)
    ok = ok and azimuth is not None and (azimuth < 1.0 or azimuth > 359.0)

    # assembly + fit -> window
    assembly = am.Assembly(name="A1", items=[
        am.AssemblyItem(kind="section", name="LW", length_m=6000.0, q_water_npm=22.0,
                        diameter_m=0.035, cd_normal=1.2),
        am.AssemblyItem(kind="body", name="Joint 1", point_load_kN=1.0),
        am.AssemblyItem(kind="section", name="DA", length_m=6000.0, q_water_npm=30.0),
    ])
    header, items = am.assembly_to_rows(assembly)
    store.save_assembly(header, items)
    store.save_fit({
        "assembly_id": assembly.assembly_id, "rpl_id": rpl_id,
        "anchor_kp_km": 0.0, "anchor_cable_dist_m": 0.0, "direction": 1,
        "params_json": "{}",
    })

    # window centred at the joint (cable 6000 m): joint KP ~= 6.0/1.02 km route
    joint_kp = eng.kp_from_cable_dist(adapter._model(rpl_id), 6.0)
    window = adapter.assembly_window(rpl_id, joint_kp, cable_back_m=1000.0, cable_fwd_m=1000.0)
    ok = ok and len(window) == 3
    kinds = [w.get("type") for w in window]
    ok = ok and kinds == ["segment", "body", "segment"]
    ok = ok and abs(window[0]["length_m"] - 1000.0) < 1.0
    ok = ok and abs(window[2]["length_m"] - 1000.0) < 1.0
    ok = ok and window[0].get("diameter_m") == 0.035  # V3 hydro keys survive

    # push results back
    layer_name = adapter.push_results(rpl_id, "test run", [
        {"kp_km": 1.0, "tension_kN": 20.0},
        {"kp_km": 2.0, "tension_kN": 21.5},
    ])
    ok = ok and layer_name is not None
    layer = store.open_layer(layer_name)
    ok = ok and layer is not None and layer.featureCount() == 2
    return _result("V3 adapter contract (routes/bathy/azimuth/window/results)", ok)


def run_all() -> list:
    return [
        test_systems_bmh_bu_example(),
        test_manual_and_unassigned_system_membership(),
        test_guided_overviews_construct(),
        test_node_line_assembly_sld_wraps(),
        test_configurable_table_columns_persist(),
        test_assembly_review_uses_section_equipment_language(),
        test_rpl_tables_populate_lazily(),
        test_v3_adapter_contract(),
    ]


if __name__ == "__main__":
    results = run_all()
    raise SystemExit(0 if all(results) else 1)

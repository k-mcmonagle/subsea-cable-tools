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

from qgis.core import QgsCoordinateReferenceSystem, QgsProject

from ..kp_range_utils import make_distance_area
from ..qgis_compat import WKB_LINESTRING, WKB_POINT
from ..workbench import assembly_model as am
from ..workbench import rpl_engine as eng
from ..workbench import schema
from ..workbench.rpl_engine import RplModel, RplPoint, RplSegment, SlackMode
from ..workbench.rpl_layer_io import model_rows_for_layers
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
        RplPoint(seq=i, pos_no=i + 1, event="", lat=50.0 + 0.01 * i, lon=0.0,
                 depth_m=-100.0 - 10.0 * i)
        for i in range(n_points)
    ]
    segments = [RplSegment(seq=i, slack_pct=slack_pct) for i in range(n_points - 1)]
    model = RplModel(points=points, segments=segments)
    eng.recompute(model, _da(), slack_mode=SlackMode.HOLD_SLACK)

    rpl_id = schema.new_id()
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
    })
    store.save_component(
        {"component_id": schema.new_id(), "kind": "rpl", "subject_id": rpl_id, "name": name},
        port_labels=["A", "B"],
    )
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
        component = store.component_for_subject(subject_or_cid)
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
        test_v3_adapter_contract(),
    ]


if __name__ == "__main__":
    results = run_all()
    raise SystemExit(0 if all(results) else 1)

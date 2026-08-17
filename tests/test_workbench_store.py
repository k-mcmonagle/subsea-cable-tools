# -*- coding: utf-8 -*-
"""Checks for the Cable Route Workbench GeoPackage store.

Round-trips the registry tables in a temp GeoPackage: assemblies + items,
RPL rows, fits, event rules, and the CRA-core topology invariants
(self-loop and over-connected port rejection, validate_topology findings).

Requires the QGIS API (run via tests/run_qgis_smoke_tests.py).
"""

from __future__ import annotations

import os
import tempfile

from ..workbench import schema
from ..workbench.store import WorkbenchStore


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


def _temp_store() -> WorkbenchStore:
    # Unique file per test: QGIS's OGR connection pool keeps GeoPackages open,
    # so deleting/reusing a shared path fails on Windows.
    folder = tempfile.mkdtemp(prefix="wb_store_test_")
    store = WorkbenchStore(os.path.join(folder, "workbench.gpkg"))
    store.ensure_created()
    return store


def test_create_and_meta() -> bool:
    store = _temp_store()
    ok = store.exists()
    meta = store.read_meta()
    ok = ok and meta.get("schema_version") == str(schema.SCHEMA_VERSION)
    ok = ok and len(store.list_event_rules()) >= 5  # defaults seeded
    return _result("create + meta + default event rules", ok, f"meta={meta}")


def test_assembly_round_trip() -> bool:
    store = _temp_store()
    aid = schema.new_id()
    header = {
        "assembly_id": aid,
        "name": "Trunk A",
        "kind": "cable",
        "source": "manual",
        "total_cable_len_m": 52000.0,
    }
    items = [
        {"kind": "section", "name": "LW-1", "length_m": 25000.0, "cable_type": "LW"},
        {"kind": "body", "name": "Joint 1", "length_m": 0.0},
        {"kind": "section", "name": "DA-1", "length_m": 27000.0, "cable_type": "DA"},
    ]
    store.save_assembly(header, items)
    got_header, got_items = store.get_assembly(aid)
    ok = got_header is not None and got_header["name"] == "Trunk A"
    ok = ok and [i["name"] for i in got_items] == ["LW-1", "Joint 1", "DA-1"]
    ok = ok and [int(i["seq"]) for i in got_items] == [0, 1, 2]

    # replace items on re-save
    store.save_assembly(header, items[:2])
    _, got_items2 = store.get_assembly(aid)
    ok = ok and len(got_items2) == 2

    store.delete_assembly(aid)
    got_header3, got_items3 = store.get_assembly(aid)
    ok = ok and got_header3 is None and not got_items3
    return _result("assembly round trip + item replace + delete", ok)


def test_rpl_and_fit_round_trip() -> bool:
    store = _temp_store()
    rid = schema.new_id()
    store.save_rpl({
        "rpl_id": rid,
        "name": "Seg 1",
        "kind": "planned",
        "points_layer": "rpl_Seg_1_points",
        "lines_layer": "rpl_Seg_1_lines",
        "slack_mode": "hold_slack",
        "depth_source_config": '{"mode": 0}',
    })
    got = store.get_rpl(rid)
    ok = got is not None and got["name"] == "Seg 1"
    ok = ok and store.rpl_depth_config(rid) == {"mode": 0}

    aid = schema.new_id()
    store.save_assembly({"assembly_id": aid, "name": "A", "kind": "cable"}, [])
    store.save_fit({"assembly_id": aid, "rpl_id": rid, "anchor_kp_km": 0.0,
                    "anchor_cable_dist_m": 0.0, "direction": 1})
    ok = ok and len(store.list_fits(rpl_id=rid)) == 1

    store.delete_rpl(rid)
    ok = ok and store.get_rpl(rid) is None
    ok = ok and not store.list_fits(rpl_id=rid)  # fits cascade
    return _result("rpl + fit round trip + cascade delete", ok)


def test_topology_invariants() -> bool:
    store = _temp_store()
    # BMH --A-- rpl1 --B-- BU --branch1/branch2--> (open)
    bmh = store.save_component({"kind": "node", "name": "BMH-1", "node_type": "bmh"}, ["A"])
    rpl1 = store.save_component({"kind": "rpl", "subject_id": "r1", "name": "Seg 1"}, ["A", "B"])
    bu = store.save_component({"kind": "node", "name": "BU-1", "node_type": "bu"},
                              ["trunk_in", "branch_1", "branch_2"])
    ports = store.list_ports()

    def port_of(cid, label):
        return next(p["port_id"] for p in ports if p["component_id"] == cid and p["label"] == label)

    store.connect_ports(port_of(bmh, "A"), port_of(rpl1, "A"))
    store.connect_ports(port_of(rpl1, "B"), port_of(bu, "trunk_in"))
    ok = len(store.list_connections()) == 2

    # over-connected port rejected
    try:
        store.connect_ports(port_of(rpl1, "B"), port_of(bu, "branch_1"))
        ok = False
    except ValueError:
        pass

    # self-loop (two ports of same component) rejected
    try:
        store.connect_ports(port_of(bu, "branch_1"), port_of(bu, "branch_2"))
        ok = False
    except ValueError:
        pass

    ok = ok and store.validate_topology() == []

    # deleting a component removes its ports and connections
    store.delete_component(rpl1)
    ok = ok and len(store.list_connections()) == 0
    ok = ok and store.validate_topology() == []
    return _result("CRA topology invariants + cascade delete", ok)


def test_registry_read_cache_tracks_mutations() -> bool:
    store = _temp_store()
    route_id = store.create_route("Cached route")
    first = store.list_routes()
    first[0]["name"] = "caller mutation"
    second = store.list_routes()
    ok = second[0].get("name") == "Cached route"
    route = store.get_route(route_id) or {}
    route["name"] = "Updated route"
    store.save_route(route)
    ok = ok and (store.get_route(route_id) or {}).get("name") == "Updated route"
    ok = ok and schema.TABLE_ROUTE in store._table_cache
    store.clear_cache()
    ok = ok and not store._table_cache
    ok = ok and (store.get_route(route_id) or {}).get("name") == "Updated route"
    return _result("registry read cache is isolated, current, and reloadable", ok)


def test_segment_makeup_orders_assemblies_and_joints() -> bool:
    store = _temp_store()
    route_id = store.create_route("Two-load segment")
    assembly_ids = []
    for name, length in (("Load 01", 42000.0), ("Load 02", 38000.0)):
        assembly_id = schema.new_id()
        store.save_assembly({
            "assembly_id": assembly_id, "name": name, "kind": "cable",
            "total_cable_len_m": length,
        }, [{"kind": "section", "name": "LW", "length_m": length,
             "cable_type": "LW"}])
        assembly_ids.append(assembly_id)
        store.add_makeup_assembly(route_id, assembly_id)

    header, items = store.current_makeup(route_id)
    ok = header is not None and header.get("route_id") == route_id
    ok = ok and [item.get("kind") for item in items] == [
        "assembly", "joint", "assembly"]
    ok = ok and items[1].get("name") == "Joint J01"
    ok = ok and [item.get("assembly_id") for item in items if item.get("kind") == "assembly"] \
        == assembly_ids
    try:
        store.delete_assembly(assembly_ids[0])
        ok = False
    except ValueError:
        pass
    store.delete_makeup_item(items[0].get("makeup_item_id") or "")
    _header, remaining = store.current_makeup(route_id)
    ok = ok and [item.get("kind") for item in remaining] == ["assembly"]
    return _result("segment make-up orders assemblies and joints", ok)


def run_all() -> list:
    return [
        test_create_and_meta(),
        test_assembly_round_trip(),
        test_rpl_and_fit_round_trip(),
        test_topology_invariants(),
        test_registry_read_cache_tracks_mutations(),
        test_segment_makeup_orders_assemblies_and_joints(),
    ]


if __name__ == "__main__":
    results = run_all()
    raise SystemExit(0 if all(results) else 1)

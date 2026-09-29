# -*- coding: utf-8 -*-
"""Checks for the Cable Route Workbench GeoPackage store.

Round-trips the registry tables in a temp GeoPackage: assemblies + items,
RPL rows, fits, event rules, and the CRA-core topology invariants
(self-loop and over-connected port rejection, validate_topology findings).
Also covers the transactional registry writes: a failure part-way through a
save or a cascade leaves the previous rows intact, a locked file fails
cleanly, and a registry written by the old OGR code path round-trips.

Requires the QGIS API (run via tests/run_qgis_smoke_tests.py).
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

from ..workbench import schema
from ..workbench.store import WorkbenchStore, WorkbenchStoreError


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


def test_add_port_continues_numbering() -> bool:
    store = _temp_store()
    bu = store.save_component({"kind": "node", "name": "BU-1", "node_type": "bu"},
                              ["Trunk", "Branch 1", "Branch 2"])
    store.add_port(bu)
    labels = sorted(p["label"] for p in store.list_ports() if p["component_id"] == bu)
    ok = labels == ["Branch 1", "Branch 2", "Branch 3", "Trunk"]
    joint = store.save_component({"kind": "node", "name": "J", "node_type": "joint"},
                                 ["Side 1", "Side 2"])
    store.add_port(joint)
    labels = sorted(p["label"] for p in store.list_ports() if p["component_id"] == joint)
    ok = ok and labels == ["Side 1", "Side 2", "Side 3"]
    return _result("add_port continues Branch/Side numbering", ok, str(labels))


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


# ---------------------------------------------------------------------------
# Transactional registry writes
# ---------------------------------------------------------------------------
def _sql(store: WorkbenchStore, statement: str) -> None:
    conn = sqlite3.connect(store.gpkg_path)
    try:
        conn.execute(statement)
        conn.commit()
    finally:
        conn.close()


def _disk_rows(store: WorkbenchStore, table: str) -> list:
    """Rows as a fresh store (no shared cache) reads them from disk."""
    return WorkbenchStore(store.gpkg_path).read_table(table)


def _seed_assembly(store: WorkbenchStore) -> str:
    aid = schema.new_id()
    store.save_assembly({"assembly_id": aid, "name": "Trunk", "kind": "cable"}, [
        {"kind": "section", "name": "LW-1", "length_m": 1000.0},
        {"kind": "section", "name": "DA-1", "length_m": 2000.0},
    ])
    return aid


def test_failed_write_keeps_previous_rows() -> bool:
    """A SQLite failure part-way through a save rolls the whole save back.

    The trigger aborts the item insert *after* the header upsert and the
    delete of the old items ran — the old whole-table writer would have left
    the table truncated at that point.
    """
    store = _temp_store()
    aid = _seed_assembly(store)
    _sql(store, "CREATE TRIGGER wb_test_fail BEFORE INSERT ON wb_assembly_item "
                "WHEN NEW.name = 'BOOM' BEGIN SELECT RAISE(ABORT, 'simulated disk full'); END")
    raised = False
    try:
        store.save_assembly({"assembly_id": aid, "name": "Renamed", "kind": "cable"},
                            [{"kind": "section", "name": "BOOM", "length_m": 1.0}])
    except WorkbenchStoreError:
        raised = True
    finally:
        _sql(store, "DROP TRIGGER wb_test_fail")
    header, items = store.get_assembly(aid)
    disk_items = [r for r in _disk_rows(store, schema.TABLE_ASSEMBLY_ITEM)
                  if r.get("assembly_id") == aid]
    ok = raised and header is not None and header.get("name") == "Trunk"
    ok = ok and [i.get("name") for i in items] == ["LW-1", "DA-1"]
    ok = ok and sorted(r.get("name") for r in disk_items) == ["DA-1", "LW-1"]
    # the store keeps working after the failure
    store.save_assembly({"assembly_id": aid, "name": "Renamed", "kind": "cable"}, items[:1])
    ok = ok and len(store.get_assembly(aid)[1]) == 1
    return _result("failed write rolls back, previous rows intact", ok,
                   f"raised={raised} items={[i.get('name') for i in items]}")


def test_unstorable_value_changes_nothing() -> bool:
    store = _temp_store()
    aid = _seed_assembly(store)
    raised = False
    try:
        store.save_assembly({"assembly_id": aid, "name": "Trunk", "kind": "cable"}, [
            {"kind": "section", "name": "ok", "length_m": 5.0},
            {"kind": "section", "name": "bad", "length_m": "not a number"},
        ])
    except WorkbenchStoreError:
        raised = True
    items = [r.get("name") for r in _disk_rows(store, schema.TABLE_ASSEMBLY_ITEM)]
    ok = raised and sorted(items) == ["DA-1", "LW-1"]
    return _result("unstorable value refused, table unchanged", ok, str(items))


def test_cascade_delete_is_atomic() -> bool:
    """delete_assembly touches four tables; a failure in the last step must
    leave the header, items and fits all in place (and the cache honest)."""
    store = _temp_store()
    aid = _seed_assembly(store)
    store.save_component({"kind": "assembly", "subject_id": aid, "name": "Trunk"}, ["A", "B"])
    store.save_fit({"assembly_id": aid, "rpl_id": "r1", "anchor_kp_km": 0.0})
    _sql(store, "CREATE TRIGGER wb_test_fail BEFORE DELETE ON wb_component "
                "BEGIN SELECT RAISE(ABORT, 'simulated lock'); END")
    raised = False
    try:
        store.delete_assembly(aid)
    except WorkbenchStoreError:
        raised = True
    finally:
        _sql(store, "DROP TRIGGER wb_test_fail")
    header, items = store.get_assembly(aid)
    ok = raised and header is not None and len(items) == 2
    ok = ok and len(store.list_fits(assembly_id=aid)) == 1
    ok = ok and store.component_for_subject(aid) is not None
    ok = ok and len(_disk_rows(store, schema.TABLE_ASSEMBLY_ITEM)) == 2
    store.delete_assembly(aid)
    ok = ok and store.get_assembly(aid) == (None, [])
    ok = ok and not store.list_fits(assembly_id=aid)
    ok = ok and store.component_for_subject(aid) is None
    return _result("cascade delete is all-or-nothing", ok, f"raised={raised}")


def test_locked_file_fails_cleanly() -> bool:
    from ..workbench import store as store_module

    store = _temp_store()
    aid = _seed_assembly(store)
    blocker = sqlite3.connect(store.gpkg_path)
    saved_timeout = store_module._BUSY_TIMEOUT_S
    store_module._BUSY_TIMEOUT_S = 0.2
    raised = False
    try:
        blocker.execute("BEGIN IMMEDIATE")  # another writer holds the file
        try:
            store.delete_assembly(aid)
        except WorkbenchStoreError:
            raised = True
    finally:
        blocker.rollback()
        blocker.close()
        store_module._BUSY_TIMEOUT_S = saved_timeout
    header, items = store.get_assembly(aid)
    ok = raised and header is not None and len(items) == 2
    return _result("locked GeoPackage: write refused, data intact", ok, f"raised={raised}")


def _legacy_store() -> WorkbenchStore:
    """A registry written entirely by the pre-SQL code path (OGR writer),
    with wb_rpl in its v2 shape (no route/revision columns)."""
    from qgis.core import QgsProject

    from ..processing.cable_lay_parsers import fields_from_specs, write_layer_to_gpkg
    from ..qgis_compat import WKB_NO_GEOMETRY

    folder = tempfile.mkdtemp(prefix="wb_store_legacy_")
    path = os.path.join(folder, "workbench.gpkg")
    context = QgsProject.instance().transformContext()
    tables = dict(schema.REGISTRY_TABLES)
    tables[schema.TABLE_RPL] = schema.RPL_FIELDS[:-5]
    rows = {
        schema.TABLE_META: [{"key": "schema_version", "value": "2"},
                            {"key": "created_utc", "value": "2025-01-01T00:00:00Z"}],
        schema.TABLE_RPL: [{"rpl_id": "rpl-old", "name": "Old route", "kind": "planned",
                            "points_layer": "p", "lines_layer": "l",
                            "slack_mode": "hold_slack", "depth_source_config": ""}],
        schema.TABLE_ASSEMBLY: [{"assembly_id": "asm-old", "name": "Old cable",
                                 "kind": "cable", "total_cable_len_m": 12.5}],
        schema.TABLE_ASSEMBLY_ITEM: [
            {"item_id": f"item-{i}", "assembly_id": "asm-old", "seq": i,
             "kind": "section", "name": f"S{i}", "length_m": 2.5 * (i + 1)}
            for i in range(3)],
        schema.TABLE_EVENT_RULE: [{"rule_id": "rule-old", "pattern": "joint",
                                   "category": "body", "body_type": "joint",
                                   "priority": 1}],
    }
    for table, specs in tables.items():
        write_layer_to_gpkg(path, table, fields_from_specs(specs), WKB_NO_GEOMETRY,
                            rows.get(table, []), context)
    return WorkbenchStore(path)


def test_legacy_store_round_trip() -> bool:
    """A store written by the old OGR code path reads identically, migrates,
    takes SQL writes, and stays a valid GeoPackage for OGR/QGIS."""
    from ..processing.cable_lay_parsers import open_gpkg_layer

    store = _legacy_store()
    header, items = store.get_assembly("asm-old")
    ok = header is not None and header.get("name") == "Old cable"
    ok = ok and header.get("total_cable_len_m") == 12.5
    ok = ok and [(i.get("name"), i.get("seq"), i.get("length_m")) for i in items] == [
        ("S0", 0, 2.5), ("S1", 1, 5.0), ("S2", 2, 7.5)]
    detail = f"read={ok}"

    store.migrate()  # v2 -> current: wb_rpl gains its revision columns via SQL
    rpl = store.get_rpl("rpl-old") or {}
    ok = ok and rpl.get("rev_label") == "Rev 1" and bool(rpl.get("route_id"))
    ok = ok and store.read_meta().get("schema_version") == str(schema.SCHEMA_VERSION)
    detail += f" migrated={ok}"

    store.save_assembly(dict(header, name="Edited cable"), items[:2])
    store.issue_rpl("rpl-old")

    # OGR (what a QGIS layer or an older plugin build sees)
    layer = open_gpkg_layer(store.gpkg_path, schema.TABLE_ASSEMBLY_ITEM)
    ogr_items = sorted(f["name"] for f in layer.getFeatures()) if layer else []
    ok = ok and layer is not None and layer.featureCount() == 2 and ogr_items == ["S0", "S1"]
    rpl_layer = open_gpkg_layer(store.gpkg_path, schema.TABLE_RPL)
    names = rpl_layer.fields().names() if rpl_layer else []
    ok = ok and all(name in names for name, _t in schema.RPL_FIELDS)
    statuses = [f["status"] for f in rpl_layer.getFeatures()] if rpl_layer else []
    ok = ok and statuses == [schema.STATUS_ISSUED]
    # the feature-count triggers kept gpkg_ogr_contents honest
    conn = sqlite3.connect(store.gpkg_path)
    try:
        counted = conn.execute(
            "SELECT feature_count FROM gpkg_ogr_contents WHERE table_name = ?",
            (schema.TABLE_ASSEMBLY_ITEM,)).fetchone()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    ok = ok and counted is not None and counted[0] in (2, None) and integrity == "ok"
    detail += f" ogr_items={ogr_items} count={counted} integrity={integrity}"
    return _result("legacy (OGR-written) store round trip + OGR compatibility", ok, detail)


def test_store_usable_from_worker_thread() -> bool:
    """Processing runs import algorithms off the main thread; the store must
    not hand a main-thread SQLite connection to them."""
    import threading

    store = _temp_store()
    errors = []

    def work():
        try:
            store.create_route("From worker")
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(repr(exc))

    thread = threading.Thread(target=work)
    thread.start()
    thread.join(30)
    ok = not errors and [r.get("name") for r in store.list_routes()] == ["From worker"]
    return _result("store writes from a worker thread", ok, "; ".join(errors))


def run_all() -> list:
    return [
        test_create_and_meta(),
        test_assembly_round_trip(),
        test_rpl_and_fit_round_trip(),
        test_topology_invariants(),
        test_add_port_continues_numbering(),
        test_registry_read_cache_tracks_mutations(),
        test_segment_makeup_orders_assemblies_and_joints(),
        test_failed_write_keeps_previous_rows(),
        test_unstorable_value_changes_nothing(),
        test_cascade_delete_is_atomic(),
        test_locked_file_fails_cleanly(),
        test_legacy_store_round_trip(),
        test_store_usable_from_worker_thread(),
    ]


if __name__ == "__main__":
    results = run_all()
    raise SystemExit(0 if all(results) else 1)

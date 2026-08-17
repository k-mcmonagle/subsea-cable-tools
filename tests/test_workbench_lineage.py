# -*- coding: utf-8 -*-
"""Smoke tests for workbench route/revision lineage and topology migration."""

from __future__ import annotations

import os
import tempfile

from qgis.core import QgsProject

from ..processing.cable_lay_parsers import WKT_KEY
from ..qgis_compat import WKB_LINESTRING, WKB_POINT
from ..workbench import schema
from ..workbench.store import WorkbenchReadOnlyError, WorkbenchStore


V2_ASSEMBLY_FIELDS = schema.ASSEMBLY_FIELDS[:-4]
V2_RPL_FIELDS = schema.RPL_FIELDS[:-5]


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" - {detail}"
    print(msg)
    return ok


def _temp_store() -> WorkbenchStore:
    folder = tempfile.mkdtemp(prefix="wb_lineage_test_")
    return WorkbenchStore(os.path.join(folder, "workbench.gpkg"),
                          QgsProject.instance().transformContext())


def _seed_spatial_pair(store: WorkbenchStore, rpl_id: str, name: str,
                       extra: bool = False) -> tuple[str, str]:
    point_specs = list(schema.RPL_POINT_FIELDS)
    line_specs = list(schema.RPL_LINE_FIELDS)
    if extra:
        point_specs.append(("ExtraFlag", "str"))
        line_specs.append(("ExtraFlag", "str"))
    points_name = schema.rpl_points_layer_name(name)
    lines_name = schema.rpl_lines_layer_name(name)
    point_rows = [
        {
            "rpl_id": rpl_id, "SeqNo": 0, "PosNo": 1, "Event": "Start",
            "DistCumulative": 0.0, "CableDistCumulative": 0.0,
            "ApproxDepth": 100.0, "Latitude": 50.0, "Longitude": 0.0,
            "ExtraFlag": "keep" if extra else None,
            WKT_KEY: "POINT (0 50)",
        },
        {
            "rpl_id": rpl_id, "SeqNo": 1, "PosNo": 2, "Event": "End",
            "DistCumulative": 1.0, "CableDistCumulative": 1.02,
            "ApproxDepth": 110.0, "Latitude": 50.01, "Longitude": 0.0,
            "ExtraFlag": "keep" if extra else None,
            WKT_KEY: "POINT (0 50.01)",
        },
    ]
    line_rows = [{
        "rpl_id": rpl_id, "SeqNo": 0, "FromPos": 1, "ToPos": 2,
        "DistBetweenPos": 1.0, "Slack": 2.0, "CableDistBetweenPos": 1.02,
        "ExtraFlag": "keep" if extra else None,
        WKT_KEY: "LINESTRING (0 50, 0 50.01)",
    }]
    store.write_spatial_layer(points_name, point_specs, WKB_POINT, point_rows)
    store.write_spatial_layer(
        lines_name, line_specs, WKB_LINESTRING, line_rows)
    return points_name, lines_name


def test_migrate_v2_to_v3() -> bool:
    store = _temp_store()
    path = store.gpkg_path
    rpl_id = "rpl-v2"
    assembly_id = "asm-v2"
    points_layer, lines_layer = _seed_spatial_pair(store, rpl_id, "Legacy")
    store._write_table_rows(schema.TABLE_META, schema.META_FIELDS, [
        {"key": "schema_version", "value": "2"},
        {"key": "created_utc", "value": "2026-01-01T00:00:00Z"},
    ])
    store._write_table_rows(schema.TABLE_RPL, V2_RPL_FIELDS, [{
        "rpl_id": rpl_id, "name": "Legacy", "kind": "planned",
        "points_layer": points_layer, "lines_layer": lines_layer,
        "slack_mode": "hold_slack", "depth_source_config": "",
        "created_utc": "2026-01-01T00:00:00Z",
    }])
    store._write_table_rows(schema.TABLE_ASSEMBLY, V2_ASSEMBLY_FIELDS, [{
        "assembly_id": assembly_id, "name": "Cable A", "kind": "cable",
        "source": "manual", "total_cable_len_m": 1.0,
    }])
    store._write_table_rows(schema.TABLE_ASSEMBLY_ITEM, schema.ASSEMBLY_ITEM_FIELDS, [])
    store._write_table_rows(schema.TABLE_FIT, schema.FIT_FIELDS, [{
        "fit_id": "fit-v2", "assembly_id": assembly_id, "rpl_id": rpl_id,
        "anchor_kp_km": 0.0, "anchor_cable_dist_m": 0.0, "direction": 1,
        "params_json": "{}", "created_utc": "2026-01-01T00:00:00Z",
    }])
    store._write_table_rows(schema.TABLE_COMPONENT, schema.COMPONENT_FIELDS, [{
        "component_id": "comp-v2", "kind": "rpl", "subject_id": rpl_id,
        "name": "Legacy", "system_id": "system-from-topology",
    }])

    store.migrate()
    meta = store.read_meta()
    routes = store.list_routes()
    rpl = store.get_rpl(rpl_id) or {}
    assembly, _items = store.get_assembly(assembly_id)
    stem, ext = os.path.splitext(path)
    ok = meta.get("schema_version") == str(schema.SCHEMA_VERSION)
    ok = ok and len(routes) == 1 and routes[0].get("name") == "Legacy"
    ok = ok and routes[0].get("system_id") == "system-from-topology"
    ok = ok and rpl.get("route_id") == routes[0].get("route_id")
    ok = ok and rpl.get("rev_label") == "Rev 1" and rpl.get("status") == schema.STATUS_DRAFT
    ok = ok and assembly and assembly.get("rev_label") == "Rev 1"
    ok = ok and len(store.list_fits(rpl_id=rpl_id)) == 1
    makeup, makeup_items = store.current_makeup(rpl.get("route_id") or "")
    ok = ok and makeup is not None
    ok = ok and [item.get("kind") for item in makeup_items] == ["assembly"]
    ok = ok and os.path.exists(f"{stem}.migrate_v2.bak{ext}")
    ok = ok and os.path.exists(f"{stem}.migrate_v3.bak{ext}")
    ok = ok and os.path.exists(f"{stem}.migrate_v4.bak{ext}")
    component = store.component_for_segment(rpl.get("route_id") or "")
    ok = ok and component is not None and component.get("kind") == "route"
    return _result("migrate v2 through cable-segment topology", ok, f"meta={meta}")


def test_new_rpl_revision_deep_copy() -> bool:
    store = _temp_store()
    store.migrate()
    route_id = store.create_route("S013")
    rpl_id = schema.new_id()
    points_layer, lines_layer = _seed_spatial_pair(store, rpl_id, "S013 Rev 1", extra=True)
    store.save_rpl({
        "rpl_id": rpl_id, "name": "S013 Rev 1", "kind": "planned",
        "points_layer": points_layer, "lines_layer": lines_layer,
        "slack_mode": "hold_slack", "depth_source_config": "",
        "route_id": route_id, "rev_label": "Rev 1",
    })
    assembly_id = schema.new_id()
    store.save_assembly({"assembly_id": assembly_id, "name": "Trunk", "kind": "cable"}, [])
    store.save_fit({
        "fit_id": "fit-original", "assembly_id": assembly_id, "rpl_id": rpl_id,
        "anchor_kp_km": 0.0, "anchor_cable_dist_m": 0.0, "direction": 1,
        "params_json": "{}",
    })

    component_before = store.component_for_segment(route_id)
    new_id = store.new_rpl_revision(rpl_id)
    old = store.get_rpl(rpl_id) or {}
    new = store.get_rpl(new_id) or {}
    new_points = store.open_layer(new.get("points_layer"))
    features = list(new_points.getFeatures()) if new_points is not None else []
    copied_fits = store.list_fits(rpl_id=new_id)
    chain = store.supersedes_chain(schema.TABLE_RPL, new_id)
    revisions = store.revisions_of_route(route_id)
    ok = new.get("supersedes_id") == rpl_id and new.get("rev_label") == "Rev 2"
    ok = ok and old.get("points_layer") != new.get("points_layer")
    ok = ok and new_points is not None and new_points.fields().indexOf("ExtraFlag") >= 0
    ok = ok and len(features) == 2 and all(f["rpl_id"] == new_id for f in features)
    ok = ok and all(f["ExtraFlag"] == "keep" for f in features)
    ok = ok and len(copied_fits) == 1 and copied_fits[0].get("fit_id") != "fit-original"
    ok = ok and [r.get("rpl_id") for r in chain] == [new_id, rpl_id]
    ok = ok and (store.latest_revision(route_id) or {}).get("rpl_id") == new_id
    ok = ok and [r.get("rpl_id") for r in revisions] == [rpl_id, new_id]
    component_after = store.component_for_segment(route_id)
    ok = ok and component_before and component_after
    ok = ok and component_before.get("component_id") == component_after.get("component_id")
    return _result("new RPL revision deep copy + lineage", ok)


def test_v3_topology_connections_move_to_stable_segment() -> bool:
    store = _temp_store()
    store.ensure_created()
    route_id = "route-v3"
    store.upsert_rows(schema.TABLE_ROUTE, [{
        "route_id": route_id, "name": "Legacy segment", "system_id": "",
    }])
    for rpl_id, label in (("rpl-v3-a", "Rev 1"), ("rpl-v3-b", "Rev 2")):
        store.upsert_rows(schema.TABLE_RPL, [{
            "rpl_id": rpl_id, "route_id": route_id,
            "name": f"Legacy segment {label}", "rev_label": label,
            "status": schema.STATUS_DRAFT,
        }])
    comp_a = store.save_component({
        "component_id": "comp-v3-a", "kind": "rpl", "subject_id": "rpl-v3-a",
        "name": "Legacy segment Rev 1",
    }, ["A", "B"])
    comp_b = store.save_component({
        "component_id": "comp-v3-b", "kind": "rpl", "subject_id": "rpl-v3-b",
        "name": "Legacy segment Rev 2",
    }, ["A", "B"])
    node_a = store.save_component({"kind": "node", "name": "BMH", "node_type": "bmh"}, ["Cable"])
    node_b = store.save_component({"kind": "node", "name": "BU", "node_type": "bu"}, ["Trunk"])

    def port(component_id, label):
        return next(p["port_id"] for p in store.list_ports()
                    if p.get("component_id") == component_id and p.get("label") == label)

    store.connect_ports(port(comp_a, "A"), port(node_a, "Cable"))
    store.connect_ports(port(comp_b, "B"), port(node_b, "Trunk"))
    store.write_meta("schema_version", "3")
    store.migrate()

    component = store.component_for_segment(route_id) or {}
    component_ports = {p.get("port_id") for p in store.list_ports()
                       if p.get("component_id") == component.get("component_id")}
    connected_component_ports = {
        pid for connection in store.list_connections()
        for pid in (connection.get("port_a_id"), connection.get("port_b_id"))
        if pid in component_ports
    }
    ok = component.get("kind") == "route" and component.get("subject_id") == route_id
    ok = ok and len(connected_component_ports) == 2
    ok = ok and not any(c.get("kind") in ("rpl", "legacy_rpl")
                        for c in store.list_components())
    return _result("v3 A/B wiring migrates to stable cable-segment endpoints", ok)


def test_issue_read_only() -> bool:
    store = _temp_store()
    store.migrate()
    route_id = store.create_route("Route A")
    rpl_id = schema.new_id()
    store.save_rpl({
        "rpl_id": rpl_id, "name": "Route A Rev 1", "kind": "planned",
        "points_layer": "p", "lines_layer": "l", "route_id": route_id,
        "rev_label": "Rev 1",
    })
    assembly_id = schema.new_id()
    store.save_assembly({"assembly_id": assembly_id, "name": "A", "kind": "cable"}, [])

    store.issue_rpl(rpl_id)
    ok = False
    try:
        store.save_rpl({"rpl_id": rpl_id, "name": "edited"})
    except WorkbenchReadOnlyError:
        ok = True
    store.save_fit({"assembly_id": assembly_id, "rpl_id": rpl_id, "anchor_kp_km": 0.0})
    store.save_assessment({"rpl_id": rpl_id, "name": "Assess", "sample_step_m": 50.0})
    store.reopen_rpl(rpl_id)
    store.save_rpl({"rpl_id": rpl_id, "name": "Route A Rev 1 reopened"})
    ok = ok and (store.get_rpl(rpl_id) or {}).get("name") == "Route A Rev 1 reopened"

    store.issue_assembly(assembly_id)
    try:
        store.save_assembly({"assembly_id": assembly_id, "name": "edited", "kind": "cable"}, [])
        ok = False
    except WorkbenchReadOnlyError:
        pass
    store.reopen_assembly(assembly_id)
    store.save_assembly({"assembly_id": assembly_id, "name": "A reopened", "kind": "cable"}, [])
    ok = ok and (store.get_assembly(assembly_id)[0] or {}).get("name") == "A reopened"
    return _result("issued entities read-only, analysis still writable", ok)


def test_next_rev_label() -> bool:
    ok = schema.next_rev_label([]) == "Rev 1"
    ok = ok and schema.next_rev_label(["Rev 1", "Rev 3"]) == "Rev 4"
    ok = ok and schema.next_rev_label([{"rev_label": "Design Rev 2"}]) == "Rev 3"
    ok = ok and schema.unique_layer_name({"rpl_A_points", "rpl_A_points_2"}, "rpl_A_points") == "rpl_A_points_3"
    return _result("revision label and layer-name helpers", ok)


def test_edit_draft_rpl_revision() -> bool:
    store = _temp_store()
    store.migrate()
    route_id = store.create_route("S021")
    rpl_id = schema.new_id()
    store.save_rpl({
        "rpl_id": rpl_id, "name": "S021 Rev 1", "kind": "planned",
        "points_layer": "p", "lines_layer": "l", "route_id": route_id,
        "rev_label": "Rev 1",
    })
    edited = store.get_rpl(rpl_id) or {}
    edited["rev_label"] = "C02"
    edited["name"] = "S021 C02"
    store.save_rpl(edited)
    got = store.get_rpl(rpl_id) or {}
    ok = got.get("rev_label") == "C02" and got.get("name") == "S021 C02"
    return _result("draft RPL revision label remains editable", ok)


def run_all() -> list:
    return [
        test_migrate_v2_to_v3(),
        test_new_rpl_revision_deep_copy(),
        test_v3_topology_connections_move_to_stable_segment(),
        test_issue_read_only(),
        test_next_rev_label(),
        test_edit_draft_rpl_revision(),
    ]


if __name__ == "__main__":
    results = run_all()
    raise SystemExit(0 if all(results) else 1)

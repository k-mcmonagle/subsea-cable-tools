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
                 for i in range(system_panel.views.count())] == [
                     "Table", "Schematic", "Endpoints"]
    ok = ok and system_panel.table.rowCount() == 1
    ok = ok and system_panel.table.item(0, 0).text() == "Guided segment"
    ok = ok and system_panel.table.item(0, 2).text() == "1"
    # Cable types are summed per type, not just listed.
    ok = ok and system_panel.table.item(0, 6).text().startswith("LW ")
    ok = ok and system_panel.table.item(0, 6).text().endswith(" km")
    ok = ok and "By cable type (route): LW " in system_panel.summary.text()
    ok = ok and system_panel.endpoints_table.rowCount() == 2
    ok = ok and system_panel.endpoints_table.item(0, 2).text() == "open"
    ok = ok and system_panel._schematic_args is not None
    system_panel.views.setCurrentIndex(1)
    ok = ok and system_panel._schematic_args is None
    ok = ok and not system_panel.schematic.scene().itemsBoundingRect().isEmpty()
    schematic_text = "\n".join(_graphics_text(item)
                                for item in system_panel.schematic.scene().items())
    # Open endpoints are labelled with the RPL event, with A/B as sublabel.
    ok = ok and "Start terminal" in schematic_text
    ok = ok and "End terminal" in schematic_text
    ok = ok and "Start (A) · open" in schematic_text
    ok = ok and "LW " in schematic_text and "km" in schematic_text
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
    ok = ok and "By cable type (route): LW " in segment_panel.endpoint_summary.text()
    ok = ok and segment_panel._schematic_args is not None
    segment_panel.views.setCurrentIndex(1)
    ok = ok and segment_panel._schematic_args is None
    segment_text = "\n".join(_graphics_text(item)
                              for item in segment_panel.schematic.scene().items())
    ok = ok and "Guided load" in segment_text
    ok = ok and "LW" in segment_text and "km" in segment_text
    ok = ok and "Start terminal" not in segment_text
    # The segment schematic can switch to the RPL-sections view.
    segment_panel.schematic_mode.setCurrentIndex(1)
    section_text = "\n".join(_graphics_text(item)
                              for item in segment_panel.schematic.scene().items())
    ok = ok and "Start terminal" in section_text and "End terminal" in section_text
    ok = ok and "Guided load" not in section_text
    system_panel.deleteLater()
    segment_panel.deleteLater()
    return _result("guided system/segment overviews and schematic construct", ok)


def _two_segment_system(store, system_name="Click system"):
    """Seg A (BMH West → JT-1 → BU-1) and Seg B (BU-1 → BMH East) in one system."""
    system_id = store.create_system(system_name)
    rpl_ids = []
    for name, events in (("Seg A", ("BMH West", "BU-1")),
                         ("Seg B", ("BU-1", "BMH East"))):
        points = [
            RplPoint(seq=i, pos_no=i + 1,
                     event=(events[0] if i == 0 else events[1] if i == 5
                            else "JT-1" if (i == 2 and name == "Seg A") else ""),
                     lat=50.0 + 0.01 * i, lon=0.0, depth_m=-100.0)
            for i in range(6)
        ]
        segments = [RplSegment(seq=i, slack_pct=1.0,
                               attrs={"CableType": "LW" if i < 3 else "DA"})
                    for i in range(5)]
        model = RplModel(points=points, segments=segments)
        eng.recompute(model, _da(), slack_mode=SlackMode.HOLD_SLACK)
        rpl_id = schema.new_id()
        route_id = store.create_route(name, system_id=system_id)
        rows = model_rows_for_layers(model, rpl_id, "synthetic")
        points_layer = schema.rpl_points_layer_name(name)
        lines_layer = schema.rpl_lines_layer_name(name)
        store.write_spatial_layer(points_layer, schema.RPL_POINT_FIELDS, WKB_POINT, rows["points"])
        store.write_spatial_layer(lines_layer, schema.RPL_LINE_FIELDS, WKB_LINESTRING, rows["lines"])
        store.save_rpl({
            "rpl_id": rpl_id, "name": name, "kind": "planned",
            "points_layer": points_layer, "lines_layer": lines_layer,
            "slack_mode": "hold_slack", "depth_source_config": "",
            "route_id": route_id, "rev_label": "Rev 1",
        })
        rpl_ids.append(rpl_id)
    assign_system_ids(store)
    return system_id, rpl_ids


def test_schematic_click_to_connect_and_expand() -> bool:
    """Two segments ending at BU-1: connect through the schematic, expand sections."""
    from ..workbench.system_schematic import _free_angles
    from ..workbench.topology_dialogs import build_proposals, next_node_name

    store = _temp_store()
    system_id, rpl_ids = _two_segment_system(store)

    # Per-type sums on the summary: LW over 3 legs, DA over 2 legs, both present.
    from ..workbench.rpl_summary import format_cable_type_lengths, rpl_summary
    summary = rpl_summary(store, store.get_rpl(rpl_ids[0]))
    types = [name for name, _r, _c in summary.cable_type_lengths]
    ok = types == ["LW", "DA"]
    lw_km = summary.cable_type_lengths[0][1]
    ok = ok and lw_km is not None and abs(sum(
        t[1] for t in summary.cable_type_lengths) - summary.route_length_km) < 1e-6
    ok = ok and format_cable_type_lengths(summary.cable_type_lengths).startswith("LW ")
    ok = ok and " · DA " in format_cable_type_lengths(summary.cable_type_lengths)

    panel = SystemOverviewPanel()
    panel.load_system(store, system_id)
    panel.views.setCurrentIndex(1)
    widget = panel.schematic
    ok = ok and len(widget.open_endpoints()) == 4
    text = "\n".join(_graphics_text(item) for item in widget.scene().items())
    ok = ok and "BMH West" in text and "BU-1" in text and "BMH East" in text

    # Suggestions: BU-1 shared by two open ends -> one BU node proposal;
    # the BMH ends each become a BMH node proposal.
    proposals = build_proposals(store, TopologyGraph.from_store(store), system_id)
    kinds = sorted((p["kind"], p["node_type"] if p["kind"] == "node" else "")
                   for p in proposals)
    ok = ok and kinds == [("node", "bmh"), ("node", "bmh"), ("node", "bu")]
    bu_proposal = next(p for p in proposals if p.get("node_type") == "bu")
    ok = ok and len(bu_proposal["ports"]) == 2 and bu_proposal["component_id"] is None
    ok = ok and next_node_name(store, "bu", system_id) == "BU-1"

    # Click-to-connect: arm one open segment end, click the other -> a
    # direct connection; the widget emits and the panel writes.
    graph = TopologyGraph.from_store(store)
    open_ports = graph.open_ports()
    seg_a_b = next(p for p in open_ports if p["label"] == "B"
                   and graph.components[p["component_id"]]["name"] == "Seg A")
    seg_b_a = next(p for p in open_ports if p["label"] == "A"
                   and graph.components[p["component_id"]]["name"] == "Seg B")
    widget.click_port(seg_a_b["port_id"])
    ok = ok and widget.pending_port_id() == seg_a_b["port_id"]
    widget.click_port(seg_a_b["port_id"])       # second click on the same end cancels
    ok = ok and widget.pending_port_id() == ""
    widget.click_port(seg_a_b["port_id"])
    widget.click_port(seg_b_a["port_id"])
    ok = ok and len(store.list_connections()) == 1
    ok = ok and len(TopologyGraph.from_store(store).open_ports()) == 2
    ok = ok and panel.views.currentIndex() == 1
    text = "\n".join(_graphics_text(item) for item in panel.schematic.scene().items())
    ok = ok and "BU-1" in text  # direct connection labelled with the shared event

    # Disconnect through the panel, then add a BU node via a proposal instead.
    connection_id = store.list_connections()[0]["connection_id"]
    panel.disconnect(connection_id)
    ok = ok and not store.list_connections()
    from ..workbench.system_topology import apply_proposal
    proposals = build_proposals(store, TopologyGraph.from_store(store), system_id)
    bu_proposal = next(p for p in proposals if p.get("node_type") == "bu")
    created = apply_proposal(store, bu_proposal, system_id)
    ok = ok and len(created) == 2
    graph = TopologyGraph.from_store(store)
    bu = next(c for c in graph.components.values() if c.get("node_type") == "bu")
    ok = ok and bu.get("name") == "BU-1"
    ok = ok and len(graph.ports_of(bu["component_id"])) == 2  # trunk + 1 branch
    # A spare branch shows as an open stub handle in the schematic.
    store.add_port(bu["component_id"])
    ports = [p["label"] for p in TopologyGraph.from_store(store).ports_of(bu["component_id"])]
    ok = ok and "Branch 2" in ports
    panel.load_system(store, system_id)
    panel.views.setCurrentIndex(1)
    widget = panel.schematic
    ok = ok and len(widget._ports) == 1 and widget._ports[0]["label"] == "Branch 2"
    text = "\n".join(_graphics_text(item) for item in widget.scene().items())
    ok = ok and "Branch 2" in text
    ok = ok and "BMH West" in text and "BMH East" in text

    # Expand one segment into its RPL sections, then all.
    route_a = store.component_for_segment(
        (store.get_rpl(rpl_ids[0]) or {}).get("route_id"))["subject_id"]
    widget.set_route_expanded(route_a, True)
    edge_count = len(widget._edges)
    ok = ok and edge_count == 3   # Seg A: 2 sections (JT-1 splits it) + Seg B collapsed
    text = "\n".join(_graphics_text(item) for item in widget.scene().items())
    ok = ok and "JT-1" in text
    widget.set_detail_all(True)
    ok = ok and len(widget._edges) == 3 and widget.is_route_expanded(route_a)
    widget.set_detail_all(False)
    widget.set_route_expanded(route_a, False)
    ok = ok and len(widget._edges) == 2
    # Stub angles avoid the directions already used by edges.
    angles = _free_angles([0.0], 2)
    ok = ok and len(angles) == 2 and all(abs(a) > 0.5 for a in angles)
    panel.deleteLater()
    return _result("schematic click-to-connect, suggestions, stubs and expand", ok)


def test_schematic_context_menus_build() -> bool:
    """Right-click menus for endpoints, nodes and segments build without exec."""
    from qgis.PyQt.QtWidgets import QMenu

    store = _temp_store()
    system_id, _rpl_ids = _two_segment_system(store, "Menu system")
    bu = store.save_component({"kind": "node", "name": "BU-1", "node_type": "bu",
                               "system_id": system_id}, ["Trunk", "Branch 1", "Branch 2"])
    graph = TopologyGraph.from_store(store)
    seg_a = next(c for c in graph.components.values() if c.get("name") == "Seg A")
    seg_a_b = next(p for p in graph.ports_of(seg_a["component_id"]) if p["label"] == "B")
    trunk = next(p for p in graph.ports_of(bu) if p["label"] == "Trunk")
    store.connect_ports(seg_a_b["port_id"], trunk["port_id"])
    panel = SystemOverviewPanel()
    panel.load_system(store, system_id)
    panel.views.setCurrentIndex(1)
    widget = panel.schematic
    graph = TopologyGraph.from_store(store)
    seg_a_a = next(p for p in graph.ports_of(seg_a["component_id"]) if p["label"] == "A")

    def texts(menu):
        return [a.text() for a in menu.actions()]

    open_menu = QMenu()
    widget._fill_port_menu(open_menu, seg_a_a["port_id"])
    ok = "Connect to" in texts(open_menu)
    ok = ok and "Add branching unit here…" in texts(open_menu)
    ok = ok and "Expand into RPL sections" in texts(open_menu)
    connect_sub = next(a.menu() for a in open_menu.actions() if a.text() == "Connect to")
    # Seg B's two open ends + the BU's two open branches, none from Seg A itself.
    ok = ok and len([a for a in connect_sub.actions() if a.isEnabled()]) == 4
    connected_menu = QMenu()
    widget._fill_port_menu(connected_menu, seg_a_b["port_id"])
    ok = ok and "Disconnect" in texts(connected_menu)
    node_menu = QMenu()
    widget._fill_node_menu(node_menu, bu)
    ok = ok and {"Disconnect", "Rename node…", "Add branch port", "Delete node"} <= set(texts(node_menu))
    route_menu = QMenu()
    widget.set_route_expanded(seg_a["subject_id"], True)
    widget._fill_route_menu(route_menu, seg_a["subject_id"])
    ok = ok and "Collapse to segment summary" in texts(route_menu)
    # An open endpoint's menu starts with the explicit arming action.
    ok = ok and texts(open_menu)[0].startswith("Start connection from here")
    # Pending state: arming an endpoint offers a direct "Connect to <pending>" entry.
    widget.start_connection(seg_a_a["port_id"])
    seg_b = next(c for c in graph.components.values() if c.get("name") == "Seg B")
    seg_b_a = next(p for p in graph.ports_of(seg_b["component_id"]) if p["label"] == "A")
    pending_menu = QMenu()
    widget._fill_port_menu(pending_menu, seg_b_a["port_id"])
    ok = ok and texts(pending_menu)[0].startswith("Connect to Seg A")
    widget.cancel_pending()
    ok = ok and widget.pending_port_id() == ""
    # Node action through the panel: add a port, and the stub count follows.
    panel._node_action("add_port", bu)
    graph = TopologyGraph.from_store(store)
    ok = ok and len(graph.ports_of(bu)) == 4
    ok = ok and len(panel.schematic._ports) == 3
    ok = ok and panel.endpoints_table.rowCount() == 8
    panel.deleteLater()
    return _result("schematic context menus and node actions", ok)


def test_topology_dialogs_construct() -> bool:
    """Node, connect and suggestion dialogs build headlessly and write correctly."""
    from ..workbench.topology_dialogs import (
        ConnectEndpointsDialog, NodeDialog, SuggestConnectionsDialog,
    )

    store = _temp_store()
    system_id, _rpl_ids = _two_segment_system(store, "Dialog system")
    graph = TopologyGraph.from_store(store)
    seg_a = next(c for c in graph.components.values() if c.get("name") == "Seg A")
    seg_a_b = next(p for p in graph.ports_of(seg_a["component_id"]) if p["label"] == "B")

    # Connect dialog: first list = the 4 open ends of this system; the second
    # list never offers the first endpoint's own segment.
    connect = ConnectEndpointsDialog(store, system_id, seg_a_b["port_id"])
    ok = connect.first_list.count() == 4
    ok = ok and connect.first_port_id() == seg_a_b["port_id"]
    ok = ok and connect.second_list.count() == 2
    second_labels = [connect.second_list.item(i).text() for i in range(connect.second_list.count())]
    ok = ok and all(label.startswith("Seg B") for label in second_labels)
    ok = ok and "Seg A" in connect.preview.text() and "⟷" in connect.preview.text()
    ok = ok and "“BU-1”" in connect.preview.text()
    connect.deleteLater()

    # Node dialog: auto-named BU-1, trunk + branches, connects to the chosen end.
    node = NodeDialog(store, system_id, "bu", seg_a_b["port_id"])
    ok = ok and node.name_edit.text() == "BU-1"
    ok = ok and node.port_labels() == ["Trunk", "Branch 1", "Branch 2"]
    node.branch_spin.setValue(3)
    ok = ok and node.port_labels() == ["Trunk", "Branch 1", "Branch 2", "Branch 3"]
    ok = ok and node.connect_port_id() == seg_a_b["port_id"]
    node.type_combo.setCurrentIndex(node.type_combo.findData("bmh"))
    ok = ok and node.name_edit.text() == "BMH-1" and node.port_labels() == ["Cable"]
    node.type_combo.setCurrentIndex(node.type_combo.findData("bu"))
    component_id = node.create()
    node.deleteLater()
    graph = TopologyGraph.from_store(store)
    ok = ok and graph.components[component_id]["node_type"] == "bu"
    ok = ok and len(graph.ports_of(component_id)) == 4
    ok = ok and graph.peer_component(seg_a_b["port_id"]) == component_id
    # Second BU continues the numbering.
    node2 = NodeDialog(store, system_id, "bu")
    ok = ok and node2.name_edit.text() == "BU-2"
    node2.deleteLater()

    # Suggestions now reuse the existing BU-1 for Seg B's open start and
    # propose the two BMH nodes; applying writes them all.
    suggest = SuggestConnectionsDialog(store, system_id)
    proposals = suggest.proposals()
    ok = ok and len(proposals) == 3 and suggest.list.count() == 3
    reuse = next((p for p in proposals if p.get("node_type") == "bu"), None)
    ok = ok and reuse is not None and reuse["component_id"] == component_id
    applied = suggest.apply()
    suggest.deleteLater()
    ok = ok and applied == 3
    graph = TopologyGraph.from_store(store)
    ok = ok and len(graph.open_ports()) == 2   # two spare BU branches
    ok = ok and all(p["component_id"] == component_id for p in graph.open_ports())
    ok = ok and len(graph.connected_systems()) == 1
    # Everything still belongs to the one named system after re-assignment.
    assign_system_ids(store)
    ok = ok and {c.get("system_id") for c in store.list_components()} == {system_id}
    return _result("topology dialogs construct and write nodes/connections", ok)


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
    # Dense fitted events: every dot is drawn but labels are thinned, and a
    # body event that is already an equipment node is not repeated.
    events = [{"cable_km": 0.5 + 0.02 * n, "category": "geographic", "label": f"AC{n}"}
              for n in range(30)]
    events.append({"cable_km": 4.0, "category": "body", "label": "BU-1"})
    widget.set_assembly(am.Assembly(name="Wrapped assembly", items=items), events)
    labels = [_graphics_text(item) for item in widget.scene().items()
              if _graphics_text(item)]
    ac_labels = [text for text in labels if text.startswith("AC")]
    ok = ok and 0 < len(ac_labels) < 30
    ok = ok and labels.count("BU-1") == 1
    dots = [item for item in widget.scene().items()
            if hasattr(item, "rect") and abs(item.rect().width() - 9.0) < 1e-6]
    ok = ok and len(dots) == 30
    # Toolbar buttons are children of the view, not the scrolling viewport.
    ok = ok and widget._home.parent() is widget
    widget.deleteLater()
    return _result("node-line assembly SLD supports wrapping and declutters events", ok,
                   f"labels={len(ac_labels)}/30")


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
    ok = ok and panel.segments_table.rowCount() == 0
    ok = ok and panel.sheet_table.rowCount() == 0
    panel.tabs.setCurrentIndex(1)
    ok = ok and panel.segments_table.rowCount() == 2
    ok = ok and panel.sheet_table.rowCount() == 0
    panel.tabs.setCurrentIndex(2)
    ok = ok and panel.sheet_table.rowCount() > 0
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
        test_schematic_click_to_connect_and_expand(),
        test_topology_dialogs_construct(),
        test_schematic_context_menus_build(),
        test_node_line_assembly_sld_wraps(),
        test_configurable_table_columns_persist(),
        test_assembly_review_uses_section_equipment_language(),
        test_rpl_tables_populate_lazily(),
        test_v3_adapter_contract(),
    ]


if __name__ == "__main__":
    results = run_all()
    raise SystemExit(0 if all(results) else 1)

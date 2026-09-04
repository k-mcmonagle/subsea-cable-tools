# -*- coding: utf-8 -*-
"""Pure checks for the cable-system topology helpers (no QGIS required).

Covers endpoint labelling, event-key normalisation and the connection
suggestions derived from matching RPL endpoint events.
"""

from __future__ import annotations

from ..workbench.system_topology import (
    TopologyGraph, bu_port_labels, default_port_labels, describe_proposal,
    endpoint_label, normalise_event_key, port_role, suggest_connections,
)


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


class _Classification:
    def __init__(self, body_type, is_assembly=True):
        self.body_type = body_type
        self.is_assembly = is_assembly


def _classify(text):
    lowered = text.lower()
    if lowered.startswith("bu"):
        return _Classification("bu")
    if lowered.startswith("bmh"):
        return _Classification("bmh")
    if lowered.startswith("jt"):
        return _Classification("joint")
    return _Classification("", is_assembly=False)


def _graph(with_existing_bu=False, connect=()):
    components = [
        {"component_id": "seg1", "kind": "route", "subject_id": "r1", "name": "Seg 1",
         "system_id": "sys"},
        {"component_id": "seg2", "kind": "route", "subject_id": "r2", "name": "Seg 2",
         "system_id": "sys"},
        {"component_id": "seg3", "kind": "route", "subject_id": "r3", "name": "Seg 3",
         "system_id": "sys"},
    ]
    ports = [
        {"port_id": "s1a", "component_id": "seg1", "label": "A"},
        {"port_id": "s1b", "component_id": "seg1", "label": "B"},
        {"port_id": "s2a", "component_id": "seg2", "label": "A"},
        {"port_id": "s2b", "component_id": "seg2", "label": "B"},
        {"port_id": "s3a", "component_id": "seg3", "label": "A"},
        {"port_id": "s3b", "component_id": "seg3", "label": "B"},
    ]
    if with_existing_bu:
        components.append({"component_id": "bu", "kind": "node", "node_type": "bu",
                           "name": "BU 1", "system_id": "sys"})
        ports.extend([
            {"port_id": "but", "component_id": "bu", "label": "Trunk"},
            {"port_id": "bu1", "component_id": "bu", "label": "Branch 1"},
            {"port_id": "bu2", "component_id": "bu", "label": "Branch 2"},
        ])
    connections = [
        {"connection_id": f"c{i}", "port_a_id": a, "port_b_id": b}
        for i, (a, b) in enumerate(connect)
    ]
    return TopologyGraph(components, ports, connections)


def test_labels_and_keys() -> bool:
    ok = port_role({"label": "a"}) == "A" and port_role({"label": "Trunk"}) == "Trunk"
    ok = ok and endpoint_label({"kind": "node"}, {"label": "branch_1"}) == "Branch 1"
    ok = ok and endpoint_label({"kind": "route", "name": "Seg 1"}, {"label": "B"}) == "End (B)"
    ok = ok and endpoint_label(
        {"kind": "route", "name": "Seg 1"}, {"label": "A"}, with_component=True
    ) == "Seg 1 · Start (A)"
    ok = ok and normalise_event_key("BU-1") == normalise_event_key(" bu 1 ") == "bu1"
    ok = ok and bu_port_labels(3) == ["Trunk", "Branch 1", "Branch 2", "Branch 3"]
    ok = ok and default_port_labels("bmh") == ["Cable"]
    ok = ok and default_port_labels("joint") == ["Side 1", "Side 2"]
    return _result("endpoint labels, roles and event keys", ok)


def test_suggestions_group_by_event() -> bool:
    graph = _graph()
    events = {"s1b": "BU-1", "s2a": "BU 1", "s3a": "bu-1",
              "s1a": "BMH West", "s2b": "JT-7", "s3b": "JT-7"}
    proposals = suggest_connections(graph, events, _classify, "sys")
    by_name = {p["name"]: p for p in proposals}
    ok = set(by_name) == {"BU-1", "BMH West", "JT-7"}
    bu = by_name["BU-1"]
    ok = ok and bu["kind"] == "node" and bu["node_type"] == "bu"
    ok = ok and sorted(bu["ports"]) == ["s1b", "s2a", "s3a"] and bu["component_id"] is None
    ok = ok and by_name["BMH West"]["node_type"] == "bmh" and by_name["BMH West"]["ports"] == ["s1a"]
    joint = by_name["JT-7"]
    ok = ok and joint["kind"] == "direct" and sorted(joint["ports"]) == ["s2b", "s3b"]
    # Nodes sort before direct connections; descriptions name the segment ends.
    ok = ok and [p["kind"] for p in proposals] == ["node", "node", "direct"]
    text = describe_proposal(bu, graph)
    ok = ok and text.startswith("Create Branching unit (BU)") and "Seg 1 End (B)" in text
    return _result("suggestions group open ends by event and body type", ok,
                   "; ".join(describe_proposal(p, graph) for p in proposals))


def test_suggestions_reuse_existing_node_and_skip_connected() -> bool:
    graph = _graph(with_existing_bu=True, connect=[("s1b", "but")])
    events = {"s1b": "BU-1", "s2a": "BU-1", "s3a": "BU-1", "s3b": "Lone event"}
    proposals = suggest_connections(graph, events, _classify, "sys")
    ok = len(proposals) == 1
    bu = proposals[0]
    # s1b is already connected so it is not proposed again; the existing
    # "BU 1" node (two open branches) is reused for the two remaining ends.
    ok = ok and bu["component_id"] == "bu" and sorted(bu["ports"]) == ["s2a", "s3a"]
    ok = ok and describe_proposal(bu, graph).startswith("Use existing Branching unit")
    # A single unmatched event yields nothing (no node type, no partner).
    ok = ok and not suggest_connections(graph, {"s3b": "Lone event"}, _classify, "sys")
    # Without a classifier a shared event between two ends is a direct connection.
    plain = suggest_connections(_graph(), {"s1b": "X", "s2a": "X"}, None, "sys")
    ok = ok and len(plain) == 1 and plain[0]["kind"] == "direct"
    return _result("suggestions reuse existing nodes and skip connected ends", ok)


def run_all() -> list:
    return [
        test_labels_and_keys(),
        test_suggestions_group_by_event(),
        test_suggestions_reuse_existing_node_and_skip_connected(),
    ]


if __name__ == "__main__":
    results = run_all()
    raise SystemExit(0 if all(results) else 1)

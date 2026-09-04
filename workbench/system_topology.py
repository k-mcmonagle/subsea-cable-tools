# -*- coding: utf-8 -*-
"""System topology — pure graph logic over the CRA-style core tables.

A "system" is a connected component of the port graph: cable segments
(linear, never branching), assemblies, and nodes (BMH / BU / joint) joined by
connections.
Branching lives here — a BU node exposes trunk/branch ports that connect
several RPLs — never inside an RPL itself.

Operates on plain row dicts (wb_component / wb_port / wb_connection), so it
is headless-testable; the store enforces the write-time invariants, this
module derives structure and re-validates.
"""

from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional, Sequence

# Default port labels per node type. BU ports are one trunk plus any number
# of branches (see ``bu_port_labels``); the store adds ports on demand.
NODE_TYPES: List[tuple] = [
    ("bu", "Branching unit (BU)"),
    ("bmh", "Beach manhole (BMH)"),
    ("joint", "Joint"),
    ("other", "Other node"),
]
NODE_TYPE_LABELS: Dict[str, str] = dict(NODE_TYPES)


def bu_port_labels(branch_count: int = 2) -> List[str]:
    return ["Trunk"] + [f"Branch {n}" for n in range(1, max(1, int(branch_count)) + 1)]


def default_port_labels(node_type: str, branch_count: int = 2) -> List[str]:
    return {
        "bu": bu_port_labels(branch_count),
        "joint": ["Side 1", "Side 2"],
        "bmh": ["Cable"],
    }.get(node_type or "other", ["Side 1", "Side 2"])


def is_segment_component(component: Optional[Dict]) -> bool:
    return bool(component) and component.get("kind") in ("route", "rpl")


def port_role(port: Optional[Dict]) -> str:
    """``"A"``/``"B"`` for cable-segment endpoints, else the raw label."""
    label = str((port or {}).get("label") or "").strip()
    return label.upper() if label.upper() in ("A", "B") else label


def endpoint_event(summary, port: Optional[Dict]) -> str:
    """RPL event text at a segment endpoint (start for A, end for B)."""
    if summary is None or port is None:
        return ""
    role = port_role(port)
    if role == "A":
        return str(summary.start_event or "").strip()
    if role == "B":
        return str(summary.end_event or "").strip()
    return ""


def endpoint_label(component: Optional[Dict], port: Optional[Dict],
                   summary=None, with_component: bool = False) -> str:
    """Human endpoint label shared by every topology UI.

    Segment endpoints read ``Start (A) · KP 0.000 · Pos 1 · "BMH East"``;
    node ports read their port label (``Trunk``, ``Branch 1``).
    """
    component = component or {}
    port = port or {}
    label = str(port.get("label") or "?")
    if not is_segment_component(component):
        text = label.replace("_", " ").title()
    else:
        is_start = port_role(port) == "A"
        role = "Start" if is_start else "End"
        bits = [f"{role} ({label})"]
        if summary is not None:
            kp = summary.start_kp_km if is_start else summary.end_kp_km
            pos = summary.start_pos if is_start else summary.end_pos
            event = summary.start_event if is_start else summary.end_event
            if kp is not None:
                bits.append(f"KP {kp:.3f}")
            if pos not in (None, ""):
                bits.append(f"Pos {pos}")
            if event:
                bits.append("\u201c" + str(event) + "\u201d")
        text = " · ".join(bits)
    if with_component:
        return (component.get("name") or "?") + " · " + text
    return text


def normalise_event_key(text: str) -> str:
    """Key for matching endpoint events across RPLs (``"BU 1"`` == ``"bu-1"``)."""
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def suggest_connections(graph: "TopologyGraph", endpoint_events: Dict[str, str],
                        classify: Optional[Callable[[str], object]] = None,
                        system_id: Optional[str] = None) -> List[Dict]:
    """Propose topology from matching RPL endpoint events.

    ``endpoint_events`` maps open segment port ids to the event text at that
    end of the segment's latest RPL. Ports sharing an event key become one
    proposal: a BU event (per the classifier) yields a BU node with one port
    per segment; a BMH event a BMH node; two ports of another shared event a
    direct joint connection. An existing node in the system whose name
    matches the event is reused when it still has enough open ports.
    Proposals are plain dicts so callers can list and apply them::

        {"kind": "node", "node_type": "bu", "name": "BU-1",
         "ports": [port_id, ...], "component_id": existing_or_None}
        {"kind": "direct", "name": "JT-3", "ports": [port_a, port_b]}
    """
    by_key: Dict[str, List[str]] = {}
    names: Dict[str, str] = {}
    for port_id, event in endpoint_events.items():
        port = graph.ports.get(port_id)
        if port is None or graph.connection_of_port(port_id) is not None:
            continue
        key = normalise_event_key(event)
        if not key:
            continue
        by_key.setdefault(key, []).append(port_id)
        names.setdefault(key, str(event).strip())

    existing_nodes: Dict[str, Dict] = {}
    for component in graph.components.values():
        if component.get("kind") != "node":
            continue
        if system_id is not None and (component.get("system_id") or "") != system_id:
            continue
        existing_nodes.setdefault(normalise_event_key(component.get("name")), component)

    proposals: List[Dict] = []
    for key, port_ids in by_key.items():
        name = names[key]
        body_type = ""
        if classify is not None:
            try:
                result = classify(name)
                body_type = str(getattr(result, "body_type", "") or "")
                if not getattr(result, "is_assembly", False):
                    body_type = ""
            except Exception:
                body_type = ""
        components = {graph.ports[pid].get("component_id") for pid in port_ids}
        existing = existing_nodes.get(key)
        if existing is not None:
            open_ports = graph.open_ports(existing["component_id"])
            if len(open_ports) >= len(port_ids):
                proposals.append({
                    "kind": "node", "node_type": existing.get("node_type") or "other",
                    "name": existing.get("name") or name, "ports": list(port_ids),
                    "component_id": existing["component_id"],
                })
                continue
        if body_type == "bu" and len(port_ids) >= 1:
            proposals.append({"kind": "node", "node_type": "bu", "name": name,
                              "ports": list(port_ids), "component_id": None})
        elif body_type == "bmh" and len(port_ids) == 1:
            proposals.append({"kind": "node", "node_type": "bmh", "name": name,
                              "ports": list(port_ids), "component_id": None})
        elif len(port_ids) == 2 and len(components) == 2:
            proposals.append({"kind": "direct", "name": name,
                              "ports": list(port_ids), "component_id": None})
        elif len(port_ids) > 2:
            proposals.append({"kind": "node", "node_type": "other", "name": name,
                              "ports": list(port_ids), "component_id": None})
    proposals.sort(key=lambda item: (item["kind"] != "node", item["name"].lower()))
    return proposals


def describe_proposal(proposal: Dict, graph: "TopologyGraph") -> str:
    """One-line description for proposal lists (``BU node "BU-1": Seg 1 End (B), Seg 2 Start (A)``)."""
    ends = []
    for port_id in proposal.get("ports") or []:
        port = graph.ports.get(port_id) or {}
        component = graph.components.get(port.get("component_id")) or {}
        role = port_role(port)
        role_text = {"A": "Start (A)", "B": "End (B)"}.get(role, role)
        ends.append(f"{component.get('name') or '?'} {role_text}")
    name = proposal.get("name") or ""
    if proposal.get("kind") == "direct":
        head = f"Direct connection at \u201c{name}\u201d"
    else:
        type_label = NODE_TYPE_LABELS.get(proposal.get("node_type") or "other", "Node")
        verb = "Use existing" if proposal.get("component_id") else "Create"
        head = f"{verb} {type_label} \u201c{name}\u201d"
    return head + ": " + ", ".join(ends)


def apply_proposal(store, proposal: Dict, system_id: str = "") -> List[str]:
    """Create the proposed node (if any) and its connections; returns connection ids."""
    from . import schema

    ports = list(proposal.get("ports") or [])
    created: List[str] = []
    if proposal.get("kind") == "direct":
        if len(ports) == 2:
            created.append(store.connect_ports(ports[0], ports[1]))
        return created
    component_id = proposal.get("component_id")
    node_type = proposal.get("node_type") or "other"
    if not component_id:
        branch_count = max(1, len(ports) - 1) if node_type == "bu" else 2
        labels = default_port_labels(node_type, branch_count)
        if node_type not in ("bu", "bmh") and len(ports) > len(labels):
            labels = [f"Side {n}" for n in range(1, len(ports) + 1)]
        component_id = store.save_component({
            "component_id": schema.new_id(), "kind": "node",
            "name": proposal.get("name") or node_type.upper(),
            "node_type": node_type, "system_id": system_id or "",
        }, port_labels=labels)
    graph = TopologyGraph.from_store(store)
    node_ports = graph.open_ports(component_id)
    for port_id, node_port in zip(ports, node_ports):
        created.append(store.connect_ports(port_id, node_port["port_id"]))
    return created


class TopologyGraph:
    def __init__(self, components: Sequence[Dict], ports: Sequence[Dict],
                 connections: Sequence[Dict]):
        self.components = {c["component_id"]: c for c in components if c.get("component_id")}
        self.ports = {p["port_id"]: p for p in ports if p.get("port_id")}
        self.connections = {c["connection_id"]: c for c in connections if c.get("connection_id")}

        self._ports_by_component: Dict[str, List[Dict]] = {}
        for port in self.ports.values():
            self._ports_by_component.setdefault(port.get("component_id"), []).append(port)

        self._connection_by_port: Dict[str, Dict] = {}
        for conn in self.connections.values():
            for key in ("port_a_id", "port_b_id"):
                pid = conn.get(key)
                if pid:
                    self._connection_by_port[pid] = conn

    @classmethod
    def from_store(cls, store) -> "TopologyGraph":
        return cls(store.list_components(), store.list_ports(), store.list_connections())

    # ------------------------------------------------------------- queries --
    def ports_of(self, component_id: str) -> List[Dict]:
        return sorted(self._ports_by_component.get(component_id, []),
                      key=lambda p: p.get("label") or "")

    def connection_of_port(self, port_id: str) -> Optional[Dict]:
        return self._connection_by_port.get(port_id)

    def peer_component(self, port_id: str) -> Optional[str]:
        """Component on the other side of the port's connection, if any."""
        conn = self._connection_by_port.get(port_id)
        if conn is None:
            return None
        other = conn["port_b_id"] if conn.get("port_a_id") == port_id else conn.get("port_a_id")
        port = self.ports.get(other)
        return port.get("component_id") if port else None

    def peer_port(self, port_id: str) -> Optional[Dict]:
        conn = self._connection_by_port.get(port_id)
        if conn is None:
            return None
        other = conn["port_b_id"] if conn.get("port_a_id") == port_id else conn.get("port_a_id")
        return self.ports.get(other)

    def open_ports(self, component_id: Optional[str] = None) -> List[Dict]:
        """Ports not participating in any connection (optionally per component)."""
        out = []
        for port in self.ports.values():
            if port["port_id"] in self._connection_by_port:
                continue
            if component_id is not None and port.get("component_id") != component_id:
                continue
            out.append(port)
        return sorted(out, key=lambda p: (p.get("component_id") or "", p.get("label") or ""))

    def connected_systems(self) -> List[List[str]]:
        """Connected components of the port graph (lists of component_ids).

        Isolated components form single-member systems.
        """
        adjacency: Dict[str, set] = {cid: set() for cid in self.components}
        for conn in self.connections.values():
            port_a = self.ports.get(conn.get("port_a_id"))
            port_b = self.ports.get(conn.get("port_b_id"))
            if not port_a or not port_b:
                continue
            ca, cb = port_a.get("component_id"), port_b.get("component_id")
            if ca in adjacency and cb in adjacency:
                adjacency[ca].add(cb)
                adjacency[cb].add(ca)

        seen = set()
        systems: List[List[str]] = []
        for start in sorted(adjacency):
            if start in seen:
                continue
            stack, members = [start], []
            seen.add(start)
            while stack:
                cid = stack.pop()
                members.append(cid)
                for neighbour in adjacency[cid]:
                    if neighbour not in seen:
                        seen.add(neighbour)
                        stack.append(neighbour)
            systems.append(sorted(members))
        return systems

    # ---------------------------------------------------------- validation --
    def validate(self) -> List[Dict]:
        """CRA core rules over the loaded rows (mirrors store.validate_topology)."""
        findings: List[Dict] = []

        def add(rule, severity, message, object_type, object_id):
            findings.append({"rule_id": rule, "severity": severity, "message": message,
                             "object_type": object_type, "object_id": object_id})

        for port in self.ports.values():
            if port.get("component_id") not in self.components:
                add("port.component_missing", "error",
                    f"Port {port['port_id']} references missing component "
                    f"{port.get('component_id')}.", "port", port["port_id"])

        port_use: Dict[str, int] = {}
        for conn in self.connections.values():
            cid = conn.get("connection_id") or ""
            pa, pb = conn.get("port_a_id"), conn.get("port_b_id")
            for pid in (pa, pb):
                if pid not in self.ports:
                    add("connection.port_missing", "error",
                        f"Connection {cid} references missing port {pid}.", "connection", cid)
                else:
                    port_use[pid] = port_use.get(pid, 0) + 1
            if pa in self.ports and pb in self.ports:
                if pa == pb or self.ports[pa].get("component_id") == self.ports[pb].get("component_id"):
                    add("connection.self_loop", "error",
                        f"Connection {cid} joins two ports of the same component.",
                        "connection", cid)
        for pid, count in port_use.items():
            if count > 1:
                add("connection.port_overconnected", "error",
                    f"Port {pid} participates in {count} connections.", "port", pid)
        return findings


def assign_system_ids(store) -> Dict[str, str]:
    """Derive systems and cache the assignment on wb_component.system_id.

    Reuses existing wb_system rows where any member already carried that
    system_id; creates named rows for new systems. Returns
    {component_id: system_id}.
    """
    from . import schema

    graph = TopologyGraph.from_store(store)
    systems = graph.connected_systems()
    existing_systems = {s["system_id"]: s for s in store.list_systems() if s.get("system_id")}

    assignment: Dict[str, str] = {}
    new_system_rows = []
    for members in systems:
        # keep a previously assigned id if any member has one that still exists
        system_id = None
        for cid in members:
            candidate = graph.components[cid].get("system_id")
            if candidate and candidate in existing_systems:
                system_id = candidate
                break
        if system_id is None and len(members) > 1:
            system_id = schema.new_id()
            names = [graph.components[cid].get("name") or "" for cid in members]
            label = next((n for n in names if n), "System")
            new_system_rows.append({
                "system_id": system_id,
                "name": label if len(members) == 1 else f"{label} system",
                "notes": "",
            })
        if system_id is None:
            # An isolated, explicitly unassigned component is not a cable
            # system by itself. This preserves the useful Unassigned group in
            # the Workbench until the user groups or connects it.
            system_id = ""
        for cid in members:
            assignment[cid] = system_id

    if new_system_rows:
        store.upsert_rows(schema.TABLE_SYSTEM, new_system_rows)

    components = store.list_components()
    components_changed = False
    for component in components:
        resolved = assignment.get(component.get("component_id"), "")
        if (component.get("system_id") or "") != resolved:
            component["system_id"] = resolved
            components_changed = True
    if components_changed:
        store.write_table(schema.TABLE_COMPONENT, components)
    route_systems = {
        component.get("subject_id"): component.get("system_id") or ""
        for component in components if component.get("kind") == "route"
    }
    routes = store.list_routes()
    routes_changed = False
    if routes:
        for route in routes:
            if route.get("route_id") in route_systems:
                resolved = route_systems[route.get("route_id")]
                if (route.get("system_id") or "") != resolved:
                    route["system_id"] = resolved
                    routes_changed = True
        if routes_changed:
            store.write_table(schema.TABLE_ROUTE, routes)
    return assignment

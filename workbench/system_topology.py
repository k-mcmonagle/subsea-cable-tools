# -*- coding: utf-8 -*-
"""System topology — pure graph logic over the CRA-style core tables.

A "system" is a connected component of the port graph: RPLs (linear, never
branching), assemblies, and nodes (BMH / BU / joint) joined by connections.
Branching lives here — a BU node exposes trunk/branch ports that connect
several RPLs — never inside an RPL itself.

Operates on plain row dicts (wb_component / wb_port / wb_connection), so it
is headless-testable; the store enforces the write-time invariants, this
module derives structure and re-validates.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence


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
        if system_id is None:
            system_id = schema.new_id()
            names = [graph.components[cid].get("name") or "" for cid in members]
            label = next((n for n in names if n), "System")
            new_system_rows.append({
                "system_id": system_id,
                "name": label if len(members) == 1 else f"{label} system",
                "notes": "",
            })
        for cid in members:
            assignment[cid] = system_id

    if new_system_rows:
        store.upsert_rows(schema.TABLE_SYSTEM, new_system_rows)

    components = store.list_components()
    for component in components:
        component["system_id"] = assignment.get(component.get("component_id"), "")
    store.write_table(schema.TABLE_COMPONENT, components)
    return assignment

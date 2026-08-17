# -*- coding: utf-8 -*-
"""Pan/zoom node-and-line schematic for a Workbench cable system."""

from __future__ import annotations

import math
from collections import deque

from qgis.PyQt.QtCore import QPointF, Qt, pyqtSignal
from qgis.PyQt.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen
from qgis.PyQt.QtWidgets import (
    QGraphicsPathItem, QGraphicsScene, QGraphicsView, QToolButton,
)

from . import schema
from .rpl_summary import rpl_summary
from .system_topology import TopologyGraph


class SystemSchematicWidget(QGraphicsView):
    """Cable segments render as edges; BUs, BMHs and ends render as nodes."""

    componentActivated = pyqtSignal(str, str)  # kind, subject_id

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setMinimumHeight(240)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet(
            "QGraphicsView { border:1px solid #c7ccd1; background:#fbfcfd; }")
        self._auto_fit = True
        self._detail_labels = []
        self._terminal_labels = []
        self._nodes = {}
        self._edges = []
        self._positions = {}
        self._linear = False
        self._wrapped = True
        self._home = QToolButton(self.viewport())
        self._home.setText("Home")
        self._home.setToolTip("Home — return to a readable starting view")
        self._home.setAutoRaise(True)
        self._home.clicked.connect(self.home)
        self._fit_all = QToolButton(self.viewport())
        self._fit_all.setText("Fit all")
        self._fit_all.setToolTip("Fit the complete schematic, even if labels become small")
        self._fit_all.setAutoRaise(True)
        self._fit_all.clicked.connect(self.fit_all)
        self._wrap_button = QToolButton(self.viewport())
        self._wrap_button.setText("Wrap")
        self._wrap_button.setToolTip(
            "Wrap a long schematic over several rows so labels remain readable")
        self._wrap_button.setCheckable(True)
        self._wrap_button.setChecked(True)
        self._wrap_button.setAutoRaise(True)
        self._wrap_button.toggled.connect(self.set_wrapped)
        buttons = (self._home, self._fit_all, self._wrap_button)
        for button in buttons:
            button.setStyleSheet(
                "QToolButton { background:#ffffff; border:1px solid #aeb7bf; "
                "border-radius:3px; padding:3px; } "
                "QToolButton:checked { background:#dcecf7; border-color:#4f86ad; }")
        # Let the active QGIS font/DPI determine the real button width. Fixed
        # pixel widths clipped these labels on Windows at larger text scales.
        for button, minimum_width in zip(buttons, (68, 76, 70)):
            hint = button.sizeHint()
            button.resize(max(minimum_width, hint.width() + 8),
                          max(36, hint.height() + 6))

    def set_system(self, store, system_id: str, graph=None, rpls=None) -> None:
        scene = self.scene()
        scene.clear()
        self._detail_labels = []
        self._terminal_labels = []
        self._nodes, self._edges = {}, []
        self._linear = False
        self.resetTransform()
        self._auto_fit = True
        if store is None or not system_id:
            scene.addText("No cable system selected.")
            return
        graph = graph or TopologyGraph.from_store(store)
        members = [component for component in graph.components.values()
                   if component.get("system_id") == system_id]
        if not members:
            scene.addText("This cable system has no components yet.")
            return

        rpl_rows = list(rpls) if rpls is not None else store.list_rpls()
        latest_by_route = _latest_rpls(rpl_rows)
        nodes, edges = _schematic_graph(store, graph, members, latest_by_route)
        self._nodes, self._edges = nodes, edges
        self._render_current()

    def set_wrapped(self, wrapped: bool) -> None:
        self._wrapped = bool(wrapped)
        if self._wrap_button.isChecked() != self._wrapped:
            self._wrap_button.setChecked(self._wrapped)
        if self._nodes:
            self._render_current()

    def _wrap_column_count(self) -> int:
        # Fewer, wider columns are intentional: Home should begin readable;
        # Fit all remains available when seeing the whole cable matters more.
        return max(3, min(6, int(max(self.viewport().width(), 750) / 250)))

    def _render_current(self):
        scene = self.scene()
        scene.clear()
        self._detail_labels = []
        self._terminal_labels = []
        self.resetTransform()
        self._auto_fit = True
        columns = self._wrap_column_count() if self._wrapped else 0
        if self._linear:
            positions = _layout_linear(list(self._nodes), columns)
        else:
            positions = _layout_nodes(self._nodes, self._edges, columns)
        self._positions = positions
        self._draw_edges(self._edges, positions)
        self._draw_nodes(self._nodes, positions)
        scene.setSceneRect(scene.itemsBoundingRect().adjusted(-55, -55, 55, 55))
        self.home()

    def _draw_edges(self, edges, positions):
        scene = self.scene()
        pair_counts = {}
        pair_index = {}
        for edge in edges:
            pair = tuple(sorted((edge["from"], edge["to"])))
            pair_counts[pair] = pair_counts.get(pair, 0) + 1
        for edge in edges:
            start = positions[edge["from"]]
            end = positions[edge["to"]]
            pair = tuple(sorted((edge["from"], edge["to"])))
            index = pair_index.get(pair, 0)
            pair_index[pair] = index + 1
            offset = (index - (pair_counts[pair] - 1) / 2.0) * 24.0
            path, midpoint, normal = _edge_path(start, end, offset)
            item = QGraphicsPathItem(path)
            item.setPen(QPen(QColor(edge.get("color") or "#356f9f"),
                             float(edge.get("width") or 3.2)))
            item.setZValue(1)
            kind = edge.get("kind") or "route"
            subject_id = edge.get("subject_id", edge.get("route_id") or "")
            item.setData(0, kind)
            item.setData(1, subject_id)
            item.setToolTip(edge["tooltip"])
            scene.addItem(item)

            labels = []
            if edge.get("above"):
                labels.append((scene.addSimpleText(edge["above"]), -1.0))
            if edge.get("below"):
                labels.append((scene.addSimpleText(edge["below"]), 1.0))
            for label, side in labels:
                font = label.font()
                if font.pointSizeF() > 0:
                    font.setPointSizeF(max(7.5, font.pointSizeF() - 1.5))
                    label.setFont(font)
                label.setBrush(QBrush(QColor("#263746")))
                label.setZValue(3)
                label.setData(0, kind)
                label.setData(1, subject_id)
                label.setToolTip(edge["tooltip"])
                self._detail_labels.append(label)
                box = label.boundingRect()
                half_extent = (
                    abs(normal.x()) * box.width() / 2.0
                    + abs(normal.y()) * box.height() / 2.0)
                label_pos = midpoint + normal * side * (half_extent + 8.0)
                _center_text(label, label_pos)

    def _draw_nodes(self, nodes, positions):
        scene = self.scene()
        for node_id, node in nodes.items():
            pos = positions[node_id]
            node_type = node.get("node_type") or "terminal"
            if node_type in ("bu", "bmh", "joint", "equipment"):
                fill = {
                    "bu": "#f3c969", "bmh": "#8fc7a8", "joint": "#b9c8d6",
                    "equipment": "#d6b9e8",
                }[node_type]
                radius = 10.0 if node_type in ("bu", "equipment") else 8.0
            else:
                fill, radius = "#ffffff", 7.0
            item = scene.addEllipse(
                pos.x() - radius, pos.y() - radius, radius * 2, radius * 2,
                QPen(QColor("#344955"), 1.7), QBrush(QColor(fill)))
            item.setZValue(5)
            item.setData(0, node.get("kind") or "node")
            item.setData(1, node.get("subject_id") or node_id)
            item.setToolTip(node.get("tooltip") or node.get("label") or "")
            label_text = node.get("label") or ""
            if not label_text:
                continue
            label = scene.addSimpleText(label_text)
            font = label.font()
            if font.pointSizeF() > 0:
                font.setPointSizeF(max(7.5, font.pointSizeF() - 1.0))
                label.setFont(font)
            label.setBrush(QBrush(QColor("#1f2933")))
            label.setZValue(6)
            label.setData(0, node.get("kind") or "node")
            label.setData(1, node.get("subject_id") or node_id)
            label.setToolTip(item.toolTip())
            if node_type in ("terminal", "event"):
                self._terminal_labels.append(label)
            box = label.boundingRect()
            label_gap = 22.0 if node_type in ("terminal", "event") else 3.0
            label.setPos(pos.x() - box.width() / 2.0, pos.y() + radius + label_gap)

    def home(self) -> None:
        bounds = self.scene().itemsBoundingRect() if self.scene() else None
        if bounds is not None and not bounds.isEmpty():
            self.resetTransform()
            padded = bounds.adjusted(-35, -50, 35, 50)
            visible_w = max(float(self.viewport().width()), 1.0)
            visible_h = max(float(self.viewport().height()), 1.0)
            center_x = (padded.center().x() if padded.width() <= visible_w
                        else padded.left() + visible_w / 2.0)
            center_y = (padded.center().y() if padded.height() <= visible_h
                        else padded.top() + visible_h / 2.0)
            self.centerOn(QPointF(center_x, center_y))
            self._auto_fit = True
            self._update_detail_visibility()

    def fit_all(self) -> None:
        bounds = self.scene().itemsBoundingRect() if self.scene() else None
        if bounds is not None and not bounds.isEmpty():
            self.fitInView(bounds.adjusted(-35, -50, 35, 50),
                           Qt.AspectRatioMode.KeepAspectRatio)
            self._auto_fit = False
            self._update_detail_visibility()

    def wheelEvent(self, event):
        factor = 1.2 if event.angleDelta().y() > 0 else 1.0 / 1.2
        current = abs(self.transform().m11())
        target = current * factor
        if 0.03 <= target <= 12.0:
            self.scale(factor, factor)
        self._auto_fit = False
        self._update_detail_visibility()
        event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        x = 8
        for button in (self._home, self._fit_all, self._wrap_button):
            button.move(x, 8)
            button.raise_()
            x += button.width() + 6
        if self._auto_fit:
            self.home()

    def mousePressEvent(self, event):
        self._auto_fit = False
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        item = self.itemAt(event.pos())
        while item is not None:
            kind, subject = item.data(0), item.data(1)
            if kind and subject:
                self.componentActivated.emit(str(kind), str(subject))
                return
            item = item.parentItem()
        super().mouseDoubleClickEvent(event)

    def _update_detail_visibility(self):
        show = abs(self.transform().m11()) >= 0.38
        for label in self._detail_labels:
            label.setVisible(show)
        show_terminals = abs(self.transform().m11()) >= 0.20
        for label in self._terminal_labels:
            label.setVisible(show_terminals)


class SegmentSchematicWidget(SystemSchematicWidget):
    """Ordered assembly-and-joint view of one cable segment's make-up."""

    def set_makeup(self, store, route_id: str, route_name: str = "") -> None:
        scene = self.scene()
        scene.clear()
        self._detail_labels = []
        self._terminal_labels = []
        self._nodes, self._edges = {}, []
        self._linear = True
        self.resetTransform()
        self._auto_fit = True
        header, items = store.current_makeup(route_id) if store and route_id else (None, [])
        placements = [item for item in items if item.get("kind") == "assembly"]
        if not placements:
            scene.addText("Add an assembly to this cable segment to display its make-up.")
            return
        assembly_rows = {row.get("assembly_id"): row for row in store.list_assemblies()}
        assembly_items = {}
        for row in store.read_table(schema.TABLE_ASSEMBLY_ITEM):
            assembly_items.setdefault(row.get("assembly_id") or "", []).append(row)
        nodes = {
            "makeup:start": {
                "label": "Start (A)", "node_type": "terminal",
                "kind": "route", "subject_id": route_id,
                "tooltip": f"{route_name or 'Cable segment'} start",
            }
        }
        edges = []
        current_id = "makeup:start"
        item_index = {item.get("makeup_item_id"): index for index, item in enumerate(items)}
        for placement_no, placement in enumerate(placements):
            assembly_id = placement.get("assembly_id") or ""
            assembly = assembly_rows.get(assembly_id) or {}
            sections = assembly_items.get(assembly_id, [])
            full_length = float(assembly.get("total_cable_len_m") or 0.0)
            start_m = float(placement.get("use_start_m") or 0.0)
            end_raw = placement.get("use_end_m")
            end_m = float(end_raw) if end_raw is not None else full_length
            used_length = max(0.0, end_m - start_m)
            cable_types = []
            for section in sections:
                value = str(section.get("cable_type") or "").strip()
                if value and value not in cable_types:
                    cable_types.append(value)
            source_index = item_index.get(placement.get("makeup_item_id"), -1)
            joint = next((
                item for item in items[source_index + 1:]
                if item.get("kind") in ("joint", "assembly")
            ), None)
            is_last = placement_no == len(placements) - 1
            end_id = f"makeup:end:{placement_no}"
            if not is_last and joint and joint.get("kind") == "joint":
                node_label = _joint_schematic_label(
                    joint.get("name") or f"Joint J{placement_no + 1:02d}")
                node_type = "joint"
                node_kind = "makeup_item"
                subject_id = joint.get("makeup_item_id") or ""
            else:
                node_label = "End (B)" if is_last else f"Joint J{placement_no + 1:02d}"
                node_type = "terminal" if is_last else "joint"
                node_kind = "route"
                subject_id = route_id
            nodes[end_id] = {
                "label": node_label, "node_type": node_type,
                "kind": node_kind, "subject_id": subject_id,
                "tooltip": node_label,
            }
            name = assembly.get("name") or placement.get("name") or "Assembly"
            cable_type = " / ".join(cable_types) or "Cable type not set"
            direction = "A → B" if int(placement.get("direction") or 1) >= 0 else "B → A"
            edges.append({
                "route_id": assembly_id, "from": current_id, "to": end_id,
                "above": _short(f"{name} · {cable_type}", 30),
                "below": _format_length_m(used_length),
                "tooltip": (
                    f"{name}\nCable type: {cable_type}\n"
                    f"Used length: {_format_length_m(used_length)}\nDirection: {direction}"),
                "kind": "assembly", "subject_id": assembly_id,
            })
            current_id = end_id
        self._nodes, self._edges = nodes, edges
        self._render_current()

    def set_segment(self, store, rpl, route_id: str, route_name: str = "") -> None:
        scene = self.scene()
        scene.clear()
        self._detail_labels = []
        self._terminal_labels = []
        self._nodes, self._edges = {}, []
        self._linear = True
        self.resetTransform()
        self._auto_fit = True
        summary = rpl_summary(store, rpl) if store and rpl else None
        if summary is None or not summary.sections:
            scene.addText("Import an RPL revision to display this cable segment.")
            return
        nodes = {}
        edges = []
        for index, section in enumerate(summary.sections):
            start_id, end_id = f"event:{index}", f"event:{index + 1}"
            if start_id not in nodes:
                nodes[start_id] = _section_node(
                    section.start_event, section.start_pos, index == 0,
                    route_id, "Start (A)" if index == 0 else "Event")
            nodes[end_id] = _section_node(
                section.end_event, section.end_pos,
                index + 1 == len(summary.sections), route_id,
                "End (B)" if index + 1 == len(summary.sections) else "Event")
            length = (f"{section.route_length_km:.3f} km"
                      if section.route_length_km is not None else "Length unavailable")
            cable_type = section.cable_type or "Cable type not set"
            edges.append({
                "route_id": route_id, "from": start_id, "to": end_id,
                "above": _short(cable_type, 34), "below": length,
                "tooltip": (
                    f"{route_name or 'Cable segment'}\nCable type: {cable_type}"
                    f"\nRoute length: {length}"),
            })
        self._nodes, self._edges = nodes, edges
        self._render_current()


def _schematic_graph(store, graph, members, latest_by_route):
    member_ids = {component["component_id"] for component in members}
    components = graph.components
    nodes = {}
    edges = []

    for component in members:
        if component.get("kind") != "route":
            node_id = "component:" + component["component_id"]
            node_type = component.get("node_type") or component.get("kind") or "node"
            label = component.get("name") or node_type.upper()
            nodes[node_id] = {
                "label": _short(label, 30), "node_type": node_type,
                "kind": component.get("kind") or "node",
                "subject_id": component.get("subject_id") or component["component_id"],
                "tooltip": label,
            }

    for component in members:
        if component.get("kind") != "route":
            continue
        route_id = component.get("subject_id") or ""
        rpl = latest_by_route.get(route_id)
        summary = rpl_summary(store, rpl)
        ports = graph.ports_of(component["component_id"])
        by_label = {str(port.get("label") or "").upper(): port for port in ports}
        selected = [by_label.get("A"), by_label.get("B")]
        remaining = [port for port in ports if port not in selected]
        selected = [port or (remaining.pop(0) if remaining else None) for port in selected]
        start = _endpoint_node(
            graph, components, member_ids, nodes, component, selected[0], "A", summary)
        end = _endpoint_node(
            graph, components, member_ids, nodes, component, selected[1], "B", summary)
        route_name = component.get("name") or "Cable segment"
        cable_type = summary.cable_type or "Cable type not set"
        length = (f"{summary.route_length_km:.3f} km"
                  if summary.route_length_km is not None else "Length unavailable")
        tooltip = route_name + f"\nCable type: {cable_type}\nRoute length: {length}"
        if summary.cable_length_km is not None:
            tooltip += f"\nCable length: {summary.cable_length_km:.3f} km"
        edges.append({
            "route_id": route_id, "from": start, "to": end,
            "above": _short(cable_type, 34),
            "below": length,
            "tooltip": tooltip,
        })
    return nodes, edges


def _endpoint_node(graph, components, member_ids, nodes, route_component,
                   port, role, summary):
    peer_port = graph.peer_port(port.get("port_id")) if port else None
    peer_component = components.get(peer_port.get("component_id")) if peer_port else None
    if peer_component and peer_component.get("component_id") in member_ids:
        if peer_component.get("kind") != "route":
            return "component:" + peer_component["component_id"]
        connection = graph.connection_of_port(port.get("port_id"))
        connection_id = (connection or {}).get("connection_id") or port.get("port_id")
        node_id = "connection:" + str(connection_id)
        nodes.setdefault(node_id, {
            "label": "Joint", "node_type": "joint", "kind": "node",
            "subject_id": peer_component.get("component_id"),
            "tooltip": "Direct cable-segment connection",
        })
        return node_id

    port_id = port.get("port_id") if port else f"{route_component['component_id']}:{role}"
    node_id = "terminal:" + str(port_id)
    default = "Start (A)" if role == "A" else "End (B)"
    label = default
    tooltip = default
    nodes.setdefault(node_id, {
        "label": _short(label, 28), "node_type": "terminal", "kind": "route",
        "subject_id": route_component.get("subject_id") or "", "tooltip": tooltip,
    })
    return node_id


def _section_node(event, pos, terminal, route_id, fallback):
    event = str(event or "").strip()
    label = event or fallback
    tooltip = label
    if pos not in (None, ""):
        tooltip += f"\nPosition {pos}"
    return {
        "label": _short(label, 28),
        "node_type": "terminal" if terminal else "event",
        "kind": "route", "subject_id": route_id, "tooltip": tooltip,
    }


def _latest_rpls(rows):
    grouped = {}
    for row in rows:
        route_id = row.get("route_id") or ""
        current = grouped.get(route_id)
        key = (row.get("created_utc") or "", row.get("name") or "")
        current_key = ((current or {}).get("created_utc") or "",
                       (current or {}).get("name") or "")
        if current is None or key >= current_key:
            grouped[route_id] = row
    return grouped


def _layout_nodes(nodes, edges, wrap_columns=0):
    adjacency = {node_id: set() for node_id in nodes}
    for edge in edges:
        adjacency.setdefault(edge["from"], set()).add(edge["to"])
        adjacency.setdefault(edge["to"], set()).add(edge["from"])
    roots = [node_id for node_id, node in nodes.items() if node.get("node_type") == "bmh"]
    roots.extend(node_id for node_id in nodes
                 if len(adjacency.get(node_id, ())) <= 1 and node_id not in roots)
    roots.extend(node_id for node_id in nodes if node_id not in roots)

    levels = {}
    seen = set()
    component_no = 0
    for root in roots:
        if root in seen:
            continue
        queue = deque([(root, 0)])
        seen.add(root)
        while queue:
            node_id, level = queue.popleft()
            levels.setdefault(level, []).append((component_no, node_id))
            neighbours = sorted(
                adjacency.get(node_id, ()),
                key=lambda value: (nodes.get(value, {}).get("label") or "").lower())
            for neighbour in neighbours:
                if neighbour not in seen:
                    seen.add(neighbour)
                    queue.append((neighbour, level + 1))
        component_no += 1

    positions = {}
    if wrap_columns and levels and max(levels) >= wrap_columns:
        band_sizes = {}
        for level, entries in levels.items():
            band = level // wrap_columns
            band_sizes[band] = max(band_sizes.get(band, 0), len(entries))
        band_offsets = {}
        cursor = 0.0
        for band in sorted(band_sizes):
            band_offsets[band] = cursor
            cursor += max(1, band_sizes[band] - 1) * 145.0 + 235.0
        for level, entries in levels.items():
            band = level // wrap_columns
            column = level % wrap_columns
            if band % 2:
                column = wrap_columns - 1 - column
            height = (len(entries) - 1) * 145.0
            for index, (_component, node_id) in enumerate(entries):
                positions[node_id] = QPointF(
                    column * 250.0,
                    band_offsets[band] + index * 145.0 - height / 2.0,
                )
        return positions
    for level, entries in levels.items():
        height = (len(entries) - 1) * 145.0
        for index, (_component, node_id) in enumerate(entries):
            positions[node_id] = QPointF(level * 250.0, index * 145.0 - height / 2.0)
    return positions


def _layout_linear(node_ids, wrap_columns=0):
    if not wrap_columns or len(node_ids) <= wrap_columns:
        return {node_id: QPointF(index * 250.0, 0.0)
                for index, node_id in enumerate(node_ids)}
    positions = {}
    for index, node_id in enumerate(node_ids):
        row, column = divmod(index, wrap_columns)
        if row % 2:
            column = wrap_columns - 1 - column
        positions[node_id] = QPointF(column * 250.0, row * 190.0)
    return positions


def _edge_path(start, end, offset):
    dx, dy = end.x() - start.x(), end.y() - start.y()
    length = max(math.hypot(dx, dy), 1.0)
    normal = QPointF(-dy / length, dx / length)
    midpoint = QPointF((start.x() + end.x()) / 2.0,
                       (start.y() + end.y()) / 2.0) + normal * offset
    path = QPainterPath(start)
    if abs(offset) < 0.1:
        path.lineTo(end)
    else:
        path.quadTo(midpoint, end)
    return path, midpoint, normal


def _center_text(item, point):
    box = item.boundingRect()
    item.setPos(point.x() - box.width() / 2.0, point.y() - box.height() / 2.0)


def _short(value: str, limit: int) -> str:
    value = " ".join(str(value or "").split())
    return value if len(value) <= limit else value[:limit - 1] + "…"


def _joint_schematic_label(value: str) -> str:
    """Keep physical joint nodes compact while preserving their identifier."""
    label = " ".join(str(value or "").split())
    if label.lower().startswith("joint "):
        label = label[6:].strip()
    return _short(label or "Joint", 18)


def _format_length_m(value_m) -> str:
    value_m = float(value_m or 0.0)
    return (f"{value_m / 1000.0:.3f} km"
            if value_m >= 1000.0 else f"{value_m:.1f} m")

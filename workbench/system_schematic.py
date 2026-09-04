# -*- coding: utf-8 -*-
"""Pan/zoom node-and-line schematic for a Workbench cable system.

The system schematic is also the primary place to *build* topology: open
endpoints (amber) are clickable — click one, then another, to connect them;
right-click any endpoint, node or segment for the full set of actions
(connect to…, add a BU/BMH/joint here, disconnect, rename, delete, expand a
segment into its RPL sections). The widget never writes to the store: it
emits signals and the owning panel applies them, then reloads.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Dict, List, Optional

from qgis.PyQt.QtCore import QPointF, Qt, pyqtSignal
from qgis.PyQt.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen
from qgis.PyQt.QtWidgets import (
    QGraphicsPathItem, QGraphicsScene, QGraphicsView, QMenu, QToolButton,
)

from ..qgis_compat import qt_exec
from . import schema
from .rpl_summary import format_cable_type_lengths, rpl_summary
from .system_topology import (
    TopologyGraph, endpoint_event, endpoint_label, is_segment_component,
    port_role,
)

OPEN_COLOR = "#e8a317"
OPEN_FILL = "#ffe9b3"
PENDING_COLOR = "#0a84ff"
NODE_FILLS = {
    "bu": "#f3c969", "bmh": "#8fc7a8", "joint": "#b9c8d6",
    "equipment": "#d6b9e8", "body": "#b9c8d6",
}


class SystemSchematicWidget(QGraphicsView):
    """Cable segments render as edges; BUs, BMHs and ends render as nodes."""

    componentActivated = pyqtSignal(str, str)   # kind, subject_id (double-click)
    connectRequested = pyqtSignal(str, str)     # port_a_id, port_b_id
    disconnectRequested = pyqtSignal(str)       # connection_id
    addNodeRequested = pyqtSignal(str, str)     # port_id to connect ("" = none), node_type
    nodeActionRequested = pyqtSignal(str, str)  # action (rename|delete|add_port), component_id
    statusMessage = pyqtSignal(str)

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
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setStyleSheet(
            "QGraphicsView { border:1px solid #c7ccd1; background:#fbfcfd; }")
        self._auto_fit = True
        self._detail_labels = []
        self._terminal_labels = []
        self._nodes = {}
        self._edges = []
        self._ports = []            # stub handles for open node ports
        self._positions = {}
        self._linear = False
        self._wrapped = True
        # interaction state (system schematic only)
        self._store = None
        self._system_id = ""
        self._graph: Optional[TopologyGraph] = None
        self._latest_by_route: Dict[str, Dict] = {}
        self._summaries: Dict[str, object] = {}
        self._classify = None
        self._expanded_routes = set()
        self._detail_all = False
        self._pending_port_id = ""
        self._port_index: Dict[str, Dict] = {}   # port_id -> {"component", "port", "open"}
        self._interactive = False

        # Buttons are children of the view itself, not of the viewport: the
        # viewport scrolls its child widgets along with the scene during a
        # hand-drag pan, which made the toolbar drift.
        self._home = QToolButton(self)
        self._home.setText("Home")
        self._home.setToolTip("Home — return to a readable starting view")
        self._home.setAutoRaise(True)
        self._home.clicked.connect(self.home)
        self._fit_all = QToolButton(self)
        self._fit_all.setText("Fit all")
        self._fit_all.setToolTip("Fit the complete schematic, even if labels become small")
        self._fit_all.setAutoRaise(True)
        self._fit_all.clicked.connect(self.fit_all)
        self._wrap_button = QToolButton(self)
        self._wrap_button.setText("Wrap")
        self._wrap_button.setToolTip(
            "Wrap a long schematic over several rows so labels remain readable")
        self._wrap_button.setCheckable(True)
        self._wrap_button.setChecked(True)
        self._wrap_button.setAutoRaise(True)
        self._wrap_button.toggled.connect(self.set_wrapped)
        self._detail_button = QToolButton(self)
        self._detail_button.setText("Sections")
        self._detail_button.setToolTip(
            "Expand every cable segment into its event-to-event RPL sections.\n"
            "Off: one line per segment with the cable types summed.\n"
            "Right-click a segment to expand or collapse it on its own.")
        self._detail_button.setCheckable(True)
        self._detail_button.setAutoRaise(True)
        self._detail_button.toggled.connect(self.set_detail_all)
        self._detail_button.setVisible(False)
        buttons = (self._home, self._fit_all, self._wrap_button, self._detail_button)
        for button in buttons:
            button.setStyleSheet(
                "QToolButton { background:#ffffff; border:1px solid #aeb7bf; "
                "border-radius:3px; padding:3px; } "
                "QToolButton:checked { background:#dcecf7; border-color:#4f86ad; }")
        # Let the active QGIS font/DPI determine the real button width. Fixed
        # pixel widths clipped these labels on Windows at larger text scales.
        for button, minimum_width in zip(buttons, (68, 76, 70, 84)):
            hint = button.sizeHint()
            button.resize(max(minimum_width, hint.width() + 8),
                          max(36, hint.height() + 6))

    # ----------------------------------------------------------- loading --
    def set_system(self, store, system_id: str, graph=None, rpls=None,
                   classify=None) -> None:
        scene = self.scene()
        scene.clear()
        self._detail_labels = []
        self._terminal_labels = []
        self._nodes, self._edges, self._ports = {}, [], []
        self._port_index = {}
        self._linear = False
        self._interactive = False
        self._store, self._system_id = store, system_id or ""
        self._graph = None
        self._classify = classify
        self.resetTransform()
        self._auto_fit = True
        if self._system_id != getattr(self, "_loaded_system_id", None):
            self._expanded_routes = set()
            self._pending_port_id = ""
        self._loaded_system_id = self._system_id
        self._detail_button.setVisible(True)
        self._place_toolbar()
        if store is None or not system_id:
            scene.addText("No cable system selected.")
            return
        graph = graph or TopologyGraph.from_store(store)
        self._graph = graph
        members = [component for component in graph.components.values()
                   if component.get("system_id") == system_id]
        if not members:
            scene.addText(
                "This cable system has no components yet.\n"
                "Add a cable segment (Import RPL…) or a node (Add BU / node…).")
            return

        rpl_rows = list(rpls) if rpls is not None else store.list_rpls()
        self._latest_by_route = _latest_rpls(rpl_rows)
        self._summaries = {}
        for component in members:
            if component.get("kind") == "route":
                route_id = component.get("subject_id") or ""
                self._summaries[route_id] = rpl_summary(
                    store, self._latest_by_route.get(route_id))
        self._interactive = True
        self._rebuild_graph()

    def _rebuild_graph(self):
        if self._graph is None:
            return
        members = [component for component in self._graph.components.values()
                   if component.get("system_id") == self._system_id]
        expanded = (set(self._summaries) if self._detail_all
                    else set(self._expanded_routes))
        nodes, edges, ports, port_index = _schematic_graph(
            self._graph, members, self._summaries, expanded, self._classify)
        self._nodes, self._edges, self._ports = nodes, edges, ports
        self._port_index = port_index
        if self._pending_port_id and not self.is_open_port(self._pending_port_id):
            self._pending_port_id = ""
        self._render_current()

    # ---------------------------------------------------------- options --
    def set_wrapped(self, wrapped: bool) -> None:
        self._wrapped = bool(wrapped)
        if self._wrap_button.isChecked() != self._wrapped:
            self._wrap_button.setChecked(self._wrapped)
        if self._nodes:
            self._render_current()

    def set_detail_all(self, detail: bool) -> None:
        self._detail_all = bool(detail)
        if self._detail_button.isChecked() != self._detail_all:
            self._detail_button.setChecked(self._detail_all)
        if self._interactive:
            self._rebuild_graph()

    def set_route_expanded(self, route_id: str, expanded: bool) -> None:
        if expanded:
            self._expanded_routes.add(route_id)
        else:
            self._expanded_routes.discard(route_id)
        if self._interactive:
            self._rebuild_graph()

    def is_route_expanded(self, route_id: str) -> bool:
        return self._detail_all or route_id in self._expanded_routes

    def expanded_routes(self) -> set:
        return set(self._expanded_routes)

    # ------------------------------------------------------- interaction --
    def is_open_port(self, port_id: str) -> bool:
        entry = self._port_index.get(port_id)
        return bool(entry and entry.get("open"))

    def pending_port_id(self) -> str:
        return self._pending_port_id

    def open_endpoints(self, exclude_component: str = "",
                       same_system_only: bool = True) -> List[Dict]:
        """Open ports available as connection targets, with display labels."""
        if self._graph is None:
            return []
        out = []
        for port in self._graph.open_ports():
            component = self._graph.components.get(port.get("component_id")) or {}
            if not component or component.get("component_id") == exclude_component:
                continue
            in_system = (component.get("system_id") or "") == self._system_id
            if same_system_only and not in_system:
                continue
            summary = self._summaries.get(component.get("subject_id") or "")
            if summary is None and is_segment_component(component) and self._store:
                summary = rpl_summary(
                    self._store,
                    self._latest_by_route.get(component.get("subject_id") or ""))
            out.append({
                "port_id": port["port_id"], "component_id": component.get("component_id"),
                "label": endpoint_label(component, port, summary, with_component=True),
                "in_system": in_system,
            })
        out.sort(key=lambda item: (not item["in_system"], item["label"].lower()))
        return out

    def start_connection(self, port_id: str) -> None:
        """Arm an open endpoint; the next click on another open endpoint connects."""
        if not self.is_open_port(port_id):
            return
        entry = self._port_index[port_id]
        self._pending_port_id = port_id
        self._render_current()
        label = endpoint_label(entry["component"], entry["port"],
                               entry.get("summary"), with_component=True)
        self.statusMessage.emit(
            f"Connecting from {label} — now click the other open endpoint "
            "(Esc cancels).")

    def click_port(self, port_id: str) -> None:
        """Connect flow: arms when nothing is pending, else completes."""
        if not self.is_open_port(port_id):
            return
        entry = self._port_index[port_id]
        if not self._pending_port_id:
            self.start_connection(port_id)
            return
        if port_id == self._pending_port_id:
            self.cancel_pending()
            return
        pending = self._port_index.get(self._pending_port_id) or {}
        if (pending.get("component") or {}).get("component_id") == \
                entry["component"].get("component_id"):
            self.statusMessage.emit(
                "Both endpoints belong to the same component — choose an endpoint "
                "of a different segment or node.")
            return
        first = self._pending_port_id
        self._pending_port_id = ""
        self.connectRequested.emit(first, port_id)

    def cancel_pending(self) -> None:
        if self._pending_port_id:
            self._pending_port_id = ""
            self._render_current()
            self.statusMessage.emit("Connection cancelled.")

    def _item_ref(self, item):
        while item is not None:
            kind, subject = item.data(0), item.data(1)
            if kind and subject:
                return str(kind), str(subject)
            item = item.parentItem()
        return None, None

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and self._pending_port_id:
            self.cancel_pending()
            event.accept()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        self._auto_fit = False
        if self._interactive and event.button() == Qt.MouseButton.LeftButton:
            kind, subject = self._item_ref(self.itemAt(event.pos()))
            if kind == "port" and self.is_open_port(subject):
                shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                if self._pending_port_id or shift:
                    # A plain click completes a connection that was started
                    # deliberately; Shift+click starts one. A bare click never
                    # arms, so a stray click cannot wire two endpoints.
                    self.click_port(subject)
                    event.accept()
                    return
                entry = self._port_index[subject]
                label = endpoint_label(entry["component"], entry["port"],
                                       entry.get("summary"), with_component=True)
                self.statusMessage.emit(
                    f"{label} is open. Right-click it and choose \u2018Start connection "
                    "from here\u2019 (or Shift+click), then click the other endpoint.")
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        kind, subject = self._item_ref(self.itemAt(event.pos()))
        if kind == "port":
            entry = self._port_index.get(subject) or {}
            component = entry.get("component") or {}
            if is_segment_component(component):
                self.componentActivated.emit("route", component.get("subject_id") or "")
            elif component:
                self.componentActivated.emit("node", component.get("component_id") or "")
            return
        if kind and subject:
            self.componentActivated.emit(kind, subject)
            return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event):
        if not self._interactive:
            super().contextMenuEvent(event)
            return
        kind, subject = self._item_ref(self.itemAt(event.pos()))
        menu = QMenu(self)
        if kind == "port":
            self._fill_port_menu(menu, subject)
        elif kind == "node":
            self._fill_node_menu(menu, subject)
        elif kind == "connection":
            menu.addAction("Disconnect",
                           lambda checked=False, cid=subject: self.disconnectRequested.emit(cid))
        elif kind == "route":
            self._fill_route_menu(menu, subject)
        if menu.isEmpty():
            menu.addAction("Add branching unit…",
                           lambda checked=False: self.addNodeRequested.emit("", "bu"))
            menu.addAction("Add beach manhole…",
                           lambda checked=False: self.addNodeRequested.emit("", "bmh"))
            menu.addAction("Add joint / other node…",
                           lambda checked=False: self.addNodeRequested.emit("", "other"))
            menu.addSeparator()
            action = menu.addAction("Show RPL sections for every segment")
            action.setCheckable(True)
            action.setChecked(self._detail_all)
            action.toggled.connect(self.set_detail_all)
        if self._pending_port_id:
            menu.addSeparator()
            menu.addAction("Cancel pending connection", self.cancel_pending)
        qt_exec(menu, event.globalPos())
        event.accept()

    def _fill_port_menu(self, menu: QMenu, port_id: str):
        entry = self._port_index.get(port_id)
        if entry is None:
            return
        component, port = entry["component"], entry["port"]
        component_id = component.get("component_id") or ""
        if entry.get("open"):
            if not self._pending_port_id:
                menu.addAction("Start connection from here (then click the other endpoint)",
                               lambda checked=False, pid=port_id: self.start_connection(pid))
                menu.addSeparator()
            if self._pending_port_id and self._pending_port_id != port_id:
                pending = self._port_index.get(self._pending_port_id) or {}
                label = endpoint_label(pending.get("component"), pending.get("port"),
                                       pending.get("summary"), with_component=True)
                menu.addAction(f"Connect to {label}",
                               lambda checked=False, pid=port_id: self.click_port(pid))
                menu.addSeparator()
            targets = menu.addMenu("Connect to")
            candidates = self.open_endpoints(exclude_component=component_id,
                                             same_system_only=False)
            in_system = [c for c in candidates if c["in_system"]]
            elsewhere = [c for c in candidates if not c["in_system"]]
            if not in_system:
                empty = targets.addAction("No other open endpoint in this system")
                empty.setEnabled(False)
            for candidate in in_system:
                targets.addAction(
                    candidate["label"],
                    lambda checked=False, other=candidate["port_id"], me=port_id:
                    self.connectRequested.emit(me, other))
            if elsewhere:
                other_menu = targets.addMenu("Endpoints outside this system")
                for candidate in elsewhere:
                    other_menu.addAction(
                        candidate["label"],
                        lambda checked=False, other=candidate["port_id"], me=port_id:
                        self.connectRequested.emit(me, other))
            if is_segment_component(component):
                menu.addSeparator()
                menu.addAction("Add branching unit here…",
                               lambda checked=False, pid=port_id:
                               self.addNodeRequested.emit(pid, "bu"))
                menu.addAction("Add beach manhole here…",
                               lambda checked=False, pid=port_id:
                               self.addNodeRequested.emit(pid, "bmh"))
                menu.addAction("Add joint / other node here…",
                               lambda checked=False, pid=port_id:
                               self.addNodeRequested.emit(pid, "other"))
        else:
            connection = self._graph.connection_of_port(port_id) if self._graph else None
            if connection:
                menu.addAction(
                    "Disconnect",
                    lambda checked=False, cid=connection.get("connection_id") or "":
                    self.disconnectRequested.emit(cid))
        if is_segment_component(component):
            menu.addSeparator()
            self._fill_route_menu(menu, component.get("subject_id") or "")
        elif component_id:
            menu.addSeparator()
            self._fill_node_menu(menu, component_id, ports=False)

    def _fill_node_menu(self, menu: QMenu, component_id: str, ports: bool = True):
        component = (self._graph.components.get(component_id) if self._graph else None) or {}
        if not component:
            return
        if ports and self._graph is not None:
            connected = []
            for port in self._graph.ports_of(component_id):
                connection = self._graph.connection_of_port(port["port_id"])
                if connection:
                    peer = self._graph.peer_port(port["port_id"]) or {}
                    peer_component = self._graph.components.get(peer.get("component_id")) or {}
                    text = (f"{port.get('label')} → {peer_component.get('name') or '?'} · "
                            f"{endpoint_label(peer_component, peer)}")
                    connected.append((text, connection.get("connection_id") or ""))
            if connected:
                sub = menu.addMenu("Disconnect")
                for text, connection_id in connected:
                    sub.addAction(text, lambda checked=False, cid=connection_id:
                                  self.disconnectRequested.emit(cid))
        menu.addAction("Rename node…",
                       lambda checked=False, cid=component_id:
                       self.nodeActionRequested.emit("rename", cid))
        if (component.get("node_type") or "") == "bu":
            menu.addAction("Add branch port",
                           lambda checked=False, cid=component_id:
                           self.nodeActionRequested.emit("add_port", cid))
        else:
            menu.addAction("Add port",
                           lambda checked=False, cid=component_id:
                           self.nodeActionRequested.emit("add_port", cid))
        menu.addAction("Delete node",
                       lambda checked=False, cid=component_id:
                       self.nodeActionRequested.emit("delete", cid))

    def _fill_route_menu(self, menu: QMenu, route_id: str):
        if not route_id:
            return
        menu.addAction("Open cable segment",
                       lambda checked=False, rid=route_id:
                       self.componentActivated.emit("route", rid))
        if self._detail_all:
            return
        if route_id in self._expanded_routes:
            menu.addAction("Collapse to segment summary",
                           lambda checked=False, rid=route_id:
                           self.set_route_expanded(rid, False))
        else:
            menu.addAction("Expand into RPL sections",
                           lambda checked=False, rid=route_id:
                           self.set_route_expanded(rid, True))

    # ----------------------------------------------------------- render --
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
        self._draw_port_stubs(self._ports, positions)
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
            pen = QPen(QColor(edge.get("color") or "#356f9f"),
                       float(edge.get("width") or 3.2))
            if edge.get("dashed"):
                pen.setStyle(Qt.PenStyle.DashLine)
            item.setPen(pen)
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
            port_id = node.get("port_id") or ""
            is_open = bool(node.get("open"))
            if node_type in NODE_FILLS:
                fill = NODE_FILLS[node_type]
                radius = 10.0 if node_type in ("bu", "equipment") else 8.0
                pen = QPen(QColor("#344955"), 1.7)
            elif node_type == "event":
                fill, radius = "#ffffff", 5.5
                pen = QPen(QColor("#52606d"), 1.4)
            else:
                fill, radius = "#ffffff", 7.0
                pen = QPen(QColor("#344955"), 1.7)
            if is_open:
                fill = OPEN_FILL
                pen = QPen(QColor(OPEN_COLOR), 2.0)
                radius = max(radius, 8.0)
            if port_id and port_id == self._pending_port_id:
                pen = QPen(QColor(PENDING_COLOR), 2.6)
                halo = scene.addEllipse(
                    pos.x() - radius - 6, pos.y() - radius - 6,
                    radius * 2 + 12, radius * 2 + 12,
                    QPen(QColor(PENDING_COLOR), 1.2, Qt.PenStyle.DashLine),
                    QBrush(QColor(10, 132, 255, 28)))
                halo.setZValue(4)
            item = scene.addEllipse(
                pos.x() - radius, pos.y() - radius, radius * 2, radius * 2,
                pen, QBrush(QColor(fill)))
            item.setZValue(5)
            if port_id:
                item.setData(0, "port")
                item.setData(1, port_id)
                if is_open:
                    item.setCursor(Qt.CursorShape.PointingHandCursor)
            else:
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
            label.setBrush(QBrush(QColor("#9a6b00" if is_open else "#1f2933")))
            label.setZValue(6)
            label.setData(0, item.data(0))
            label.setData(1, item.data(1))
            label.setToolTip(item.toolTip())
            if node_type in ("terminal", "event"):
                self._terminal_labels.append(label)
            box = label.boundingRect()
            label_gap = 22.0 if node_type in ("terminal", "event") else 3.0
            label.setPos(pos.x() - box.width() / 2.0, pos.y() + radius + label_gap)
            if node.get("sublabel"):
                sub = scene.addSimpleText(node["sublabel"])
                sub_font = sub.font()
                if sub_font.pointSizeF() > 0:
                    sub_font.setPointSizeF(max(7.0, sub_font.pointSizeF() - 2.0))
                    sub.setFont(sub_font)
                sub.setBrush(QBrush(QColor("#9a6b00" if is_open else "#52606d")))
                sub.setZValue(6)
                sub.setData(0, item.data(0))
                sub.setData(1, item.data(1))
                sub.setToolTip(item.toolTip())
                sub_box = sub.boundingRect()
                sub.setPos(pos.x() - sub_box.width() / 2.0,
                           label.pos().y() + box.height() + 1.0)
                self._terminal_labels.append(sub)

    def _draw_port_stubs(self, ports, positions):
        """Open ports of nodes (a spare BU branch) as short dashed stubs."""
        if not ports:
            return
        scene = self.scene()
        by_node: Dict[str, List[Dict]] = {}
        for port in ports:
            by_node.setdefault(port["node_id"], []).append(port)
        for node_id, stubs in by_node.items():
            if node_id not in positions:
                continue
            origin = positions[node_id]
            used = []
            for edge in self._edges:
                other = None
                if edge["from"] == node_id:
                    other = edge["to"]
                elif edge["to"] == node_id:
                    other = edge["from"]
                if other in positions:
                    delta = positions[other] - origin
                    used.append(math.atan2(delta.y(), delta.x()))
            angles = _free_angles(used, len(stubs))
            for port, angle in zip(stubs, angles):
                length = 70.0
                end = QPointF(origin.x() + math.cos(angle) * length,
                              origin.y() + math.sin(angle) * length)
                line = scene.addLine(origin.x(), origin.y(), end.x(), end.y(),
                                     QPen(QColor(OPEN_COLOR), 1.6, Qt.PenStyle.DashLine))
                line.setZValue(0)
                line.setData(0, "port")
                line.setData(1, port["port_id"])
                line.setToolTip(port["tooltip"])
                radius = 7.0
                pen = QPen(QColor(OPEN_COLOR), 2.0)
                if port["port_id"] == self._pending_port_id:
                    pen = QPen(QColor(PENDING_COLOR), 2.6)
                handle = scene.addEllipse(
                    end.x() - radius, end.y() - radius, radius * 2, radius * 2,
                    pen, QBrush(QColor(OPEN_FILL)))
                handle.setZValue(5)
                handle.setData(0, "port")
                handle.setData(1, port["port_id"])
                handle.setToolTip(port["tooltip"])
                handle.setCursor(Qt.CursorShape.PointingHandCursor)
                label = scene.addSimpleText(port["label"])
                font = label.font()
                if font.pointSizeF() > 0:
                    font.setPointSizeF(max(7.0, font.pointSizeF() - 2.0))
                    label.setFont(font)
                label.setBrush(QBrush(QColor("#9a6b00")))
                label.setZValue(6)
                label.setData(0, "port")
                label.setData(1, port["port_id"])
                label.setToolTip(port["tooltip"])
                box = label.boundingRect()
                label.setPos(end.x() + math.cos(angle) * 12.0 - box.width() / 2.0
                             + math.cos(angle) * box.width() / 2.0,
                             end.y() + math.sin(angle) * 12.0 - box.height() / 2.0
                             + math.sin(angle) * box.height() / 2.0)
                self._terminal_labels.append(label)

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
        self._place_toolbar()
        if self._auto_fit:
            self.home()

    def _place_toolbar(self):
        frame = self.frameWidth()
        x = frame + 8
        for button in (self._home, self._fit_all, self._wrap_button, self._detail_button):
            if button is self._detail_button and button.isHidden():
                continue
            button.move(x, frame + 8)
            button.raise_()
            x += button.width() + 6

    def scrollContentsBy(self, dx, dy):
        super().scrollContentsBy(dx, dy)
        # Belt and braces: keep the toolbar pinned even if a style scrolls
        # child widgets of the view.
        self._place_toolbar()

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
        self._nodes, self._edges, self._ports = {}, [], []
        self._linear = True
        self._interactive = False
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
            type_lengths = _assembly_type_lengths(sections)
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
            cable_type = format_cable_type_lengths(
                type_lengths, limit=3) or "Cable type not set"
            direction = "A → B" if int(placement.get("direction") or 1) >= 0 else "B → A"
            edges.append({
                "route_id": assembly_id, "from": current_id, "to": end_id,
                "above": _short(f"{name} · {cable_type}", 40),
                "below": _format_length_m(used_length),
                "tooltip": (
                    f"{name}\nCable types: {cable_type}\n"
                    f"Used length: {_format_length_m(used_length)}\nDirection: {direction}"),
                "kind": "assembly", "subject_id": assembly_id,
            })
            current_id = end_id
        self._nodes, self._edges = nodes, edges
        self._render_current()

    def set_segment(self, store, rpl, route_id: str, route_name: str = "",
                    classify=None) -> None:
        scene = self.scene()
        scene.clear()
        self._detail_labels = []
        self._terminal_labels = []
        self._nodes, self._edges, self._ports = {}, [], []
        self._linear = True
        self._interactive = False
        self.resetTransform()
        self._auto_fit = True
        summary = rpl_summary(store, rpl) if store and rpl else None
        if summary is None or not summary.sections:
            scene.addText("Import an RPL revision to display this cable segment.")
            return
        nodes, edges = _section_chain(
            summary, route_id, route_name or "Cable segment", "event", classify)
        self._nodes, self._edges = nodes, edges
        self._render_current()


# ------------------------------------------------------------- builders --
def _schematic_graph(graph, members, summaries, expanded_routes, classify=None):
    member_ids = {component["component_id"] for component in members}
    components = graph.components
    nodes: Dict[str, Dict] = {}
    edges: List[Dict] = []
    ports: List[Dict] = []
    port_index: Dict[str, Dict] = {}

    for component in members:
        if component.get("kind") == "route":
            continue
        node_id = "component:" + component["component_id"]
        node_type = component.get("node_type") or component.get("kind") or "node"
        label = component.get("name") or node_type.upper()
        component_ports = graph.ports_of(component["component_id"])
        open_ports = [p for p in component_ports if graph.connection_of_port(p["port_id"]) is None]
        tooltip = label
        if component_ports:
            tooltip += (f"\n{len(component_ports) - len(open_ports)} of "
                        f"{len(component_ports)} ports connected")
        nodes[node_id] = {
            "label": _short(label, 30), "node_type": node_type,
            "kind": "node", "subject_id": component["component_id"],
            "tooltip": tooltip + "\nRight-click for actions",
        }
        for port in component_ports:
            is_open = graph.connection_of_port(port["port_id"]) is None
            port_index[port["port_id"]] = {
                "component": component, "port": port, "open": is_open, "summary": None}
            if is_open:
                port_label = str(port.get("label") or "port").replace("_", " ").title()
                ports.append({
                    "port_id": port["port_id"], "node_id": node_id, "label": port_label,
                    "tooltip": (f"{label} · {port_label} — open\n"
                                "Right-click to start a connection (or Shift+click)"),
                })

    for component in members:
        if component.get("kind") != "route":
            continue
        route_id = component.get("subject_id") or ""
        summary = summaries.get(route_id)
        component_ports = graph.ports_of(component["component_id"])
        by_label = {port_role(port): port for port in component_ports}
        selected = [by_label.get("A"), by_label.get("B")]
        remaining = [port for port in component_ports if port not in selected]
        selected = [port or (remaining.pop(0) if remaining else None) for port in selected]
        for port in component_ports:
            port_index[port["port_id"]] = {
                "component": component, "port": port,
                "open": graph.connection_of_port(port["port_id"]) is None,
                "summary": summary}
        start = _endpoint_node(
            graph, components, member_ids, nodes, component, selected[0], "A", summary)
        end = _endpoint_node(
            graph, components, member_ids, nodes, component, selected[1], "B", summary)
        route_name = component.get("name") or "Cable segment"
        if route_id in expanded_routes and summary is not None and summary.sections:
            chain_nodes, chain_edges = _section_chain(
                summary, route_id, route_name, f"section:{route_id}:", classify,
                start_id=start, end_id=end)
            nodes.update(chain_nodes)
            edges.extend(chain_edges)
            continue
        type_text = format_cable_type_lengths(
            getattr(summary, "cable_type_lengths", ()) or ()) if summary else ""
        cable_type = type_text or (summary.cable_type if summary else "") or "Cable type not set"
        length = (f"{summary.route_length_km:.3f} km"
                  if summary is not None and summary.route_length_km is not None
                  else "Length unavailable")
        tooltip = route_name + f"\nCable types: {cable_type}\nRoute length: {length}"
        if summary is not None and summary.cable_length_km is not None:
            tooltip += f"\nCable length: {summary.cable_length_km:.3f} km"
        if summary is not None and summary.section_count:
            tooltip += (f"\n{summary.section_count} RPL section"
                        f"{'' if summary.section_count == 1 else 's'} — "
                        "right-click to expand")
        above = (format_cable_type_lengths(
            getattr(summary, "cable_type_lengths", ()) or (), limit=3)
            if summary is not None else "")
        edges.append({
            "route_id": route_id, "from": start, "to": end,
            "above": _short(above or cable_type, 44),
            "below": length,
            "tooltip": tooltip,
        })
    return nodes, edges, ports, port_index


def _section_chain(summary, route_id, route_name, id_prefix, classify=None,
                   start_id=None, end_id=None):
    """Event-to-event section chain for one RPL revision."""
    nodes: Dict[str, Dict] = {}
    edges: List[Dict] = []
    sections = list(summary.sections)
    count = len(sections)
    for index, section in enumerate(sections):
        first, last = index == 0, index + 1 == count
        a_id = start_id if (first and start_id) else f"{id_prefix}{index}"
        b_id = end_id if (last and end_id) else f"{id_prefix}{index + 1}"
        if a_id not in nodes and not (first and start_id):
            nodes[a_id] = _section_node(
                section.start_event, section.start_pos, first, route_id,
                "Start (A)" if first else "Event", classify)
        if not (last and end_id):
            nodes[b_id] = _section_node(
                section.end_event, section.end_pos, last, route_id,
                "End (B)" if last else "Event", classify)
        length = (f"{section.route_length_km:.3f} km"
                  if section.route_length_km is not None else "Length unavailable")
        type_lengths = getattr(section, "cable_type_lengths", ()) or ()
        named = [entry for entry in type_lengths if entry[0]]
        if len(named) > 1:
            cable_type = format_cable_type_lengths(type_lengths, limit=3)
        else:
            cable_type = section.cable_type or "Cable type not set"
        edges.append({
            "route_id": route_id, "from": a_id, "to": b_id,
            "above": _short(cable_type, 34), "below": length,
            "tooltip": (
                f"{route_name}\nSection {index + 1} of {count}: "
                f"{section.start_event or 'start'} → {section.end_event or 'end'}"
                f"\nCable type: {cable_type}\nRoute length: {length}"),
            "kind": "route", "subject_id": route_id,
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
        event = endpoint_event(summary, port)
        nodes.setdefault(node_id, {
            "label": _short(event or "Joint", 24), "node_type": "joint", "kind": "connection",
            "subject_id": str(connection_id),
            "tooltip": (f"Direct cable-segment connection"
                        + (f" at “{event}”" if event else "")
                        + "\nRight-click to disconnect"),
        })
        return node_id

    port_id = port.get("port_id") if port else f"{route_component['component_id']}:{role}"
    node_id = "terminal:" + str(port_id)
    default = "Start (A)" if role == "A" else "End (B)"
    event = endpoint_event(summary, port)
    label = event or default
    detail = endpoint_label(route_component, port, summary) if port else default
    tooltip = f"{route_component.get('name') or 'Cable segment'} · {detail}"
    if port:
        tooltip += ("\nOpen endpoint — right-click to start a connection "
                    "(or Shift+click), then click the other endpoint")
    nodes.setdefault(node_id, {
        "label": _short(label, 28), "node_type": "terminal", "kind": "route",
        "subject_id": route_component.get("subject_id") or "", "tooltip": tooltip,
        "port_id": port.get("port_id") if port else "", "open": bool(port),
        "sublabel": f"{default} · open" if event else "open",
    })
    return node_id


def _section_node(event, pos, terminal, route_id, fallback, classify=None):
    event = str(event or "").strip()
    label = event or fallback
    tooltip = label
    if pos not in (None, ""):
        tooltip += f"\nPosition {pos}"
    node_type = "terminal" if terminal else "event"
    if not terminal and event and classify is not None:
        try:
            result = classify(event)
            if getattr(result, "is_assembly", False):
                node_type = "body"
                body_type = getattr(result, "body_type", "") or ""
                if body_type in ("bu", "bmh", "joint"):
                    node_type = body_type
                tooltip += f"\nAssembly body ({body_type or 'body'})"
        except Exception:
            pass
    return {
        "label": _short(label, 28),
        "node_type": node_type,
        "kind": "route", "subject_id": route_id, "tooltip": tooltip,
    }


def _assembly_type_lengths(sections):
    """Per-cable-type section lengths of an assembly as (type, km, km)."""
    order, totals = [], {}
    for section in sections:
        if (section.get("kind") or "section") != "section":
            continue
        name = str(section.get("cable_type") or "").strip()
        try:
            length_km = float(section.get("length_m") or 0.0) / 1000.0
        except (TypeError, ValueError):
            length_km = 0.0
        if name not in totals:
            order.append(name)
            totals[name] = 0.0
        totals[name] += length_km
    return tuple((name, totals[name], totals[name]) for name in order if name)


def _latest_rpls(rows):
    grouped = {}
    for row in rows:
        route_id = row.get("route_id") or ""
        current = grouped.get(route_id)
        if current is None or schema.revision_sort_key(row) >= schema.revision_sort_key(current):
            grouped[route_id] = row
    return grouped


# ---------------------------------------------------------------- layout --
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


def _free_angles(used, count):
    """Pick ``count`` stub directions far from the node's existing edges."""
    candidates = [math.radians(a) for a in (90, 270, 45, 135, 225, 315, 0, 180)]
    chosen = []
    for _ in range(count):
        best, best_score = None, -1.0
        for angle in candidates:
            if angle in chosen:
                continue
            score = min([_angle_gap(angle, other) for other in used + chosen] or [math.pi])
            if score > best_score:
                best, best_score = angle, score
        if best is None:
            best = math.radians(90 + 30 * len(chosen))
        chosen.append(best)
    return chosen


def _angle_gap(a, b):
    diff = abs(a - b) % (2 * math.pi)
    return min(diff, 2 * math.pi - diff)


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

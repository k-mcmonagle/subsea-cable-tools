# -*- coding: utf-8 -*-
"""Node-and-line assembly schematic with the legacy SLD interaction API."""

from __future__ import annotations

import math
from typing import Dict, List, Optional

from qgis.PyQt.QtCore import QPointF, Qt, pyqtSignal
from qgis.PyQt.QtGui import QBrush, QColor, QPen

from .assembly_model import Assembly
from .system_schematic import SystemSchematicWidget, _center_text


class SldWidget(SystemSchematicWidget):
    """Render assembly sections as labelled lines and equipment as nodes.

    The public signals/methods intentionally match the former bar plot so map,
    table and fit selection continue to work without a second visual model.
    """

    itemClicked = pyqtSignal(int)
    cableDistClicked = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._linear = True
        self._assembly: Optional[Assembly] = None
        self._events: List[Dict] = []
        self._distance_spans = []
        self._kp_mapping = None
        self._highlight_index = None
        self._marked_cable_m = None
        self._marker_item = None
        self._home.setToolTip("Home — fit the complete assembly schematic")

    def set_assembly(self, assembly: Optional[Assembly],
                     events: Optional[List[Dict]] = None):
        self._assembly = assembly
        self._events = list(events or [])
        self._nodes, self._edges = {}, []
        self._distance_spans = []
        self._linear = True
        if assembly is None or not assembly.items:
            self.scene().clear()
            self.scene().addText("This assembly has no sections or equipment yet.")
            return

        cursor_m = 0.0
        current_id = "boundary:0"
        self._nodes[current_id] = _boundary_node("Start")
        boundary_no = 0
        for item_index, item in enumerate(assembly.items):
            if item.is_section:
                boundary_no += 1
                next_id = f"boundary:{boundary_no}"
                self._nodes[next_id] = _boundary_node("")
                length_m = max(float(item.length_m or 0.0), 0.0)
                end_m = cursor_m + length_m
                cable_type = item.cable_type or item.name or "Cable section"
                tooltip = (
                    f"{item.name or 'Assembly section'}\n"
                    f"Cable type: {cable_type}\nLength: {_length(length_m)}")
                self._edges.append({
                    "route_id": str(item_index), "from": current_id, "to": next_id,
                    "above": cable_type, "below": _length(length_m),
                    "tooltip": tooltip, "kind": "assembly_item",
                    "subject_id": str(item_index),
                    "color": item.color_hex or "#356f9f", "width": 3.2,
                    "start_m": cursor_m, "end_m": end_m,
                })
                self._distance_spans.append((cursor_m, end_m, current_id, next_id))
                cursor_m = end_m
                current_id = next_id
            else:
                node_id = f"equipment:{item_index}"
                name = item.name or "Equipment"
                tooltip = name
                if item.point_load_kN is not None:
                    tooltip += f"\nPoint load: {float(item.point_load_kN):.3g} kN"
                tooltip += f"\nCable distance: {_length(cursor_m)}"
                self._nodes[node_id] = {
                    "label": name, "node_type": "equipment",
                    "kind": "assembly_item", "subject_id": str(item_index),
                    "tooltip": tooltip,
                }
                self._edges.append({
                    "route_id": str(item_index), "from": current_id, "to": node_id,
                    "above": "", "below": "", "tooltip": tooltip,
                    "kind": "assembly_item", "subject_id": str(item_index),
                    "color": "#9aa8b4", "width": 1.5,
                })
                current_id = node_id

        if current_id.startswith("boundary:"):
            self._nodes[current_id]["label"] = "End"
            self._nodes[current_id]["node_type"] = "terminal"
        else:
            boundary_no += 1
            end_id = f"boundary:{boundary_no}"
            self._nodes[end_id] = _boundary_node("End")
            self._edges.append({
                "route_id": "end", "from": current_id, "to": end_id,
                "above": "", "below": "", "tooltip": "Assembly end",
                "kind": "assembly_boundary", "subject_id": "end",
                "color": "#9aa8b4", "width": 1.5,
            })
        self._render_current()

    def _render_current(self):
        # The base renderer clears the scene, which also deletes any previous
        # marker graphics item.
        self._marker_item = None
        super()._render_current()
        self._draw_route_events()
        self._draw_marker()
        self._apply_highlight()
        self.scene().setSceneRect(
            self.scene().itemsBoundingRect().adjusted(-55, -55, 55, 55))
        self.home()

    def _draw_route_events(self):
        for event in self._events:
            cable_km = event.get("cable_km")
            if cable_km is None:
                continue
            point = self._point_at_cable(float(cable_km) * 1000.0)
            if point is None:
                continue
            color = {
                "geographic": "#cc6677", "body": "#117733",
                "both": "#6f42c1",
                "installation": "#cc6677",  # legacy category reads as geographic
            }.get(event.get("category") or "geographic", "#cc6677")
            marker = self.scene().addEllipse(
                point.x() - 4.5, point.y() - 4.5, 9.0, 9.0,
                QPen(QColor(color), 1.2), QBrush(QColor(color)))
            marker.setZValue(7)
            label_text = str(event.get("label") or "Event")
            label = self.scene().addSimpleText(label_text)
            label.setBrush(QBrush(QColor("#52606d")))
            label.setZValue(7)
            _center_text(label, point + QPointF(0.0, 34.0))
            tooltip = f"{label_text}\nCable distance: {float(cable_km):.3f} km"
            marker.setToolTip(tooltip)
            label.setToolTip(tooltip)
            self._detail_labels.append(label)

    def set_kp_mapping(self, mapping):
        """Keep the fit mapping for marker tooltips; no plot axis is needed."""
        self._kp_mapping = mapping
        self._draw_marker()

    def highlight_item(self, item_index: Optional[int]):
        self._highlight_index = item_index
        self._apply_highlight()

    def _apply_highlight(self):
        if self.scene() is None:
            return
        for item in self.scene().items():
            if item.data(0) == "assembly_item":
                selected = (self._highlight_index is None
                            or str(item.data(1)) == str(self._highlight_index))
                item.setOpacity(1.0 if selected else 0.28)

    def mark_cable_dist(self, cable_m: Optional[float]):
        self._marked_cable_m = cable_m
        self._draw_marker()

    def _draw_marker(self):
        if self._marker_item is not None and self._marker_item.scene() is self.scene():
            self.scene().removeItem(self._marker_item)
        self._marker_item = None
        if self._marked_cable_m is None or not self._positions:
            return
        point = self._point_at_cable(float(self._marked_cable_m))
        if point is None:
            return
        marker = self.scene().addEllipse(
            point.x() - 7.0, point.y() - 7.0, 14.0, 14.0,
            QPen(QColor("#00aaff"), 2.2), QBrush(QColor(0, 170, 255, 45)))
        marker.setZValue(9)
        tooltip = f"Cable distance: {_length(float(self._marked_cable_m))}"
        if self._kp_mapping is not None:
            try:
                kp = self._kp_mapping(float(self._marked_cable_m))
            except Exception:
                kp = None
            if kp is not None:
                tooltip += f"\nRoute KP: {float(kp):.3f} km"
        marker.setToolTip(tooltip)
        self._marker_item = marker

    def _point_at_cable(self, cable_m: float):
        if not self._distance_spans:
            return next(iter(self._positions.values()), None)
        cable_m = max(self._distance_spans[0][0],
                      min(cable_m, self._distance_spans[-1][1]))
        for start_m, end_m, start_id, end_id in self._distance_spans:
            if start_m <= cable_m <= end_m or end_m == self._distance_spans[-1][1]:
                start, end = self._positions[start_id], self._positions[end_id]
                fraction = 0.0 if end_m <= start_m else (cable_m - start_m) / (end_m - start_m)
                return start + (end - start) * max(0.0, min(1.0, fraction))
        return None

    def _cable_at_point(self, point):
        best = None
        for start_m, end_m, start_id, end_id in self._distance_spans:
            start, end = self._positions[start_id], self._positions[end_id]
            fraction, distance = _project_fraction(point, start, end)
            if best is None or distance < best[0]:
                best = (distance, start_m + fraction * (end_m - start_m))
        return best[1] if best is not None else None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            item = self.itemAt(event.pos())
            if item is not None and item.data(0) == "assembly_item":
                try:
                    self.itemClicked.emit(int(item.data(1)))
                except (TypeError, ValueError):
                    pass
            cable_m = self._cable_at_point(self.mapToScene(event.pos()))
            if cable_m is not None:
                self.cableDistClicked.emit(float(cable_m))
        super().mousePressEvent(event)


def _boundary_node(label):
    return {
        "label": label, "node_type": "terminal" if label else "joint",
        "kind": "assembly_boundary", "subject_id": label.lower() if label else "boundary",
        "tooltip": label or "Assembly section boundary",
    }


def _length(value_m):
    value_m = float(value_m or 0.0)
    return f"{value_m / 1000.0:.3f} km" if value_m >= 1000.0 else f"{value_m:.1f} m"


def _project_fraction(point, start, end):
    dx, dy = end.x() - start.x(), end.y() - start.y()
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-12:
        return 0.0, math.hypot(point.x() - start.x(), point.y() - start.y())
    fraction = ((point.x() - start.x()) * dx + (point.y() - start.y()) * dy) / length_sq
    fraction = max(0.0, min(1.0, fraction))
    projected = QPointF(start.x() + fraction * dx, start.y() + fraction * dy)
    return fraction, math.hypot(point.x() - projected.x(), point.y() - projected.y())

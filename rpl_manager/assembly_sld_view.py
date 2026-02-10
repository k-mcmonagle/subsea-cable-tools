from __future__ import annotations

from typing import Dict, List, Optional

from qgis.PyQt.QtCore import Qt, QPointF
from qgis.PyQt.QtGui import QBrush, QFont, QPainter, QPainterPath, QPen, QPolygonF
from qgis.PyQt.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsLineItem,
    QGraphicsPathItem,
    QGraphicsPolygonItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsTextItem,
    QGraphicsView,
    QVBoxLayout,
    QWidget,
)


class _AssemblySldView(QWidget):
    """Straight line diagram for the RPL Manager assembly table.

    Renders BODY rows as nodes and SEGMENT rows as links between nodes.
    Distances are based on the assembly table's cable length (km), not KP/geography.
    """

    def __init__(self, parent=None):
        super().__init__(parent)

        self._rows: List[Dict[str, object]] = []
        self._layout_mode: str = "wrap"  # "wrap" or "single"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._scene = QGraphicsScene(self)
        self._view = _AssemblySldGraphicsView(self._scene, self)
        self._view.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        self._view.setViewportUpdateMode(QGraphicsView.FullViewportUpdate)

        layout.addWidget(self._view, 1)

    def set_layout_mode(self, mode: str):
        mode_n = (mode or "").strip().lower()
        mode_eff = "wrap" if mode_n in ("wrap", "wrapped") else "single"
        if self._layout_mode == mode_eff:
            return
        self._layout_mode = mode_eff
        self._redraw()

    @staticmethod
    def _try_float(v) -> Optional[float]:
        if v in (None, ""):
            return None
        if isinstance(v, (int, float)):
            try:
                return float(v)
            except Exception:
                return None
        try:
            s = str(v).strip().replace(",", "")
            return float(s) if s else None
        except Exception:
            return None

    @staticmethod
    def _fmt_km(v: Optional[float]) -> str:
        if v is None:
            return ""
        if abs(v) >= 100:
            return f"{v:.1f} km"
        if abs(v) >= 10:
            return f"{v:.2f} km"
        return f"{v:.3f} km"

    @staticmethod
    def _wrap_label(text: str, max_chars: int = 22, max_lines: int = 2) -> str:
        s = (text or "").strip()
        if not s:
            return ""
        if len(s) <= max_chars:
            return s

        words = s.split()
        if len(words) <= 1:
            lines = [s[i : i + max_chars] for i in range(0, len(s), max_chars)]
            return "\n".join(lines[:max_lines])

        lines: List[str] = []
        cur = ""
        for w in words:
            if not cur:
                cur = w
                continue
            if len(cur) + 1 + len(w) <= max_chars:
                cur = f"{cur} {w}"
            else:
                lines.append(cur)
                cur = w
                if len(lines) >= max_lines:
                    break
        if len(lines) < max_lines and cur:
            lines.append(cur)
        joined = "\n".join(lines[:max_lines])
        if len(joined.replace("\n", " ")) < len(s) and not joined.endswith("…"):
            joined = joined.rstrip(".")
            if not joined.endswith("…"):
                joined = joined + "…"
        return joined

    @staticmethod
    def _body_kind(label: str) -> str:
        s = (label or "").lower()
        if "repeater" in s:
            return "repeater"
        if "joint" in s or "splice" in s:
            return "joint"
        if "transition" in s or "tj" in s:
            return "transition"
        return "body"

    @staticmethod
    def _poly(points: List[tuple[float, float]]) -> QPolygonF:
        return QPolygonF([QPointF(float(x), float(y)) for (x, y) in points])

    @staticmethod
    def _polyline_midpoint(points: List[tuple[float, float]]) -> tuple[float, float]:
        """Return the point half-way along a polyline.

        This is better than picking the middle vertex when the polyline has
        wrap corners; it keeps labels visually centered on the segment.
        """

        if not points:
            return (0.0, 0.0)
        if len(points) == 1:
            return (float(points[0][0]), float(points[0][1]))

        # Total length
        seg_lens: List[float] = []
        total = 0.0
        for (x1, y1), (x2, y2) in zip(points[:-1], points[1:]):
            dx = float(x2) - float(x1)
            dy = float(y2) - float(y1)
            L = (dx * dx + dy * dy) ** 0.5
            seg_lens.append(L)
            total += L

        if total <= 0.0:
            return (float(points[0][0]), float(points[0][1]))

        half = total / 2.0
        acc = 0.0
        for idx, L in enumerate(seg_lens):
            if acc + L >= half and L > 0.0:
                t = (half - acc) / L
                x1, y1 = points[idx]
                x2, y2 = points[idx + 1]
                return (float(x1) + (float(x2) - float(x1)) * t, float(y1) + (float(y2) - float(y1)) * t)
            acc += L

        return (float(points[-1][0]), float(points[-1][1]))

    @staticmethod
    def _polyline_midpoint_and_dir(points: List[tuple[float, float]]) -> tuple[float, float, float, float]:
        """Return (x, y, dx, dy) at the polyline half-length.

        dx/dy are the direction of the segment containing the midpoint.
        """

        if not points:
            return (0.0, 0.0, 1.0, 0.0)
        if len(points) == 1:
            return (float(points[0][0]), float(points[0][1]), 1.0, 0.0)

        seg_lens: List[float] = []
        total = 0.0
        for (x1, y1), (x2, y2) in zip(points[:-1], points[1:]):
            dx = float(x2) - float(x1)
            dy = float(y2) - float(y1)
            L = (dx * dx + dy * dy) ** 0.5
            seg_lens.append(L)
            total += L

        if total <= 0.0:
            return (float(points[0][0]), float(points[0][1]), 1.0, 0.0)

        half = total / 2.0
        acc = 0.0
        for idx, L in enumerate(seg_lens):
            x1, y1 = points[idx]
            x2, y2 = points[idx + 1]
            dx = float(x2) - float(x1)
            dy = float(y2) - float(y1)
            if acc + L >= half and L > 0.0:
                t = (half - acc) / L
                return (float(x1) + dx * t, float(y1) + dy * t, dx, dy)
            acc += L

        x1, y1 = points[-2]
        x2, y2 = points[-1]
        dx = float(x2) - float(x1)
        dy = float(y2) - float(y1)
        return (float(points[-1][0]), float(points[-1][1]), dx, dy)

    def set_rows(self, rows: List[Dict[str, object]]):
        self._rows = list(rows or [])
        self._redraw()

    def resizeEvent(self, event):
        try:
            if self._layout_mode == "wrap":
                self._redraw()
        except Exception:
            pass
        super().resizeEvent(event)

    def _redraw(self):
        self._scene.clear()

        nodes: List[Dict[str, object]] = []
        edges: List[Dict[str, object]] = []
        pending_seg: Optional[Dict[str, object]] = None

        for row in self._rows:
            rt = str(row.get("row_type") or "").strip().upper()
            if rt == "BODY":
                label = str(row.get("label") or "").strip() or "Body"
                nodes.append({"label": label})
                if pending_seg is not None and len(nodes) >= 2:
                    edges.append({
                        "from_idx": len(nodes) - 2,
                        "to_idx": len(nodes) - 1,
                        "cable_type": str(pending_seg.get("cable_type") or "").strip(),
                        "cable_len_km": self._try_float(pending_seg.get("cable_len")),
                    })
                    pending_seg = None
            elif rt == "SEGMENT":
                pending_seg = row

        if len(nodes) == 0:
            return

        viewport_w = 0
        try:
            viewport_w = int(self._view.viewport().width())
        except Exception:
            viewport_w = 0

        max_row_w = max(900, viewport_w - 60) if viewport_w > 0 else 1400
        seg_dx = 220.0
        row_h = 220.0
        y0 = 0.0

        node_pts: List[tuple[float, float]] = [(0.0, y0)] * len(nodes)
        edge_paths: List[Dict[str, object]] = []

        x = 0.0
        y = y0
        direction = 1

        node_pts[0] = (x, y)
        for i in range(0, len(nodes) - 1):
            seg_len_km: Optional[float] = None
            cable_type = ""
            for e in edges:
                if e.get("from_idx") == i and e.get("to_idx") == i + 1:
                    seg_len_km = self._try_float(e.get("cable_len_km"))
                    cable_type = str(e.get("cable_type") or "").strip()
                    break

            dx = seg_dx

            x_next = x + direction * dx
            points: List[tuple[float, float]] = [(x, y)]

            if self._layout_mode == "wrap" and direction == 1 and x_next > max_row_w:
                overflow = x_next - max_row_w
                y_next = y + row_h
                direction = -1
                x_next = max_row_w - overflow
                points.extend([(max_row_w, y), (max_row_w, y_next), (x_next, y_next)])
                y = y_next
            elif self._layout_mode == "wrap" and direction == -1 and x_next < 0.0:
                overflow = -x_next
                y_next = y + row_h
                direction = 1
                x_next = overflow
                points.extend([(0.0, y), (0.0, y_next), (x_next, y_next)])
                y = y_next
            else:
                if self._layout_mode != "wrap":
                    direction = 1
                points.append((x_next, y))

            edge_paths.append({
                "from_idx": i,
                "to_idx": i + 1,
                "points": points,
                "cable_type": cable_type,
                "cable_len_km": seg_len_km,
            })

            x = x_next
            node_pts[i + 1] = (x, y)

        # Styles
        bg = QBrush(Qt.white)
        self._scene.setBackgroundBrush(bg)

        pen_line = QPen(Qt.black)
        pen_line.setWidthF(2.0)

        pen_node = QPen(Qt.black)
        pen_node.setWidthF(2.0)

        font = QFont()
        font.setPointSize(9)

        def node_brush(kind: str) -> QBrush:
            k = (kind or "").lower()
            if k == "repeater":
                return QBrush(Qt.yellow)
            if k == "joint":
                return QBrush(Qt.cyan)
            if k == "transition":
                return QBrush(Qt.magenta)
            return QBrush(Qt.lightGray)

        # Draw edges
        for e in edge_paths:
            pts = e.get("points") or []
            if len(pts) < 2:
                continue
            path = QPainterPath(QPointF(pts[0][0], pts[0][1]))
            for (px, py) in pts[1:]:
                path.lineTo(QPointF(px, py))
            item = QGraphicsPathItem(path)
            item.setPen(pen_line)
            self._scene.addItem(item)

            mid_x, mid_y, dir_x, dir_y = self._polyline_midpoint_and_dir(list(pts))

            # In wrapped mode the midpoint may land on a vertical segment (wrap corner).
            # To avoid overlapping the line/nodes, place labels above/below for mostly
            # horizontal segments, or left/right for mostly vertical segments.
            clen = e.get("cable_len_km")
            ctype = str(e.get("cable_type") or "").strip()

            label_gap = 4.0
            label_pad = 2.0

            def add_label(text: str, center_x: float, center_y: float):
                if not text:
                    return
                t = QGraphicsTextItem(text)
                t.setFont(font)
                t.setDefaultTextColor(Qt.darkBlue)
                br = t.boundingRect()

                # Background box (improves legibility when labels cross other lines)
                bg_rect = QGraphicsRectItem(0, 0, br.width() + 2 * label_pad, br.height() + 2 * label_pad)
                bg_rect.setPen(QPen(Qt.NoPen))
                bg_rect.setBrush(QBrush(Qt.white))

                x0 = center_x - br.width() / 2.0
                y0 = center_y - br.height() / 2.0
                t.setPos(QPointF(x0, y0))
                bg_rect.setPos(QPointF(x0 - label_pad, y0 - label_pad))

                # Keep labels on top of lines/nodes
                bg_rect.setZValue(5)
                t.setZValue(6)
                self._scene.addItem(bg_rect)
                self._scene.addItem(t)

            is_horizontal = abs(float(dir_x)) >= abs(float(dir_y))

            if ctype:
                # For horizontal segments, keep type above; for vertical, keep it left.
                tmp = QGraphicsTextItem(ctype)
                tmp.setFont(font)
                br = tmp.boundingRect()
                if is_horizontal:
                    cy = mid_y - (br.height() / 2.0 + label_gap)
                    add_label(ctype, mid_x, cy)
                else:
                    cx = mid_x - (br.width() / 2.0 + label_gap)
                    add_label(ctype, cx, mid_y)

            if clen is not None:
                len_txt = self._fmt_km(self._try_float(clen))
                if len_txt:
                    tmp = QGraphicsTextItem(len_txt)
                    tmp.setFont(font)
                    br = tmp.boundingRect()
                    if is_horizontal:
                        cy = mid_y + (br.height() / 2.0 + label_gap)
                        add_label(len_txt, mid_x, cy)
                    else:
                        cx = mid_x + (br.width() / 2.0 + label_gap)
                        add_label(len_txt, cx, mid_y)

        # Draw nodes
        r = 16.0
        for i, n in enumerate(nodes):
            x, y = node_pts[i]
            kind = self._body_kind(str(n.get("label") or ""))
            ell = QGraphicsEllipseItem(x - r, y - r, 2 * r, 2 * r)
            ell.setPen(pen_node)
            ell.setBrush(node_brush(kind))
            self._scene.addItem(ell)

            label = self._wrap_label(str(n.get("label") or ""))
            if label:
                t = QGraphicsTextItem(label)
                t.setFont(font)
                t.setDefaultTextColor(Qt.black)
                br = t.boundingRect()
                t.setPos(QPointF(x - br.width() / 2.0, y + r + 6.0))
                self._scene.addItem(t)

        try:
            rect = self._scene.itemsBoundingRect()
            if rect.isValid():
                rect = rect.adjusted(-80.0, -80.0, 80.0, 80.0)
                self._scene.setSceneRect(rect)
        except Exception:
            pass


class _AssemblySldGraphicsView(QGraphicsView):
    """QGraphicsView with simple zoom + pan for the assembly SLD."""

    def __init__(self, scene, parent=None):
        super().__init__(scene, parent)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorUnderMouse)

    def wheelEvent(self, event):
        try:
            if event.modifiers() & Qt.ControlModifier:
                delta = event.angleDelta().y()
                if delta == 0:
                    return
                factor = 1.15 if delta > 0 else 1.0 / 1.15
                self.scale(factor, factor)
                event.accept()
                return
        except Exception:
            pass
        super().wheelEvent(event)

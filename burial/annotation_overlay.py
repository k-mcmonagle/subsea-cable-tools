# -*- coding: utf-8 -*-
"""Bounded, viewport-aware plain-text overlay using the existing Qt plot stack."""

import bisect
import html
import math

import pyqtgraph as pg
from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QColor, QFont


class AnnotationOverlay(QObject):
    noticeChanged = pyqtSignal(str)
    MAX_VISIBLE = 150
    MAX_CANDIDATES = 600

    def __init__(self, plot):
        super().__init__(plot)
        self.plot = plot
        self.entries, self.starts, self.items = [], [], []
        self.extent = (0.0, 0.0)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self.redraw)
        plot.getViewBox().sigRangeChanged.connect(self.queue_redraw)
        plot.getViewBox().sigResized.connect(self.queue_redraw)

    def queue_redraw(self, *_args):
        self._timer.start()

    def set_groups(self, groups):
        entries = [(label["kp"], label["depth"], label["text"], tuple(label.get("anchor", [0, 1])),
                    group.get("colour") or "#444444")
                   for group in groups if group.get("visible", True) for label in group["labels"]]
        entries.sort(key=lambda e: e[0])
        if entries == self.entries:
            return
        self.entries = entries
        self.starts = [e[0] for e in entries]
        self.extent = (min((e[1] for e in entries), default=0), max((e[1] for e in entries), default=0))
        # Clear synchronously: plan changes must not leave a stale label visible.
        for item in self.items:
            item.hide()
            item.setText("")
        self.noticeChanged.emit("")
        self.queue_redraw()

    def redraw(self):
        self._timer.stop()
        for item in self.items:
            item.hide()
        if not self.entries:
            self.noticeChanged.emit("")
            return
        (lo, hi), (bottom, top) = self.plot.viewRange()
        bottom, top = sorted((bottom, top))
        first, last = bisect.bisect_left(self.starts, lo), bisect.bisect_right(self.starts, hi)
        visible = [e for e in self.entries[first:last] if bottom <= e[1] <= top]
        step = max(1, math.ceil(len(visible) / self.MAX_CANDIDATES))
        boxes, count = [], 0
        for kp, depth, text, anchor, colour in visible[::step]:
            if count >= self.MAX_VISIBLE:
                break
            if count == len(self.items):
                item = pg.TextItem(color="#444444", fill=(255, 255, 255, 210))
                item.setFont(QFont("", 9))
                item.setZValue(12)
                self.plot.addItem(item, ignoreBounds=True)
                self.items.append(item)
            item = self.items[count]
            lines = text.splitlines()
            display = "\n".join(lines[:8])
            if len(lines) > 8 or len(display) > 500:
                display = display[:500] + "…"
            item.setAnchor(anchor)
            item.setColor(colour if QColor(colour).isValid() else "#444444")
            item.setText(display)  # setPlainText internally; never HTML from a DXF
            item.textItem.setTextWidth(280 if max(map(len, display.splitlines()), default=0) > 45 else -1)
            item.setToolTip("<pre>" + html.escape(text) + "</pre>")
            item.setPos(kp, depth)
            box = item.mapRectToScene(item.boundingRect()).adjusted(-3, -2, 3, 2)
            if any(box.intersects(other) for other in boxes):
                continue
            boxes.append(box)
            item.show()
            count += 1
        self.noticeChanged.emit(f"DXF annotations: {count} of {len(visible)} in view"
                                + (" — zoom in to see more" if count < len(visible) else ""))

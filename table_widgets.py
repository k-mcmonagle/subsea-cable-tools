# -*- coding: utf-8 -*-
"""Content-sized table columns which remain manually resizable.

Auto-fit only untouched columns, with a cap for long notes and source paths.
A dragged width survives data refreshes; double-click its divider to fit again.
This is local to opted-in plugin tables and never changes QGIS's own widgets.
"""
from qgis.PyQt.QtCore import QEvent, Qt, QTimer
from qgis.PyQt.QtWidgets import QTableWidget

from .qgis_compat import HEADER_RESIZE_MODE_INTERACTIVE

_EVENT = getattr(QEvent, "Type", QEvent)
_LEFT = getattr(Qt, "MouseButton", Qt).LeftButton


class ResizableTableWidget(QTableWidget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._manual_widths = {}
        self._sizing_columns = False
        self._dragging_header = False
        self._column_timer = QTimer(self)
        self._column_timer.setSingleShot(True)
        self._column_timer.timeout.connect(self.resizeColumnsToContents)
        header = self.horizontalHeader()
        header.setResizeContentsPrecision(200)
        header.viewport().installEventFilter(self)
        header.sectionResized.connect(self._column_resized)
        header.sectionHandleDoubleClicked.connect(self._fit_column)
        header.setToolTip("Drag dividers to resize columns. Double-click a divider to fit its contents.")
        model = self.model()
        for signal in (model.modelReset, model.rowsInserted, model.columnsInserted,
                       model.dataChanged, model.headerDataChanged):
            signal.connect(self._schedule_column_fit)

    def eventFilter(self, watched, event):
        if watched is self.horizontalHeader().viewport():
            if event.type() == _EVENT.MouseButtonPress and event.button() == _LEFT:
                self._dragging_header = True
            elif event.type() in (_EVENT.MouseButtonRelease, _EVENT.UngrabMouse):
                self._dragging_header = False
        return super().eventFilter(watched, event)

    def _column_resized(self, column, old, width):
        if self._sizing_columns:
            return
        if self._dragging_header:
            self._manual_widths[column] = width
        elif column in self._manual_widths and width != self._manual_widths[column]:
            self._schedule_column_fit()

    def _schedule_column_fit(self, *args):
        if not self._column_timer.isActive():
            self._column_timer.start(0)

    def showEvent(self, event):
        super().showEvent(event)
        self._schedule_column_fit()

    def _fit_column(self, column):
        self._manual_widths.pop(column, None)
        self.resizeColumnToContents(column)

    def resizeColumnToContents(self, column):
        self._sizing_columns = True
        try:
            header = self.horizontalHeader()
            header.setStretchLastSection(False)
            header.setSectionResizeMode(column, HEADER_RESIZE_MODE_INTERACTIVE)
            if column in self._manual_widths:
                super().setColumnWidth(column, self._manual_widths[column])
                return
            super().resizeColumnToContents(column)
            # Avoid one notes/path column consuming the dock at first show;
            # users can widen it to inspect the full text.
            if not self.isColumnHidden(column):
                super().setColumnWidth(column, min(480, max(36, self.columnWidth(column))))
        finally:
            self._sizing_columns = False

    def resizeColumnsToContents(self):
        self._column_timer.stop()
        header = self.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(HEADER_RESIZE_MODE_INTERACTIVE)
        for column in range(self.columnCount()):
            self.resizeColumnToContents(column)

    def setColumnWidth(self, column, width):
        if column not in self._manual_widths:
            super().setColumnWidth(column, width)

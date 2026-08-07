# -*- coding: utf-8 -*-
"""Planner reports window: charts + tables over the Qt-free reports module.

Standard reports (fuel ROB, category breakdown, S-curve, plan-vs-actual
variance) are fixed views; a custom report is a saved (measure, group-by)
configuration of the breakdown report, persisted per scenario by the dock.
"""

from __future__ import annotations

import csv
from datetime import datetime

import pyqtgraph as pg

from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QFileDialog,
    QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)
from qgis.PyQt.QtCore import Qt

from ..qgis_compat import (
    ITEM_DATA_USER_ROLE, WINDOW_HINT_CLOSE, WINDOW_HINT_CUSTOMIZE,
    WINDOW_HINT_MIN_MAX, WINDOW_HINT_TITLE, WINDOW_TYPE_WINDOW,
)
from . import reports

_HORIZONTAL = getattr(getattr(Qt, "Orientation", Qt), "Horizontal")
_NO_EDIT = getattr(getattr(QAbstractItemView, "EditTrigger", QAbstractItemView),
                   "NoEditTriggers")
_DASH = getattr(getattr(Qt, "PenStyle", Qt), "DashLine")

# Chart chrome + series colors (validated light-mode dataviz palette).
_SURFACE = "#fcfcfb"
_INK = "#0b0b0b"
_INK_SECONDARY = "#52514e"
_INK_MUTED = "#898781"
_AXIS = "#c3c2b7"
_ACCENT = "#2a78d6"       # single-measure bars, planned curve
_ACCENT_WARM = "#eb6834"  # actual curve
_LATE = "#e34948"         # diverging poles for variance bars
_EARLY = "#2a78d6"
_CRITICAL = "#d03b3b"
_SERIES = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100",
           "#e87ba4", "#008300", "#4a3aa7", "#e34948")

_STANDARD_REPORTS = (
    ("fuel", "Fuel ROB && consumption"),
    ("cable", "Cable onboard && laid"),
    ("breakdown", "Breakdown by category"),
    ("s_curve", "Progress S-curve"),
    ("variance", "Plan vs actual variance"),
    ("key_dates", "Milestones && key dates"),
)
_CABLE_LAID_COLOR = "#1baf7a"  # distinct from the default resource blue


_EPOCH = datetime(1970, 1, 1)


def _ts(when):
    """Chart x-value for a naive plan datetime.

    Epoch-delta seconds paired with a utcOffset=0 date axis shows the plan's
    wall-clock times exactly as entered, independent of machine timezone/DST
    (datetime.timestamp() would shift them and can raise on Windows).
    """
    return (when - _EPOCH).total_seconds()


def _fmt(value, decimals=1):
    text = ("%.*f" % (decimals, float(value or 0.0))).rstrip("0").rstrip(".")
    return text or "0"


def _fmt_dt(value):
    return value.strftime("%d/%m/%Y %H:%M") if value is not None else ""


def _elide(text, limit=32):
    text = str(text or "")
    return text if len(text) <= limit else text[:limit - 1] + "…"


class ReportsWindow(QDialog):
    """Non-modal reports window; data arrives via dock-provided callbacks."""

    def __init__(self, context_provider, custom_provider, save_custom,
                 delete_custom, parent=None):
        super().__init__(parent)
        self._context_provider = context_provider
        self._custom_provider = custom_provider
        self._save_custom = save_custom
        self._delete_custom = delete_custom
        self._context = {}
        self._table_headers = []
        self._table_rows = []
        self.setWindowTitle("Planner reports")
        self.setWindowFlags(
            WINDOW_TYPE_WINDOW | WINDOW_HINT_CUSTOMIZE | WINDOW_HINT_TITLE |
            WINDOW_HINT_MIN_MAX | WINDOW_HINT_CLOSE)
        self.resize(980, 660)

        layout = QVBoxLayout(self)
        splitter = QSplitter(_HORIZONTAL)
        layout.addWidget(splitter, 1)

        self.report_list = QListWidget()
        self.report_list.setMaximumWidth(230)
        splitter.addWidget(self.report_list)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)

        options = QHBoxLayout()
        self.measure_label = QLabel("Measure:")
        self.measure_combo = QComboBox()
        for key, label, unit in reports.MEASURES:
            self.measure_combo.addItem(
                "%s%s" % (label, " (%s)" % unit if unit else ""), key)
        self.group_label = QLabel("Group by:")
        self.group_combo = QComboBox()
        for key, label in reports.GROUP_KEYS:
            self.group_combo.addItem(label, key)
        self.weight_label = QLabel("Progress in:")
        self.weight_combo = QComboBox()
        for key, label in reports.S_CURVE_WEIGHTS:
            self.weight_combo.addItem(label, key)
        self.save_report_btn = QPushButton("Save as report…")
        self.save_report_btn.setToolTip(
            "Save this measure/grouping as a named report for this scenario.")
        self.delete_report_btn = QPushButton("Delete report")
        for widget in (self.measure_label, self.measure_combo, self.group_label,
                       self.group_combo, self.weight_label, self.weight_combo,
                       self.save_report_btn, self.delete_report_btn):
            options.addWidget(widget)
        options.addStretch(1)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.setToolTip("Re-read the current schedule and progress.")
        refresh_btn.clicked.connect(self.refresh)
        options.addWidget(refresh_btn)
        right_layout.addLayout(options)

        self.plot = pg.PlotWidget(background=_SURFACE)
        self.plot.setMinimumHeight(260)
        right_layout.addWidget(self.plot, 3)

        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(
            getattr(getattr(Qt, "TextInteractionFlag", Qt),
                    "TextSelectableByMouse"))
        right_layout.addWidget(self.summary)

        self.table = QTableWidget()
        self.table.setEditTriggers(_NO_EDIT)
        self.table.setAlternatingRowColors(True)
        right_layout.addWidget(self.table, 2)

        exports = QHBoxLayout()
        for label, slot, tip in (
                ("Copy table", self._copy_table, "Copy the table as tab-separated text"),
                ("Save CSV…", self._save_csv, "Save the table as a CSV file"),
                ("Save Excel…", self._save_xlsx, "Save the table as an Excel workbook"),
                ("Save chart…", self._save_chart, "Save the chart as a PNG or SVG image")):
            button = QPushButton(label)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            exports.addWidget(button)
        exports.addStretch(1)
        right_layout.addLayout(exports)

        self.report_list.currentItemChanged.connect(lambda *_args: self._render())
        self.measure_combo.currentIndexChanged.connect(lambda *_args: self._render())
        self.group_combo.currentIndexChanged.connect(lambda *_args: self._render())
        self.weight_combo.currentIndexChanged.connect(lambda *_args: self._render())
        self.save_report_btn.clicked.connect(self._save_current_as_report)
        self.delete_report_btn.clicked.connect(self._delete_current_report)
        self._populate_report_list()

    # ----- report list -----------------------------------------------------

    def _populate_report_list(self, select_key="fuel"):
        self.report_list.blockSignals(True)
        self.report_list.clear()
        for key, label in _STANDARD_REPORTS:
            item = QListWidgetItem(label.replace("&&", "&"))
            item.setData(ITEM_DATA_USER_ROLE, ("standard", key))
            self.report_list.addItem(item)
        customs = list(self._custom_provider() or [])
        if customs:
            header = QListWidgetItem("— Saved reports —")
            header.setFlags(Qt.ItemFlag.NoItemFlags if hasattr(Qt, "ItemFlag")
                            else Qt.NoItemFlags)
            self.report_list.addItem(header)
            for config in customs:
                item = QListWidgetItem(str(config.get("name") or "Report"))
                item.setData(ITEM_DATA_USER_ROLE, ("custom", dict(config)))
                self.report_list.addItem(item)
        self.report_list.blockSignals(False)
        for index in range(self.report_list.count()):
            data = self.report_list.item(index).data(ITEM_DATA_USER_ROLE)
            if data and (data[0] == "standard" and data[1] == select_key
                         or data[0] == "custom" and select_key == data[1].get("name")):
                self.report_list.setCurrentRow(index)
                break
        else:
            self.report_list.setCurrentRow(0)

    def _current_report(self):
        item = self.report_list.currentItem()
        data = item.data(ITEM_DATA_USER_ROLE) if item is not None else None
        return data or ("standard", "fuel")

    def refresh(self):
        self._context = self._context_provider() or {}
        kind, value = self._current_report()
        select = value if kind == "standard" else value.get("name")
        self._populate_report_list(select)
        self._render()

    # ----- rendering -------------------------------------------------------

    def _render(self):
        if not self._context:
            self._context = self._context_provider() or {}
        kind, value = self._current_report()
        is_breakdown = kind == "custom" or (kind == "standard" and value == "breakdown")
        for widget in (self.measure_label, self.measure_combo,
                       self.group_label, self.group_combo):
            widget.setVisible(is_breakdown)
        is_s_curve = kind == "standard" and value == "s_curve"
        self.weight_label.setVisible(is_s_curve)
        self.weight_combo.setVisible(is_s_curve)
        self.save_report_btn.setVisible(is_breakdown)
        self.delete_report_btn.setVisible(kind == "custom")
        if kind == "custom":
            self._select_combo(self.measure_combo, value.get("measure"))
            self._select_combo(self.group_combo, value.get("group_key"))
        if is_breakdown:
            self._render_breakdown()
        elif value == "fuel":
            self._render_fuel()
        elif value == "cable":
            self._render_cable()
        elif value == "s_curve":
            self._render_s_curve()
        elif value == "variance":
            self._render_variance()
        elif value == "key_dates":
            self._render_key_dates()

    @staticmethod
    def _select_combo(combo, data):
        index = combo.findData(data)
        if index >= 0 and index != combo.currentIndex():
            combo.blockSignals(True)
            combo.setCurrentIndex(index)
            combo.blockSignals(False)

    def _reset_plot(self, time_axis=False):
        plot_item = self.plot.getPlotItem()
        if plot_item.legend is not None:
            try:
                plot_item.legend.scene().removeItem(plot_item.legend)
            except Exception:
                pass
            plot_item.legend = None
        plot_item.clear()
        bottom = (pg.DateAxisItem(orientation="bottom", utcOffset=0) if time_axis
                  else pg.AxisItem(orientation="bottom"))
        plot_item.setAxisItems({"bottom": bottom,
                                "left": pg.AxisItem(orientation="left")})
        for name in ("bottom", "left"):
            axis = plot_item.getAxis(name)
            axis.setPen(pg.mkPen(_AXIS))
            axis.setTextPen(pg.mkPen(_INK_SECONDARY))
        plot_item.setTitle(None)
        plot_item.getViewBox().invertY(False)
        plot_item.showGrid(x=False, y=True, alpha=0.15)
        plot_item.enableAutoRange()
        plot_item.setLabel("bottom", "")
        plot_item.setLabel("left", "")

    def _axis_label(self, name, text):
        self.plot.getPlotItem().setLabel(
            name, text, color=_INK_MUTED, **{"font-size": "10pt"})

    def _legend(self):
        return self.plot.getPlotItem().addLegend(
            offset=(10, 10), labelTextColor=_INK_SECONDARY,
            brush=pg.mkBrush(_SURFACE), pen=pg.mkPen(_AXIS))

    def _set_table(self, headers, rows):
        self._table_headers = list(headers)
        self._table_rows = [[str(cell) for cell in row] for row in rows]
        self.table.clear()
        self.table.setColumnCount(len(headers))
        self.table.setRowCount(len(rows))
        self.table.setHorizontalHeaderLabels(list(headers))
        for row_index, row in enumerate(self._table_rows):
            for col_index, cell in enumerate(row):
                self.table.setItem(row_index, col_index, QTableWidgetItem(cell))
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)

    # ----- fuel report -----------------------------------------------------

    def _render_fuel(self):
        self._reset_plot(time_axis=True)
        context = self._context
        dataset = context.get("dataset") or []
        resources = context.get("resources") or []
        series = reports.fuel_series(dataset, resources)
        if not series:
            self.summary.setText(
                "No fuel data yet.\n\nSet fuel rates, start fuel, and optionally "
                "cost per unit in Resources…, then choose a fuel mode "
                "(Transit/DP/Anchor/Port) for each task. Enter a Bunker amount "
                "on port-call tasks to take fuel on.")
            self._set_table([], [])
            return
        units = sorted({item.unit for item in series})
        unit_text = units[0] if len(units) == 1 else "mixed units"
        legend = self._legend() if len(series) > 1 else None
        lowest = 0.0
        for index, item in enumerate(series):
            color = item.color_hex or _SERIES[index % len(_SERIES)]
            xs = [_ts(when) for when, _rob in item.points]
            ys = [rob for _when, rob in item.points]
            lowest = min([lowest] + ys)
            self.plot.plot(xs, ys, pen=pg.mkPen(color, width=2),
                           name=item.resource_name if legend else None,
                           antialias=True)
        if lowest < 0.0:
            zero = pg.InfiniteLine(pos=0.0, angle=0, pen=pg.mkPen(
                _CRITICAL, width=1, style=_DASH))
            self.plot.addItem(zero)
        self._axis_label("left", "Remaining on board (%s)" % unit_text)
        self._axis_label("bottom", "Date")

        fuel = context.get("fuel")
        summaries = getattr(fuel, "by_resource", {}) or {}
        resource_names = {str(row.get("resource_id") or ""): str(row.get("name") or "")
                          for row in resources}
        lines = []
        for resource_id, summary in summaries.items():
            if not (summary.rob_start or summary.total_burn or summary.total_bunker):
                continue
            parts = ["%s: start %s, burned %s" % (
                resource_names.get(resource_id) or "Resource",
                _fmt(summary.rob_start), _fmt(summary.total_burn))]
            if summary.total_bunker:
                parts.append("bunkered %s" % _fmt(summary.total_bunker))
            parts.append("end ROB %s (lowest %s) %s" % (
                _fmt(summary.rob_end), _fmt(summary.min_rob), summary.unit))
            if summary.cost:
                parts.append("cost {:,.2f}".format(summary.cost))
            line = ", ".join(parts)
            for warning in summary.warnings:
                line += "\n⚠ %s" % warning
            lines.append(line)
        self.summary.setText("\n".join(lines))

        headers = ["Task", "Resource", "Fuel mode", "Start", "Finish",
                   "Burn", "Bunker", "ROB end"]
        rows = []
        for rec in dataset:
            if rec.is_phase or not (rec.fuel_burn or rec.fuel_bunker):
                continue
            rows.append([rec.name, rec.resource_name, rec.fuel_mode,
                         _fmt_dt(rec.start), _fmt_dt(rec.finish),
                         _fmt(rec.fuel_burn, 2), _fmt(rec.fuel_bunker, 2),
                         _fmt(rec.rob_end, 2)])
        self._set_table(headers, rows)

    # ----- cable report ----------------------------------------------------

    def _render_cable(self):
        self._reset_plot(time_axis=True)
        context = self._context
        dataset = context.get("dataset") or []
        resources = context.get("resources") or []
        series = reports.cable_series(dataset, resources)
        laid = reports.cumulative_cable_laid(dataset)
        if not series:
            self.summary.setText(
                "No cable movements yet.\n\nSet the Cable column on tasks: "
                "Load or Recover brings cable onboard, Lay or Discharge pays "
                "it off. Lay/Recover tasks use the task's distance "
                "automatically when the Cable (km) cell is left blank.")
            self._set_table([], [])
            return
        legend = self._legend() if (len(series) + (1 if laid else 0)) > 1 else None
        lowest = 0.0
        for index, item in enumerate(series):
            color = item.color_hex or _SERIES[index % len(_SERIES)]
            xs = [_ts(when) for when, _km in item.points]
            ys = [km for _when, km in item.points]
            lowest = min([lowest] + ys)
            self.plot.plot(
                xs, ys, pen=pg.mkPen(color, width=2),
                name=("%s onboard" % item.resource_name) if legend else None,
                antialias=True)
        if laid:
            self.plot.plot(
                [_ts(when) for when, _km in laid], [km for _when, km in laid],
                pen=pg.mkPen(_CABLE_LAID_COLOR, width=2, style=_DASH),
                name="Total laid" if legend else None, antialias=True)
        if lowest < 0.0:
            self.plot.addItem(pg.InfiniteLine(pos=0.0, angle=0, pen=pg.mkPen(
                _CRITICAL, width=1, style=_DASH)))
        self._axis_label("left", "Cable (km)")
        self._axis_label("bottom", "Date")

        cable_labels = dict(context.get("cable_labels") or {})
        totals = {"lay": 0.0, "load": 0.0, "recover": 0.0, "discharge": 0.0}
        for rec in dataset:
            if not rec.is_phase and rec.cable_mode in totals:
                totals[rec.cable_mode] += rec.cable_amount_m
        lines = ["Laid %s km, loaded %s km, recovered %s km%s." % (
            _fmt(totals["lay"] / 1000.0, 2), _fmt(totals["load"] / 1000.0, 2),
            _fmt(totals["recover"] / 1000.0, 2),
            (", discharged %s km" % _fmt(totals["discharge"] / 1000.0, 2))
            if totals["discharge"] else "")]
        for item in series:
            low = min(km for _when, km in item.points)
            end = item.points[-1][1]
            line = "%s: ends with %s km onboard (lowest %s km)." % (
                item.resource_name, _fmt(end, 2), _fmt(low, 2))
            if low < -1e-9:
                line += " ⚠ More cable paid off than onboard — add a Load "\
                        "task or check the amounts."
            lines.append(line)
        self.summary.setText("\n".join(lines))

        headers = ["Task", "Resource", "Cable", "Start", "Finish",
                   "Amount (km)", "Onboard after (km)"]
        rows = []
        for rec in dataset:
            if rec.is_phase or not rec.cable_mode:
                continue
            rows.append([
                rec.name, rec.resource_name,
                cable_labels.get(rec.cable_mode, rec.cable_mode),
                _fmt_dt(rec.start), _fmt_dt(rec.finish),
                _fmt(rec.cable_amount_m / 1000.0, 3),
                _fmt(rec.cable_onboard_end_m / 1000.0, 3)])
        self._set_table(headers, rows)

    # ----- breakdown / custom ---------------------------------------------

    def _render_breakdown(self):
        self._reset_plot()
        context = self._context
        dataset = context.get("dataset") or []
        measure = self.measure_combo.currentData()
        group_key = self.group_combo.currentData()
        labels = dict(context.get("op_labels") or {})
        if group_key == "progress_status":
            labels = dict(context.get("status_labels") or {})
        elif group_key == "cable_mode":
            labels = dict(context.get("cable_labels") or {})
        totals = reports.aggregate(dataset, measure, group_key, labels)
        measure_entry = next((entry for entry in reports.MEASURES
                              if entry[0] == measure), reports.MEASURES[0])
        measure_text = "%s%s" % (
            measure_entry[1], " (%s)" % measure_entry[2] if measure_entry[2] else "")
        if not totals:
            self.summary.setText(
                "Nothing to report for this measure yet — schedule some tasks "
                "(and set fuel modes for the fuel measures).")
            self._set_table([], [])
            return
        # Horizontal bars, largest at the top, category names on the axis.
        count = len(totals)
        ys = list(range(count))
        widths = [value for _label, value in totals]
        bars = pg.BarGraphItem(
            x0=0, y=ys, height=0.7, width=widths,
            brush=pg.mkBrush(_ACCENT), pen=pg.mkPen(_SURFACE))
        self.plot.addItem(bars)
        axis = self.plot.getPlotItem().getAxis("left")
        axis.setTicks([[(index, _elide(label)) for index, (label, _value)
                        in enumerate(totals)]])
        self.plot.getPlotItem().getViewBox().invertY(True)
        self.plot.getPlotItem().showGrid(x=True, y=False, alpha=0.15)
        self._axis_label("bottom", measure_text)
        total = sum(widths)
        top_label, top_value = totals[0]
        self.summary.setText(
            "Total %s: %s across %d group(s). Largest: %s (%s, %s%%)." % (
                measure_text, _fmt(total), count, top_label, _fmt(top_value),
                _fmt(top_value / total * 100.0 if total else 0.0)))
        group_text = self.group_combo.currentText()
        self._set_table(
            [group_text, measure_text, "% of total"],
            [[label, _fmt(value, 2),
              _fmt(value / total * 100.0 if total else 0.0)]
             for label, value in totals])

    def _save_current_as_report(self):
        name, ok = QInputDialog.getText(
            self, "Save report", "Report name:",
            text="%s by %s" % (self.measure_combo.currentText(),
                               self.group_combo.currentText().lower()))
        if not ok or not name.strip():
            return
        config = {"name": name.strip(), "report": "breakdown",
                  "measure": self.measure_combo.currentData(),
                  "group_key": self.group_combo.currentData()}
        self._save_custom(config)
        self._populate_report_list(select_key=config["name"])

    def _delete_current_report(self):
        kind, value = self._current_report()
        if kind != "custom":
            return
        self._delete_custom(str(value.get("name") or ""))
        self._populate_report_list(select_key="breakdown")

    # ----- S-curve ---------------------------------------------------------

    def _render_s_curve(self):
        self._reset_plot(time_axis=True)
        context = self._context
        dataset = context.get("dataset") or []
        now = context.get("now")
        weight = self.weight_combo.currentData() or "duration"
        curves = reports.s_curve(dataset, now=now, weight=weight)
        if not curves.planned:
            self.summary.setText(
                "No cable-lay tasks to chart yet — set the Cable column to "
                "Lay on the laying tasks (their distance supplies the "
                "quantity), or switch Progress in: back to Duration."
                if weight == "cable_laid" else
                "No planned work to chart yet — add tasks with durations first.")
            self._set_table([], [])
            return
        legend = self._legend()
        drawn = 0
        for points, name, pen in (
                (curves.baseline, "Baseline",
                 pg.mkPen(_INK_MUTED, width=2, style=_DASH)),
                (curves.planned, "Planned", pg.mkPen(_ACCENT, width=2)),
                (curves.actual, "Actual", pg.mkPen(_ACCENT_WARM, width=2))):
            if not points:
                continue
            xs = [_ts(when) for when, _pct in points]
            self.plot.plot(xs, [pct for _when, pct in points], pen=pen,
                           name=name, antialias=True)
            drawn += 1
        if drawn < 2 and legend is not None:
            try:
                legend.scene().removeItem(legend)
                self.plot.getPlotItem().legend = None
            except Exception:
                pass
        self.plot.setYRange(0.0, 100.0)
        self._axis_label(
            "left", "Cumulative %% complete (%s)" %
            ("by cable laid" if weight == "cable_laid" else "duration-weighted"))
        self._axis_label("bottom", "Date")
        lines = []
        if now is not None:
            lines.append("At %s: planned %s%%, earned %s%%." % (
                _fmt_dt(now), _fmt(curves.planned_pct_now),
                _fmt(curves.earned_pct_now)))
            if curves.spi is not None:
                lines.append(
                    "Schedule performance index (earned ÷ planned): %s%s" % (
                        _fmt(curves.spi, 2),
                        " — behind plan" if curves.spi < 1.0 else
                        " — on/ahead of plan"))
        if not curves.baseline:
            lines.append("No baseline set — use Baseline / actuals… to freeze "
                         "one for comparison.")
        self.summary.setText("\n".join(lines))
        headers = ["Task", "Planned finish", "Baseline finish",
                   "Actual finish", "% complete"]
        rows = [[rec.name, _fmt_dt(rec.finish), _fmt_dt(rec.baseline_finish),
                 _fmt_dt(rec.actual_finish), _fmt(rec.percent_complete)]
                for rec in dataset if not rec.is_phase]
        self._set_table(headers, rows)

    # ----- variance --------------------------------------------------------

    def _render_variance(self):
        self._reset_plot()
        context = self._context
        dataset = context.get("dataset") or []
        rows = reports.variance_rows(dataset)
        if not rows:
            self.summary.setText(
                "No baseline to compare against — set one with "
                "Baseline / actuals… → Set or replace baseline, then record "
                "actual progress on tasks.")
            self._set_table([], [])
            return
        shown = rows[:20]
        ys = list(range(len(shown)))
        x0 = [min(0.0, row.variance_hours) for row in shown]
        widths = [abs(row.variance_hours) for row in shown]
        brushes = [pg.mkBrush(_LATE if row.variance_hours > 0 else _EARLY)
                   for row in shown]
        bars = pg.BarGraphItem(x0=x0, y=ys, height=0.7, width=widths,
                               brushes=brushes, pen=pg.mkPen(_SURFACE))
        self.plot.addItem(bars)
        zero = pg.InfiniteLine(pos=0.0, angle=90, pen=pg.mkPen(_AXIS, width=1))
        self.plot.addItem(zero)
        axis = self.plot.getPlotItem().getAxis("left")
        axis.setTicks([[(index, _elide(row.name)) for index, row
                        in enumerate(shown)]])
        self.plot.getPlotItem().getViewBox().invertY(True)
        self.plot.getPlotItem().showGrid(x=True, y=False, alpha=0.15)
        self._axis_label("bottom", "Finish variance vs baseline (hours; "
                                   "late → right)")
        late = sum(1 for row in rows if row.variance_hours > 1e-9)
        early = sum(1 for row in rows if row.variance_hours < -1e-9)
        span_end = context.get("span_end")
        baseline_end = reports.parse_datetime(
            (context.get("baseline") or {}).get("span_end"))
        lines = ["%d task(s) compared: %d late, %d early, %d on time." % (
            len(rows), late, early, len(rows) - late - early)]
        if span_end is not None and baseline_end is not None:
            overall = (span_end - baseline_end).total_seconds() / 3600.0
            lines.append("Plan finish variance: %+.1f h (%s vs baseline %s)."
                         % (overall, _fmt_dt(span_end), _fmt_dt(baseline_end)))
        if len(rows) > len(shown):
            lines.append("Chart shows the %d largest variances; the table has "
                         "all %d." % (len(shown), len(rows)))
        self.summary.setText("\n".join(lines))
        self._set_table(
            ["Task", "Baseline finish", "Forecast/actual finish",
             "Variance (h)", "Finish recorded?"],
            [[row.name, _fmt_dt(row.baseline_finish),
              _fmt_dt(row.forecast_finish), "%+.1f" % row.variance_hours,
              "yes" if row.is_actual else "forecast"] for row in rows])

    # ----- milestones & key dates ------------------------------------------

    def _render_key_dates(self):
        self._reset_plot(time_axis=True)
        context = self._context
        dataset = context.get("dataset") or []
        rows = reports.key_dates(dataset)
        if not rows:
            self.summary.setText(
                "No groups or milestones yet.\n\nIndent tasks beneath a "
                "summary row (Edit / outline… → Group selected tasks) to form "
                "groups, or mark key tasks as milestones in Advanced task "
                "settings — each then appears here with its planned, baseline "
                "and actual dates.")
            self._set_table([], [])
            return
        shown = rows[:30]
        plot_item = self.plot.getPlotItem()
        legend = self._legend()
        planned_y, planned_x0, planned_w = [], [], []
        baseline_y, baseline_x0, baseline_w = [], [], []
        mile_planned, mile_baseline, mile_actual = [], [], []
        for index, row in enumerate(shown):
            if row.kind == "group":
                if row.planned_start and row.planned_finish:
                    planned_y.append(index - 0.18)
                    planned_x0.append(_ts(row.planned_start))
                    planned_w.append(max(
                        1.0, _ts(row.planned_finish) - _ts(row.planned_start)))
                if row.baseline_start and row.baseline_finish:
                    baseline_y.append(index + 0.18)
                    baseline_x0.append(_ts(row.baseline_start))
                    baseline_w.append(max(
                        1.0, _ts(row.baseline_finish) - _ts(row.baseline_start)))
            else:
                when = row.planned_finish or row.planned_start
                if when is not None:
                    mile_planned.append((_ts(when), index))
                if row.baseline_finish is not None:
                    mile_baseline.append((_ts(row.baseline_finish), index))
                if row.actual_finish is not None:
                    mile_actual.append((_ts(row.actual_finish), index))
        if planned_y:
            bars = pg.BarGraphItem(
                x0=planned_x0, y=planned_y, height=0.32, width=planned_w,
                brush=pg.mkBrush(_ACCENT), pen=pg.mkPen(_SURFACE))
            self.plot.addItem(bars)
            legend.addItem(bars, "Planned")
        if baseline_y:
            base_bars = pg.BarGraphItem(
                x0=baseline_x0, y=baseline_y, height=0.32, width=baseline_w,
                brush=pg.mkBrush(_INK_MUTED), pen=pg.mkPen(_SURFACE))
            self.plot.addItem(base_bars)
            legend.addItem(base_bars, "Baseline")
        for points, name, color in (
                (mile_planned, "Milestone (planned)", _ACCENT),
                (mile_baseline, "Milestone (baseline)", _INK_MUTED),
                (mile_actual, "Milestone (actual)", _ACCENT_WARM)):
            if points:
                scatter = pg.ScatterPlotItem(
                    [x for x, _y in points], [y for _x, y in points],
                    symbol="d", size=12, brush=pg.mkBrush(color),
                    pen=pg.mkPen(_SURFACE))
                self.plot.addItem(scatter)
                legend.addItem(scatter, name)
        axis = plot_item.getAxis("left")
        axis.setTicks([[(index, _elide("  " * row.level + row.name, 36))
                        for index, row in enumerate(shown)]])
        plot_item.getViewBox().invertY(True)
        plot_item.showGrid(x=True, y=False, alpha=0.15)
        self._axis_label("bottom", "Date")
        groups = sum(1 for row in rows if row.kind == "group")
        milestones = len(rows) - groups
        lines = ["%d group(s), %d milestone(s)." % (groups, milestones)]
        if not any(row.baseline_start or row.baseline_finish for row in rows):
            lines.append("No baseline set — use Baseline / actuals… to freeze "
                         "one for the baseline columns.")
        if len(rows) > len(shown):
            lines.append("Chart shows the first %d rows; the table has all %d."
                         % (len(shown), len(rows)))
        self.summary.setText("\n".join(lines))
        self._set_table(
            ["Item", "Type", "Planned start", "Planned finish",
             "Baseline start", "Baseline finish", "Actual start",
             "Actual finish", "% complete"],
            [["  " * row.level + row.name,
              "Group" if row.kind == "group" else "Milestone",
              _fmt_dt(row.planned_start), _fmt_dt(row.planned_finish),
              _fmt_dt(row.baseline_start), _fmt_dt(row.baseline_finish),
              _fmt_dt(row.actual_start), _fmt_dt(row.actual_finish),
              _fmt(row.percent_complete)] for row in rows])

    # ----- exports ---------------------------------------------------------

    def _copy_table(self):
        lines = ["\t".join(self._table_headers)]
        lines.extend("\t".join(row) for row in self._table_rows)
        QApplication.clipboard().setText("\n".join(lines))

    def _save_csv(self):
        if not self._table_headers:
            return
        path, _filter = QFileDialog.getSaveFileName(
            self, "Save report table", "planner_report.csv", "CSV (*.csv)")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(self._table_headers)
            writer.writerows(self._table_rows)

    def _save_xlsx(self):
        if not self._table_headers:
            return
        try:
            from openpyxl import Workbook
        except ImportError:
            QMessageBox.warning(self, "Planner reports",
                                "The bundled openpyxl library is unavailable.")
            return
        path, _filter = QFileDialog.getSaveFileName(
            self, "Save report table", "planner_report.xlsx", "Excel (*.xlsx)")
        if not path:
            return
        book = Workbook()
        sheet = book.active
        sheet.title = "Report"
        sheet.append(self._table_headers)
        for row in self._table_rows:
            sheet.append(row)
        book.save(path)

    def _save_chart(self):
        path, _filter = QFileDialog.getSaveFileName(
            self, "Save chart", "planner_chart.png",
            "PNG image (*.png);;SVG image (*.svg)")
        if not path:
            return
        from pyqtgraph import exporters
        if path.lower().endswith(".svg"):
            exporter = exporters.SVGExporter(self.plot.plotItem)
        else:
            exporter = exporters.ImageExporter(self.plot.plotItem)
            exporter.parameters()["width"] = 1600
        exporter.export(path)

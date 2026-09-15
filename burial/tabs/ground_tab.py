# -*- coding: utf-8 -*-
"""Ground Model tab — soil units along the route, in KP × depth below seabed.

A KP-linked plot (aligned with the persistent bathymetry profile) over an
editable unit table. Units are imported from CSV/XLSX with column mapping
and a stated KP reference (which RPL revision the document's KPs follow),
or entered and edited here; edits are one logged, undoable replacement of
the plan's units when *Apply* is pressed.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from qgis.PyQt.QtCore import QSettings, Qt
from qgis.PyQt.QtGui import QBrush, QColor
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...qgis_compat import (
    DIALOG_ACCEPTED,
    HEADER_RESIZE_MODE_INTERACTIVE,
    ITEM_DATA_USER_ROLE,
    MESSAGE_BOX_YES,
    SELECTION_BEHAVIOR_SELECT_ROWS,
    SELECTION_MODE_EXTENDED,
    qt_exec,
)
from .. import change_log, ground_model, schema, ui_helpers
from ..ground_dialogs import ClassesDialog, GroundImportDialog, RereferenceDialog
from ..ground_plot import GroundModelPlot

_VERTICAL = getattr(Qt, "Orientation", Qt).Vertical
_SETTINGS_ROOT = "SubseaCableTools/BurialPlanner"

# Table columns: (header, unit key, kind) — kind: kp | depth | class |
# confidence | text.
_COLUMNS = [
    ("Start KP", "start_kp", "kp"),
    ("End KP", "end_kp", "kp"),
    ("Top (m)", "top_m", "depth"),
    ("Base (m)", "base_m", "depth"),
    ("Class", "soil_class", "class"),
    ("Description", "description", "text"),
    ("Strength", "strength", "text"),
    ("Confidence", "confidence", "confidence"),
    ("Source", "source_ref", "text"),
    ("Top @ end (m)", "top_end_m", "depth"),
    ("Base @ end (m)", "base_end_m", "depth"),
    ("Src start KP", "src_start_kp", "kp_ro"),
    ("Src end KP", "src_end_kp", "kp_ro"),
    ("Src RPL", "src_rpl", "text_ro"),
    ("Re-ref flags", "rereference_flags", "text_ro"),
    ("Notes", "notes", "text"),
]
_DEFAULT_HIDDEN = ("Top @ end (m)", "Base @ end (m)", "Src start KP",
                   "Src end KP", "Src RPL", "Re-ref flags", "Notes")
_COL_CLASS = 4
_COL_CONFIDENCE = 7


class GroundTab(QWidget):
    def __init__(self, model, dock, parent=None):
        super().__init__(parent)
        self.model = model
        self.dock = dock
        self._loading = False
        self._dirty = False
        self._loaded_plan_id = ""
        self._working: List[Dict] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        intro = QLabel(
            "Soil units along the route by depth below seabed. Import the "
            "ground model from a CSV/XLSX table (stating which RPL revision "
            "its KPs follow), or add and edit units here. The KP axis is "
            "linked to the bathymetry profile below; the dashed line is the "
            "plan's target burial depth.")
        intro.setWordWrap(True)
        intro.setStyleSheet(ui_helpers.hint_style())
        layout.addWidget(intro)

        toolbar = QHBoxLayout()
        self.import_button = QPushButton("Import…")
        self.import_button.setToolTip("Import units from a CSV/XLSX table with "
                                      "column mapping and KP re-referencing.")
        self.import_button.clicked.connect(self._import)
        self.export_button = QPushButton("Export CSV")
        self.export_button.clicked.connect(self._export)
        self.rereference_button = QPushButton("Re-reference KPs…")
        self.rereference_button.setToolTip(
            "Bring the selected units (or all) onto the plan's current route "
            "from the RPL revision their KPs were delivered against.")
        self.rereference_button.clicked.connect(self._rereference)
        self.classes_button = QPushButton("Classes…")
        self.classes_button.setToolTip("Soil-class codes, labels, groups and "
                                       "colours (shared across plans).")
        self.classes_button.clicked.connect(self._edit_classes)
        for button in (self.import_button, self.export_button,
                       self.rereference_button, self.classes_button):
            toolbar.addWidget(button)
        self.show_map = QCheckBox("Show on map")
        self.show_map.setToolTip(
            "Draw the seabed soil class (and the class at the target burial "
            "depth) as coloured ribbons beside the route, offset to port so "
            "the burial/skip sections on the line stay readable. The layer "
            "lives in the plan's group; identify a ribbon for its unit.")
        self.show_map.setChecked(bool(QSettings().value(
            f"{_SETTINGS_ROOT}/ground_layer_visible", True, type=bool)))
        self.show_map.toggled.connect(self._map_toggled)
        toolbar.addWidget(self.show_map)
        toolbar.addStretch(1)
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        toolbar.addWidget(self.status_label, 2)
        layout.addLayout(toolbar)

        self.plot_box = QGroupBox("Ground model — depth below seabed vs KP")
        plot_layout = QVBoxLayout(self.plot_box)
        plot_layout.setContentsMargins(4, 4, 4, 4)
        self.plot = GroundModelPlot()
        self.plot.kpHovered.connect(self._plot_hover)
        self.plot.kpClicked.connect(self._plot_clicked)
        self.plot.unitClicked.connect(self._plot_unit_clicked)
        plot_layout.addWidget(self.plot, 1)
        self.plot_hint = QLabel(
            "Hover for KP / depth / unit; click to go to that KP on the map "
            "and profile and select the unit in the table. Right-click the "
            "plot for axis and export controls.")
        self.plot_hint.setWordWrap(True)
        self.plot_hint.setStyleSheet(ui_helpers.hint_style())
        plot_layout.addWidget(self.plot_hint)

        table_pane = QWidget()
        table_layout = QVBoxLayout(table_pane)
        table_layout.setContentsMargins(0, 0, 0, 0)
        edit_row = QHBoxLayout()
        self.add_button = QPushButton("Add unit")
        self.add_button.clicked.connect(self._add_unit)
        self.duplicate_button = QPushButton("Duplicate")
        self.duplicate_button.clicked.connect(self._duplicate_units)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self._remove_units)
        self.split_button = QPushButton("Split at KP…")
        self.split_button.setToolTip("Split the selected unit(s) at a KP "
                                     "(type it, or pick it on the map).")
        self.split_button.clicked.connect(self._split_units)
        for button in (self.add_button, self.duplicate_button,
                       self.remove_button, self.split_button):
            edit_row.addWidget(button)
        edit_row.addStretch(1)
        self.apply_button = QPushButton("Apply changes")
        self.apply_button.setToolTip("Validate and save the table as the "
                                     "plan's ground model (one undoable entry).")
        self.apply_button.clicked.connect(self._apply)
        self.revert_button = QPushButton("Revert")
        self.revert_button.clicked.connect(self._revert)
        edit_row.addWidget(self.apply_button)
        edit_row.addWidget(self.revert_button)
        table_layout.addLayout(edit_row)

        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels([c[0] for c in _COLUMNS])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(HEADER_RESIZE_MODE_INTERACTIVE)
        header.setStretchLastSection(True)
        self.table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.table.setSelectionMode(SELECTION_MODE_EXTENDED)
        self.table.setToolTip(
            "Edit cells directly; Class and Confidence are drop-downs. "
            "Right-click the header to show the optional columns "
            "(sloping tops/bases, delivered source KPs, flags).")
        self._delegate = ui_helpers.ComboColumnDelegate(
            self.table, self._combo_options, self._combo_commit)
        self.table.setItemDelegateForColumn(_COL_CLASS, self._delegate)
        self.table.setItemDelegateForColumn(_COL_CONFIDENCE, self._delegate)
        self.table.itemChanged.connect(self._item_changed)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.cellDoubleClicked.connect(self._row_activated)
        ui_helpers.enable_column_menu(
            self.table, f"{_SETTINGS_ROOT}/ground_table_columns",
            always_visible=(0, 1, _COL_CLASS), default_hidden=_DEFAULT_HIDDEN)
        table_layout.addWidget(self.table, 1)

        self.splitter = QSplitter(_VERTICAL)
        self.splitter.addWidget(self.plot_box)
        self.splitter.addWidget(table_pane)
        self.splitter.setStretchFactor(0, 2)
        self.splitter.setStretchFactor(1, 3)
        self.splitter.setCollapsible(0, False)
        self.splitter.setCollapsible(1, False)
        state = QSettings().value(f"{_SETTINGS_ROOT}/ground_splitter_state")
        if state is not None:
            try:
                self.splitter.restoreState(state)
            except Exception:
                pass
        self.splitter.splitterMoved.connect(
            lambda *_a: QSettings().setValue(
                f"{_SETTINGS_ROOT}/ground_splitter_state", self.splitter.saveState()))
        layout.addWidget(self.splitter, 1)

        refresh_soon = ui_helpers.coalesced(self, self.refresh)
        model.planChanged.connect(refresh_soon)
        model.groundChanged.connect(refresh_soon)
        self.refresh()

    # -- refresh ---------------------------------------------------------------
    def refresh(self) -> None:
        plan_id = self.model.plan_id
        has_plan = bool(self.model.plan)
        for button in (self.import_button, self.export_button,
                       self.rereference_button, self.add_button,
                       self.duplicate_button, self.remove_button,
                       self.split_button, self.apply_button, self.revert_button):
            button.setEnabled(has_plan)
        self.classes_button.setEnabled(True)
        if not has_plan:
            self._working = []
            self._dirty = False
            self._loaded_plan_id = ""
            self._rebuild_table()
            self.plot.clear()
            self._set_status("No plan selected.")
            return
        if self._dirty and plan_id == self._loaded_plan_id:
            # Never clobber unapplied edits with a background refresh; the
            # plot still follows the working copy.
            self._refresh_plot()
            return
        self._working = [ground_model.normalise_unit(u) for u in self.model.ground_units]
        self._dirty = False
        self._loaded_plan_id = plan_id
        self._rebuild_table()
        self._refresh_plot()
        self._update_status()
        self._apply_map_visibility()

    def _refresh_plot(self) -> None:
        self.plot.set_units(self._working, self.model.ground_classes)
        scope = self.model.gen_params().scope
        self.plot.set_scope(scope.start_km, scope.end_km)
        self.plot.set_target_depth(self.model.plan.get("target_burial_m"))

    def _update_status(self) -> None:
        if not self._working:
            self._set_status("No ground-model units for this plan yet — "
                             "import a table or add units.")
            return
        codes = {u["soil_class"] for u in self._working if u["soil_class"]}
        lo = min(u["start_kp"] for u in self._working if u["start_kp"] is not None)
        hi = max(u["end_kp"] for u in self._working if u["end_kp"] is not None)
        text = (f"{len(self._working)} unit(s), {len(codes)} class(es), "
                f"KP {schema.format_kp(lo)}–{schema.format_kp(hi)}.")
        target = self.model.plan.get("target_burial_m")
        scope = self.model.gen_params().scope
        kind = ""
        try:
            target_m = float(target) if target is not None else None
        except (TypeError, ValueError):
            target_m = None
        if target_m and scope.length_km > 0:
            gaps = ground_model.coverage_gaps(
                self._working, scope.start_km, scope.end_km, target_m)
            missing = sum(b - a for a, b in gaps)
            if missing > 1e-6:
                text += (f" No unit at the target burial depth ({target_m:.2f} m) "
                         f"over {missing:.3f} km of the scope.")
                kind = "warn"
        meta = self.model.ground_meta()
        if meta.get("source_ref"):
            text += f" Source: {meta['source_ref']}"
            if meta.get("source_rpl"):
                text += f" (KPs re-referenced from {meta['source_rpl']})"
            text += "."
        if self._dirty:
            text += " Unapplied edits."
            kind = kind or "info"
        self._set_status(text, kind)

    def _set_status(self, text: str, kind: str = "") -> None:
        self.status_label.setText(text)
        self.status_label.setStyleSheet(ui_helpers.status_style(kind) if kind
                                        else ui_helpers.hint_style())

    # -- table ---------------------------------------------------------------
    def _rebuild_table(self) -> None:
        self._loading = True
        try:
            with ui_helpers.preserve_table_view(self.table, id_column=0):
                with ui_helpers.silent_rebuild(self.table):
                    self.table.setRowCount(len(self._working))
                    for i, unit in enumerate(self._working):
                        self._fill_row(i, unit)
            self.table.resizeColumnsToContents()
            self.table.horizontalHeader().setSectionResizeMode(
                HEADER_RESIZE_MODE_INTERACTIVE)
        finally:
            self._loading = False

    def _fill_row(self, row: int, unit: Dict) -> None:
        by_code = ground_model.class_lookup(self.model.ground_classes)
        for col, (header, key, kind) in enumerate(_COLUMNS):
            value = unit.get(key)
            item = QTableWidgetItem()
            flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            if kind == "kp" or kind == "kp_ro":
                item.setText("" if value is None else f"{float(value):.3f}")
            elif kind == "depth":
                item.setText("" if value is None else f"{float(value):.2f}")
            elif kind == "class":
                code = str(value or "")
                label = ground_model.label_for(code, by_code) if code else "(unclassified)"
                text = code if not code or label.casefold() == code.casefold() \
                    else f"{code} — {label}"
                ui_helpers.ComboColumnDelegate.mark_item(item, code, text)
                if code:
                    item.setBackground(QColor(ground_model.color_for(code, by_code)))
            elif kind == "confidence":
                code = str(value or "")
                ui_helpers.ComboColumnDelegate.mark_item(
                    item, code, ground_model.CONFIDENCE_LABELS.get(code, code))
            else:
                item.setText("" if value is None else str(value))
            if not kind.endswith("_ro"):
                flags |= Qt.ItemFlag.ItemIsEditable
            item.setFlags(flags)
            if col == 0:
                item.setData(ITEM_DATA_USER_ROLE, unit.get("unit_id"))
            self.table.setItem(row, col, item)

    def _combo_options(self, index):
        column = index.column()
        if column == _COL_CLASS:
            options = [("", "(unclassified)")]
            seen = set()
            for cls in self.model.ground_classes:
                code = str(cls.get("code") or "")
                if code and code.casefold() not in seen:
                    seen.add(code.casefold())
                    label = str(cls.get("label") or code)
                    options.append((code, code if label.casefold() == code.casefold()
                                    else f"{code} — {label}"))
            for unit in self._working:
                code = unit.get("soil_class") or ""
                if code and code.casefold() not in seen:
                    seen.add(code.casefold())
                    options.append((code, code))
            return options
        if column == _COL_CONFIDENCE:
            return [(c, ground_model.CONFIDENCE_LABELS[c])
                    for c in ground_model.CONFIDENCE_LEVELS]
        return None

    def _combo_commit(self, index, value) -> None:
        row = index.row()
        if not (0 <= row < len(self._working)):
            return
        key = _COLUMNS[index.column()][1]
        self._working[row][key] = value or ""
        self._mark_dirty()
        if index.column() == _COL_CLASS:
            self._fill_row_cell(row, index.column())
            self._refresh_plot()

    def _fill_row_cell(self, row: int, column: int) -> None:
        self._loading = True
        try:
            with ui_helpers.silent_rebuild(self.table):
                unit = self._working[row]
                by_code = ground_model.class_lookup(self.model.ground_classes)
                item = self.table.item(row, column)
                if item is None:
                    return
                code = unit.get("soil_class") or ""
                label = ground_model.label_for(code, by_code) if code else "(unclassified)"
                text = code if not code or label.casefold() == code.casefold() \
                    else f"{code} — {label}"
                ui_helpers.ComboColumnDelegate.mark_item(item, code, text)
                item.setBackground(QColor(ground_model.color_for(code, by_code))
                                   if code else QBrush())
        finally:
            self._loading = False

    def _item_changed(self, item) -> None:
        if self._loading:
            return
        row, col = item.row(), item.column()
        if not (0 <= row < len(self._working)) or col >= len(_COLUMNS):
            return
        header, key, kind = _COLUMNS[col]
        text = item.text().strip()
        unit = self._working[row]
        if kind in ("kp", "depth"):
            if text == "":
                if kind == "kp" or key == "top_m":
                    self._restore_cell(row, col)
                    return
                unit[key] = None
            else:
                try:
                    value = float(text.replace(",", "."))
                except ValueError:
                    self._restore_cell(row, col)
                    return
                unit[key] = value
            self._restore_cell(row, col)  # canonical formatting
        elif kind in ("class", "confidence"):
            return  # handled by the delegate
        else:
            unit[key] = text
        self._mark_dirty()
        if kind in ("kp", "depth"):
            self._refresh_plot()

    def _restore_cell(self, row: int, col: int) -> None:
        self._loading = True
        try:
            with ui_helpers.silent_rebuild(self.table):
                unit = self._working[row]
                _header, key, kind = _COLUMNS[col]
                value = unit.get(key)
                item = self.table.item(row, col)
                if item is None:
                    return
                if kind.startswith("kp"):
                    item.setText("" if value is None else f"{float(value):.3f}")
                elif kind == "depth":
                    item.setText("" if value is None else f"{float(value):.2f}")
        finally:
            self._loading = False

    def _mark_dirty(self) -> None:
        self._dirty = True
        self._update_status()

    def _selected_rows(self) -> List[int]:
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        return [r for r in rows if 0 <= r < len(self._working)]

    def _selection_changed(self) -> None:
        rows = self._selected_rows()
        if len(rows) == 1:
            unit = self._working[rows[0]]
            self.plot.set_selected(str(unit.get("unit_id") or ""))
            if unit.get("start_kp") is not None and self.dock is not None:
                mid = (float(unit["start_kp"]) + float(unit.get("end_kp") or unit["start_kp"])) / 2.0
                self.dock.highlight_kp(mid)
                self.plot.show_kp(mid, float(unit.get("top_m") or 0.0))
        else:
            self.plot.set_selected("")

    def _row_activated(self, row: int, _column: int) -> None:
        if 0 <= row < len(self._working) and self.dock is not None:
            unit = self._working[row]
            if unit.get("start_kp") is not None and unit.get("end_kp") is not None:
                self.dock.goto_range(unit["start_kp"], unit["end_kp"])

    # -- editing -------------------------------------------------------------
    def _add_unit(self) -> None:
        scope = self.model.gen_params().scope
        rows = self._selected_rows()
        if rows:
            base = self._working[rows[-1]]
            start = float(base.get("end_kp") or scope.start_km)
        elif self._working:
            start = max(float(u["end_kp"]) for u in self._working if u.get("end_kp") is not None)
        else:
            start = float(scope.start_km)
        end = start + 1.0 if scope.length_km <= 0 else min(start + 1.0, max(scope.end_km, start + 0.01))
        self._working.append(ground_model.normalise_unit({
            "unit_id": schema.new_id(), "start_kp": start, "end_kp": end,
            "top_m": 0.0, "base_m": None, "soil_class": "", "description": "",
        }))
        self._mark_dirty()
        self._rebuild_table()
        self._refresh_plot()
        self.table.selectRow(len(self._working) - 1)

    def _duplicate_units(self) -> None:
        rows = self._selected_rows()
        if not rows:
            return
        for row in rows:
            copy = dict(self._working[row])
            copy["unit_id"] = schema.new_id()
            copy["src_start_kp"] = copy["src_end_kp"] = None
            copy["src_rpl"] = copy["rereference_flags"] = ""
            self._working.append(ground_model.normalise_unit(copy))
        self._mark_dirty()
        self._rebuild_table()
        self._refresh_plot()

    def _remove_units(self) -> None:
        rows = self._selected_rows()
        if not rows:
            return
        for row in reversed(rows):
            del self._working[row]
        self._mark_dirty()
        self._rebuild_table()
        self._refresh_plot()

    def _split_units(self) -> None:
        rows = self._selected_rows()
        if not rows:
            self._set_status("Select the unit(s) to split first.", "warn")
            return
        default = (float(self._working[rows[0]].get("start_kp") or 0.0)
                   + float(self._working[rows[0]].get("end_kp") or 0.0)) / 2.0
        kp, ok = QInputDialog.getDouble(self, "Split at KP", "KP (km):",
                                        default, -1e6, 1e6, 3)
        if not ok:
            return
        self._split_at(rows, float(kp))

    def _split_at(self, rows: List[int], kp: float) -> None:
        added = []
        for row in rows:
            unit = self._working[row]
            start, end = unit.get("start_kp"), unit.get("end_kp")
            if start is None or end is None or not (start + 1e-6 < kp < end - 1e-6):
                continue
            top_kp, base_kp = ground_model.unit_depths_at(unit, kp)
            right = dict(unit)
            right["unit_id"] = schema.new_id()
            right["start_kp"] = kp
            right["top_m"] = top_kp
            right["base_m"] = base_kp
            right["src_start_kp"] = right["src_end_kp"] = None
            right["rereference_flags"] = ""
            unit["end_kp"] = kp
            if unit.get("top_end_m") is not None:
                unit["top_end_m"] = top_kp
            if unit.get("base_end_m") is not None:
                unit["base_end_m"] = base_kp
            added.append(ground_model.normalise_unit(right))
        if not added:
            self._set_status(f"KP {schema.format_kp(kp)} is not inside the "
                             "selected unit(s).", "warn")
            return
        self._working.extend(added)
        self._working = ground_model.sort_units(self._working)
        self._mark_dirty()
        self._rebuild_table()
        self._refresh_plot()

    def _apply(self) -> None:
        issues = ground_model.validate_units(self._working)
        blocking = [i for i in issues if "required" in i or "greater than" in i]
        if blocking:
            QMessageBox.warning(self, "Ground model",
                                "Fix these before applying:\n\n" + "\n".join(blocking[:12]))
            return
        if issues:
            answer = QMessageBox.question(
                self, "Ground model",
                f"{len(issues)} note(s):\n\n" + "\n".join(issues[:10])
                + ("\n…" if len(issues) > 10 else "") + "\n\nApply anyway?")
            if answer != MESSAGE_BOX_YES:
                return
        new_classes = ground_model.missing_classes(self._working, self.model.ground_classes)
        if self.model.save_ground_units(self._working, reason="edited in Ground Model tab",
                                        new_classes=new_classes or None):
            self._dirty = False
            self.refresh()

    def _revert(self) -> None:
        self._dirty = False
        self.refresh()

    # -- import / export / re-reference / classes ------------------------------
    def _import(self) -> None:
        if not self.model.plan:
            return
        dialog = GroundImportDialog(self.model, self.dock, self)
        if qt_exec(dialog) != DIALOG_ACCEPTED:
            return
        units = list(dialog.units)
        if not dialog.replace_existing:
            units = self._working + units
        new_classes = ground_model.missing_classes(units, self.model.ground_classes)
        meta = {
            "source_ref": dialog.source_ref,
            "source_rpl": dialog.kp_map.source_label if dialog.kp_map is not None
            and not dialog.kp_map.is_identity else "",
            "method": dialog.kp_map.method if dialog.kp_map is not None else "",
            "kp_map": dialog.kp_map.to_dict() if dialog.kp_map is not None else {},
            "imported_utc": schema.utc_now_iso(),
        }
        if self.model.save_ground_units(
                units, action=change_log.ACTION_IMPORT_GROUND,
                reason=f"imported {len(dialog.units)} unit(s) from {dialog.source_ref or 'file'}",
                new_classes=new_classes or None,
                params_updates={"ground_model": meta}):
            self._dirty = False
            self.refresh()

    def _export(self) -> None:
        if not self.model.plan:
            return
        default = f"{schema.sanitize_slug(self.model.plan.get('name') or 'plan')}_ground_model.csv"
        path, _filter = QFileDialog.getSaveFileName(
            self, "Export ground model", default, "CSV (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(ground_model.units_csv(
                    self.model.plan, self._working, self.model.ground_classes))
        except OSError as exc:
            QMessageBox.warning(self, "Ground model", f"Export failed: {exc}")
            return
        self._set_status(f"Exported {len(self._working)} unit(s) to {path}.", "ok")

    def _rereference(self) -> None:
        if not self._working:
            self._set_status("There are no units to re-reference.", "warn")
            return
        if self._dirty:
            QMessageBox.information(
                self, "Ground model",
                "Apply or revert the table edits first — re-referencing works "
                "on the saved units.")
            return
        rows = self._selected_rows()
        targets = rows if rows else list(range(len(self._working)))
        dialog = RereferenceDialog(self.model, self.dock,
                                   [self._working[r] for r in targets], self)
        if qt_exec(dialog) != DIALOG_ACCEPTED or dialog.kp_map is None:
            return
        merged = list(self._working)
        for row, unit in zip(targets, dialog.units):
            merged[row] = unit
        meta = dict(self.model.ground_meta())
        meta.update({
            "source_rpl": dialog.kp_map.source_label or meta.get("source_rpl", ""),
            "method": dialog.kp_map.method,
            "kp_map": dialog.kp_map.to_dict(),
            "rereferenced_utc": schema.utc_now_iso(),
        })
        if self.model.save_ground_units(
                merged, action=change_log.ACTION_REREFERENCE_GROUND,
                reason=f"re-referenced {len(targets)} unit(s): "
                       f"{dialog.kp_map.diagnostics.summary()[:200]}",
                params_updates={"ground_model": meta}):
            self._dirty = False
            self.refresh()

    def _edit_classes(self) -> None:
        dialog = ClassesDialog(self.model, self)
        qt_exec(dialog)
        # Colours/labels may have changed even if no code did.
        self._rebuild_table()
        self._refresh_plot()

    # -- map layer ---------------------------------------------------------------
    def _map_toggled(self, checked: bool) -> None:
        QSettings().setValue(f"{_SETTINGS_ROOT}/ground_layer_visible", bool(checked))
        self._apply_map_visibility()

    def _apply_map_visibility(self) -> None:
        if not self.model.plan or self.dock is None:
            return
        try:
            from qgis.core import QgsProject

            from .. import map_layers
            map_layers.set_plan_layer_visibility(
                QgsProject.instance(), self.model.store.gpkg_path, self.model.plan,
                schema.ground_layer_name, self.show_map.isChecked())
        except Exception:
            pass

    # -- plot sync -------------------------------------------------------------
    def _plot_hover(self, kp: float) -> None:
        if self.dock is not None:
            self.dock.highlight_kp(kp)

    def _plot_clicked(self, kp: float) -> None:
        if self.dock is not None:
            self.dock.goto_kp(kp)

    def _plot_unit_clicked(self, unit_id: str) -> None:
        for row, unit in enumerate(self._working):
            if str(unit.get("unit_id") or "") == unit_id:
                self.table.selectRow(row)
                self.table.scrollToItem(self.table.item(row, 0))
                break

    def sync_kp(self, kp: float) -> None:
        """Mirror the profile crosshair (called by the dock)."""
        if self._working:
            self.plot.show_kp(kp, 0.0)

# -*- coding: utf-8 -*-
"""BAS tab — the Burial Assessment Study register as an editable
KP-range spreadsheet.

Fixed columns (start/end KP) plus the plan's own column list, imported
from a CSV/XLSX table with the KP reference stated (which RPL revision
the study's KPs follow) or built by hand. Every cell is editable; the
table copies/pastes tab-separated text, fills down, clears with Delete
and undoes cell edits with Ctrl+Z; header clicks sort; a coverage strip
shows which part of the scope the study covers. *Apply* validates and
saves the rows as one undoable change-log entry.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from qgis.PyQt.QtCore import QSettings, Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...qgis_compat import (
    CONTEXT_MENU_POLICY_CUSTOM,
    DIALOG_ACCEPTED,
    EDIT_TRIGGER_DOUBLE_CLICKED,
    EDIT_TRIGGER_EDIT_KEY_PRESSED,
    EDIT_TRIGGER_SELECTED_CLICKED,
    HEADER_RESIZE_MODE_INTERACTIVE,
    ITEM_DATA_USER_ROLE,
    MESSAGE_BOX_YES,
    SELECTION_MODE_EXTENDED,
    qt_exec,
)
from ...workbench.kp_bars import VerdictStrip
from .. import bas_model, change_log, schema, ui_helpers
from ..bas_dialogs import BasColumnsDialog, BasImportDialog
from ..ground_dialogs import RereferenceDialog
from ..spreadsheet_table import SpreadsheetTable

_SETTINGS_ROOT = "SubseaCableTools/BurialPlanner"
_SORT_ORDER = getattr(Qt, "SortOrder", Qt)
_PROVENANCE = [("Src start KP", "src_start_kp", "kp_ro"),
               ("Src end KP", "src_end_kp", "kp_ro"),
               ("Src RPL", "src_rpl", "text_ro"),
               ("Re-ref flags", "rereference_flags", "text_ro"),
               ("Notes", "notes", "text")]
_DEFAULT_HIDDEN = ("Src start KP", "Src end KP", "Src RPL", "Re-ref flags")


class BasTab(QWidget):
    def __init__(self, model, dock, parent=None):
        super().__init__(parent)
        self.model = model
        self.dock = dock
        self._loading = False
        self._dirty = False
        self._loaded_plan_id = ""
        self._working: List[Dict] = []
        self._columns: List[Dict] = []
        self._specs: List[tuple] = []   # (header, key, kind) per table column
        self._sort: Optional[tuple] = None  # (column index, descending)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        intro = QLabel(
            "Burial Assessment Study conclusions as KP ranges: required depth "
            "of lowering, achievable depth per tool, risk category, soil "
            "province — whatever the study delivers. Import the study's table "
            "(stating which RPL revision its KPs follow) or build the register "
            "here; every cell is editable, and the columns are yours to shape.")
        intro.setWordWrap(True)
        intro.setStyleSheet(ui_helpers.hint_style())
        layout.addWidget(intro)

        toolbar = QHBoxLayout()
        self.import_button = QPushButton("Import…")
        self.import_button.setToolTip("Import KP ranges and their columns from a "
                                      "CSV/XLSX table with KP re-referencing.")
        self.import_button.clicked.connect(self._import)
        self.export_button = QPushButton("Export CSV")
        self.export_button.clicked.connect(self._export)
        self.rereference_button = QPushButton("Re-reference KPs…")
        self.rereference_button.setToolTip(
            "Bring the selected rows (or all) onto the plan's current route "
            "from the RPL revision their KPs were delivered against.")
        self.rereference_button.clicked.connect(self._rereference)
        self.columns_button = QPushButton("Columns…")
        self.columns_button.setToolTip("Add, rename, retype, reorder or remove "
                                       "the register's columns.")
        self.columns_button.clicked.connect(self._edit_columns)
        for button in (self.import_button, self.export_button,
                       self.rereference_button, self.columns_button):
            toolbar.addWidget(button)
        self.show_map = QCheckBox("Show on map")
        self.show_map.setToolTip(
            "Draw every register row as a line beside the route (offset to "
            "starboard, the ground model sits to port) carrying all its "
            "columns as attributes — identify or open the attribute table "
            "to read the study's values along the route.")
        self.show_map.setChecked(bool(QSettings().value(
            f"{_SETTINGS_ROOT}/bas_layer_visible", True, type=bool)))
        self.show_map.toggled.connect(self._map_toggled)
        toolbar.addWidget(self.show_map)
        toolbar.addStretch(1)
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        toolbar.addWidget(self.status_label, 2)
        layout.addLayout(toolbar)

        self.strip = VerdictStrip()
        self.strip.setToolTip("Scope coverage of the register (green = covered; "
                              "dark = selected rows). Click to select the row at a KP.")
        self.strip.kpClicked.connect(self._strip_clicked)
        layout.addWidget(self.strip)

        edit_row = QHBoxLayout()
        self.add_button = QPushButton("Add row")
        self.add_button.clicked.connect(lambda: self._insert_rows(None, 1))
        self.duplicate_button = QPushButton("Duplicate")
        self.duplicate_button.clicked.connect(self._duplicate_rows)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self._remove_rows)
        self.sort_button = QPushButton("Sort by KP")
        self.sort_button.clicked.connect(self._sort_by_kp)
        for button in (self.add_button, self.duplicate_button,
                       self.remove_button, self.sort_button):
            edit_row.addWidget(button)
        edit_row.addStretch(1)
        self.apply_button = QPushButton("Apply changes")
        self.apply_button.setToolTip("Validate and save the register (one undoable entry).")
        self.apply_button.clicked.connect(self._apply)
        self.revert_button = QPushButton("Revert")
        self.revert_button.clicked.connect(self._revert)
        edit_row.addWidget(self.apply_button)
        edit_row.addWidget(self.revert_button)
        layout.addLayout(edit_row)

        self.table = SpreadsheetTable(0, 0)
        self.table.setSelectionMode(SELECTION_MODE_EXTENDED)
        self.table.setEditTriggers(EDIT_TRIGGER_DOUBLE_CLICKED
                                   | EDIT_TRIGGER_EDIT_KEY_PRESSED
                                   | EDIT_TRIGGER_SELECTED_CLICKED)
        self.table.setToolTip(
            "Double-click or start typing to edit. Ctrl+C / Ctrl+V copy and "
            "paste tab-separated cells (paste from Excel adds rows as needed); "
            "Delete clears; Ctrl+D fills down; Ctrl+Z / Ctrl+Y undo and redo. "
            "Click a header to sort; right-click it to hide or show columns.")
        # Paste overflow appends at the end (spreadsheet semantics), never
        # in the middle of the register.
        self.table.add_rows_callback = lambda n: self._insert_rows(
            len(self._working), n, select=False)
        self.table.itemChanged.connect(self._item_changed)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.cellDoubleClicked.connect(self._row_activated)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(HEADER_RESIZE_MODE_INTERACTIVE)
        header.setStretchLastSection(True)
        header.setSectionsClickable(True)
        header.sectionClicked.connect(self._header_clicked)
        self.table.setContextMenuPolicy(CONTEXT_MENU_POLICY_CUSTOM)
        self.table.customContextMenuRequested.connect(self._context_menu)
        layout.addWidget(self.table, 1)

        refresh_soon = ui_helpers.coalesced(self, self.refresh)
        model.planChanged.connect(refresh_soon)
        model.basChanged.connect(refresh_soon)
        self.refresh()

    # -- refresh -----------------------------------------------------------------
    def refresh(self) -> None:
        plan_id = self.model.plan_id
        has_plan = bool(self.model.plan)
        for button in (self.import_button, self.export_button, self.rereference_button,
                       self.columns_button, self.add_button, self.duplicate_button,
                       self.remove_button, self.sort_button, self.apply_button,
                       self.revert_button):
            button.setEnabled(has_plan)
        if not has_plan:
            self._working, self._columns = [], []
            self._dirty = False
            self._loaded_plan_id = ""
            self._rebuild_table()
            self.strip.set_spans(0.0, [])
            self._set_status("No plan selected.")
            return
        columns = self.model.bas_columns()
        if self._dirty and plan_id == self._loaded_plan_id:
            if columns != self._columns:
                # Columns changed underneath unapplied edits: reshape the
                # table but keep the working rows (values are keyed).
                self._columns = columns
                self._rebuild_table()
            self._refresh_strip()
            self._update_status()
            return
        self._working = [bas_model.decode_row(r) for r in self.model.bas_rows]
        self._columns = columns
        self._dirty = False
        self._loaded_plan_id = plan_id
        self._sort = None
        self._rebuild_table()
        self.table.clear_history()
        self._refresh_strip()
        self._update_status()
        self._apply_map_visibility()

    def _refresh_strip(self) -> None:
        scope = self.model.gen_params().scope
        if scope.length_km <= 0:
            self.strip.set_spans(0.0, [])
            return
        selected = {self._working[r].get("row_id") for r in self._selected_rows()}
        spans = []
        for row in self._working:
            if row.get("start_kp") is None or row.get("end_kp") is None:
                continue
            color = QColor(27, 100, 45) if row.get("row_id") in selected \
                else QColor(120, 190, 120)
            spans.append((float(row["start_kp"]), float(row["end_kp"]), color))
        self.strip.set_spans(scope.length_km, spans, "BAS coverage",
                             domain_start_km=scope.start_km)

    def _update_status(self) -> None:
        if not self._working:
            self._set_status("No BAS rows for this plan yet — import the study's "
                             "table or add rows.")
            return
        with_kp = [r for r in self._working
                   if r.get("start_kp") is not None and r.get("end_kp") is not None]
        text = f"{len(self._working)} row(s), {len(self._columns)} column(s)"
        if with_kp:
            lo = min(r["start_kp"] for r in with_kp)
            hi = max(r["end_kp"] for r in with_kp)
            text += f", KP {schema.format_kp(lo)}–{schema.format_kp(hi)}"
        text += "."
        kind = ""
        scope = self.model.gen_params().scope
        if scope.length_km > 0:
            gaps = bas_model.coverage_gaps(with_kp, scope.start_km, scope.end_km)
            missing = sum(b - a for a, b in gaps)
            if missing > 1e-6:
                text += f" {missing:.3f} km of the scope has no BAS row."
                kind = "warn"
        meta = self.model.bas_meta()
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

    # -- table ---------------------------------------------------------------------
    def _build_specs(self) -> None:
        self._specs = [("Start KP", "start_kp", "kp"), ("End KP", "end_kp", "kp")]
        for col in self._columns:
            self._specs.append((col["label"], col["key"], col.get("kind") or "text"))
        self._specs.extend(_PROVENANCE)

    def _rebuild_table(self) -> None:
        self._loading = True
        try:
            self._build_specs()
            with ui_helpers.preserve_table_view(self.table, id_column=0):
                with self.table.rebuilding():
                    self.table.setColumnCount(len(self._specs))
                    self.table.setHorizontalHeaderLabels([s[0] for s in self._specs])
                    self.table.setRowCount(len(self._working))
                    for i, row in enumerate(self._working):
                        self._fill_row(i, row)
            self.table.editable_columns = {
                i for i, spec in enumerate(self._specs) if not spec[2].endswith("_ro")}
            ui_helpers.enable_column_menu(
                self.table, f"{_SETTINGS_ROOT}/bas_table_columns",
                always_visible=(0, 1), default_hidden=_DEFAULT_HIDDEN)
            self.table.resizeColumnsToContents()
            for c in range(self.table.columnCount()):
                if self.table.columnWidth(c) > 260:
                    self.table.setColumnWidth(c, 260)
            self._show_sort_indicator()
        finally:
            self._loading = False

    def _cell_text(self, row: Dict, key: str, kind: str) -> str:
        if key in ("start_kp", "end_kp", "src_start_kp", "src_end_kp"):
            value = row.get(key)
            return "" if value is None else f"{float(value):.3f}"
        if key in ("src_rpl", "rereference_flags", "notes"):
            return str(row.get(key) or "")
        return str(row.get("values", {}).get(key, ""))

    def _fill_row(self, index: int, row: Dict) -> None:
        for col, (header, key, kind) in enumerate(self._specs):
            item = QTableWidgetItem(self._cell_text(row, key, kind))
            flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            if not kind.endswith("_ro"):
                flags |= Qt.ItemFlag.ItemIsEditable
            if kind in ("kp", "kp_ro", "number"):
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                      | Qt.AlignmentFlag.AlignVCenter)
            item.setFlags(flags)
            if col == 0:
                item.setData(ITEM_DATA_USER_ROLE, row.get("row_id"))
            self.table.setItem(index, col, item)

    def _item_changed(self, item) -> None:
        if self._loading:
            return
        r, c = item.row(), item.column()
        if not (0 <= r < len(self._working)) or c >= len(self._specs):
            return
        _header, key, kind = self._specs[c]
        text = item.text().strip()
        row = self._working[r]
        if kind == "kp":
            value = None
            if text:
                try:
                    value = float(text.replace(",", "."))
                except ValueError:
                    value = row.get(key)
            row[key] = value
            self._loading = True
            try:
                self.table.amend_last(r, c, self._cell_text(row, key, kind))
            finally:
                self._loading = False
        elif kind.endswith("_ro"):
            return
        elif key in ("notes",):
            row[key] = text
        else:
            row.setdefault("values", {})[key] = text
        self._mark_dirty()
        if kind == "kp":
            self._refresh_strip()

    def _mark_dirty(self) -> None:
        self._dirty = True
        self._update_status()

    def _selected_rows(self) -> List[int]:
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        return [r for r in rows if 0 <= r < len(self._working)]

    def _selection_changed(self) -> None:
        self._refresh_strip()
        rows = self._selected_rows()
        if self.dock is None or not rows:
            return
        ranges = [(float(self._working[r]["start_kp"]), float(self._working[r]["end_kp"]))
                  for r in rows
                  if self._working[r].get("start_kp") is not None
                  and self._working[r].get("end_kp") is not None]
        if not ranges:
            return
        try:
            if len(ranges) == 1:
                self.dock.highlight_range(*ranges[0])
            else:
                self.dock.highlight_ranges(ranges)
        except Exception:
            pass

    def _row_activated(self, row: int, _column: int) -> None:
        if 0 <= row < len(self._working) and self.dock is not None:
            entry = self._working[row]
            if entry.get("start_kp") is not None and entry.get("end_kp") is not None:
                self.dock.goto_range(entry["start_kp"], entry["end_kp"])

    def _strip_clicked(self, kp: float) -> None:
        hits = bas_model.rows_at_kp(self._working, kp)
        if not hits:
            if self.dock is not None:
                self.dock.goto_kp(kp)
            return
        wanted = hits[0].get("row_id")
        for r, row in enumerate(self._working):
            if row.get("row_id") == wanted:
                self.table.selectRow(r)
                self.table.scrollToItem(self.table.item(r, 0))
                break
        if self.dock is not None:
            self.dock.goto_kp(kp)

    # -- map layer ---------------------------------------------------------------
    def _map_toggled(self, checked: bool) -> None:
        QSettings().setValue(f"{_SETTINGS_ROOT}/bas_layer_visible", bool(checked))
        self._apply_map_visibility()

    def _apply_map_visibility(self) -> None:
        if not self.model.plan or self.dock is None:
            return
        try:
            from qgis.core import QgsProject

            from .. import map_layers
            map_layers.set_plan_layer_visibility(
                QgsProject.instance(), self.model.store.gpkg_path, self.model.plan,
                schema.bas_layer_name, self.show_map.isChecked())
        except Exception:
            pass

    # -- sorting -------------------------------------------------------------------
    def _header_clicked(self, column: int) -> None:
        if column >= len(self._specs):
            return
        descending = bool(self._sort and self._sort[0] == column and not self._sort[1])
        self._sort = (column, descending)
        _header, key, kind = self._specs[column]

        def sort_key(row: Dict):
            if key in ("start_kp", "end_kp", "src_start_kp", "src_end_kp"):
                value = row.get(key)
                return (1, 0.0, "") if value is None else (0, float(value), "")
            if key in ("src_rpl", "rereference_flags", "notes"):
                text = str(row.get(key) or "")
            else:
                text = str(row.get("values", {}).get(key, ""))
            if kind == "number":
                number = bas_model._to_number(text)
                return (1, 0.0, text.casefold()) if number is None else (0, number, "")
            return (1 if not text else 0, 0.0, text.casefold())

        self._working.sort(key=sort_key, reverse=descending)
        self._mark_dirty()
        self._rebuild_table()

    def _sort_by_kp(self) -> None:
        self._sort = (0, False)
        self._working = bas_model.sort_rows(self._working)
        self._mark_dirty()
        self._rebuild_table()

    def _show_sort_indicator(self) -> None:
        header = self.table.horizontalHeader()
        if self._sort is None:
            header.setSortIndicatorShown(False)
            return
        header.setSortIndicatorShown(True)
        header.setSortIndicator(self._sort[0], _SORT_ORDER.DescendingOrder
                                if self._sort[1] else _SORT_ORDER.AscendingOrder)

    # -- row editing ---------------------------------------------------------------
    def _new_row(self, after: Optional[Dict]) -> Dict:
        scope = self.model.gen_params().scope
        if after is not None and after.get("end_kp") is not None:
            start = float(after["end_kp"])
        elif self._working:
            ends = [float(r["end_kp"]) for r in self._working if r.get("end_kp") is not None]
            start = max(ends) if ends else float(scope.start_km)
        else:
            start = float(scope.start_km)
        end = start + 1.0
        if scope.length_km > 0:
            end = min(end, max(scope.end_km, start + 0.01))
        return bas_model.decode_row({"row_id": schema.new_id(), "start_kp": start,
                                     "end_kp": end, "values": {}})

    def _insert_rows(self, at: Optional[int], count: int, select: bool = True) -> None:
        if not self.model.plan:
            return
        if at is None:
            selected = self._selected_rows()
            at = (selected[-1] + 1) if selected else len(self._working)
        at = max(0, min(at, len(self._working)))
        previous = self._working[at - 1] if at > 0 else None
        fresh = []
        for _ in range(max(1, count)):
            row = self._new_row(previous)
            fresh.append(row)
            previous = row
        self._working[at:at] = fresh
        self._mark_dirty()
        self._rebuild_table()
        self._refresh_strip()
        if select:
            self.table.clearSelection()
            self.table.selectRow(at)
            self.table.scrollToItem(self.table.item(at, 0))

    def _duplicate_rows(self) -> None:
        rows = self._selected_rows()
        if not rows:
            return
        copies = []
        for r in rows:
            copy = bas_model.decode_row(self._working[r])
            copy["row_id"] = schema.new_id()
            copy["values"] = dict(copy["values"])
            copy["src_start_kp"] = copy["src_end_kp"] = None
            copy["src_rpl"] = copy["rereference_flags"] = ""
            copies.append(copy)
        at = rows[-1] + 1
        self._working[at:at] = copies
        self._mark_dirty()
        self._rebuild_table()
        self._refresh_strip()

    def _remove_rows(self) -> None:
        rows = self._selected_rows()
        if not rows:
            return
        for r in reversed(rows):
            del self._working[r]
        self._mark_dirty()
        self._rebuild_table()
        self._refresh_strip()

    def _context_menu(self, pos) -> None:
        menu = QMenu(self)
        index = self.table.indexAt(pos)
        at = index.row() if index.isValid() else len(self._working)
        menu.addAction("Insert row above", lambda: self._insert_rows(at, 1))
        menu.addAction("Insert row below", lambda: self._insert_rows(at + 1, 1))
        menu.addAction("Duplicate row(s)", self._duplicate_rows)
        menu.addAction("Delete row(s)", self._remove_rows)
        menu.addSeparator()
        menu.addAction("Copy\tCtrl+C", self.table.copy_selection)
        menu.addAction("Paste\tCtrl+V", self.table.paste)
        menu.addAction("Fill down\tCtrl+D", self.table.fill_down)
        menu.addAction("Clear cells\tDel", self.table.clear_selection_cells)
        menu.addSeparator()
        menu.addAction("Undo cell edit\tCtrl+Z", self.table.undo)
        menu.addAction("Redo\tCtrl+Y", self.table.redo)
        qt_exec(menu, self.table.viewport().mapToGlobal(pos))

    # -- apply / revert ----------------------------------------------------------------
    def _apply(self) -> None:
        issues = bas_model.validate_rows(self._working, self._columns)
        blocking = [i for i in issues if "required" in i or "greater than" in i]
        if blocking:
            QMessageBox.warning(self, "BAS register",
                                "Fix these before applying:\n\n" + "\n".join(blocking[:12]))
            return
        if issues:
            answer = QMessageBox.question(
                self, "BAS register",
                f"{len(issues)} note(s):\n\n" + "\n".join(issues[:10])
                + ("\n…" if len(issues) > 10 else "") + "\n\nApply anyway?")
            if answer != MESSAGE_BOX_YES:
                return
        keep = {c["key"] for c in self._columns}
        for row in self._working:
            row["values"] = {k: v for k, v in row.get("values", {}).items() if k in keep}
        if self.model.save_bas(self._working, reason="edited in BAS tab"):
            self._dirty = False
            self.refresh()

    def _revert(self) -> None:
        self._dirty = False
        self.refresh()

    # -- import / export / re-reference / columns ----------------------------------
    def _import(self) -> None:
        if not self.model.plan:
            return
        dialog = BasImportDialog(self.model, self.dock, self)
        if qt_exec(dialog) != DIALOG_ACCEPTED:
            return
        rows = list(dialog.rows)
        columns = list(dialog.columns)
        if not dialog.replace_existing:
            rows = self._working + rows
        meta = {
            "source_ref": dialog.source_ref,
            "source_rpl": dialog.kp_map.source_label
            if dialog.kp_map is not None and not dialog.kp_map.is_identity else "",
            "method": dialog.kp_map.method if dialog.kp_map is not None else "",
            "kp_map": dialog.kp_map.to_dict() if dialog.kp_map is not None else {},
            "imported_utc": schema.utc_now_iso(),
        }
        if self.model.save_bas(
                rows, columns=columns, action=change_log.ACTION_IMPORT_BAS,
                reason=f"imported {len(dialog.rows)} row(s) from {dialog.source_ref or 'file'}",
                meta_updates=meta):
            self._dirty = False
            self.refresh()

    def _export(self) -> None:
        if not self.model.plan:
            return
        default = f"{schema.sanitize_slug(self.model.plan.get('name') or 'plan')}_bas.csv"
        path, _filter = QFileDialog.getSaveFileName(
            self, "Export BAS register", default, "CSV (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(bas_model.rows_csv(self.model.plan, self._working,
                                                self._columns))
        except OSError as exc:
            QMessageBox.warning(self, "BAS register", f"Export failed: {exc}")
            return
        self._set_status(f"Exported {len(self._working)} row(s) to {path}.", "ok")

    def _rereference(self) -> None:
        if not self._working:
            self._set_status("There are no rows to re-reference.", "warn")
            return
        if self._dirty:
            QMessageBox.information(
                self, "BAS register",
                "Apply or revert the table edits first — re-referencing works "
                "on the saved rows.")
            return
        rows = self._selected_rows()
        targets = rows if rows else list(range(len(self._working)))
        dialog = RereferenceDialog(self.model, self.dock,
                                   [self._working[r] for r in targets], self,
                                   mapper=bas_model.rereference_rows,
                                   title="Re-reference BAS KPs")
        if qt_exec(dialog) != DIALOG_ACCEPTED or dialog.kp_map is None:
            return
        merged = list(self._working)
        for r, row in zip(targets, dialog.units):
            merged[r] = bas_model.decode_row(row)
        meta = {
            "source_rpl": dialog.kp_map.source_label or self.model.bas_meta().get("source_rpl", ""),
            "method": dialog.kp_map.method,
            "kp_map": dialog.kp_map.to_dict(),
            "rereferenced_utc": schema.utc_now_iso(),
        }
        if self.model.save_bas(
                merged, action=change_log.ACTION_REREFERENCE_BAS,
                reason=f"re-referenced {len(targets)} row(s): "
                       f"{dialog.kp_map.diagnostics.summary()[:200]}",
                meta_updates=meta):
            self._dirty = False
            self.refresh()

    def _edit_columns(self) -> None:
        dialog = BasColumnsDialog(self._columns, self)
        if qt_exec(dialog) != DIALOG_ACCEPTED:
            return
        if self._dirty:
            # Keep the unapplied rows; only the shape changes now, the
            # values are saved with the next Apply.
            self._columns = dialog.columns
            self.model.save_bas_columns(dialog.columns, reason="BAS columns edited")
            self._rebuild_table()
            self._update_status()
            return
        self.model.save_bas_columns(dialog.columns, reason="BAS columns edited")

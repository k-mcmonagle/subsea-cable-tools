# -*- coding: utf-8 -*-
"""Assembly Manager panel (hosted by the Cable Route Workbench dock).

Import/create/edit cable and rigging assemblies for the project, with a
Straight Line Diagram (SLD) that stays workable at hundreds of
sections/bodies. Assemblies persist in the workbench GeoPackage
(wb_assembly / wb_assembly_item) and round-trip to the catenary calculators'
JSON format.

Import sources: catenary JSON (clipboard), extract from a registered RPL
(with an event-classification review step). Export: catenary JSON to
clipboard — paste straight into Catenary Calculator V2 / Cable Lay Simulator.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional

from qgis.PyQt.QtCore import Qt, QSettings, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from . import assembly_model as am
from . import schema
from .assembly_model import Assembly, AssemblyItem
from .readonly import make_readonly_banner
from .selection_bus import selection_bus
from .store import (
    WorkbenchReadOnlyError,
    WorkbenchStore,
    default_project_gpkg_path,
    project_gpkg_path,
)

ITEM_COLUMNS = [
    ("Kind", "kind"), ("Name", "name"), ("Length (m)", "length_m"),
    ("q water (N/m)", "q_water_npm"), ("q air (N/m)", "q_air_npm"),
    ("Load (kN)", "point_load_kN"), ("µ", "friction_mu"),
    ("EI (kN·m²)", "bending_stiffness_kNm2"), ("MBR (m)", "min_bend_radius_m"),
    ("Ø (m)", "diameter_m"), ("Cd n", "cd_normal"), ("Cd t", "cd_tangential"),
    ("Cable type", "cable_type"), ("Code", "cable_code"), ("Fibre", "fiber_pair"),
    ("Colour", "color_hex"), ("Remarks", "remarks"),
]
_FLOAT_COLS = {2, 3, 4, 5, 6, 7, 8, 9, 10, 11}
_ENGINEERING_COLS = list(range(3, 12))
_ENGINEERING_COLS_SETTING = "SubseaCableTools/workbench/assemblyEngineeringColumns"


class AssemblyManagerPanel(QWidget):
    """Assembly library + SLD + items table.

    A plain widget so it can be embedded in the unified Workbench dock
    (``embedded=True`` hides the library column — the dock's entity tree
    drives selection instead).
    """

    assembly_saved = pyqtSignal(str)   # assembly_id
    assemblies_changed = pyqtSignal()  # library membership changed

    def __init__(self, iface, parent=None, embedded=False):
        super().__init__(parent)
        self.iface = iface
        self.embedded = embedded
        self.settings = QSettings()
        self.store: Optional[WorkbenchStore] = None
        self.assembly: Optional[Assembly] = None
        self._current_header: Optional[Dict] = None
        self._loading = False
        self._fit_row: Optional[Dict] = None  # active fit context for the KP axis
        self._read_only = False

        self._build_ui()
        self._open_store()
        self.refresh_assembly_list()
        selection_bus().cableDistSelected.connect(self._on_bus_cable_dist)

    # ------------------------------------------------------------------ UI --
    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(splitter)

        # left: assembly library (hidden when embedded in the Workbench dock)
        self.browser = QWidget()
        left_layout = QVBoxLayout(self.browser)
        left_layout.addWidget(QLabel("Assemblies"))
        self.assembly_list = QListWidget()
        self.assembly_list.currentItemChanged.connect(self._on_assembly_selected)
        left_layout.addWidget(self.assembly_list)

        row1 = QHBoxLayout()
        new_btn = QPushButton("New")
        new_btn.clicked.connect(self._new_assembly)
        dup_btn = QPushButton("Duplicate")
        dup_btn.clicked.connect(self._duplicate_assembly)
        del_btn = QPushButton("Delete")
        del_btn.clicked.connect(self._delete_assembly)
        for btn in (new_btn, dup_btn, del_btn):
            row1.addWidget(btn)
        left_layout.addLayout(row1)

        row2 = QHBoxLayout()
        import_btn = QPushButton("Import ▾")
        import_menu = QMenu(import_btn)
        import_menu.addAction("From catenary JSON (clipboard)", self._import_catenary_clipboard)
        import_menu.addAction("Extract from registered RPL…", lambda: self._extract_from_rpl())
        import_menu.addSeparator()
        import_menu.addAction("Reset event classification rules to defaults",
                              self._reset_event_rules)
        import_btn.setMenu(import_menu)
        export_btn = QPushButton("Export ▾")
        export_menu = QMenu(export_btn)
        export_menu.addAction("Catenary JSON to clipboard", self._export_catenary_clipboard)
        export_btn.setMenu(export_menu)
        row2.addWidget(import_btn)
        row2.addWidget(export_btn)
        left_layout.addLayout(row2)
        splitter.addWidget(self.browser)
        if self.embedded:
            self.browser.setVisible(False)

        # right: SLD + items table
        right = QSplitter(Qt.Orientation.Vertical)
        try:
            from .sld_widget import SldWidget

            self.sld = SldWidget()
            self.sld.itemClicked.connect(self._on_sld_item_clicked)
            right.addWidget(self.sld)
        except Exception:
            self.sld = None
            right.addWidget(QLabel("SLD unavailable (pyqtgraph could not be loaded)."))

        table_container = QWidget()
        table_layout = QVBoxLayout(table_container)
        self.readonly_banner = make_readonly_banner(table_container)
        table_layout.addWidget(self.readonly_banner)
        toolbar = QHBoxLayout()
        self.add_section_btn = QPushButton("Add section")
        self.add_section_btn.clicked.connect(lambda: self._add_item(am.KIND_SECTION))
        self.add_body_btn = QPushButton("Add body")
        self.add_body_btn.clicked.connect(lambda: self._add_item(am.KIND_BODY))
        self.remove_item_btn = QPushButton("Delete item")
        self.remove_item_btn.clicked.connect(self._delete_item)
        self.move_up_btn = QPushButton("Move up")
        self.move_up_btn.clicked.connect(lambda: self._move_item(-1))
        self.move_down_btn = QPushButton("Move down")
        self.move_down_btn.clicked.connect(lambda: self._move_item(1))
        for btn in (self.add_section_btn, self.add_body_btn, self.remove_item_btn,
                    self.move_up_btn, self.move_down_btn):
            toolbar.addWidget(btn)
        self.engineering_cols_check = QCheckBox("Engineering columns")
        self.engineering_cols_check.stateChanged.connect(self._on_engineering_columns_changed)
        toolbar.addWidget(self.engineering_cols_check)
        self.summary_label = QLabel("")
        toolbar.addWidget(self.summary_label)
        toolbar.addStretch()
        table_layout.addLayout(toolbar)

        self.items_table = QTableWidget(0, len(ITEM_COLUMNS))
        self.items_table.setHorizontalHeaderLabels([c[0] for c in ITEM_COLUMNS])
        self.items_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.items_table.itemChanged.connect(self._on_item_changed)
        self.items_table.itemSelectionChanged.connect(self._on_table_selection)
        table_layout.addWidget(self.items_table)
        right.addWidget(table_container)
        right.setStretchFactor(0, 0)
        right.setStretchFactor(1, 1)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

    def set_read_only(self, read_only: bool) -> None:
        self._read_only = bool(read_only)
        self.readonly_banner.setVisible(self._read_only)
        for btn in (self.add_section_btn, self.add_body_btn, self.remove_item_btn,
                    self.move_up_btn, self.move_down_btn):
            btn.setEnabled(not self._read_only)

    def _can_edit_current(self) -> bool:
        return not self._read_only

    # --------------------------------------------------------------- store --
    def _open_store(self):
        path = project_gpkg_path() or default_project_gpkg_path()
        self.store = WorkbenchStore(path)

    def refresh_assembly_list(self):
        self._open_store()
        previous = self.assembly.assembly_id if self.assembly is not None else None
        self.assembly_list.blockSignals(True)
        self.assembly_list.clear()
        restore_row = -1
        if self.store and self.store.exists():
            for row in self.store.list_assemblies():
                total_km = (row.get("total_cable_len_m") or 0.0) / 1000.0
                item = QListWidgetItem(f"{row.get('name')}  ({row.get('kind')}, {total_km:.2f} km)")
                item.setData(Qt.ItemDataRole.UserRole, row.get("assembly_id"))
                self.assembly_list.addItem(item)
                if row.get("assembly_id") == previous:
                    restore_row = self.assembly_list.count() - 1
        if restore_row >= 0:
            # restore without reloading the assembly being edited
            self.assembly_list.setCurrentRow(restore_row)
        self.assembly_list.blockSignals(False)
        if restore_row < 0 and self.assembly_list.count():
            self.assembly_list.setCurrentRow(0)

    def select_assembly(self, assembly_id: str):
        """Programmatic selection (used by the Workbench entity tree)."""
        self._select_assembly(assembly_id)

    def set_fit_context(self, fit_row: Optional[Dict]):
        """Use a specific fit for the SLD's KP axis (None = auto/first fit)."""
        self._fit_row = fit_row
        self._apply_kp_axis()

    def _apply_kp_axis(self):
        if self.sld is None:
            return
        mapping = None
        if self.assembly is not None and self.store is not None and self.store.exists():
            fit_row = self._fit_row
            if fit_row is None or fit_row.get("assembly_id") != self.assembly.assembly_id:
                fits = self.store.list_fits(assembly_id=self.assembly.assembly_id)
                fit_row = fits[0] if fits else None
            if fit_row is not None:
                from .fit import build_fit_mapping

                mapping = build_fit_mapping(self.store, fit_row)
        self.sld.set_kp_mapping(mapping)

    def _on_assembly_selected(self, current, _previous=None):
        if current is None:
            self.assembly = None
            self._current_header = None
            self.set_read_only(False)
            self._refresh_views()
            return
        assembly_id = current.data(Qt.ItemDataRole.UserRole)
        header, items = self.store.get_assembly(assembly_id)
        self._current_header = header
        self.assembly = am.assembly_from_rows(header, items) if header else None
        self.set_read_only(bool(header and header.get("status") == schema.STATUS_ISSUED))
        self._refresh_views()
        self._apply_kp_axis()

    def _persist(self, library_changed: bool = False):
        if self.assembly is None or self.store is None:
            return
        self.store.ensure_created()
        header, items = am.assembly_to_rows(self.assembly)
        try:
            self.store.save_assembly(header, items)
        except WorkbenchReadOnlyError as exc:
            self.iface.messageBar().pushWarning("Assembly read-only", str(exc))
            return
        self._current_header, _ = self.store.get_assembly(self.assembly.assembly_id)
        self.assembly_saved.emit(self.assembly.assembly_id)
        if library_changed:
            self.assemblies_changed.emit()

    # ----------------------------------------------------------- rendering --
    def _refresh_views(self):
        self._loading = True
        try:
            self.items_table.setRowCount(0)
            if self.assembly is not None:
                self.items_table.setRowCount(len(self.assembly.items))
                for row, item in enumerate(self.assembly.items):
                    for col, (_, attr) in enumerate(ITEM_COLUMNS):
                        value = getattr(item, attr)
                        if value is None:
                            text = ""
                        elif col in _FLOAT_COLS:
                            text = f"{float(value):.6g}"
                        else:
                            text = str(value)
                        cell = QTableWidgetItem(text)
                        if self._read_only or attr == "kind":
                            cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                        if attr == "color_hex" and value:
                            color = QColor(str(value))
                            if color.isValid():
                                cell.setBackground(color)
                        self.items_table.setItem(row, col, cell)
                total = self.assembly.total_length_m()
                bodies = sum(1 for i in self.assembly.items if not i.is_section)
                self.summary_label.setText(
                    f"  {len(self.assembly.items)} items · {bodies} bodies · {total / 1000.0:.3f} km")
            else:
                self.summary_label.setText("")
        finally:
            self._loading = False
        if self.sld is not None:
            self.sld.set_assembly(self.assembly)
        self._sync_engineering_columns()

    def _engineering_columns_visible(self) -> bool:
        value = self.settings.value(_ENGINEERING_COLS_SETTING, None)
        if value is None:
            return bool(self.assembly and self.assembly.kind == am.ASSEMBLY_KIND_RIGGING)
        return str(value).lower() in ("1", "true", "yes")

    def _sync_engineering_columns(self):
        visible = self._engineering_columns_visible()
        self.engineering_cols_check.blockSignals(True)
        self.engineering_cols_check.setChecked(visible)
        self.engineering_cols_check.blockSignals(False)
        for col in _ENGINEERING_COLS:
            self.items_table.setColumnHidden(col, not visible)

    def _on_engineering_columns_changed(self):
        visible = self.engineering_cols_check.isChecked()
        self.settings.setValue(_ENGINEERING_COLS_SETTING, visible)
        for col in _ENGINEERING_COLS:
            self.items_table.setColumnHidden(col, not visible)

    # -------------------------------------------------------------- editing --
    def _on_item_changed(self, cell: QTableWidgetItem):
        if self._loading or self.assembly is None or not self._can_edit_current():
            return
        row, col = cell.row(), cell.column()
        if not (0 <= row < len(self.assembly.items)):
            return
        attr = ITEM_COLUMNS[col][1]
        text = cell.text().strip()
        item = self.assembly.items[row]
        if col in _FLOAT_COLS:
            if text == "":
                value = None if attr != "length_m" else 0.0
            else:
                try:
                    value = float(text)
                except ValueError:
                    self._refresh_views()
                    return
            setattr(item, attr, value)
        else:
            setattr(item, attr, text)
        self._persist()
        self._refresh_views()
        self._refresh_list_label()

    def _add_item(self, kind: str):
        if not self._can_edit_current():
            return
        if self.assembly is None:
            self._new_assembly()
            if self.assembly is None:
                return
        row = self.items_table.currentRow()
        insert_at = row + 1 if row >= 0 else len(self.assembly.items)
        if kind == am.KIND_SECTION:
            item = AssemblyItem(kind=kind, name="Cable", length_m=1000.0)
        else:
            item = AssemblyItem(kind=kind, name="Body", length_m=0.0, point_load_kN=0.0)
        self.assembly.items.insert(insert_at, item)
        self._persist()
        self._refresh_views()
        self._refresh_list_label()
        self.items_table.selectRow(insert_at)

    def _delete_item(self):
        if self.assembly is None or not self._can_edit_current():
            return
        row = self.items_table.currentRow()
        if not (0 <= row < len(self.assembly.items)):
            return
        del self.assembly.items[row]
        self._persist()
        self._refresh_views()
        self._refresh_list_label()

    def _move_item(self, delta: int):
        if self.assembly is None or not self._can_edit_current():
            return
        row = self.items_table.currentRow()
        target = row + delta
        if not (0 <= row < len(self.assembly.items)) or not (0 <= target < len(self.assembly.items)):
            return
        items = self.assembly.items
        items[row], items[target] = items[target], items[row]
        self._persist()
        self._refresh_views()
        self.items_table.selectRow(target)

    def _refresh_list_label(self):
        current = self.assembly_list.currentItem()
        if current is not None and self.assembly is not None:
            total_km = self.assembly.total_length_m() / 1000.0
            current.setText(f"{self.assembly.name}  ({self.assembly.kind}, {total_km:.2f} km)")

    # ---------------------------------------------------- assembly actions --
    def _new_assembly(self):
        name, ok = QInputDialog.getText(self, "New assembly", "Assembly name:")
        if not ok or not name.strip():
            return
        kinds = ["cable", "rigging"]
        kind, ok = QInputDialog.getItem(self, "New assembly", "Kind:", kinds, 0, False)
        if not ok:
            return
        self.set_read_only(False)
        self._current_header = None
        self.assembly = Assembly(name=name.strip(), kind=kind)
        self._persist(library_changed=True)
        self.refresh_assembly_list()
        self._select_assembly(self.assembly.assembly_id)

    def _duplicate_assembly(self):
        if self.assembly is None:
            return
        copy = am.assembly_from_rows(*am.assembly_to_rows(self.assembly))
        copy.assembly_id = schema.new_id()
        copy.name = f"{self.assembly.name} (copy)"
        for item in copy.items:
            item.item_id = schema.new_id()
        self.set_read_only(False)
        self._current_header = None
        self.assembly = copy
        self._persist(library_changed=True)
        self.refresh_assembly_list()
        self._select_assembly(copy.assembly_id)

    def _delete_assembly(self):
        if self.assembly is None or self.store is None:
            return
        answer = QMessageBox.question(
            self, "Delete assembly",
            f"Delete assembly '{self.assembly.name}' (and its fits)?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self._current_header and self._current_header.get("status") == schema.STATUS_ISSUED:
            answer = QMessageBox.question(
                self, "Delete issued assembly",
                "This assembly is issued. Delete it anyway?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.store.delete_assembly(self.assembly.assembly_id)
        self.assembly = None
        self._current_header = None
        self.set_read_only(False)
        self.refresh_assembly_list()
        self._refresh_views()
        self.assemblies_changed.emit()

    def _select_assembly(self, assembly_id: str):
        for i in range(self.assembly_list.count()):
            if self.assembly_list.item(i).data(Qt.ItemDataRole.UserRole) == assembly_id:
                if self.assembly_list.currentRow() == i:
                    # Row is already current (e.g. just set by refresh_assembly_list
                    # with signals blocked): setCurrentRow won't emit, so load and
                    # render this assembly directly — otherwise the items table and
                    # SLD stay empty until the next edit.
                    self._on_assembly_selected(self.assembly_list.item(i))
                else:
                    self.assembly_list.setCurrentRow(i)
                return

    # -------------------------------------------------------------- import --
    def _import_catenary_clipboard(self):
        raw = QApplication.clipboard().text()
        if not raw.strip():
            QMessageBox.information(self, "Import", "Clipboard is empty.")
            return
        try:
            assembly = am.from_catenary_json(raw)
        except Exception as exc:
            QMessageBox.warning(self, "Import", f"Could not parse catenary assembly JSON:\n{exc}")
            return
        name, ok = QInputDialog.getText(self, "Import assembly", "Assembly name:",
                                        text="Imported assembly")
        if not ok:
            return
        assembly.name = name.strip() or assembly.name
        self.assembly = assembly
        self._persist(library_changed=True)
        self.refresh_assembly_list()
        self._select_assembly(assembly.assembly_id)

    def _export_catenary_clipboard(self):
        if self.assembly is None:
            return
        QApplication.clipboard().setText(am.to_catenary_json(self.assembly))
        QMessageBox.information(
            self, "Export",
            "Assembly JSON copied to the clipboard.\n\n"
            "Paste it into the Catenary Calculator V2 assembly JSON box or the "
            "Cable Lay Simulator assembly editor.")

    def extract_from_rpl_id(self, rpl_id: str) -> bool:
        """Run the extract workflow for a specific RPL (no picker).

        Returns True when an assembly was created.
        """
        self._open_store()
        rpl = self.store.get_rpl(rpl_id) if self.store and self.store.exists() else None
        if rpl is None:
            QMessageBox.information(self, "Extract from RPL", "RPL not found in the workbench.")
            return False
        return self._extract_from_rpl(rpl)

    def _extract_from_rpl(self, rpl: Optional[Dict] = None) -> bool:
        if self.store is None or not self.store.exists():
            QMessageBox.information(self, "Extract from RPL",
                                    "No workbench GeoPackage yet — register an RPL first.")
            return False
        if rpl is None:
            rpls = self.store.list_rpls()
            if not rpls:
                QMessageBox.information(self, "Extract from RPL", "No registered RPLs found.")
                return False
            names = [r.get("name") or r.get("rpl_id") for r in rpls]
            choice, ok = QInputDialog.getItem(self, "Extract from RPL", "RPL:", names, 0, False)
            if not ok:
                return False
            rpl = rpls[names.index(choice)]

        from .rpl_layer_io import RplLayerSync

        points_layer = self.store.open_layer(rpl.get("points_layer"))
        lines_layer = self.store.open_layer(rpl.get("lines_layer"))
        if points_layer is None or lines_layer is None:
            QMessageBox.warning(self, "Extract from RPL", "Could not open the RPL's layers.")
            return False
        model = RplLayerSync(points_layer, lines_layer, rpl.get("rpl_id") or "").load_model()

        classifier = am.EventClassifier(self.store.list_event_rules())
        review = am.classify_events(model, classifier)

        dialog = ExtractReviewDialog(model, review, f"{rpl.get('name')} assembly", self)
        if dialog.exec_() != QDialog.DialogCode.Accepted:
            return False
        assembly = dialog.result_assembly()
        if assembly is None or not assembly.items:
            QMessageBox.information(self, "Extract from RPL", "Nothing to extract.")
            return False
        assembly.source_ref = rpl.get("rpl_id") or ""
        self.assembly = assembly
        self._persist(library_changed=True)
        self.refresh_assembly_list()
        self._select_assembly(assembly.assembly_id)
        return True

    def _reset_event_rules(self):
        if self.store is None or not self.store.exists():
            QMessageBox.information(self, "Event rules", "No workbench GeoPackage yet.")
            return
        answer = QMessageBox.question(
            self, "Event rules",
            "Replace this project's event classification rules with the current "
            "plugin defaults? Custom rules will be lost.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.store.seed_default_event_rules()
        QMessageBox.information(self, "Event rules", "Rules reset to defaults.")

    # ----------------------------------------------------------- selection --
    def _on_sld_item_clicked(self, item_index: int):
        self._loading = True
        try:
            self.items_table.selectRow(item_index)
        finally:
            self._loading = False
        if self.sld is not None:
            self.sld.highlight_item(item_index)
        self._announce_selection(item_index)

    def _on_table_selection(self):
        if self._loading or self.assembly is None or self.sld is None:
            return
        row = self.items_table.currentRow()
        if 0 <= row < len(self.assembly.items):
            self.sld.highlight_item(row)
            self._announce_selection(row)

    def _announce_selection(self, item_index: int):
        """Push the selected item's landing KP to the map via the bus."""
        if self.assembly is None or self.store is None:
            return
        fits = self.store.list_fits(assembly_id=self.assembly.assembly_id)
        if not fits:
            return
        fit_row = fits[0]
        try:
            params = json.loads(fit_row.get("params_json") or "{}")
        except ValueError:
            params = {}
        starts = self.assembly.cable_dist_starts_m()
        if not (0 <= item_index < len(starts)):
            return
        cable_m = starts[item_index]
        kp = _fit_kp_for_cable_dist(self.store, fit_row, cable_m)
        if kp is not None:
            selection_bus().kpSelected.emit(fit_row.get("rpl_id") or "", kp)

    def _on_bus_cable_dist(self, assembly_id: str, cable_m: float):
        if self.assembly is not None and assembly_id == self.assembly.assembly_id and self.sld is not None:
            self.sld.mark_cable_dist(cable_m)


def _fit_kp_for_cable_dist(store: WorkbenchStore, fit_row: Dict, cable_m: float) -> Optional[float]:
    """Map an assembly cable distance to route KP through a stored fit."""
    from . import rpl_engine
    from .rpl_layer_io import RplLayerSync

    rpl = store.get_rpl(fit_row.get("rpl_id") or "")
    if not rpl:
        return None
    points_layer = store.open_layer(rpl.get("points_layer"))
    lines_layer = store.open_layer(rpl.get("lines_layer"))
    if points_layer is None or lines_layer is None:
        return None
    model = RplLayerSync(points_layer, lines_layer).load_model()
    anchor_kp = float(fit_row.get("anchor_kp_km") or 0.0)
    anchor_cable_m = float(fit_row.get("anchor_cable_dist_m") or 0.0)
    direction = 1 if int(fit_row.get("direction") or 1) >= 0 else -1
    anchor_cable_km = rpl_engine.cable_dist_from_kp(model, anchor_kp)
    if anchor_cable_km is None:
        return None
    route_cable_km = anchor_cable_km + direction * (cable_m - anchor_cable_m) / 1000.0
    return rpl_engine.kp_from_cable_dist(model, route_cable_km)


class ExtractReviewDialog(QDialog):
    """Interactive extract-from-RPL: reclassify events, choose section
    grouping, and watch a live preview of the resulting assembly."""

    CATEGORIES = ["body", "geographic", "installation"]

    def __init__(self, model, review: List[Dict], default_name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Extract from RPL — review")
        self.resize(980, 680)
        self._model = model
        self._review = review
        self._assembly: Optional[Assembly] = None
        self._default_name = default_name
        self._category_combos: List = []

        from qgis.PyQt.QtWidgets import QComboBox, QSplitter

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Set the Category of each event — only 'body' events become assembly "
            "bodies, and every body splits the cable into sections. Unmatched "
            "events (orange) were defaulted to 'installation'; correct them here."))

        options = QHBoxLayout()
        options.addWidget(QLabel("Section grouping:"))
        self.grouping_combo = QComboBox()
        self.grouping_combo.addItem("By cable type change (and bodies)", am.GROUP_BY_CABLE_TYPE)
        self.grouping_combo.addItem("Between bodies only", am.GROUP_BETWEEN_BODIES)
        self.grouping_combo.currentIndexChanged.connect(self._rebuild)
        options.addWidget(self.grouping_combo)
        options.addStretch()
        self.summary_label = QLabel("")
        options.addWidget(self.summary_label)
        layout.addLayout(options)

        splitter = QSplitter(Qt.Orientation.Vertical)

        # live preview (falls back to summary-only if pyqtgraph unavailable)
        try:
            from .sld_widget import SldWidget

            self.preview = SldWidget()
            self.preview.setMinimumHeight(160)
            splitter.addWidget(self.preview)
        except Exception:
            self.preview = None

        self.table = QTableWidget(len(review), 4)
        self.table.setHorizontalHeaderLabels(["PosNo", "Event", "Category", "Matched rule?"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        for row, entry in enumerate(review):
            pos_cell = QTableWidgetItem(
                "" if entry.get("pos_no") is None else str(entry["pos_no"]))
            event_cell = QTableWidgetItem(str(entry.get("event") or ""))
            matched_cell = QTableWidgetItem("yes" if entry.get("matched") else "NO — defaulted")
            for cell in (pos_cell, event_cell, matched_cell):
                cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if not entry.get("matched"):
                    cell.setForeground(QColor(200, 80, 0))
            self.table.setItem(row, 0, pos_cell)
            self.table.setItem(row, 1, event_cell)
            self.table.setItem(row, 3, matched_cell)

            combo = QComboBox()
            combo.addItems(self.CATEGORIES)
            combo.setCurrentText(entry.get("category") or "installation")
            combo.currentIndexChanged.connect(self._rebuild)
            self.table.setCellWidget(row, 2, combo)
            self._category_combos.append(combo)
        self.table.resizeColumnsToContents()
        splitter.addWidget(self.table)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter)

        actions = QHBoxLayout()
        all_bodies_btn = QPushButton("Selected rows → body")
        all_bodies_btn.setToolTip("Mark every selected row as a body")
        all_bodies_btn.clicked.connect(lambda: self._set_selected_category("body"))
        none_bodies_btn = QPushButton("Selected rows → installation")
        none_bodies_btn.clicked.connect(lambda: self._set_selected_category("installation"))
        actions.addWidget(all_bodies_btn)
        actions.addWidget(none_bodies_btn)
        actions.addStretch()
        layout.addLayout(actions)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._rebuild()

    def _set_selected_category(self, category: str):
        rows = {index.row() for index in self.table.selectionModel().selectedRows()}
        if not rows:
            return
        for row in rows:
            combo = self._category_combos[row]
            combo.blockSignals(True)
            combo.setCurrentText(category)
            combo.blockSignals(False)
        self._rebuild()

    def _classifications(self) -> Dict[int, str]:
        return {
            entry["seq"]: combo.currentText()
            for entry, combo in zip(self._review, self._category_combos)
        }

    def _rebuild(self, *_args):
        self._assembly = am.build_assembly_from_rpl(
            self._model,
            self._classifications(),
            name=self._default_name,
            grouping=self.grouping_combo.currentData(),
        )
        sections = sum(1 for i in self._assembly.items if i.is_section)
        bodies = len(self._assembly.items) - sections
        self.summary_label.setText(
            f"{sections} sections · {bodies} bodies · "
            f"{self._assembly.total_length_m() / 1000.0:.3f} km cable")
        if self.preview is not None:
            self.preview.set_assembly(self._assembly)

    def result_assembly(self) -> Optional[Assembly]:
        return self._assembly

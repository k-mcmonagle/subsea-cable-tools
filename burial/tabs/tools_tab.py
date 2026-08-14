# -*- coding: utf-8 -*-
"""Burial Tools tab — the project-scoped tool registry.

Register the burial vehicles (ploughs, trenchers…) available to plans in
this project, each with its operating configurations and an optional
body-fixed DXF footprint. Tools are shared by every plan (the Planner
vessels model): deleting a plan never deletes a tool, deleting a tool never
edits a plan (assignments render as "(unregistered tool)").

No engineering values are shipped: every parameter is user-entered, with a
source-reference field carried on the tool and on each configuration.
Registries travel between projects/organisations as versioned JSON.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional

from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...qgis_compat import (
    BUTTON_BOX_CANCEL,
    BUTTON_BOX_OK,
    DIALOG_ACCEPTED,
    EDIT_TRIGGER_NONE,
    ITEM_DATA_USER_ROLE,
    MESSAGE_BOX_NO,
    MESSAGE_BOX_YES,
    SELECTION_BEHAVIOR_SELECT_ROWS,
    SELECTION_MODE_SINGLE,
    qt_exec,
)
from .. import schema
from .. import tools as tools_mod

_TABLE_COLUMNS = ["Name", "Type", "Configurations", "Footprint",
                  "Source ref", "Notes"]

_CONFIG_COLUMNS = (["Label", "Mode"]
                   + [label for _key, label in tools_mod.CONFIG_NUMERIC_FIELDS]
                   + ["Source ref", "Notes"])
_CONFIG_KEYS = (["label", "mode"]
                + [key for key, _label in tools_mod.CONFIG_NUMERIC_FIELDS]
                + ["source_ref", "notes"])


class DxfParamsDialog(QDialog):
    """Scale/CRP/rotation for a footprint DXF import (drawing units)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("DXF import parameters")
        layout = QVBoxLayout(self)
        note = QLabel(
            "The outline is normalised to a body-fixed frame: metres, CRP "
            "at the origin, front of the tool along +Y (0° rotation assumes "
            "the drawing points up). Same conventions as Import Ship "
            "Outline (DXF).")
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.scale_spin = QDoubleSpinBox()
        self.scale_spin.setDecimals(6)
        self.scale_spin.setRange(0.000001, 1000000.0)
        self.scale_spin.setValue(1.0)
        self.scale_spin.setToolTip("0.001 converts mm drawings to metres.")
        form.addRow("Drawing scale to metres:", self.scale_spin)
        self.crp_x_spin = QDoubleSpinBox()
        self.crp_y_spin = QDoubleSpinBox()
        for spin in (self.crp_x_spin, self.crp_y_spin):
            spin.setDecimals(3)
            spin.setRange(-1e9, 1e9)
        form.addRow("CRP offset X (drawing units):", self.crp_x_spin)
        form.addRow("CRP offset Y (drawing units):", self.crp_y_spin)
        self.rotation_spin = QDoubleSpinBox()
        self.rotation_spin.setDecimals(2)
        self.rotation_spin.setRange(-360.0, 360.0)
        self.rotation_spin.setSuffix(" °")
        form.addRow("Rotation (0 = front up):", self.rotation_spin)
        layout.addLayout(form)
        buttons = QDialogButtonBox()
        buttons.setStandardButtons(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def params(self):
        return (self.scale_spin.value(), self.crp_x_spin.value(),
                self.crp_y_spin.value(), self.rotation_spin.value())


class ToolDialog(QDialog):
    """Edit one bp_tool row (identity, configurations, footprint)."""

    def __init__(self, tool: Optional[Dict] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Burial tool")
        self.resize(760, 520)
        self._tool = dict(tool or {})
        self._footprint_wkt = str(self._tool.get("footprint_wkt") or "")
        self._footprint_info = {
            "source": self._tool.get("footprint_source") or "",
            "scale": self._tool.get("footprint_scale"),
            "crp_x": self._tool.get("footprint_crp_x"),
            "crp_y": self._tool.get("footprint_crp_y"),
            "rotation_deg": self._tool.get("footprint_rotation_deg"),
            "length_m": self._tool.get("length_m"),
            "width_m": self._tool.get("width_m"),
        }

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name_edit = QLineEdit(self._tool.get("name") or "")
        form.addRow("Name:", self.name_edit)
        self.type_combo = QComboBox()
        for method in schema.METHODS:
            self.type_combo.addItem(schema.METHOD_LABELS[method], method)
        index = self.type_combo.findData(
            schema.normalise_method(self._tool.get("tool_type") or ""))
        self.type_combo.setCurrentIndex(max(0, index))
        form.addRow("Type:", self.type_combo)
        self.source_edit = QLineEdit(self._tool.get("source_ref") or "")
        self.source_edit.setPlaceholderText(
            "Document + revision the parameters come from")
        form.addRow("Source reference:", self.source_edit)
        self.notes_edit = QLineEdit(self._tool.get("notes") or "")
        form.addRow("Notes:", self.notes_edit)
        layout.addLayout(form)

        configs_box = QGroupBox("Operating configurations")
        configs_layout = QVBoxLayout(configs_box)
        hint = QLabel(
            "e.g. a plough's jetting vs passive modes or share-length "
            "options. All values are user-entered (metres); leave blank "
            "when not applicable. The Plan Builder assigns a configuration "
            "per section.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #666;")
        configs_layout.addWidget(hint)
        self.configs_table = QTableWidget(0, len(_CONFIG_COLUMNS))
        self.configs_table.setHorizontalHeaderLabels(_CONFIG_COLUMNS)
        self.configs_table.verticalHeader().setVisible(False)
        self.configs_table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.configs_table.setSelectionMode(SELECTION_MODE_SINGLE)
        self.configs_table.horizontalHeader().setStretchLastSection(True)
        configs_layout.addWidget(self.configs_table, 1)
        config_buttons = QHBoxLayout()
        add_config = QPushButton("Add configuration")
        add_config.clicked.connect(self._add_config_row)
        config_buttons.addWidget(add_config)
        remove_config = QPushButton("Remove selected")
        remove_config.clicked.connect(self._remove_config_row)
        config_buttons.addWidget(remove_config)
        config_buttons.addStretch(1)
        configs_layout.addLayout(config_buttons)
        layout.addWidget(configs_box, 1)

        footprint_box = QGroupBox("Footprint (optional)")
        footprint_layout = QHBoxLayout(footprint_box)
        self.footprint_label = QLabel("")
        self.footprint_label.setWordWrap(True)
        footprint_layout.addWidget(self.footprint_label, 1)
        load_button = QPushButton("Load from DXF…")
        load_button.setToolTip(
            "Import a scaled outline of the tool (plan view). The outline "
            "is stored with the tool, so the registry JSON keeps it when "
            "shared without the DXF file.")
        load_button.clicked.connect(self._load_footprint)
        footprint_layout.addWidget(load_button)
        clear_button = QPushButton("Clear")
        clear_button.clicked.connect(self._clear_footprint)
        footprint_layout.addWidget(clear_button)
        layout.addWidget(footprint_box)

        buttons = QDialogButtonBox()
        buttons.setStandardButtons(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        for config in tools_mod.parse_configs(self._tool):
            self._add_config_row(config)
        self._refresh_footprint_label()

    # -- configurations ------------------------------------------------------
    def _add_config_row(self, config: Optional[Dict] = None) -> None:
        config = config if isinstance(config, dict) else {}
        row = self.configs_table.rowCount()
        self.configs_table.insertRow(row)
        for column, key in enumerate(_CONFIG_KEYS):
            value = config.get(key)
            item = QTableWidgetItem("" if value in (None, "") else str(value))
            if column == 0:
                item.setData(ITEM_DATA_USER_ROLE,
                             config.get("config_id") or schema.new_id())
            self.configs_table.setItem(row, column, item)

    def _remove_config_row(self) -> None:
        row = self.configs_table.currentRow()
        if row >= 0:
            self.configs_table.removeRow(row)

    def _configs(self) -> List[Dict]:
        configs: List[Dict] = []
        for row in range(self.configs_table.rowCount()):
            def cell(column: int) -> str:
                item = self.configs_table.item(row, column)
                return (item.text() if item is not None else "").strip()

            config: Dict = {}
            id_item = self.configs_table.item(row, 0)
            config["config_id"] = (id_item.data(ITEM_DATA_USER_ROLE)
                                   if id_item is not None else "") or schema.new_id()
            for column, key in enumerate(_CONFIG_KEYS):
                text = cell(column)
                if key in ("label", "mode", "source_ref", "notes"):
                    config[key] = text
                elif text:
                    try:
                        config[key] = float(text.replace(",", "."))
                    except ValueError:
                        raise ValueError(
                            f"Configuration row {row + 1}: "
                            f"'{text}' is not a number "
                            f"({_CONFIG_COLUMNS[column]}).")
            if not tools_mod.config_label(config):
                raise ValueError(
                    f"Configuration row {row + 1} needs a label.")
            configs.append(config)
        return configs

    # -- footprint -----------------------------------------------------------
    def _load_footprint(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, "Tool outline DXF", "", "DXF (*.dxf)")
        if not path:
            return
        params_dialog = DxfParamsDialog(self)
        if qt_exec(params_dialog) != DIALOG_ACCEPTED:
            return
        scale, crp_x, crp_y, rotation = params_dialog.params()
        try:
            from .. import footprint as footprint_mod

            wkt, info = footprint_mod.load_dxf_outline(
                path, scale, crp_x, crp_y, rotation)
        except Exception as exc:
            QMessageBox.warning(self, "Burial Planner",
                                f"The footprint could not be imported: {exc}")
            return
        self._footprint_wkt = wkt
        self._footprint_info = info
        self._refresh_footprint_label()

    def _clear_footprint(self) -> None:
        self._footprint_wkt = ""
        self._footprint_info = {}
        self._refresh_footprint_label()

    def _refresh_footprint_label(self) -> None:
        if not self._footprint_wkt:
            self.footprint_label.setText("No footprint loaded.")
            return
        info = self._footprint_info or {}
        parts = [info.get("source") or "outline"]
        length, width = info.get("length_m"), info.get("width_m")
        if length and width:
            parts.append(f"{float(length):.1f} m × {float(width):.1f} m")
        self.footprint_label.setText("Loaded: " + " — ".join(parts))

    # -- result --------------------------------------------------------------
    def _accept(self) -> None:
        if not self.name_edit.text().strip():
            QMessageBox.warning(self, "Burial Planner",
                                "The tool needs a name.")
            return
        try:
            self._configs()
        except ValueError as exc:
            QMessageBox.warning(self, "Burial Planner", str(exc))
            return
        self.accept()

    def result_row(self) -> Dict:
        row = dict(self._tool)
        # After "Clear", every footprint-derived field must clear with the
        # outline — otherwise a deleted footprint's dimensions linger.
        info = (self._footprint_info or {}) if self._footprint_wkt else {}
        row.update({
            "name": self.name_edit.text().strip(),
            "tool_type": self.type_combo.currentData() or "",
            "source_ref": self.source_edit.text().strip(),
            "notes": self.notes_edit.text(),
            "configs_json": json.dumps(self._configs()),
            "footprint_wkt": self._footprint_wkt,
            "footprint_source": info.get("source") or "",
            "footprint_scale": info.get("scale"),
            "footprint_crp_x": info.get("crp_x"),
            "footprint_crp_y": info.get("crp_y"),
            "footprint_rotation_deg": info.get("rotation_deg"),
            "length_m": info.get("length_m"),
            "width_m": info.get("width_m"),
        })
        return row


class ToolsTab(QWidget):
    def __init__(self, model, dock, parent=None):
        super().__init__(parent)
        self.model = model
        self.dock = dock
        self._loading = False

        layout = QVBoxLayout(self)
        note = QLabel(
            "Burial tools are registered once per project and shared by "
            "every plan. Set the plan's default tool on the Plan tab; "
            "override it per section in the Plan Builder. All values are "
            "user-entered — record where each came from in the source "
            "reference fields.")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.table = QTableWidget(0, len(_TABLE_COLUMNS))
        self.table.setHorizontalHeaderLabels(_TABLE_COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.table.setSelectionMode(SELECTION_MODE_SINGLE)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(EDIT_TRIGGER_NONE)
        self.table.cellDoubleClicked.connect(lambda _r, _c: self._edit_tool())
        layout.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        for label, slot in (("Add tool…", self._add_tool),
                            ("Edit…", self._edit_tool),
                            ("Delete", self._delete_tool),
                            ("Import registry JSON…", self._import_json),
                            ("Export registry JSON…", self._export_json)):
            button = QPushButton(label)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #666;")
        layout.addWidget(self.status_label)

        model.toolsChanged.connect(self.refresh)
        self.refresh()

    # -- table ---------------------------------------------------------------
    def refresh(self) -> None:
        self._loading = True
        try:
            tools = self.model.tools
            self.table.setRowCount(len(tools))
            for i, tool in enumerate(tools):
                configs = tools_mod.parse_configs(tool)
                config_text = ", ".join(
                    tools_mod.config_label(c) for c in configs) or "—"
                footprint = "yes" if tool.get("footprint_wkt") else ""
                if footprint:
                    try:
                        footprint = (f"{float(tool['length_m']):.1f} × "
                                     f"{float(tool['width_m']):.1f} m")
                    except (TypeError, ValueError, KeyError):
                        pass  # imported rows may lack numeric dimensions
                values = [
                    tool.get("name") or "",
                    schema.METHOD_LABELS.get(
                        schema.normalise_method(tool.get("tool_type") or ""),
                        tool.get("tool_type") or ""),
                    config_text,
                    footprint,
                    tool.get("source_ref") or "",
                    tool.get("notes") or "",
                ]
                for j, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    if j == 0:
                        item.setData(ITEM_DATA_USER_ROLE,
                                     tool.get("tool_id") or "")
                    if j == 2 and configs:
                        item.setToolTip(config_text)
                    self.table.setItem(i, j, item)
        finally:
            self._loading = False

    def _selected_tool(self) -> Optional[Dict]:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        tool_id = item.data(ITEM_DATA_USER_ROLE) if item is not None else ""
        return tools_mod.tool_by_id(self.model.tools, tool_id or "")

    # -- actions -------------------------------------------------------------
    def _add_tool(self) -> None:
        dialog = ToolDialog(parent=self)
        if qt_exec(dialog) != DIALOG_ACCEPTED:
            return
        if self.model.save_tool(dialog.result_row()):
            self.status_label.setText("Tool registered.")

    def _edit_tool(self) -> None:
        tool = self._selected_tool()
        if tool is None:
            return
        dialog = ToolDialog(tool, parent=self)
        if qt_exec(dialog) != DIALOG_ACCEPTED:
            return
        if self.model.save_tool(dialog.result_row()):
            self.status_label.setText("Tool updated.")

    def _delete_tool(self) -> None:
        tool = self._selected_tool()
        if tool is None:
            return
        answer = QMessageBox.question(
            self, "Delete tool",
            f"Delete '{tool.get('name')}' from the registry? Plans that "
            "reference it keep their assignments and show '(unregistered "
            "tool)'.",
            MESSAGE_BOX_YES | MESSAGE_BOX_NO, MESSAGE_BOX_NO)
        if answer != MESSAGE_BOX_YES:
            return
        if self.model.delete_tool(tool.get("tool_id") or ""):
            self.status_label.setText("Tool deleted.")

    def _import_json(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, "Import tool registry", "", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as handle:
                rows = tools_mod.parse_registry_json(handle.read())
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Burial Planner",
                                f"The registry could not be read: {exc}")
            return
        existing = {str(t.get("tool_id") or "") for t in self.model.tools}
        updated = sum(1 for r in rows if r.get("tool_id") in existing)
        saved = len(self.model.save_tools(rows))
        self.status_label.setText(
            f"Imported {saved} tool(s)"
            + (f" ({updated} updated existing)" if updated else "") + ".")

    def _export_json(self) -> None:
        if not self.model.tools:
            return
        path, _filter = QFileDialog.getSaveFileName(
            self, "Export tool registry", "burial_tools.json",
            "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(tools_mod.registry_json(self.model.tools))
        except OSError as exc:
            QMessageBox.warning(self, "Burial Planner",
                                f"The registry could not be written: {exc}")
            return
        self.status_label.setText(
            f"Exported {len(self.model.tools)} tool(s).")

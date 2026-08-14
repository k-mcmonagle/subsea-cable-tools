# -*- coding: utf-8 -*-
"""Plan tab — the open plan's identity, lineage and notes.

Plan selection and New / Duplicate / Rename / Delete live in the dock's top
strip (Planner scenario-management UX); this tab shows and edits the open
plan's descriptive fields.
"""

from __future__ import annotations

from qgis.PyQt.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import schema
from .. import tools as tools_mod


class PlanTab(QWidget):
    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.model = model
        self._loading = False

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.description_edit = QLineEdit()
        self.method_label = QLabel("—")
        self.rpl_label = QLabel("—")
        self.rpl_revision_label = QLabel("—")
        self.rev_label = QLabel("—")
        self.status_label = QLabel("—")
        self.notes_edit = QPlainTextEdit()
        self.notes_edit.setPlaceholderText(
            "Assumptions, review-basis notes, references…")
        form.addRow("Name:", self.name_edit)
        form.addRow("Description:", self.description_edit)
        form.addRow("Method:", self.method_label)
        # Default burial tool: sections inherit this unless overridden in
        # the Plan Builder. Registered on the Burial Tools tab.
        self.tool_combo = QComboBox()
        self.tool_combo.setToolTip(
            "The plan's default burial tool. Candidate sections inherit it "
            "unless a section is given its own tool in the Plan Builder. "
            "Register tools on the Burial Tools tab.")
        self.tool_config_combo = QComboBox()
        self.tool_config_combo.setToolTip(
            "The default tool's operating configuration "
            "(e.g. jetting vs passive mode).")
        self.tool_combo.currentIndexChanged.connect(self._tool_combo_changed)
        self.tool_combo.activated.connect(self._save_default_tool)
        self.tool_config_combo.activated.connect(self._save_default_tool)
        tool_row = QHBoxLayout()
        tool_row.addWidget(self.tool_combo, 2)
        tool_row.addWidget(self.tool_config_combo, 1)
        form.addRow("Default burial tool:", tool_row)
        form.addRow("RPL:", self.rpl_label)
        form.addRow("RPL revision:", self.rpl_revision_label)
        form.addRow("Plan revision:", self.rev_label)
        form.addRow("Status:", self.status_label)
        layout.addLayout(form)
        layout.addWidget(QLabel("Notes:"))
        layout.addWidget(self.notes_edit, 1)
        self.save_button = QPushButton("Save plan details")
        self.save_button.clicked.connect(self._save)
        layout.addWidget(self.save_button)

        self.hint = QLabel(
            "Create or open a plan with the selector above, register the RPL "
            "and survey inputs on Inputs, build the reusable depth/slope data "
            "on Bathymetry Profile, configure the Exclusion stack, then "
            "generate candidate sections in Plan Builder.")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

        model.planChanged.connect(self.refresh)
        # Registry changes refresh only the tool combos: a full refresh()
        # would clobber unsaved name/description/notes edits.
        model.toolsChanged.connect(self._on_tools_changed)
        self.refresh()

    def refresh(self) -> None:
        self._loading = True
        try:
            plan = self.model.plan
            has_plan = bool(plan)
            for widget in (self.name_edit, self.description_edit,
                           self.notes_edit, self.save_button,
                           self.tool_combo, self.tool_config_combo):
                widget.setEnabled(has_plan)
            self.name_edit.setText(plan.get("name") or "")
            self.description_edit.setText(plan.get("description") or "")
            self.notes_edit.setPlainText(plan.get("notes") or "")
            self.method_label.setText(schema.METHOD_LABELS.get(
                schema.normalise_method(plan.get("method") or ""), "—"))
            self._refresh_tool_combos()
            self.rpl_label.setText(plan.get("rpl_name") or "—")
            self.rpl_revision_label.setText(plan.get("rpl_revision") or "—")
            self.rev_label.setText(plan.get("rev_label") or "—")
            self.status_label.setText(plan.get("status") or "draft")
        finally:
            self._loading = False

    def _on_tools_changed(self) -> None:
        was_loading = self._loading
        self._loading = True
        try:
            self._refresh_tool_combos()
        finally:
            self._loading = was_loading

    def _refresh_tool_combos(self) -> None:
        tool_id, config_id = self.model.default_tool()
        self.tool_combo.clear()
        self.tool_combo.addItem("(none)", "")
        for tool in self.model.tools:
            label = tool.get("name") or "?"
            tool_type = schema.METHOD_LABELS.get(
                schema.normalise_method(tool.get("tool_type") or ""), "")
            if tool_type:
                label += f"  [{tool_type}]"
            self.tool_combo.addItem(label, tool.get("tool_id") or "")
        if tool_id and self.tool_combo.findData(tool_id) < 0:
            self.tool_combo.addItem("(unregistered tool)", tool_id)
        self.tool_combo.setCurrentIndex(
            max(0, self.tool_combo.findData(tool_id)))
        self._refresh_config_combo(config_id)

    def _refresh_config_combo(self, config_id: str = "") -> None:
        self.tool_config_combo.clear()
        self.tool_config_combo.addItem("(no configuration)", "")
        tool = tools_mod.tool_by_id(self.model.tools,
                                    self.tool_combo.currentData() or "")
        for config in tools_mod.parse_configs(tool):
            self.tool_config_combo.addItem(
                tools_mod.config_label(config) or "?",
                config.get("config_id") or "")
        index = self.tool_config_combo.findData(config_id)
        self.tool_config_combo.setCurrentIndex(max(0, index))

    def _tool_combo_changed(self, _index: int) -> None:
        if self._loading:
            return
        self._refresh_config_combo()

    def _save_default_tool(self, _index: int = 0) -> None:
        if self._loading or not self.model.plan:
            return
        tool_id = self.tool_combo.currentData() or ""
        config_id = self.tool_config_combo.currentData() or ""
        current = self.model.default_tool()
        if (tool_id, config_id) == current:
            return
        if self.model.update_gen_params(
                {"tool_id": tool_id, "tool_config_id": config_id},
                reason="default burial tool", stale=False):
            # Sections inheriting the default bake the resolved tool text
            # into the map layer at write time — rewrite it now.
            self.model.refresh_layers()

    def _save(self) -> None:
        if self._loading or not self.model.plan:
            return
        self.model.update_plan({
            "name": self.name_edit.text().strip() or (self.model.plan.get("name") or "Plan"),
            "description": self.description_edit.text(),
            "notes": self.notes_edit.toPlainText(),
        })

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
from .. import ui_helpers


class PlanTab(QWidget):
    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.model = model
        self._loading = False
        self._dirty = False
        self._loaded_plan_id = ""

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.description_edit = QLineEdit()
        self.method_label = QLabel("—")
        self.method_label.setToolTip(
            "Chosen when the plan is created; tools of a different type can "
            "still be assigned per section in the Plan Builder.")
        self.rpl_label = QLabel("—")
        self.rpl_label.setToolTip("The plan's route — set on the Inputs tab.")
        self.rpl_revision_label = QLabel("—")
        self.rpl_revision_label.setToolTip(
            "Revision of the Workbench RPL the plan is anchored to.")
        self.rev_label = QLabel("—")
        self.status_label = QLabel("—")
        self.status_label.setToolTip(
            "draft while being edited; stale when the route, scope or "
            "inputs changed after the last generation; issued when locked "
            "for release.")
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
        save_row = QHBoxLayout()
        self.save_button = QPushButton("Save plan details")
        self.save_button.setToolTip(
            "Save name, description and notes. The default tool saves "
            "immediately when picked.")
        self.save_button.clicked.connect(self._save)
        save_row.addWidget(self.save_button)
        self.save_status = QLabel("")
        self.save_status.setStyleSheet(ui_helpers.hint_style())
        save_row.addWidget(self.save_status, 1)
        layout.addLayout(save_row)
        # Dirty tracking: unsaved edits mark the save button and survive
        # background refreshes of the same plan.
        self.name_edit.textEdited.connect(self._mark_dirty)
        self.description_edit.textEdited.connect(self._mark_dirty)
        self.notes_edit.textChanged.connect(self._notes_changed)

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
            plan_id = str(plan.get("plan_id") or "")
            same_plan = plan_id == self._loaded_plan_id
            for widget in (self.name_edit, self.description_edit,
                           self.notes_edit, self.save_button,
                           self.tool_combo, self.tool_config_combo):
                widget.setEnabled(has_plan)
            # A background refresh of the same plan must not clobber
            # unsaved name/description/notes edits.
            if not (same_plan and self._dirty):
                self.name_edit.setText(plan.get("name") or "")
                self.description_edit.setText(plan.get("description") or "")
                self.notes_edit.setPlainText(plan.get("notes") or "")
                self._set_dirty(False)
            if not same_plan:
                self.save_status.setText("")
            self._loaded_plan_id = plan_id
            self.method_label.setText(schema.METHOD_LABELS.get(
                schema.normalise_method(plan.get("method") or ""), "—"))
            self._refresh_tool_combos()
            self.rpl_label.setText(plan.get("rpl_name") or "—")
            self.rpl_revision_label.setText(plan.get("rpl_revision") or "—")
            self.rev_label.setText(plan.get("rev_label") or "—")
            self.status_label.setText(plan.get("status") or "draft")
        finally:
            self._loading = False

    def _mark_dirty(self, *_args) -> None:
        if not self._loading:
            self._set_dirty(True)

    def _notes_changed(self) -> None:
        self._mark_dirty()

    def _set_dirty(self, dirty: bool) -> None:
        self._dirty = bool(dirty)
        self.save_button.setText("Save plan details *" if self._dirty
                                 else "Save plan details")
        if self._dirty:
            self.save_status.setText("Unsaved changes.")
            self.save_status.setStyleSheet(ui_helpers.status_style("warn"))

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
        name = self.name_edit.text().strip()
        blank_name = not name
        if blank_name:
            name = self.model.plan.get("name") or "Plan"
            self.name_edit.setText(name)
        if self.model.update_plan({
            "name": name,
            "description": self.description_edit.text(),
            "notes": self.notes_edit.toPlainText(),
        }):
            # Cleared only after the write succeeded: on a store failure
            # the edits stay dirty-protected instead of silently reverting
            # on the next refresh.
            self._set_dirty(False)
            if blank_name:
                self.save_status.setText(
                    "Saved — the plan needs a name, so the previous name "
                    "was kept.")
                self.save_status.setStyleSheet(ui_helpers.status_style("warn"))
            else:
                self.save_status.setText("Saved.")
                self.save_status.setStyleSheet(ui_helpers.status_style("ok"))

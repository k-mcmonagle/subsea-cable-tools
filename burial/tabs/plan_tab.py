# -*- coding: utf-8 -*-
"""Plan tab — the open plan's identity, lineage and notes.

Plan selection and New / Duplicate / Rename / Delete live in the dock's top
strip (Planner scenario-management UX); this tab shows and edits the open
plan's descriptive fields.
"""

from __future__ import annotations

from qgis.PyQt.QtWidgets import (
    QFormLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import schema


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
        self.rev_label = QLabel("—")
        self.status_label = QLabel("—")
        self.notes_edit = QPlainTextEdit()
        self.notes_edit.setPlaceholderText(
            "Assumptions, review-basis notes, references…")
        form.addRow("Name:", self.name_edit)
        form.addRow("Description:", self.description_edit)
        form.addRow("Method:", self.method_label)
        form.addRow("RPL:", self.rpl_label)
        form.addRow("Revision:", self.rev_label)
        form.addRow("Status:", self.status_label)
        layout.addLayout(form)
        layout.addWidget(QLabel("Notes:"))
        layout.addWidget(self.notes_edit, 1)
        self.save_button = QPushButton("Save plan details")
        self.save_button.clicked.connect(self._save)
        layout.addWidget(self.save_button)

        self.hint = QLabel(
            "Create or open a plan with the selector above, register the RPL "
            "and survey inputs on the Inputs tab, build the Exclusion stack, "
            "then generate candidate sections in the Plan Builder.")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

        model.planChanged.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        self._loading = True
        try:
            plan = self.model.plan
            has_plan = bool(plan)
            for widget in (self.name_edit, self.description_edit,
                           self.notes_edit, self.save_button):
                widget.setEnabled(has_plan)
            self.name_edit.setText(plan.get("name") or "")
            self.description_edit.setText(plan.get("description") or "")
            self.notes_edit.setPlainText(plan.get("notes") or "")
            self.method_label.setText(
                schema.METHOD_LABELS.get(plan.get("method") or "", "—"))
            self.rpl_label.setText(plan.get("rpl_name") or "—")
            self.rev_label.setText(plan.get("rev_label") or "—")
            self.status_label.setText(plan.get("status") or "draft")
        finally:
            self._loading = False

    def _save(self) -> None:
        if self._loading or not self.model.plan:
            return
        self.model.update_plan({
            "name": self.name_edit.text().strip() or (self.model.plan.get("name") or "Plan"),
            "description": self.description_edit.text(),
            "notes": self.notes_edit.toPlainText(),
        })

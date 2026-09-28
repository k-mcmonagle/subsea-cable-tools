"""Import an existing burial plan from a CSV / Excel KP-range table.

Three pages, all logic in :mod:`burial.plan_import`:

1. **Source & columns** — file, sheet, header row, KP units, and one role
   per column (Start KP, End KP, Action, Tool, Notes) over a live preview.
2. **Values** — how each distinct Action value maps to Bury / Skip / Ignore
   (e.g. "Plough" → Bury, "Surface lay" → Skip) and which registered tool it
   (or the Tool column) names. Choices are remembered for next time.
3. **Review & import** — replace the plan or overlay only the KP ranges the
   file covers; the resulting sections, warnings and errors. Errors block
   the import; the import is one change-log entry (Ctrl+Z undoes it).
"""
from __future__ import annotations

import json
import os
from typing import Dict, List, Optional

from qgis.PyQt.QtCore import QSettings, Qt
from qgis.PyQt.QtGui import QBrush, QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QButtonGroup, QComboBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
    QRadioButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWizard, QWizardPage,
)

from . import plan_import as pi
from . import schema
from . import tools as tools_mod

_SETTINGS = "SubseaCableTools/BurialPlanner/planImport"
_PREVIEW_ROWS = 200
_HEADER_BG = QColor(225, 235, 245)
_DIM_FG = QColor(150, 150, 150)


def _item(text, editable=False) -> QTableWidgetItem:
    item = QTableWidgetItem("" if text is None else str(text))
    if not editable:
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    return item


def _col_letter(index: int) -> str:
    out = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        out = chr(65 + rem) + out
    return out


def _load_map(key: str) -> Dict[str, str]:
    try:
        data = json.loads(QSettings().value(f"{_SETTINGS}/{key}", "{}") or "{}")
    except (TypeError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def _save_map(key: str, updates: Dict[str, str]) -> None:
    data = _load_map(key)
    data.update({k.casefold(): v for k, v in updates.items() if k})
    QSettings().setValue(f"{_SETTINGS}/{key}", json.dumps(data))


# ---------------------------------------------------------------- page 1
class _SourcePage(QWizardPage):
    def __init__(self, wizard: "ImportPlanWizard"):
        super().__init__()
        self.wiz = wizard
        self.setTitle("Source and columns")
        self.setSubTitle("Choose the plan table, confirm the header row and tell the "
                         "importer what each column holds. Only Start KP and End KP are required.")
        self.rows: List[List[str]] = []
        self.roles: List[str] = []
        self._role_combos: List[QComboBox] = []
        self._loading = False

        layout = QVBoxLayout(self)
        form = QFormLayout()
        file_row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        file_row.addWidget(self.path_edit, 1)
        file_row.addWidget(browse)
        form.addRow("File:", file_row)
        opts = QHBoxLayout()
        self.sheet_combo = QComboBox()
        self.sheet_combo.setEnabled(False)
        self.sheet_combo.currentIndexChanged.connect(self._sheet_changed)
        opts.addWidget(QLabel("Sheet:"))
        opts.addWidget(self.sheet_combo, 1)
        opts.addWidget(QLabel("Header row:"))
        self.header_spin = QSpinBox()
        self.header_spin.setRange(0, 9999)
        self.header_spin.setSpecialValueText("none")
        self.header_spin.setToolTip("Row holding the column titles (0 = the table has no header). "
                                    "Rows above it are ignored.")
        self.header_spin.valueChanged.connect(self._header_changed)
        opts.addWidget(self.header_spin)
        opts.addWidget(QLabel("KP units:"))
        self.unit_combo = QComboBox()
        self.unit_combo.addItem("km", 1.0)
        self.unit_combo.addItem("m", 0.001)
        self.unit_combo.currentIndexChanged.connect(self.completeChanged)
        opts.addWidget(self.unit_combo)
        form.addRow(opts)
        from .rpl_reference import RplReferencePicker
        self.reference = RplReferencePicker(wizard.model, self)
        self.reference.changed.connect(self.completeChanged)
        form.addRow(self.reference)
        layout.addLayout(form)

        self.table = QTableWidget()
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setToolTip("Row 1 of the grid assigns each column a role; the rows below "
                              "preview the file (header row shaded, rows above it greyed).")
        layout.addWidget(self.table, 1)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

    # -- file
    def _browse(self) -> None:
        start = QSettings().value(f"{_SETTINGS}/lastDir", "") or ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Import burial plan", start,
            "Plan tables (*.csv *.tsv *.txt *.xlsx *.xlsm);;All files (*)")
        if path:
            QSettings().setValue(f"{_SETTINGS}/lastDir", os.path.dirname(path))
            self.load(path)

    def load(self, path: str, sheet: Optional[str] = None) -> None:
        try:
            rows, sheets = pi.read_grid(path, sheet)
        except Exception as exc:  # noqa: BLE001 — shown to the user
            QMessageBox.warning(self, "Import burial plan", f"Could not read the file:\n{exc}")
            return
        self.path_edit.setText(path)
        self._loading = True
        if sheet is None:
            self.sheet_combo.clear()
            self.sheet_combo.addItems(sheets)
            self.sheet_combo.setEnabled(len(sheets) > 1)
        self.rows = rows
        self.header_spin.setMaximum(max(0, len(rows)))
        self.header_spin.setValue(pi.guess_header_row(rows) + 1)
        self._loading = False
        self._guess_roles()

    def _sheet_changed(self, *_):
        if not self._loading and self.path_edit.text():
            self.load(self.path_edit.text(), self.sheet_combo.currentText())

    def _header_changed(self, *_):
        if not self._loading:
            self._guess_roles()

    # -- grid
    @property
    def header_index(self) -> int:
        return self.header_spin.value() - 1

    @property
    def headers(self) -> List[str]:
        width = max((len(r) for r in self.rows), default=0)
        if 0 <= self.header_index < len(self.rows):
            head = list(self.rows[self.header_index])
            return head + [""] * (width - len(head))
        return [""] * width

    @property
    def data_rows(self) -> List[List[str]]:
        return self.rows[self.header_index + 1:]

    @property
    def first_row_number(self) -> int:
        return self.header_index + 2

    def _guess_roles(self) -> None:
        self.roles = pi.guess_roles(self.headers, self.data_rows)
        # KP columns titled in metres ("KP (m)", "Chainage m") imply metres.
        start = self.roles.index(pi.ROLE_START) if pi.ROLE_START in self.roles else -1
        if start >= 0:
            title = self.headers[start].casefold()
            metres = "(m)" in title or title.endswith(" m") or "[m]" in title
            self.unit_combo.setCurrentIndex(1 if metres else 0)
        self._rebuild_table()

    def _rebuild_table(self) -> None:
        headers = self.headers
        width = len(headers)
        shown = self.rows[:_PREVIEW_ROWS]
        self.table.clear()
        self.table.setColumnCount(width)
        self.table.setRowCount(len(shown) + 1)
        self.table.setHorizontalHeaderLabels(
            [f"{_col_letter(i)}  {h}".strip() for i, h in enumerate(headers)])
        self.table.setVerticalHeaderLabels(["Role"] + [str(i + 1) for i in range(len(shown))])
        self._role_combos = []
        for col in range(width):
            combo = QComboBox()
            for role in pi.ROLES:
                combo.addItem(pi.ROLE_LABELS[role], role)
            combo.setCurrentIndex(max(0, combo.findData(self.roles[col] if col < len(self.roles) else "")))
            combo.currentIndexChanged.connect(lambda _i, c=col: self._role_changed(c))
            self.table.setCellWidget(0, col, combo)
            self._role_combos.append(combo)
        for r, row in enumerate(shown):
            for c in range(width):
                item = _item(row[c] if c < len(row) else "")
                if r == self.header_index:
                    item.setBackground(QBrush(_HEADER_BG))
                elif r < self.header_index:
                    item.setForeground(QBrush(_DIM_FG))
                self.table.setItem(r + 1, c, item)
        self.table.resizeColumnsToContents()
        self._update_status()

    def _role_changed(self, column: int) -> None:
        role = self._role_combos[column].currentData() or ""
        # One column per role: clear the role from any other column.
        for i, combo in enumerate(self._role_combos):
            if i != column and role and combo.currentData() == role:
                combo.blockSignals(True)
                combo.setCurrentIndex(0)
                combo.blockSignals(False)
        self.roles = [combo.currentData() or "" for combo in self._role_combos]
        self._update_status()

    def _update_status(self) -> None:
        missing = [pi.ROLE_LABELS[r] for r in (pi.ROLE_START, pi.ROLE_END) if r not in self.roles]
        if not self.rows:
            text = "Choose a CSV or Excel file."
        elif missing:
            text = "Assign a column to: " + ", ".join(missing) + "."
        else:
            text = f"{len(self.data_rows)} data row(s) below the header."
            if pi.ROLE_ACTION not in self.roles:
                text += (" No Action column: every row is treated as a burial range "
                         "(gaps become skips) unless you change it on the next page.")
        self.status.setText(text)
        self.completeChanged.emit()

    def isComplete(self) -> bool:
        return bool(self.data_rows) and pi.ROLE_START in self.roles and pi.ROLE_END in self.roles

    def kp_factor(self) -> float:
        return float(self.unit_combo.currentData() or 1.0)


# ---------------------------------------------------------------- page 2
class _ValuesPage(QWizardPage):
    def __init__(self, wizard: "ImportPlanWizard"):
        super().__init__()
        self.wiz = wizard
        self.setTitle("Values")
        self.setSubTitle("Tell the importer what each value means. Guesses come from common "
                         "wording (Plough, PLB, Trench, Jet, MFE → Bury; Skip, Surface lay, "
                         "No → Skip) and from your previous imports.")
        layout = QVBoxLayout(self)
        self.no_action_box = QGroupBox("Rows without an Action column")
        row = QHBoxLayout(self.no_action_box)
        row.addWidget(QLabel("Treat every row as:"))
        self.default_action = QComboBox()
        for action in (pi.ACTION_BURY, pi.ACTION_SKIP):
            self.default_action.addItem(pi.ACTION_LABELS[action], action)
        row.addWidget(self.default_action)
        row.addStretch(1)
        layout.addWidget(self.no_action_box)

        self.action_box = QGroupBox("Action values")
        box = QVBoxLayout(self.action_box)
        self.action_table = QTableWidget(0, 4)
        self.action_table.setHorizontalHeaderLabels(["Value", "Rows", "Treat as", "Tool"])
        self.action_table.horizontalHeader().setStretchLastSection(True)
        box.addWidget(self.action_table)
        layout.addWidget(self.action_box, 1)

        self.tool_box = QGroupBox("Tool values")
        box = QVBoxLayout(self.tool_box)
        self.tool_table = QTableWidget(0, 3)
        self.tool_table.setHorizontalHeaderLabels(["Value", "Rows", "Burial tool"])
        self.tool_table.horizontalHeader().setStretchLastSection(True)
        box.addWidget(self.tool_table)
        layout.addWidget(self.tool_box, 1)
        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

    def _tool_combo(self, selected: str) -> QComboBox:
        combo = QComboBox()
        combo.addItem("(plan default tool)", "")
        for tool in self.wiz.model.tools or []:
            combo.addItem(tools_mod.tool_display(self.wiz.model.tools, tool.get("tool_id") or ""),
                          str(tool.get("tool_id") or ""))
        combo.setCurrentIndex(max(0, combo.findData(selected or "")))
        return combo

    def initializePage(self) -> None:
        src = self.wiz.source
        tools = self.wiz.model.tools or []
        known_ids = {str(t.get("tool_id") or "") for t in tools}
        saved_actions, saved_tools = _load_map("actions"), _load_map("tools")
        c_action = src.roles.index(pi.ROLE_ACTION) if pi.ROLE_ACTION in src.roles else -1
        c_tool = src.roles.index(pi.ROLE_TOOL) if pi.ROLE_TOOL in src.roles else -1
        self.no_action_box.setVisible(c_action < 0)
        self.action_box.setVisible(c_action >= 0)
        self.tool_box.setVisible(c_tool >= 0)
        self.action_table.setColumnHidden(3, c_tool >= 0 or not tools)

        def tool_guess(value):
            saved = saved_tools.get(value.casefold())
            return saved if saved in known_ids else pi.guess_tool(value, tools)

        self.action_table.setRowCount(0)
        if c_action >= 0:
            for value, count in pi.distinct_values(src.data_rows, c_action):
                r = self.action_table.rowCount()
                self.action_table.insertRow(r)
                self.action_table.setItem(r, 0, _item(value or "(blank)"))
                self.action_table.item(r, 0).setData(Qt.ItemDataRole.UserRole, value)
                self.action_table.setItem(r, 1, _item(count))
                combo = QComboBox()
                for action in pi.ACTIONS:
                    combo.addItem(pi.ACTION_LABELS[action], action)
                guess = saved_actions.get(value.casefold()) or pi.guess_action(value)
                combo.setCurrentIndex(max(0, combo.findData(guess)))
                self.action_table.setCellWidget(r, 2, combo)
                self.action_table.setCellWidget(r, 3, self._tool_combo(tool_guess(value)))
            self.action_table.resizeColumnsToContents()
        self.tool_table.setRowCount(0)
        if c_tool >= 0:
            for value, count in pi.distinct_values(src.data_rows, c_tool):
                if not value:
                    continue
                r = self.tool_table.rowCount()
                self.tool_table.insertRow(r)
                self.tool_table.setItem(r, 0, _item(value))
                self.tool_table.item(r, 0).setData(Qt.ItemDataRole.UserRole, value)
                self.tool_table.setItem(r, 1, _item(count))
                self.tool_table.setCellWidget(r, 2, self._tool_combo(tool_guess(value)))
            self.tool_table.resizeColumnsToContents()
        if not tools:
            self.hint.setText("No burial tools are registered (Tools tab), so sections use the "
                              "plan default tool. Register tools to assign them per section.")
        else:
            self.hint.setText("Tool choices are stamped on the imported burial sections; "
                              "\"(plan default tool)\" leaves the section inheriting the plan default.")

    def spec(self) -> pi.ImportSpec:
        src = self.wiz.source
        spec = pi.ImportSpec(roles=list(src.roles), kp_factor=src.kp_factor(),
                             default_action=self.default_action.currentData() or pi.ACTION_BURY)
        for r in range(self.action_table.rowCount()):
            value = self.action_table.item(r, 0).data(Qt.ItemDataRole.UserRole) or ""
            spec.action_map[value] = self.action_table.cellWidget(r, 2).currentData()
            if not self.action_table.isColumnHidden(3):
                spec.tool_map[value] = self.action_table.cellWidget(r, 3).currentData() or ""
        for r in range(self.tool_table.rowCount()):
            value = self.tool_table.item(r, 0).data(Qt.ItemDataRole.UserRole) or ""
            spec.tool_map[value] = self.tool_table.cellWidget(r, 2).currentData() or ""
        return spec

    def remember(self, spec: pi.ImportSpec) -> None:
        _save_map("actions", {k: v for k, v in spec.action_map.items()})
        _save_map("tools", {k: v for k, v in spec.tool_map.items() if v})


# ---------------------------------------------------------------- page 3
class _ReviewPage(QWizardPage):
    def __init__(self, wizard: "ImportPlanWizard"):
        super().__init__()
        self.wiz = wizard
        self.result: Optional[pi.ImportResult] = None
        self.events: List[Dict] = []
        self.dropped: List[Dict] = []
        self.setTitle("Review and import")
        self.setCommitPage(True)
        layout = QVBoxLayout(self)
        mode_box = QGroupBox("Existing plan")
        mode_layout = QVBoxLayout(mode_box)
        self.replace_radio = QRadioButton("Replace the plan's burial sections with the imported plan")
        self.overlay_radio = QRadioButton(
            "Overlay: replace only the KP ranges the file covers; keep the rest of the plan")
        self.replace_radio.setChecked(True)
        group = QButtonGroup(self)
        for radio in (self.replace_radio, self.overlay_radio):
            group.addButton(radio)
            radio.toggled.connect(self._recompute)
            mode_layout.addWidget(radio)
        layout.addWidget(mode_box)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Start KP", "End KP", "Length (km)", "Tool", "Notes"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table, 1)
        self.messages = QLabel("")
        self.messages.setWordWrap(True)
        self.messages.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.messages)

    def initializePage(self) -> None:
        self.overlay_radio.setEnabled(bool(self.wiz.model.events))
        if not self.wiz.model.events:
            self.replace_radio.setChecked(True)
        self._recompute()
        self.wiz.setButtonText(QWizard.WizardButton.CommitButton, "Import")

    def mode(self) -> str:
        return pi.MODE_MERGE if self.overlay_radio.isChecked() else pi.MODE_REPLACE

    def _recompute(self, *_):
        model, src = self.wiz.model, self.wiz.source
        spec = self.wiz.values.spec()
        scope = model._scope_bounds()
        names = {str(t.get("tool_id") or ""): t.get("name") or "" for t in model.tools or []}
        try:
            map_range, _kp_map = src.reference.transform()
        except ValueError as exc:
            map_range = None
            self.result = pi.ImportResult(errors=[f"KP reference: {exc}"])
        else:
            self.result = pi.build_plan(src.data_rows, spec, scope, model.direction,
                                        src.first_row_number, names, map_range)
        result = self.result
        self.events, self.dropped = ([], [])
        if not result.errors:
            self.events, self.dropped = pi.plan_events(model.events, result, self.mode(),
                                                       model.direction, scope, model.sections)
        self.table.setRowCount(0)
        for rng in result.burial:
            r = self.table.rowCount()
            self.table.insertRow(r)
            tool = tools_mod.tool_display(model.tools, rng.tool_id) if rng.tool_id else "(plan default)"
            for c, text in enumerate((schema.format_kp(rng.start_kp), schema.format_kp(rng.end_kp),
                                      f"{rng.end_kp - rng.start_kp:.3f}", tool, rng.notes)):
                self.table.setItem(r, c, _item(text))
        self.table.resizeColumnsToContents()
        lo, hi = sorted(scope)
        scope_km = max(0.0, hi - lo)
        skip_rows = sum(1 for r in result.ranges if r.action == pi.ACTION_SKIP)
        text = (f"{len(result.ranges)} range(s) read ({skip_rows} skip), "
                f"{result.skipped_rows} row(s) ignored. <b>{len(result.burial)} burial section(s), "
                f"{result.burial_km:.3f} km</b>")
        if scope_km > 0:
            text += f" of the {scope_km:.3f} km scope ({100.0 * result.burial_km / scope_km:.1f}%)"
        text += "."
        if src.reference.rpl_id():
            text += (f" KPs were quoted on <i>{src.reference.label()}</i> and are shown "
                     "translated to this plan's RPL (same seabed positions).")
        if self.mode() == pi.MODE_MERGE and not result.errors:
            text += (f" After overlaying: {len(self.events) // 2} burial section(s) in the plan.")
        self.summary.setText(text)
        notes = [f"<span style='color:#b00020'>✖ {e}</span>" for e in result.errors]
        if result.transitions:
            notes.append(f"ℹ {result.transitions} tool transition(s): continuous burial changes tool "
                         "at one KP (e.g. PLUP / Start PLB), with no skip between.")
        notes += [f"⚠ {w}" for w in result.warnings[:20]]
        if len(result.warnings) > 20:
            notes.append(f"… and {len(result.warnings) - 20} more warning(s).")
        protected = [e for e in self.dropped if int(e.get("locked") or 0)
                     or e.get("status") == schema.EVENT_STATUS_CONFIRMED]
        if protected:
            notes.append(f"⚠ {len(protected)} locked or confirmed event(s) in the current plan "
                         "will be replaced.")
        if model.events and self.mode() == pi.MODE_REPLACE:
            notes.append("The current plan's burial events are replaced; the import is one "
                         "change-log entry, so Ctrl+Z (Undo last edit) or the Review && Export "
                         "change log restores them.")
        self.messages.setText("<br>".join(notes))
        self.completeChanged.emit()

    def isComplete(self) -> bool:
        return self.result is not None and not self.result.errors

    def validatePage(self) -> bool:
        return self.wiz.commit()


# ---------------------------------------------------------------- wizard
class ImportPlanWizard(QWizard):
    """Import a CSV/Excel plan into the open burial plan (``model``)."""

    def __init__(self, model, parent=None, path: Optional[str] = None):
        super().__init__(parent)
        self.model = model
        self.setWindowTitle("Import burial plan")
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.resize(1000, 700)
        self.source = _SourcePage(self)
        self.values = _ValuesPage(self)
        self.review = _ReviewPage(self)
        for page in (self.source, self.values, self.review):
            self.addPage(page)
        self.imported = False
        if path:
            self.source.load(path)

    def commit(self) -> bool:
        review = self.review
        if review.result is None or review.result.errors:
            return False
        spec = self.values.spec()
        result = review.result
        skip_rows = [r for r in result.ranges if r.action == pi.ACTION_SKIP]

        def patch(sections):
            return pi.section_updates(sections, result.burial, skip_rows)

        label = os.path.basename(self.source.path_edit.text()) or "plan table"
        reason = "overlay" if review.mode() == pi.MODE_MERGE else "replace"
        if self.source.reference.rpl_id():
            reason += f"; KPs translated from {self.source.reference.label()}"
        try:
            ok = self.model.import_plan(review.events, label, section_patch=patch,
                                        reason=f"imported plan ({reason})")
        except ValueError as exc:
            QMessageBox.warning(self, "Import burial plan", f"The plan could not be imported:\n{exc}")
            return False
        if ok:
            self.values.remember(spec)
            self.imported = True
        return bool(ok)

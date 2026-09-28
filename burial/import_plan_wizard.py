"""Import an existing burial plan from a KP-range table or an RPL's events.

From an RPL (logic in :mod:`burial.rpl_plan_import`): one page picks a
registered RPL, reads its PLDN / PLUP / PLB / skip events into boundary
tokens (editable per row, or by selecting the rows of a section), and the
shared Review page below commits the result.

From a table, three pages, all logic in :mod:`burial.plan_import`:

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
    QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMenu, QMessageBox, QPushButton,
    QRadioButton, QSpinBox, QTableWidget, QTableWidgetItem, QToolButton,
    QVBoxLayout, QWizard, QWizardPage,
)

from . import plan_import as pi
from . import rpl_plan_import as rpi
from . import schema
from . import tools as tools_mod

SOURCE_TABLE = "table"
SOURCE_RPL = "rpl"

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


# ---------------------------------------------------------------- RPL events
_SECTION_BG = {
    rpi.M_PLOUGH: QColor(176, 208, 240),
    rpi.M_TRENCHER: QColor(190, 226, 176),
    rpi.M_MFE: QColor(241, 216, 164),
    rpi.M_BURIAL: QColor(208, 206, 238),
    rpi.M_SKIP: QColor(228, 228, 228),
}
_WARN_BG = QColor(255, 214, 196)
_INFO_BG = QColor(255, 244, 196)
_RPL_COLUMNS = ["Pos", "RPL KP", "Plan KP", "Off route (m)", "Event", "Remarks",
                "Protection (to next)", "Read as", "Section after"]
_C_PROT, _C_READ, _C_SECTION = 6, 7, 8
MODE_EVENTS = "events"
MODE_PROTECTION = "protection"
_TOOL_METHODS = (rpi.M_PLOUGH, rpi.M_TRENCHER, rpi.M_MFE, rpi.M_BURIAL)


def _kp_text(kp) -> str:
    return "" if kp is None else schema.format_kp(kp)


class _RplEventsPage(QWizardPage):
    """Pick a registered RPL and turn its boundary events (or per-segment
    protection method) into a plan."""

    def __init__(self, wizard: "ImportPlanWizard"):
        super().__init__()
        self.wiz = wizard
        self.setTitle("Burial plan from an RPL")
        self.setSubTitle(
            "The plan is read from the RPL's boundary events (PLDN / PLUP, Start / End PLB, "
            "Start / End skip…) or from its per-segment Protection method, and placed on this "
            "plan's route by position; everything between burial sections is a skip. Check "
            "the Read as column — select rows to change it, or select the rows of a section "
            "and say what it is.")
        self.rows: List[rpi.RplRow] = []
        self.auto: Dict[int, List[rpi.Token]] = {}
        self.overrides: Dict[int, List[rpi.Token]] = {}
        self.tokens: Dict[int, List[rpi.Token]] = {}
        self.walk = rpi.Walk()
        self.rpl_label = ""
        self._loaded = False

        layout = QVBoxLayout(self)
        from .rpl_reference import RplReferencePicker
        self.picker = RplReferencePicker(wizard.model, self, label="RPL:")
        self.picker.combo.setToolTip(
            "A registered RPL (Cable Workbench register). Its positions are placed on this "
            "plan's route by seabed position, so another revision or route translates "
            "correctly; positions off the plan route are flagged.")
        self.picker.changed.connect(self.load)
        layout.addWidget(self.picker)

        opts = QHBoxLayout()
        opts.addWidget(QLabel("Read from:"))
        self.events_radio = QRadioButton("Events in")
        self.events_radio.setToolTip("Boundary events written at positions (PLDN, PLUP, "
                                     "Start PLB…) in the chosen text columns.")
        self.protection_radio = QRadioButton("Protection method (per segment)")
        self.protection_radio.setToolTip(
            "The ProtectionMethod column of the RPL's segments (\"Plough 1.0 m\", \"PLB\", "
            "\"Surface laid\"…): each change of method between segments is a boundary.")
        self.events_radio.setChecked(True)
        mode_group = QButtonGroup(self)
        mode_group.addButton(self.events_radio)
        mode_group.addButton(self.protection_radio)
        opts.addWidget(self.events_radio)
        self.use_event = QCheckBox("Event")
        self.use_remarks = QCheckBox("Remarks")
        for box in (self.use_event, self.use_remarks):
            box.setChecked(True)
            box.toggled.connect(self._rescan)
            opts.addWidget(box)
        opts.addWidget(self.protection_radio)
        self.protection_radio.toggled.connect(self._mode_changed)
        opts.addSpacing(16)
        self.swap = QCheckBox("Swap starts and ends")
        self.swap.setToolTip(
            "Read every detected start as an end and vice versa — for events written for "
            "the opposite lay direction, or labelled the wrong way round. Set automatically "
            "when the events pair up better swapped; rows you edited are not swapped.")
        self.swap.toggled.connect(self._swap_toggled)
        opts.addWidget(self.swap)
        self.swap_note = QLabel("")
        opts.addWidget(self.swap_note)
        opts.addStretch(1)
        self.only_events = QCheckBox("Only rows with events")
        self.only_events.toggled.connect(self._apply_filter)
        opts.addWidget(self.only_events)
        layout.addLayout(opts)

        settings = QHBoxLayout()
        settings.addWidget(QLabel("Import window: KP"))
        self.win_lo, self.win_hi = QDoubleSpinBox(), QDoubleSpinBox()
        for spin in (self.win_lo, self.win_hi):
            spin.setDecimals(3)
            spin.setRange(-100000.0, 100000.0)
            spin.setToolTip("Only this KP range is imported (default: the plan scope). "
                            "Overlay on the next page replaces just this range.")
            spin.valueChanged.connect(self._update_status)
        settings.addWidget(self.win_lo)
        settings.addWidget(QLabel("to"))
        settings.addWidget(self.win_hi)
        settings.addSpacing(16)
        self.tool_combos: Dict[str, QComboBox] = {}
        for method in _TOOL_METHODS:
            settings.addWidget(QLabel(rpi.METHOD_LABELS[method].split(" (")[0] + ":"))
            combo = QComboBox()
            combo.setToolTip(f"Registered burial tool for {rpi.METHOD_LABELS[method]} sections.")
            self.tool_combos[method] = combo
            settings.addWidget(combo, 1)
        layout.addLayout(settings)

        self.protection_box = QGroupBox("Protection method values")
        box_layout = QVBoxLayout(self.protection_box)
        self.protection_table = QTableWidget(0, 3)
        self.protection_table.setHorizontalHeaderLabels(["Value", "Segments", "Read as"])
        self.protection_table.horizontalHeader().setStretchLastSection(False)
        self.protection_table.verticalHeader().setVisible(False)
        self.protection_table.setMaximumHeight(140)
        box_layout.addWidget(self.protection_table)
        self.protection_box.setVisible(False)
        layout.addWidget(self.protection_box)

        self.table = QTableWidget(0, len(_RPL_COLUMNS))
        self.table.setHorizontalHeaderLabels(_RPL_COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        layout.addWidget(self.table, 1)

        actions = QHBoxLayout()
        for text, fill in (("Set start", self._fill_start_menu), ("Set end", self._fill_end_menu),
                           ("Selected rows are", self._fill_paint_menu)):
            button = QToolButton()
            button.setText(text)
            button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            menu = QMenu(button)
            fill(menu)
            button.setMenu(menu)
            actions.addWidget(button)
        clear = QPushButton("Clear events")
        clear.setToolTip("Remove the boundary events of the selected rows.")
        clear.clicked.connect(self._clear_selected)
        reset = QPushButton("Reset to detected")
        reset.setToolTip("Drop your edits on the selected rows (all rows if none selected).")
        reset.clicked.connect(self._reset_selected)
        actions.addWidget(clear)
        actions.addWidget(reset)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.issue_list = QListWidget()
        self.issue_list.setMaximumHeight(110)
        self.issue_list.setToolTip("Click an issue to select its row.")
        self.issue_list.itemClicked.connect(self._issue_clicked)
        layout.addWidget(self.issue_list)

    # -- setup
    def initializePage(self) -> None:
        if self._loaded:
            return
        lo, hi = sorted(self.wiz.model._scope_bounds())
        self.win_lo.setValue(lo)
        self.win_hi.setValue(hi)
        self._fill_tool_combos()
        self.load()

    def _fill_tool_combos(self) -> None:
        tools = self.wiz.model.tools or []
        known = {str(t.get("tool_id") or "") for t in tools}
        saved = _load_map("rplTools")
        for method, combo in self.tool_combos.items():
            combo.clear()
            combo.addItem("(plan default tool)", "")
            for tool in tools:
                combo.addItem(tools_mod.tool_display(tools, tool.get("tool_id") or ""),
                              str(tool.get("tool_id") or ""))
            guess = saved.get(method, "")
            if guess not in known:
                guess = ""
                if method != rpi.M_BURIAL:
                    same = [t for t in tools
                            if schema.normalise_method(t.get("tool_type") or "") == method]
                    guess = str(same[0].get("tool_id") or "") if same else ""
            combo.setCurrentIndex(max(0, combo.findData(guess)))

    def load(self) -> None:
        from .rpl_reference import rpl_event_rows
        try:
            rows, label = rpl_event_rows(self.wiz.model, self.picker.rpl_id())
            error = ""
        except ValueError as exc:
            rows, label, error = [], "", str(exc)
        self.set_rows(rows, label, error)

    def set_rows(self, rows: List[rpi.RplRow], label: str = "RPL", error: str = "") -> None:
        """Use ``rows`` (already placed on the plan route) and pick the source:
        events when the RPL has any, else a Protection method column that
        names burial, else events (so the row tools can build the plan)."""
        self._loaded = True
        self.rows, self.rpl_label, self.load_error = list(rows), label, error
        self.overrides = {}
        use_protection = (not rpi.auto_tokens(self.rows, self._fields())
                          and rpi.has_burial_protection(self.rows))
        self._set_mode(MODE_PROTECTION if use_protection else MODE_EVENTS)
        self._fill_protection_values()
        self._fill_table()
        self._rescan()

    def mode(self) -> str:
        return MODE_PROTECTION if self.protection_radio.isChecked() else MODE_EVENTS

    def _set_mode(self, mode: str) -> None:
        for radio in (self.events_radio, self.protection_radio):
            radio.blockSignals(True)
        (self.protection_radio if mode == MODE_PROTECTION else self.events_radio).setChecked(True)
        for radio in (self.events_radio, self.protection_radio):
            radio.blockSignals(False)
        self._mode_widgets()

    def _mode_widgets(self) -> None:
        protection = self.mode() == MODE_PROTECTION
        self.use_event.setEnabled(not protection)
        self.use_remarks.setEnabled(not protection)
        self.protection_box.setVisible(protection)

    def _mode_changed(self, *_):
        # Row edits belong to the source they were made against.
        self.overrides = {}
        self._mode_widgets()
        self._rescan()

    def _fill_protection_values(self) -> None:
        saved = _load_map("protectionValues")
        values = rpi.protection_values(self.rows)
        self.protection_radio.setEnabled(bool(values))
        self.protection_table.setRowCount(0)
        for value, count in values:
            r = self.protection_table.rowCount()
            self.protection_table.insertRow(r)
            item = _item(value)
            item.setData(Qt.ItemDataRole.UserRole, value)
            self.protection_table.setItem(r, 0, item)
            self.protection_table.setItem(r, 1, _item(count))
            combo = QComboBox()
            for method in rpi.METHODS:
                combo.addItem(rpi.METHOD_LABELS[method], method)
            guess = saved.get(value.casefold())
            if guess not in rpi.METHODS:
                guess = rpi.classify_protection(value)
            combo.setCurrentIndex(max(0, combo.findData(guess)))
            combo.currentIndexChanged.connect(self._rescan)
            self.protection_table.setCellWidget(r, 2, combo)
        self.protection_table.resizeColumnsToContents()
        self.protection_table.setColumnWidth(2, max(self.protection_table.columnWidth(2), 200))

    def protection_map(self) -> Dict[str, str]:
        out = {}
        for r in range(self.protection_table.rowCount()):
            value = self.protection_table.item(r, 0).data(Qt.ItemDataRole.UserRole) or ""
            out[value] = self.protection_table.cellWidget(r, 2).currentData() or rpi.M_SKIP
        return out

    def _fields(self) -> List[str]:
        return [name for name, box in (("event", self.use_event), ("remarks", self.use_remarks))
                if box.isChecked()]

    def _rescan(self, *_):
        if self.mode() == MODE_PROTECTION:
            self.auto = rpi.protection_tokens(self.rows, self.protection_map())
        else:
            self.auto = rpi.auto_tokens(self.rows, self._fields())
        swapped = rpi.auto_swap(self.rows, self.auto, self.wiz.model.direction)
        self.swap.blockSignals(True)
        self.swap.setChecked(swapped)
        self.swap.blockSignals(False)
        self.swap_note.setText("(auto: the events read the other way round)" if swapped else "")
        self._recompute()

    def _swap_toggled(self, *_):
        self.swap_note.setText("")
        self._recompute()

    def _recompute(self) -> None:
        self.tokens = rpi.effective_tokens(self.auto, self.overrides, self.swap.isChecked())
        self.walk = rpi.walk(self.rows, self.tokens, self.wiz.model.direction)
        self._update_state_cells()
        self._fill_issues()
        self._apply_filter()
        self._update_status()

    # -- table
    def _fill_table(self) -> None:
        self.table.setColumnHidden(_C_PROT, False)   # hidden columns are not resized
        self.table.setRowCount(0)
        self.table.setRowCount(len(self.rows))
        for r, row in enumerate(self.rows):
            offset = "" if row.offset_m is None else f"{row.offset_m:.1f}"
            values = ("" if row.pos_no is None else row.pos_no, _kp_text(row.stated_kp),
                      _kp_text(row.kp) if row.kp is not None else "not placed", offset,
                      row.event, row.remarks, row.protection, "", "")
            for c, value in enumerate(values):
                item = _item(value)
                if c == 3 and row.offset_m is not None and row.offset_m > rpi.OFFSET_TOL_M:
                    item.setBackground(QBrush(_WARN_BG))
                    item.setToolTip("More than %.0f m from the plan route: the routes differ "
                                    "here." % rpi.OFFSET_TOL_M)
                self.table.setItem(r, c, item)
        self.table.resizeColumnsToContents()
        for c in (4, 5, _C_PROT):
            self.table.setColumnWidth(c, min(self.table.columnWidth(c), 260))
        self.table.setColumnHidden(_C_PROT, not any(r.protection for r in self.rows))

    def _update_state_cells(self) -> None:
        state = ""
        order = rpi.travel_order(self.rows, self.wiz.model.direction)
        after = {}
        for index in order:
            state = self.walk.after.get(index, state)
            after[index] = state
        for r in range(len(self.rows)):
            tokens = self.tokens.get(r, [])
            read = QTableWidgetItem(" + ".join(t.label for t in tokens))
            read.setFlags(read.flags() & ~Qt.ItemFlag.ItemIsEditable)
            tips = []
            if r in self.overrides:
                font = read.font()
                font.setBold(True)
                read.setFont(font)
                tips.append("Edited (Reset to detected restores the RPL's own events).")
            issues = self.walk.issues.get(r, [])
            if issues:
                warn = any(i.level == rpi.LEVEL_WARN for i in issues)
                read.setBackground(QBrush(_WARN_BG if warn else _INFO_BG))
                tips += [i.text for i in issues]
            read.setToolTip("\n".join(tips))
            self.table.setItem(r, _C_READ, read)
            method = after.get(r, "")
            label = rpi.METHOD_LABELS.get(method, "") if method else "skip"
            section = _item(label)
            section.setBackground(QBrush(_SECTION_BG.get(method or rpi.M_SKIP)))
            if not method:
                section.setForeground(QBrush(_DIM_FG))
            self.table.setItem(r, _C_SECTION, section)
        self.table.resizeColumnToContents(_C_READ)

    def _apply_filter(self, *_):
        only = self.only_events.isChecked()
        for r in range(len(self.rows)):
            show = (not only or r in self.tokens or r in self.overrides
                    or r in self.walk.issues or bool(self.rows[r].event))
            self.table.setRowHidden(r, not show)

    def _fill_issues(self) -> None:
        self.issue_list.clear()
        rows = self.rows
        for index in sorted(self.walk.issues, key=lambda i: (rows[i].kp is None, rows[i].kp or 0.0)):
            row = rows[index]
            for issue in self.walk.issues[index]:
                mark = "⚠" if issue.level == rpi.LEVEL_WARN else "ℹ"
                where = row.name + (f" (KP {schema.format_kp(row.kp)})" if row.kp is not None else "")
                item = QListWidgetItem(f"{mark} {where}: {issue.text}")
                item.setData(Qt.ItemDataRole.UserRole, index)
                self.issue_list.addItem(item)
        self.issue_list.setVisible(self.issue_list.count() > 0)

    def _issue_clicked(self, item) -> None:
        index = item.data(Qt.ItemDataRole.UserRole)
        if index is None:
            return
        self.table.setRowHidden(int(index), False)
        self.table.selectRow(int(index))
        self.table.scrollToItem(self.table.item(int(index), 0),
                                QAbstractItemView.ScrollHint.PositionAtCenter)

    def _update_status(self, *_):
        if not self.rows:
            self.status.setText(f"<span style='color:#b00020'>{self.load_error}</span>"
                                if getattr(self, "load_error", "") else "Choose a registered RPL.")
            self.completeChanged.emit()
            return
        placed = sum(1 for r in self.rows if r.kp is not None)
        spans = [s for s in self.walk.spans if s.method != rpi.M_SKIP]
        by_method: Dict[str, float] = {}
        for span in spans:
            by_method[span.method] = by_method.get(span.method, 0.0) + span.hi - span.lo
        parts = [f"{rpi.METHOD_LABELS[m]} {km:.3f} km" for m, km in by_method.items()]
        text = (f"<b>{self.rpl_label}</b>: {len(self.rows)} position(s), {placed} placed on the "
                f"plan route, {len(self.tokens)} with events. <b>{len(spans)} burial section(s)</b>"
                + (f" — {', '.join(parts)}" if parts else "") + ".")
        warns = self.walk.count(rpi.LEVEL_WARN)
        if warns:
            text += f" <span style='color:#b05000'>{warns} event(s) need a look (listed below).</span>"
        if not self.auto and self.mode() == MODE_EVENTS and rpi.has_burial_protection(self.rows):
            text += (" No boundary events were recognised, but the Protection method column "
                     "names burial: choose <i>Read from: Protection method</i>.")
        elif not self.auto:
            text += (" No boundary events were recognised: select the rows of each section "
                     "and use <i>Selected rows are</i>, or set starts and ends row by row.")
        self.status.setText(text)
        self.completeChanged.emit()

    # -- edits
    def selected_rows(self) -> List[int]:
        return sorted({index.row() for index in self.table.selectionModel().selectedRows()})

    def _fill_start_menu(self, menu: QMenu) -> None:
        for method in rpi.METHODS:
            token = rpi.Token(rpi.START, method)
            menu.addAction(f"{token.label}  ({rpi.METHOD_LABELS[method]})",
                           lambda t=token: self.set_token(t))

    def _fill_end_menu(self, menu: QMenu) -> None:
        for method in rpi.METHODS:
            token = rpi.Token(rpi.END, method)
            menu.addAction(f"{token.label}  ({rpi.METHOD_LABELS[method]})",
                           lambda t=token: self.set_token(t))

    def _fill_paint_menu(self, menu: QMenu) -> None:
        for method in rpi.METHODS:
            menu.addAction(rpi.METHOD_LABELS[method], lambda m=method: self.paint(m))

    def _context_menu(self, pos) -> None:
        menu = QMenu(self.table)
        self._fill_start_menu(menu.addMenu("Set start"))
        self._fill_end_menu(menu.addMenu("Set end"))
        self._fill_paint_menu(menu.addMenu("Selected rows are"))
        menu.addSeparator()
        menu.addAction("Clear events", self._clear_selected)
        menu.addAction("Reset to detected", self._reset_selected)
        from ..qgis_compat import qt_exec
        qt_exec(menu, self.table.viewport().mapToGlobal(pos))

    def set_token(self, token: rpi.Token, rows: Optional[List[int]] = None) -> None:
        for r in (rows if rows is not None else self.selected_rows()):
            self.overrides[r] = rpi.set_token(self.tokens.get(r, []), token)
        self._recompute()

    def paint(self, method: str, rows: Optional[List[int]] = None) -> bool:
        try:
            updates = rpi.paint(self.rows, self.tokens,
                                rows if rows is not None else self.selected_rows(),
                                method, self.wiz.model.direction)
        except ValueError as exc:
            QMessageBox.information(self, "Burial plan from RPL", str(exc))
            return False
        self.overrides.update(updates)
        self._recompute()
        return True

    def _clear_selected(self) -> None:
        for r in self.selected_rows():
            self.overrides[r] = []
        self._recompute()

    def _reset_selected(self) -> None:
        rows = self.selected_rows()
        if rows:
            for r in rows:
                self.overrides.pop(r, None)
        else:
            self.overrides = {}
        self._recompute()

    # -- result
    def tool_map(self) -> Dict[str, str]:
        return {m: combo.currentData() or "" for m, combo in self.tool_combos.items()}

    def window(self):
        return (self.win_lo.value(), self.win_hi.value())

    def build_result(self) -> pi.ImportResult:
        return rpi.build_result(self.rows, self.walk, self.tool_map(), self.window(),
                                self.wiz.model.direction,
                                protection_notes=self.mode() == MODE_PROTECTION)

    def remember(self) -> None:
        _save_map("rplTools", {m: v for m, v in self.tool_map().items() if v})
        if self.mode() == MODE_PROTECTION:
            _save_map("protectionValues", self.protection_map())

    def isComplete(self) -> bool:
        return any(r.kp is not None for r in self.rows)


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
        if self.wiz.rpl is not None:
            lo, hi = sorted(self.wiz.rpl.window())
            self.overlay_radio.setText(
                f"Overlay: replace only the import window KP {schema.format_kp(lo)}–"
                f"{schema.format_kp(hi)}; keep the rest of the plan")
        self._recompute()
        self.wiz.setButtonText(QWizard.WizardButton.CommitButton, "Import")

    def mode(self) -> str:
        return pi.MODE_MERGE if self.overlay_radio.isChecked() else pi.MODE_REPLACE

    def _recompute(self, *_):
        model, src = self.wiz.model, self.wiz.source
        scope = model._scope_bounds()
        if self.wiz.rpl is not None:
            self.result = self.wiz.rpl.build_result()
        else:
            spec = self.wiz.values.spec()
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
        if self.wiz.rpl is not None:
            text = (f"Read from the events on <i>{self.wiz.rpl.rpl_label}</i>, placed on this "
                    f"plan's route by position. <b>{len(result.burial)} burial section(s), "
                    f"{result.burial_km:.3f} km</b>")
        else:
            text = (f"{len(result.ranges)} range(s) read ({skip_rows} skip), "
                    f"{result.skipped_rows} row(s) ignored. <b>{len(result.burial)} burial "
                    f"section(s), {result.burial_km:.3f} km</b>")
        if scope_km > 0:
            text += f" of the {scope_km:.3f} km scope ({100.0 * result.burial_km / scope_km:.1f}%)"
        text += "."
        if src is not None and src.reference.rpl_id():
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
    """Import a plan into the open burial plan (``model``) from a CSV/Excel
    KP-range table (``source_kind`` SOURCE_TABLE) or from the boundary
    events of a registered RPL (SOURCE_RPL)."""

    def __init__(self, model, parent=None, path: Optional[str] = None,
                 source_kind: str = SOURCE_TABLE):
        super().__init__(parent)
        self.model = model
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.resize(1100, 760)
        self.source: Optional[_SourcePage] = None
        self.values: Optional[_ValuesPage] = None
        self.rpl: Optional[_RplEventsPage] = None
        if source_kind == SOURCE_RPL:
            self.setWindowTitle("Import burial plan from an RPL")
            self.rpl = _RplEventsPage(self)
            pages = [self.rpl]
        else:
            self.setWindowTitle("Import burial plan")
            self.source = _SourcePage(self)
            self.values = _ValuesPage(self)
            pages = [self.source, self.values]
        self.review = _ReviewPage(self)
        for page in pages + [self.review]:
            self.addPage(page)
        self.imported = False
        if path and self.source is not None:
            self.source.load(path)

    def commit(self) -> bool:
        review = self.review
        if review.result is None or review.result.errors:
            return False
        result = review.result
        skip_rows = [r for r in result.ranges if r.action == pi.ACTION_SKIP]

        def patch(sections):
            return pi.section_updates(sections, result.burial, skip_rows)

        reason = "overlay" if review.mode() == pi.MODE_MERGE else "replace"
        if self.rpl is not None:
            label = f"RPL events: {self.rpl.rpl_label}"
            reason += "; boundary events placed by position"
            try:
                ok = self.model.import_plan(review.events, label, section_patch=patch,
                                            reason=f"imported plan from RPL ({reason})")
            except ValueError as exc:
                QMessageBox.warning(self, "Import burial plan",
                                    f"The plan could not be imported:\n{exc}")
                return False
            if ok:
                self.rpl.remember()
                self.imported = True
            return bool(ok)
        spec = self.values.spec()
        label = os.path.basename(self.source.path_edit.text()) or "plan table"
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

# -*- coding: utf-8 -*-
"""The column-mapping screen: give each delivered column its role, on a live preview of the file.

Above each column of the preview sit two chips: its **role** (KP, DOL,
Timestamp, Easting…, *Ignore*, or unmapped) and its **units** (km or m,
positive down or up, the time format and UTC offset, the CRS of eastings
and northings, a tension's name…). A click on a chip opens its choices.
The first rows are read through the mapping as it changes: a cell that
will not read is red with the reason, and the line below says what the
mapping provides (KP, DOL, time, position) with the parsed time range in
UTC. Suggested roles are tinted until the user moves on; nothing is mapped
silently. Unmapped numeric columns are kept as extra channels (shown as
*Extra (auto)*). How the file is split (delimiter, header and units lines,
decimal comma, missing values, encoding, sheet) folds away under *Reading*.

Pure Qt over :mod:`.profile` / :mod:`.reader`; no QGIS beyond the CRS picker.
"""

from __future__ import annotations

import copy
from typing import Dict, List, Optional, Sequence, Set, Tuple

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QBrush, QColor
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...qgis_compat import DIALOG_ACCEPTED, EDIT_TRIGGER_NONE, qt_exec
from ..ui_helpers import color, hint_style, status_style
from . import profile as P
from . import reader as rd
from . import roles as R

_RICH = getattr(getattr(Qt, "TextFormat", Qt), "RichText")
_ITEM_ENABLED = getattr(getattr(Qt, "ItemFlag", Qt), "ItemIsEnabled")
_ITEM_SELECTABLE = getattr(getattr(Qt, "ItemFlag", Qt), "ItemIsSelectable")
PREVIEW_ROWS = 200
CHIP_ROLE, CHIP_UNITS = 0, 1
FIXED_ROWS = 2

DELIMITERS = [("Auto", None), ("Comma", ","), ("Semicolon", ";"), ("Tab", "\t"), ("Pipe |", "|"),
              ("Spaces", "whitespace")]
ENCODINGS = [("Auto", None), ("UTF-8", "utf-8"), ("Windows-1252", "cp1252"), ("Latin-1", "latin-1")]
OFFSETS = ["UTC", "+01:00", "+02:00", "+03:00", "+04:00", "+05:30", "+08:00", "+09:00", "+10:00", "-03:00",
           "-05:00", "-06:00", "-08:00"]
TIME_PATTERNS = ["%d/%m/%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%d.%m.%Y %H:%M:%S", "%Y/%m/%d %H:%M:%S",
                 "%d-%m-%Y %H:%M:%S", "%Y:%j:%H:%M:%S"]
DATE_PATTERNS = ["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d.%m.%Y", "%Y/%m/%d", "%Y:%j"]
_QT_TOKENS = [("yyyy", "%Y"), ("MMM", "%b"), ("DDD", "%j"), ("zzz", "%f"), ("yy", "%y"), ("MM", "%m"),
              ("dd", "%d"), ("HH", "%H"), ("mm", "%M"), ("ss", "%S")]

_SUGGESTED_STYLE = "QToolButton { background: #fff4c2; border: 1px solid #e0c060; border-radius: 3px; padding: 1px 4px; }"
_MAPPED_STYLE = "QToolButton { background: #e3eefc; border: 1px solid #9bb8e0; border-radius: 3px; padding: 1px 4px; font-weight: 600; }"
_PLAIN_STYLE = "QToolButton { border: 1px solid #c8c8c8; border-radius: 3px; padding: 1px 4px; color: #777; }"
_UNITS_STYLE = "QToolButton { border: 1px solid #c8c8c8; border-radius: 3px; padding: 1px 4px; }"
_MISSING_STYLE = "QToolButton { border: 1px solid #d08080; border-radius: 3px; padding: 1px 4px; color: #b71c1c; }"


def strftime_to_qt(pattern: str) -> str:
    """``%d/%m/%Y %H:%M:%S`` -> ``dd/MM/yyyy HH:mm:ss`` (how patterns are shown)."""
    out = pattern
    for qt, py in _QT_TOKENS:
        out = out.replace(py, qt)
    return out


def qt_to_strftime(pattern: str) -> str:
    """``dd/MM/yyyy HH:mm:ss`` -> ``%d/%m/%Y %H:%M:%S`` (patterns as typed); ``%`` patterns pass through."""
    if "%" in pattern:
        return pattern
    out, i = "", 0
    while i < len(pattern):
        for qt, py in _QT_TOKENS:
            if pattern.startswith(qt, i):
                out += py
                i += len(qt)
                break
        else:
            out += pattern[i]
            i += 1
    return out


def format_text(fmt: str) -> str:
    return {"auto": "auto", "iso": "ISO 8601", "epoch": "epoch s", "epoch_ms": "epoch ms"}.get(
        fmt or "auto", strftime_to_qt(fmt or ""))


def _chip(tip: str = "") -> QToolButton:
    button = QToolButton()
    button.setToolTip(tip)
    button.setAutoRaise(False)
    return button


class ColumnMappingWidget(QWidget):
    """Map a delivery's columns to roles on a preview (see the module notes)."""

    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._name = ""
        self._content = b""
        self._raw: Optional[rd.RawTable] = None
        self._parsed: Optional[rd.ParsedData] = None
        self._profile = P.new_profile()
        self._suggested: Set[str] = set()
        self._saved: List[Tuple[str, Dict]] = []
        self._building = False
        self._preview_error = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        self.info = QLabel("")
        self.info.setWordWrap(True)
        top.addWidget(self.info, 1)
        self.reading_button = QToolButton()
        self.reading_button.setText("Reading ▸")
        self.reading_button.setCheckable(True)
        self.reading_button.setToolTip("How the file is split into columns: delimiter, header and units lines, "
                                       "decimal comma, missing values, text encoding, worksheet.")
        self.reading_button.toggled.connect(self._toggle_reading)
        top.addWidget(self.reading_button)
        layout.addLayout(top)

        self.reading = QWidget()
        reading = QHBoxLayout(self.reading)
        reading.setContentsMargins(0, 0, 0, 0)
        self.delimiter = QComboBox()
        for text, value in DELIMITERS:
            self.delimiter.addItem(text, value)
        self.header_line = QSpinBox()
        self.header_line.setRange(1, 100000)
        self.header_line.setToolTip("The line (or worksheet row) holding the column names.")
        self.units_line = QSpinBox()
        self.units_line.setRange(0, 100000)
        self.units_line.setSpecialValueText("none")
        self.units_line.setToolTip("A line of units under the header (none: the data starts after the header).")
        self.decimal_comma = QCheckBox("Decimal comma")
        self.missing = QLineEdit()
        self.missing.setToolTip("Values that mean 'no value', separated by commas (letter case ignored). "
                                "Leave a blank first to treat empty cells as missing.")
        self.encoding = QComboBox()
        for text, value in ENCODINGS:
            self.encoding.addItem(text, value)
        self.sheet = QComboBox()
        self.sheet.setToolTip("The worksheet to read.")
        for label, widget in (("Delimiter", self.delimiter), ("Header line", self.header_line),
                              ("Units line", self.units_line), ("", self.decimal_comma),
                              ("Missing", self.missing), ("Encoding", self.encoding), ("Sheet", self.sheet)):
            if label:
                reading.addWidget(QLabel(label))
            reading.addWidget(widget)
        reading.addStretch(1)
        self.reading.hide()
        layout.addWidget(self.reading)
        for signal in (self.delimiter.currentIndexChanged, self.header_line.valueChanged,
                       self.units_line.valueChanged, self.decimal_comma.toggled, self.missing.editingFinished,
                       self.encoding.currentIndexChanged, self.sheet.currentIndexChanged):
            signal.connect(self._reading_changed)

        row = QHBoxLayout()
        row.addWidget(QLabel("Mapping:"))
        self.saved_combo = QComboBox()
        self.saved_combo.setToolTip("Start from a saved mapping (import profile) of this data file.")
        self.saved_combo.activated.connect(self._apply_saved)
        row.addWidget(self.saved_combo)
        self.profile_note = QLabel("")
        self.profile_note.setStyleSheet(hint_style())
        row.addWidget(self.profile_note, 1)
        self.suggest_button = QPushButton("Suggest")
        self.suggest_button.setToolTip("Propose roles from the column names (shown tinted until you move on).")
        self.suggest_button.clicked.connect(self.suggest)
        row.addWidget(self.suggest_button)
        clear = QPushButton("Clear")
        clear.setToolTip("Unmap every column.")
        clear.clicked.connect(self.clear)
        row.addWidget(clear)
        layout.addLayout(row)

        self.table = QTableWidget()
        self.table.setEditTriggers(EDIT_TRIGGER_NONE)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setDefaultSectionSize(22)
        layout.addWidget(self.table, 1)

        self.needed = QLabel("")
        self.needed.setTextFormat(_RICH)
        self.needed.setWordWrap(True)
        layout.addWidget(self.needed)
        rows = QHBoxLayout()
        self.rows_label = QLabel("")
        rows.addWidget(self.rows_label, 1)
        self.keep_extra = QCheckBox("Keep other numeric columns as extra channels")
        self.keep_extra.setChecked(True)
        self.keep_extra.setToolTip("Unmapped columns of numbers are kept under their column name; the original "
                                   "file is kept in full either way.")
        self.keep_extra.toggled.connect(self._keep_extra_toggled)
        rows.addWidget(self.keep_extra)
        layout.addLayout(rows)
        self.problem_label = QLabel("")
        self.problem_label.setWordWrap(True)
        self.problem_label.setStyleSheet(status_style("error"))
        layout.addWidget(self.problem_label)
        self._refresh_saved_combo()

    # -- inputs ------------------------------------------------------------------
    def set_saved_profiles(self, profiles: Sequence[Tuple[str, Dict]]) -> None:
        """``[(label, profile)]`` offered in *Mapping*."""
        self._saved = list(profiles)
        self._refresh_saved_combo()

    def _refresh_saved_combo(self) -> None:
        self.saved_combo.blockSignals(True)
        self.saved_combo.clear()
        self.saved_combo.addItem("This file's mapping", None)
        for index, (label, _profile) in enumerate(self._saved):
            self.saved_combo.addItem(label, index)
        self.saved_combo.blockSignals(False)
        self.saved_combo.setVisible(bool(self._saved))

    def set_file(self, name: str, content: bytes, profile: Optional[Dict] = None, suggest: bool = True,
                 note: str = "") -> None:
        """Show ``name`` with ``profile`` (its reading settings and roles) or, without one, sniffed
        settings and (``suggest``) proposed roles."""
        self._name, self._content = name, content
        if profile is not None:
            self._profile = P.normalise(copy.deepcopy(profile))
        else:
            kept = self._profile
            self._profile = P.new_profile(kept.get("name", ""))
            self._profile["timezone"], self._profile["crs"] = kept.get("timezone", "UTC"), kept.get("crs", "")
            self._profile["table"] = rd.sniff(content, name)
        self._suggested = set()
        self._load_reading()
        self._read()
        if profile is None and suggest and self._raw is not None:
            self.suggest()
        else:
            self._rebuild()
            self.profile_note.setText("")
        if note:
            self.profile_note.setText(note)

    def profile(self) -> Dict:
        return P.normalise(copy.deepcopy(self._profile))

    def headers(self) -> List[str]:
        return list(self._raw.headers) if self._raw is not None else []

    def problems(self) -> List[str]:
        if self._preview_error:
            return [self._preview_error]
        if self._raw is None:
            return ["The file could not be read."]
        return P.problems(self._profile, self._raw.headers)

    def is_complete(self) -> bool:
        return not self.problems()

    def confirm_suggestions(self) -> None:
        """The user moved on: suggestions count as chosen (the tint goes)."""
        self._suggested = set()
        self._refresh_chips()

    # -- reading settings ----------------------------------------------------------
    def _toggle_reading(self, on: bool) -> None:
        self.reading.setVisible(on)
        self.reading_button.setText("Reading ▾" if on else "Reading ▸")

    def _load_reading(self) -> None:
        table = self._profile["table"]
        self._building = True
        try:
            self.delimiter.setCurrentIndex(max(0, self.delimiter.findData(table.get("delimiter"))))
            self.header_line.setValue(int(table.get("header_line") or 1))
            self.units_line.setValue(int(table.get("units_line") or 0))
            self.decimal_comma.setChecked(bool(table.get("decimal_comma")))
            self.missing.setText(", ".join(m for m in table.get("missing") or [] if m))
            self.encoding.setCurrentIndex(max(0, self.encoding.findData(table.get("encoding"))))
        finally:
            self._building = False

    def _reading_changed(self, *_args) -> None:
        if self._building:
            return
        table = self._profile["table"]
        table["delimiter"] = self.delimiter.currentData()
        table["header_line"] = self.header_line.value()
        table["units_line"] = self.units_line.value()
        table["decimal_comma"] = self.decimal_comma.isChecked()
        table["missing"] = [""] + [m.strip() for m in self.missing.text().split(",") if m.strip()]
        table["encoding"] = self.encoding.currentData()
        if self.sheet.isVisible() and self.sheet.currentText():
            table["sheet"] = self.sheet.currentText()
        self._read()
        self._rebuild()

    def _read(self) -> None:
        self._preview_error = ""
        try:
            self._raw = rd.read_table(self._content, self._name, self._profile["table"], max_rows=PREVIEW_ROWS)
        except (rd.ReadError, UnicodeDecodeError, ValueError) as exc:
            self._raw = None
            self._preview_error = f"{self._name} could not be read with these settings: {exc}"
        raw = self._raw
        self._building = True
        try:
            self.sheet.clear()
            if raw is not None and raw.kind == "xlsx":
                self.sheet.addItems(raw.sheets)
                self.sheet.setCurrentText(raw.sheet or "")
            self.sheet.setVisible(raw is not None and raw.kind == "xlsx")
        finally:
            self._building = False
        if raw is None:
            self.info.setText(self._preview_error)
            return
        if raw.kind == "xlsx":
            how = f"worksheet '{raw.sheet}'"
        else:
            how = dict((v, t) for t, v in DELIMITERS).get(raw.delimiter, raw.delimiter).lower() + "-separated"
        table = self._profile["table"]
        units = f", units line {table['units_line']}" if table.get("units_line") else ""
        self.info.setText(f"<b>{self._name}</b> · {how} · header line {table['header_line']}{units}"
                          f"{' · decimal comma' if table.get('decimal_comma') else ''}")

    # -- roles -----------------------------------------------------------------------
    def entry(self, header: str) -> Optional[Dict]:
        return next((c for c in self._profile["columns"] if c["header"] == header), None)

    def set_role(self, header: str, role_key: Optional[str], **options) -> None:
        """Give ``header`` a role (None: unmap it); a single-use role moves from its old column."""
        columns = [c for c in self._profile["columns"] if c["header"] != header]
        if role_key is not None:
            role = R.role(role_key)
            if not role.multiple:
                columns = [c for c in columns if c["role"] != role_key]
            if role_key == R.TIME:
                columns = [c for c in columns if c["role"] not in (R.DATE, R.CLOCK, R.DAY)]
            elif role_key in (R.DATE, R.CLOCK, R.DAY):
                columns = [c for c in columns if c["role"] != R.TIME]
            entry = P.column(header, role_key, **options)
            order = {h: i for i, h in enumerate(self.headers())}
            columns.append(entry)
            columns.sort(key=lambda c: order.get(c["header"], 10 ** 6))
        self._profile["columns"] = columns
        self._suggested.discard(header)
        self._rebuild_chips_and_preview()

    def update_entry(self, header: str, **changes) -> None:
        entry = self.entry(header)
        if entry is None:
            return
        for key, value in changes.items():
            if value in (None, ""):
                entry.pop(key, None)
            else:
                entry[key] = value
        self._suggested.discard(header)
        self._rebuild_chips_and_preview()

    def suggest(self) -> None:
        if self._raw is None:
            return
        self._profile["columns"] = P.suggest(self._raw.headers, self._raw.units)
        self._suggested = {c["header"] for c in self._profile["columns"]}
        self.profile_note.setText("Suggested from the column names: check the tinted roles and units."
                                  if self._suggested else "No column names were recognised: choose the roles.")
        self._rebuild()

    def clear(self) -> None:
        self._profile["columns"] = []
        self._suggested = set()
        self.profile_note.setText("")
        self._rebuild()

    def _apply_saved(self, index: int) -> None:
        choice = self.saved_combo.itemData(index)
        if choice is None:
            return
        label, profile = self._saved[int(choice)]
        self.set_file(self._name, self._content, profile, note=f"Mapping '{label}' applied.")

    def _keep_extra_toggled(self, on: bool) -> None:
        self._profile["keep_extra"] = bool(on)
        self._rebuild_chips_and_preview()

    # -- the table ------------------------------------------------------------------
    def _rebuild(self) -> None:
        raw = self._raw
        self._building = True
        try:
            self.keep_extra.setChecked(bool(self._profile.get("keep_extra", True)))
            self.table.clear()
            if raw is None:
                self.table.setRowCount(0)
                self.table.setColumnCount(0)
                return
            headers = raw.headers
            self.table.setColumnCount(len(headers))
            self.table.setRowCount(FIXED_ROWS + len(raw.rows))
            self.table.setHorizontalHeaderLabels(headers)
            self.table.setVerticalHeaderLabels(["Role", "Units"] + [str(n) for n in raw.lines])
            for col, header in enumerate(headers):
                role_chip = _chip("The column's role. Click to choose.")
                role_chip.clicked.connect(lambda _c=False, h=header, b=role_chip: self._open_role_menu(h, b))
                units_chip = _chip("Units, sign, format or CRS for this role. Click to choose.")
                units_chip.clicked.connect(lambda _c=False, h=header, b=units_chip: self._open_units_menu(h, b))
                self.table.setCellWidget(CHIP_ROLE, col, role_chip)
                self.table.setCellWidget(CHIP_UNITS, col, units_chip)
            for r, cells in enumerate(raw.rows):
                for col in range(len(headers)):
                    item = QTableWidgetItem(cells[col] if col < len(cells) else "")
                    item.setFlags(_ITEM_ENABLED | _ITEM_SELECTABLE)
                    self.table.setItem(FIXED_ROWS + r, col, item)
        finally:
            self._building = False
        self._rebuild_chips_and_preview()
        self.table.resizeColumnsToContents()
        for col in range(self.table.columnCount()):
            self.table.setColumnWidth(col, min(max(self.table.columnWidth(col), 90), 220))

    def _rebuild_chips_and_preview(self) -> None:
        self._preview()

    def _auto_extra(self) -> Set[str]:
        if self._parsed is None:
            return set()
        return {c.header for c in self._parsed.channels if c.auto}

    def _refresh_chips(self) -> None:
        if self._raw is None:
            return
        auto = self._auto_extra()
        for col, header in enumerate(self._raw.headers):
            role_chip = self.table.cellWidget(CHIP_ROLE, col)
            units_chip = self.table.cellWidget(CHIP_UNITS, col)
            if role_chip is None or units_chip is None:
                continue
            entry = self.entry(header)
            if entry is None:
                role_chip.setText("Extra (auto)" if header in auto else "—")
                role_chip.setStyleSheet(_PLAIN_STYLE)
                role_chip.setToolTip("Not mapped: kept as an extra channel when it holds numbers, else not "
                                     "imported (the original file keeps it). Click to choose a role.")
                units_chip.setText(self._unit_of(header))
                units_chip.setStyleSheet(_PLAIN_STYLE)
                units_chip.setEnabled(False)
                continue
            role = R.role(entry["role"])
            text = role.label
            if role.key in (R.TENSION, R.EXTRA):
                text = f"{role.label} · {entry.get('name', header)}"
            role_chip.setText(text)
            if role.key == R.IGNORE:
                role_chip.setStyleSheet(_PLAIN_STYLE)
            elif header in self._suggested:
                role_chip.setStyleSheet(_SUGGESTED_STYLE)
                role_chip.setToolTip("Suggested from the column name: click to confirm or change.")
            else:
                role_chip.setStyleSheet(_MAPPED_STYLE)
                role_chip.setToolTip("The column's role. Click to choose.")
            units, missing = self._units_text(entry)
            units_chip.setText(units)
            units_chip.setEnabled(role.kind not in (R.KIND_IGNORE, R.KIND_TEXT))
            units_chip.setStyleSheet(_MISSING_STYLE if missing else _UNITS_STYLE)
        self._fit_chip_widths()

    def _fit_chip_widths(self) -> None:
        """Widen a column to show its chips in full (column widths ignore cell widgets)."""
        for col in range(self.table.columnCount()):
            need = max((w.sizeHint().width() + 6 for w in (self.table.cellWidget(CHIP_ROLE, col),
                                                           self.table.cellWidget(CHIP_UNITS, col)) if w), default=0)
            if need > self.table.columnWidth(col):
                self.table.setColumnWidth(col, min(need, 320))

    def _unit_of(self, header: str) -> str:
        raw = self._raw
        if raw is None or header not in raw.headers:
            return ""
        index = raw.headers.index(header)
        return raw.units[index].strip() if index < len(raw.units) else ""

    def _units_text(self, entry: Dict) -> Tuple[str, bool]:
        """``(chip text, something still needed)`` for a mapped column."""
        role = R.role(entry["role"])
        zone = self._profile.get("timezone") or "UTC"
        if role.kind == R.KIND_TIME:
            return f"{format_text(entry.get('format', 'auto'))} · {zone}", False
        if role.kind == R.KIND_DATE:
            return f"{format_text(entry.get('format', 'auto'))} · {zone}", False
        if role.kind == R.KIND_CLOCK:
            return f"HH:mm:ss · {zone}", False
        if role.kind == R.KIND_DAY:
            start = self._profile.get("start_date")
            return (f"day 1 = {start}", False) if start else ("day 1 = ?", True)
        if role.kind == R.KIND_COORD:
            return "degrees (decimal or d° m')", False
        if role.key in (R.EASTING, R.NORTHING):
            crs = self._profile.get("crs")
            return (crs, False) if crs else ("CRS?", True)
        if role.kind == R.KIND_TEXT:
            return "text", False
        if role.key == R.EXTRA:
            return entry.get("unit") or "(no unit)", False
        unit = role.unit(entry.get("unit") or role.default_unit)
        text = unit.label if unit else ""
        sign = entry.get("sign")
        if role.sign and sign:
            text += ", " + dict(R.sign_choices(role.key)).get(sign, sign)
        return text, False

    def _open_role_menu(self, header: str, button: QToolButton) -> None:
        menu = QMenu(button)
        current = self.entry(header)
        used = {c["role"]: c["header"] for c in self._profile["columns"] if c["header"] != header}
        for group in R.GROUPS:
            menu.addSection(group)
            for role in R.roles_in_group(group):
                text = role.label
                if not role.multiple and role.key in used:
                    text += f"   (now {used[role.key]})"
                action = menu.addAction(text)
                action.setCheckable(True)
                action.setChecked(current is not None and current["role"] == role.key)
                action.setToolTip(role.tip)
                action.triggered.connect(lambda _c=False, h=header, k=role.key: self.set_role(h, k))
        menu.addSeparator()
        none = menu.addAction("Not mapped (kept as an extra channel if numeric)")
        none.triggered.connect(lambda _c=False, h=header: self.set_role(h, None))
        menu.setToolTipsVisible(True)
        qt_exec(menu, button.mapToGlobal(button.rect().bottomLeft()))

    def _open_units_menu(self, header: str, button: QToolButton) -> None:
        entry = self.entry(header)
        if entry is None:
            return
        role = R.role(entry["role"])
        menu = QMenu(button)
        if role.units and role.key != R.EXTRA:
            menu.addSection("Units")
            for unit in role.units:
                action = menu.addAction(unit.label)
                action.setCheckable(True)
                action.setChecked((entry.get("unit") or role.default_unit) == unit.key)
                action.triggered.connect(lambda _c=False, h=header, u=unit.key: self.update_entry(h, unit=u))
        if role.sign:
            menu.addSection("Sign")
            for key, label in R.sign_choices(role.key):
                action = menu.addAction(label)
                action.setCheckable(True)
                action.setChecked(entry.get("sign") == key)
                action.triggered.connect(lambda _c=False, h=header, s=key: self.update_entry(h, sign=s))
        if role.kind in (R.KIND_TIME, R.KIND_DATE):
            menu.addSection("Format")
            choices = [("auto", "Automatic")]
            if role.kind == R.KIND_TIME:
                choices += [("iso", "ISO 8601"), ("epoch", "Seconds since 1970"), ("epoch_ms", "Milliseconds since 1970")]
                choices += [(p, strftime_to_qt(p)) for p in TIME_PATTERNS]
            else:
                choices += [(p, strftime_to_qt(p)) for p in DATE_PATTERNS]
            for key, label in choices:
                action = menu.addAction(label)
                action.setCheckable(True)
                action.setChecked(entry.get("format", "auto") == key)
                action.triggered.connect(lambda _c=False, h=header, f=key: self.update_entry(h, format=f))
            menu.addAction("Other pattern…", lambda h=header: self._custom_format(h))
        if role.kind in (R.KIND_TIME, R.KIND_DATE, R.KIND_CLOCK):
            zone_menu = menu.addMenu(f"Time zone: {self._profile.get('timezone') or 'UTC'}")
            for zone in OFFSETS:
                action = zone_menu.addAction(zone)
                action.setCheckable(True)
                action.setChecked((self._profile.get("timezone") or "UTC") == zone)
                action.triggered.connect(lambda _c=False, z=zone: self._set_profile_value("timezone", z))
            zone_menu.addAction("Other offset…", self._custom_offset)
        if role.kind == R.KIND_DAY:
            menu.addAction("Start date (day 1)…", self._start_date)
        if role.key in (R.EASTING, R.NORTHING):
            menu.addAction("Choose the CRS…", self._choose_crs)
        if role.key in (R.TENSION, R.EXTRA):
            menu.addSeparator()
            menu.addAction("Name…", lambda h=header: self._rename(h))
        if role.key == R.EXTRA:
            menu.addAction("Unit…", lambda h=header: self._extra_unit(h))
        if menu.isEmpty():
            return
        qt_exec(menu, button.mapToGlobal(button.rect().bottomLeft()))

    def _set_profile_value(self, key: str, value: str) -> None:
        self._profile[key] = value
        self._rebuild_chips_and_preview()

    def _custom_format(self, header: str) -> None:
        current = self.entry(header) or {}
        text, ok = QInputDialog.getText(self, "Time format",
                                        "Pattern, e.g. dd/MM/yyyy HH:mm:ss (dd day, MM month, yyyy year, "
                                        "DDD day of year, HH hour, mm minute, ss second, zzz fraction):",
                                        text=strftime_to_qt(current.get("format", "")) if "%" in current.get(
                                            "format", "") else "")
        if ok and text.strip():
            self.update_entry(header, format=qt_to_strftime(text.strip()))

    def _custom_offset(self) -> None:
        text, ok = QInputDialog.getText(self, "Time zone", "UTC offset of the times in the file, e.g. +05:45 or "
                                        "-03:30:", text=self._profile.get("timezone") or "UTC")
        if not ok:
            return
        if P.parse_utc_offset(text) is None:
            self.problem_label.setText(f"'{text}' is not UTC or an offset such as +01:00.")
            return
        self._set_profile_value("timezone", text.strip())

    def _start_date(self) -> None:
        text, ok = QInputDialog.getText(self, "Day 1", "The date of day 1 (YYYY-MM-DD):",
                                        text=self._profile.get("start_date") or "")
        if ok:
            self._set_profile_value("start_date", text.strip())

    def _choose_crs(self) -> None:
        from qgis.core import QgsCoordinateReferenceSystem
        from qgis.gui import QgsProjectionSelectionDialog
        dialog = QgsProjectionSelectionDialog(self)
        current = QgsCoordinateReferenceSystem(self._profile.get("crs") or "")
        if current.isValid():
            dialog.setCrs(current)
        if qt_exec(dialog) == DIALOG_ACCEPTED:
            crs = dialog.crs()
            if crs.isValid():
                self._set_profile_value("crs", crs.authid() or crs.toWkt())

    def _rename(self, header: str) -> None:
        entry = self.entry(header) or {}
        text, ok = QInputDialog.getText(self, "Channel name", "Name:", text=entry.get("name", header))
        if ok and text.strip():
            self.update_entry(header, name=text.strip())

    def _extra_unit(self, header: str) -> None:
        entry = self.entry(header) or {}
        text, ok = QInputDialog.getText(self, "Unit", "Unit of this channel (as you want it labelled):",
                                        text=entry.get("unit", "") or self._unit_of(header))
        if ok:
            self.update_entry(header, unit=text.strip())

    # -- preview -------------------------------------------------------------------
    def _preview(self) -> None:
        raw = self._raw
        self._parsed = None
        problems = self.problems()
        # A missing CRS does not change how values read: preview with a stand-in so the cells show.
        blocking = [p for p in problems if not p.startswith("Choose the CRS")]
        if raw is not None and not blocking:
            preview = copy.deepcopy(self._profile)
            preview["crs"] = preview.get("crs") or "EPSG:4326"
            try:
                self._parsed = rd.parse(raw, preview)
            except rd.ReadError as exc:
                problems = [str(exc)]
        self._refresh_chips()
        self._colour_cells()
        self._update_lines(problems)
        self.changed.emit()

    def _colour_cells(self) -> None:
        raw = self._raw
        if raw is None:
            return
        bad = QBrush(QColor(255, 205, 205))
        mapped = QBrush(QColor(240, 246, 255))
        plain = QBrush()
        headers = raw.headers
        mapped_cols = {i for i, h in enumerate(headers)
                       if (self.entry(h) or {}).get("role") not in (None, R.IGNORE)}
        reasons: Dict[int, str] = {}
        if self._parsed is not None:
            reasons = {line: reason for line, reason, _text in self._parsed.rejected}
        for r, line in enumerate(raw.lines):
            reason = reasons.get(line)
            column = None
            if reason:
                prefix = reason.split(":", 1)[0]
                column = headers.index(prefix) if prefix in headers else None
            for col in range(len(headers)):
                item = self.table.item(FIXED_ROWS + r, col)
                if item is None:
                    continue
                whole_row = reason is not None and column is None
                if (reason and col == column) or whole_row:
                    item.setBackground(bad)
                    item.setToolTip(reason)
                else:
                    item.setBackground(mapped if col in mapped_cols else plain)
                    item.setToolTip("")

    def _update_lines(self, problems: List[str]) -> None:
        status = P.status(self._profile)
        ok, missing = color("ok"), color("hint")

        def mark(on: bool, text: str) -> str:
            return f"<span style='color:{ok if on else missing}'>{'✓' if on else '○'} {text}</span>"

        parts = [mark(status["kp"], "KP"), mark(status["dol"], "DOL")]
        time_text = "Time (for overlaps and the time axis)"
        if status["time"] and self._parsed is not None and self._parsed.time_range[0] is not None:
            lo, hi = self._parsed.time_range
            from .assessment import utc_text
            time_text = f"Time → {utc_text(lo)} … {utc_text(hi)} UTC"
        parts.append(mark(status["time"], time_text))
        parts.append(mark(status["position"], "Position (to check KPs)"))
        if self._parsed is not None and self._parsed.kp_range[0] is not None:
            lo, hi = self._parsed.kp_range
            parts.append(f"KP {lo:.3f} … {hi:.3f} (preview)")
        self.needed.setText("&nbsp;&nbsp;&nbsp;".join(parts))
        if self._parsed is not None:
            rejected = len(self._parsed.rejected)
            self.rows_label.setText(
                f"Preview of the first {self._parsed.rows_read} rows: {self._parsed.n} read, {rejected} rejected"
                + (" (hover the red cells for the reason)" if rejected else "")
                + ("" if not self._parsed.warnings else " · " + " ".join(self._parsed.warnings)))
            self.rows_label.setStyleSheet(status_style("warn") if rejected or self._parsed.warnings else "")
        else:
            self.rows_label.setText("")
        self.problem_label.setText("  ".join(problems))
        self.problem_label.setVisible(bool(problems))

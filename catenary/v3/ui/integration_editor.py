# -*- coding: utf-8 -*-
"""BU integration editor: the whole Y described outward from the BU.

One widget, three tabs (trunk / leg 1 / leg 2). Each tab is the branch's
straight-line diagram as a table read top-to-bottom AWAY from the BU: the
BU tail first, then the tail joint, then the cable beyond, ending in one
"rest of line" row that absorbs whatever length the geometry demands. A
read-only "From BU (m)" column shows where every row sits, live, so placing
a joint is "type its distance from the BU and see it land there".

The editor produces / consumes the :mod:`engine.bu_integration` dict format
(:class:`BranchMakeup` per branch). The BU body's weight/CdA stay in the
dialog's existing spins and are merged in at config-build time.

Kept deliberately lean: per-row fields are the ones that drive the quick
tri-catenary model (length, submerged weight, friction, point load) plus
diameter for the full-solver confirm. Anything else (Cd, EI, colours...)
falls back to the scenario defaults.
"""

from __future__ import annotations

import json
from typing import List, Optional, Tuple

try:
    from qgis.PyQt.QtCore import Qt, pyqtSignal
    from qgis.PyQt.QtWidgets import (
        QComboBox, QDoubleSpinBox, QHBoxLayout, QLabel, QPushButton,
        QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
    )
except Exception:  # pragma: no cover - standalone testing
    from PyQt5.QtCore import Qt, pyqtSignal
    from PyQt5.QtWidgets import (
        QComboBox, QDoubleSpinBox, QHBoxLayout, QLabel, QPushButton,
        QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
    )

from ..engine import bu_integration as bi
from ..engine import cable_system as cs

_ITEM_FLAGS = getattr(Qt, "ItemFlag", Qt)

COL_KIND, COL_NAME, COL_LEN, COL_FROM_BU, COL_QW, COL_LOAD, COL_DIA, COL_MU = range(8)
HEADERS = ["Kind", "Name", "Length\n(m)", "From BU\n(m)", "Wt water\n(N/m)",
           "Load\n(kN)", "Dia\n(m)", "Friction\nmu"]

KIND_CABLE = "cable"
KIND_JOINT = "joint / body"
KIND_REST = "rest of line"

_BRANCH_TITLES = (("trunk", "Trunk"), ("leg1", "Leg 1"), ("leg2", "Leg 2"))


def _num(item: Optional[QTableWidgetItem]) -> Optional[float]:
    if item is None:
        return None
    t = item.text().strip()
    if not t:
        return None
    try:
        return float(t)
    except ValueError:
        return None


class _BranchPage(QWidget):
    """One branch tab: the outward-from-BU table plus count referencing."""

    changed = pyqtSignal()

    def __init__(self, key: str, parent=None):
        super().__init__(parent)
        self.key = key
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 4, 0, 0)

        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(110)
        self.table.setMaximumHeight(170)
        try:
            self.table.horizontalHeaderItem(COL_FROM_BU).setToolTip(
                "Computed live: where this row sits, measured along the "
                "cable outward from the BU.")
            self.table.horizontalHeaderItem(COL_LEN).setToolTip(
                "Cable rows only. The 'rest of line' row has no entered "
                "length — it takes whatever the geometry demands.")
        except Exception:
            pass
        self.table.cellChanged.connect(self._on_cell_changed)
        v.addWidget(self.table)

        btns = QHBoxLayout()
        for text, cb in (("Add cable", self._add_cable),
                         ("Add joint/body", self._add_joint),
                         ("Remove", self._remove_row),
                         ("Up", lambda: self._move(-1)),
                         ("Down", lambda: self._move(1))):
            b = QPushButton(text)
            b.clicked.connect(cb)
            btns.addWidget(b)
        btns.addStretch(1)
        v.addLayout(btns)

        crow = QHBoxLayout()
        crow.addWidget(QLabel("Count at BU"))
        self.count_spin = QDoubleSpinBox()
        self.count_spin.setRange(-1.0, 1e8)
        self.count_spin.setDecimals(0)
        self.count_spin.setSingleStep(100.0)
        self.count_spin.setValue(-1.0)
        self.count_spin.setSpecialValueText("off")
        self.count_spin.setSuffix(" m")
        self.count_spin.setToolTip(
            "Cable count (marking) at the BU end of this line, from the "
            "jointing records. Counts anywhere on the line — including the "
            "laid end — are derived from it. 'off' disables counts.")
        self.count_spin.valueChanged.connect(self.changed)
        crow.addWidget(self.count_spin)
        self.count_dir = QComboBox()
        self.count_dir.addItem("counts increase away from the BU", True)
        self.count_dir.addItem("counts increase toward the BU", False)
        self.count_dir.currentIndexChanged.connect(self.changed)
        crow.addWidget(self.count_dir)
        crow.addStretch(1)
        v.addLayout(crow)

        self._loading = False

    # ---- row helpers -------------------------------------------------------

    def _rest_row(self) -> int:
        for r in range(self.table.rowCount()):
            it = self.table.item(r, COL_KIND)
            if it is not None and it.text() == KIND_REST:
                return r
        return -1

    def _insert_row(self, kind: str, name: str = "", length: str = "",
                    qw: str = "", load: str = "", dia: str = "", mu: str = "",
                    row: Optional[int] = None):
        """Insert before the rest-of-line row (which always stays last)."""
        if row is None:
            rest = self._rest_row()
            row = rest if (rest >= 0 and kind != KIND_REST) else self.table.rowCount()
        self._loading = True
        try:
            self.table.insertRow(row)
            for col, text in ((COL_KIND, kind), (COL_NAME, name),
                              (COL_LEN, length), (COL_FROM_BU, ""),
                              (COL_QW, qw), (COL_LOAD, load),
                              (COL_DIA, dia), (COL_MU, mu)):
                item = QTableWidgetItem(str(text))
                if col == COL_KIND or col == COL_FROM_BU:
                    item.setFlags(item.flags() & ~_ITEM_FLAGS.ItemIsEditable)
                if (kind == KIND_JOINT and col in (COL_LEN, COL_QW, COL_DIA, COL_MU)) or \
                   (kind != KIND_JOINT and col == COL_LOAD) or \
                   (kind == KIND_REST and col == COL_LEN):
                    item.setFlags(item.flags() & ~_ITEM_FLAGS.ItemIsEditable)
                    item.setText("" if col != COL_LEN or kind != KIND_REST else "(auto)")
                self.table.setItem(row, col, item)
        finally:
            self._loading = False

    def _add_cable(self):
        self._insert_row(KIND_CABLE, name="Cable", length="100")
        self._after_edit()

    def _add_joint(self):
        self._insert_row(KIND_JOINT, name="Joint", load="1.0")
        self._after_edit()

    def _remove_row(self):
        r = self.table.currentRow()
        if r < 0:
            r = self.table.rowCount() - 1
        if r < 0:
            return
        it = self.table.item(r, COL_KIND)
        if it is not None and it.text() == KIND_REST:
            return          # the rest-of-line row is structural; keep it
        self.table.removeRow(r)
        self._after_edit()

    def _move(self, delta: int):
        r = self.table.currentRow()
        n = self.table.rowCount()
        rest = self._rest_row()
        top = n - 1 if rest < 0 else rest       # rows may not pass the rest row
        r2 = r + delta
        if r < 0 or r2 < 0 or r2 >= max(top, 1) or r >= top:
            return
        self._loading = True
        try:
            for c in range(self.table.columnCount()):
                a = self.table.takeItem(r, c)
                b = self.table.takeItem(r2, c)
                self.table.setItem(r, c, b)
                self.table.setItem(r2, c, a)
        finally:
            self._loading = False
        self.table.setCurrentCell(r2, self.table.currentColumn())
        self._after_edit()

    def _on_cell_changed(self, *_a):
        if not self._loading:
            self._after_edit()

    def _after_edit(self):
        self.refresh_positions()
        self.changed.emit()

    # ---- the live From BU column ------------------------------------------

    def refresh_positions(self):
        self._loading = True
        try:
            s = 0.0
            for r in range(self.table.rowCount()):
                kind_it = self.table.item(r, COL_KIND)
                pos_it = self.table.item(r, COL_FROM_BU)
                if kind_it is None or pos_it is None:
                    continue
                kind = kind_it.text()
                if kind == KIND_JOINT:
                    pos_it.setText(f"at {s:.0f}")
                elif kind == KIND_REST:
                    pos_it.setText(f"{s:.0f} → end")
                else:
                    L = _num(self.table.item(r, COL_LEN)) or 0.0
                    pos_it.setText(f"{s:.0f} – {s + L:.0f}")
                    s += max(0.0, L)
        finally:
            self._loading = False

    # ---- (de)serialisation -------------------------------------------------

    def to_makeup_dict(self) -> dict:
        items: List[dict] = []
        for r in range(self.table.rowCount()):
            kind = (self.table.item(r, COL_KIND) or QTableWidgetItem("")).text()
            name = (self.table.item(r, COL_NAME) or QTableWidgetItem("")).text().strip()
            if kind == KIND_JOINT:
                items.append({
                    "type": "body", "name": name or "Joint",
                    "point_load_kN": _num(self.table.item(r, COL_LOAD)) or 0.0,
                })
                continue
            d = {
                "type": "segment",
                "name": name or ("Rest of line" if kind == KIND_REST else "Cable"),
                "length_m": (0.0 if kind == KIND_REST
                             else _num(self.table.item(r, COL_LEN)) or 0.0),
                "q_water_npm": _num(self.table.item(r, COL_QW)) or 0.0,
            }
            dia = _num(self.table.item(r, COL_DIA))
            if dia:
                d["diameter_m"] = dia
            mu = _num(self.table.item(r, COL_MU))
            if mu is not None:
                d["friction_mu"] = mu
            if kind == KIND_REST:
                d["fill"] = True
            items.append(d)
        count = float(self.count_spin.value())
        return {
            "items": items,
            "joints": [],
            "count_at_bu_m": (None if count < 0.0 else count),
            "count_increases_from_bu": bool(self.count_dir.currentData()),
        }

    def set_from_makeup_dict(self, d: dict):
        self._loading = True
        try:
            self.table.setRowCount(0)
        finally:
            self._loading = False
        items = cs.parse_assembly(d.get("items") or [])
        for it in items:
            if isinstance(it, cs.BodySpec):
                self._insert_row(KIND_JOINT, name=it.name,
                                 load=f"{it.point_load_kN:g}")
            elif getattr(it, "fill", False):
                self._insert_row(
                    KIND_REST, name=it.name,
                    qw=(f"{it.q_water_npm:g}" if it.q_water_npm else ""),
                    dia=(f"{it.diameter_m:g}" if it.diameter_m else ""),
                    mu=("" if it.friction_mu is None else f"{it.friction_mu:g}"))
            else:
                self._insert_row(
                    KIND_CABLE, name=it.name, length=f"{it.length_m:g}",
                    qw=(f"{it.q_water_npm:g}" if it.q_water_npm else ""),
                    dia=(f"{it.diameter_m:g}" if it.diameter_m else ""),
                    mu=("" if it.friction_mu is None else f"{it.friction_mu:g}"))
        if self._rest_row() < 0:
            self._insert_row(KIND_REST, name="Rest of line")
        count = d.get("count_at_bu_m")
        self.count_spin.blockSignals(True)
        self.count_spin.setValue(-1.0 if count is None else float(count))
        self.count_spin.blockSignals(False)
        self.count_dir.blockSignals(True)
        self.count_dir.setCurrentIndex(
            0 if d.get("count_increases_from_bu", True) else 1)
        self.count_dir.blockSignals(False)
        self.refresh_positions()


class BUIntegrationEditor(QWidget):
    """The three-branch integration editor (see module docstring)."""

    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        note = QLabel(
            "Each line is read OUTWARD from the BU: the BU tail first, then "
            "the tail joint, then the cable beyond. The last row is always "
            "'rest of line' — it absorbs whatever length the geometry "
            "demands, so every other row keeps its distance from the BU. "
            "Joints entered here are tracked in the 3D view and exports.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#777; font-size: small;")
        v.addWidget(note)
        self.tabs = QTabWidget()
        self.pages = {}
        for key, title in _BRANCH_TITLES:
            page = _BranchPage(key)
            page.changed.connect(self._on_changed)
            self.tabs.addTab(page, title)
            self.pages[key] = page
        v.addWidget(self.tabs)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#b64; font-size: small;")
        v.addWidget(self.status)
        self.set_from_dict({})           # nominal defaults

    def _on_changed(self):
        self._refresh_status()
        self.changed.emit()

    def _refresh_status(self):
        probs = self.problems()
        self.status.setText("\n".join(probs[:4]))
        self.status.setVisible(bool(probs))

    # ---- public API --------------------------------------------------------

    def integration(self, *, bu_weight_kN: float = 15.0,
                    bu_cda_m2: float = 1.0) -> bi.BUIntegration:
        return bi.BUIntegration.from_dict(
            self.to_dict(bu_weight_kN=bu_weight_kN, bu_cda_m2=bu_cda_m2))

    def to_dict(self, *, bu_weight_kN: float = 15.0,
                bu_cda_m2: float = 1.0) -> dict:
        return {
            "bu_weight_kN": float(bu_weight_kN),
            "bu_cda_m2": float(bu_cda_m2),
            "trunk": self.pages["trunk"].to_makeup_dict(),
            "leg1": self.pages["leg1"].to_makeup_dict(),
            "leg2": self.pages["leg2"].to_makeup_dict(),
        }

    def set_from_dict(self, d: dict):
        """Load from a BUIntegration dict; blanks get the nominal default
        branch (90 m / 300 N/m tail, 1 kN tail joint, rest of line)."""
        for key, _title in _BRANCH_TITLES:
            branch = (d or {}).get(key)
            if not branch or not branch.get("items"):
                branch = bi.default_branch(
                    joint_name=f"{key} tail joint").to_dict()
            self.pages[key].set_from_makeup_dict(branch)
        self._refresh_status()

    def problems(self) -> List[str]:
        return self.integration().problems()

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    def set_from_json(self, raw: str):
        try:
            self.set_from_dict(json.loads(raw) if raw else {})
        except Exception:
            self.set_from_dict({})

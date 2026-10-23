# -*- coding: utf-8 -*-
"""Held sections from the burial on the route: the RPL's events or the Burial Planner plan.

A plough buries the cable as it is laid (PLDN to PLUP), so over a plough
section the cable is held: the lay control's *Held* mode. These helpers
find the burial sections:

* :func:`rpl_burial_ranges` reads the RPL's boundary events (PLDN / PLUP,
  Start / End PLB, Start / End burial … in many phrasings) with the Burial
  Planner's RPL import (:mod:`..burial.rpl_plan_import`), walked in the lay
  direction; an RPL without such events may give its plan per segment in the
  ProtectionMethod column;
* :func:`plan_burial_ranges` takes the burial sections of a Burial Planner
  plan (the caller checks the plan shares the analysis's KP basis).

Post-lay burial (PLB, MFE) buries the cable after it is laid: during lay it
is not held, so only ploughs (and burial with no tool named) are held by
default. :class:`HoldsDialog` chooses the source, the tools and the tension.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ..qgis_compat import BUTTON_BOX_CANCEL, BUTTON_BOX_OK

PLOUGH, BURIAL, TRENCHER, MFE = "plough", "burial", "trencher", "mfe"
# (key, label, held by default, tip)
TOOLS = (
    (PLOUGH, "Plough", True, "Simultaneous lay and burial: the cable is buried as it is laid (PLDN to PLUP)."),
    (BURIAL, "Burial, tool not stated", True, "Start / End burial with no tool named (often a plough)."),
    (TRENCHER, "PLB / trencher", False, "Post-lay burial: the cable lies on the seabed as laid and is buried "
                                        "later, so it is not held during lay."),
    (MFE, "MFE", False, "Mass flow excavation, after lay: not held during lay."),
)
TOOL_LABELS = {key: label for key, label, _on, _tip in TOOLS}
TOOL_NAMES = {PLOUGH: "Plough", BURIAL: "Burial", TRENCHER: "PLB", MFE: "MFE"}


@dataclass
class BurialRange:
    kp_start: float
    kp_end: float
    tool: str                          # PLOUGH, BURIAL, TRENCHER or MFE
    note: str = ""                     # where it came from (e.g. "PLDN Pos 12 to PLUP Pos 30")


def _tool_of_rpl(method: str) -> str:
    from ..burial import rpl_plan_import as rpi

    return {rpi.M_PLOUGH: PLOUGH, rpi.M_TRENCHER: TRENCHER, rpi.M_MFE: MFE, rpi.M_BURIAL: BURIAL}.get(method, "")


def rpl_burial_ranges(model, measured_kps: Sequence[float], direction: int = 1
                      ) -> Tuple[List[BurialRange], List[str]]:
    """Burial sections of an RPL at the analysis's measured KPs, and notes on what could not be read."""
    from ..burial import rpl_plan_import as rpi

    rows = []
    for i, point in enumerate(model.points):
        attrs = getattr(point, "attrs", None) or {}
        kp = measured_kps[i] if i < len(measured_kps) else None
        segment = model.segments[i] if i < len(model.segments) else None
        protection = (getattr(segment, "attrs", None) or {}).get("ProtectionMethod") if segment is not None else ""
        rows.append(rpi.RplRow(seq=i, pos_no=point.pos_no, event=str(point.event or "").strip(),
                               remarks=str(attrs.get("Remarks") or "").strip(),
                               kp=float(kp) if kp is not None and math.isfinite(kp) else None,
                               protection=str(protection or "").strip()))
    notes: List[str] = []
    auto = rpi.auto_tokens(rows)
    if not auto and rpi.has_burial_protection(rows):
        auto = rpi.protection_tokens(rows)
        notes.append("No burial events in the RPL: sections read from its ProtectionMethod column.")
    if not auto:
        return [], ["The RPL has no burial events (PLDN / PLUP, Start / End PLB or burial) and no burial in its "
                    "ProtectionMethod column."]
    swap = rpi.auto_swap(rows, auto, direction)
    if swap:
        notes.append("The RPL's burial events read the other way round for this lay direction: starts and ends "
                     "swapped.")
    walk = rpi.walk(rows, rpi.effective_tokens(auto, {}, swap), direction)
    out = []
    for span in walk.spans:
        tool = _tool_of_rpl(span.method)
        if not tool:
            continue      # a skip
        first, last = rows[span.first], rows[span.last]
        out.append(BurialRange(span.lo, span.hi, tool, f"{first.name} to {last.name}"))
    warnings = walk.count()
    if warnings:
        notes.append(f"{warnings} burial event(s) could not be paired cleanly (e.g. a PLUP with no PLDN): check "
                     "the sections, or the RPL in the Burial Planner's Import plan.")
    return sorted(out, key=lambda r: r.kp_start), notes


def plan_burial_ranges(plan: Dict, sections: Sequence[Dict]) -> List[BurialRange]:
    """Burial sections of a Burial Planner plan (sections of kind burial), by their tool."""
    from ..burial import schema as bs

    default = bs.normalise_method(str(plan.get("method") or ""))
    out = []
    for s in sections:
        if str(s.get("kind") or "") != bs.SECTION_BURIAL or s.get("start_kp") is None or s.get("end_kp") is None:
            continue
        method = bs.normalise_method(str(s.get("method") or "")) or default
        tool = {bs.METHOD_PLOUGH: PLOUGH, bs.METHOD_TRENCHER: TRENCHER, bs.METHOD_ROV_JET: TRENCHER,
                bs.METHOD_MFE: MFE}.get(method, "")
        if not tool:
            continue      # inspection, or a tool this tool does not know
        lo, hi = sorted((float(s["start_kp"]), float(s["end_kp"])))
        out.append(BurialRange(lo, hi, tool, f"plan '{plan.get('name') or 'Plan'}'"))
    return sorted(out, key=lambda r: r.kp_start)


def hold_rows(ranges: Sequence[BurialRange], tools: Sequence[str], tension_kn: float,
              kp_range: Optional[Tuple[float, float]] = None) -> List[Dict]:
    """Lay control rows holding ``ranges`` of the chosen ``tools`` (within ``kp_range``) at ``tension_kn``."""
    out = []
    count: Dict[str, int] = {}
    for r in ranges:
        if r.tool not in tools:
            continue
        if kp_range is not None and (r.kp_end < kp_range[0] or r.kp_start > kp_range[1]):
            continue
        count[r.tool] = count.get(r.tool, 0) + 1
        out.append({"kp0": round(r.kp_start, 6), "kp1": round(r.kp_end, 6), "value": float(tension_kn),
                    "name": f"{TOOL_NAMES[r.tool]} {count[r.tool]}"})
    return out


class HoldsDialog(QDialog):
    """Choose where the held sections come from, which tools hold the cable, and the tension."""

    def __init__(self, parent=None, sources: Sequence[Tuple[str, str, List[BurialRange], List[str]]] = (),
                 unavailable: Sequence[str] = (), tension_kn: float = 5.0,
                 kp_range: Optional[Tuple[float, float]] = None):
        """``sources``: ``(key, label, ranges, notes)``; ``unavailable``: why a source is not offered."""
        super().__init__(parent)
        self.setWindowTitle("Hold plough sections")
        self.sources = list(sources)
        self.kp_range = kp_range
        layout = QVBoxLayout(self)
        intro = QLabel("Hold the cable over the burial sections of the route: a plough buries it as it is laid "
                       "(PLDN to PLUP). Each section is laid at the tension below, which pulls back on the cable "
                       "laid before it; then it neither spans nor slides.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        self.source = QComboBox()
        for key, label, _ranges, _notes in self.sources:
            self.source.addItem(label, key)
        form.addRow("From", self.source)
        tools = QWidget()
        tools_layout = QHBoxLayout(tools)
        tools_layout.setContentsMargins(0, 0, 0, 0)
        self.tools: Dict[str, QCheckBox] = {}
        for key, label, on, tip in TOOLS:
            box = QCheckBox(label)
            box.setChecked(on)
            box.setToolTip(tip)
            box.toggled.connect(self._update)
            tools_layout.addWidget(box)
            self.tools[key] = box
        tools_layout.addStretch(1)
        form.addRow("Hold", tools)
        self.tension = QDoubleSpinBox()
        self.tension.setRange(0.0, 1000.0)
        self.tension.setDecimals(2)
        self.tension.setSingleStep(0.5)
        self.tension.setSuffix(" kN")
        self.tension.setValue(float(tension_kn))
        self.tension.setToolTip("The tension each section is laid and held at: the cable tension at the plough "
                                "as it goes down (PLDN). Edit each row afterwards for different values.")
        form.addRow("Held at", self.tension)
        self.replace = QCheckBox("Replace the held sections already in the table")
        self.replace.setChecked(True)
        form.addRow(self.replace)
        layout.addLayout(form)
        self.found = QLabel("")
        self.found.setWordWrap(True)
        layout.addWidget(self.found)
        if unavailable:
            missing = QLabel(" ".join(unavailable))
            missing.setWordWrap(True)
            missing.setStyleSheet("color: #555;")
            layout.addWidget(missing)
        self.buttons = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.source.currentIndexChanged.connect(self._update)
        self.resize(560, self.sizeHint().height())
        self._update()

    def _current(self) -> Tuple[List[BurialRange], List[str]]:
        index = self.source.currentIndex()
        if not (0 <= index < len(self.sources)):
            return [], []
        _key, _label, ranges, notes = self.sources[index]
        return ranges, notes

    def chosen_tools(self) -> List[str]:
        return [key for key, box in self.tools.items() if box.isChecked()]

    def rows(self) -> List[Dict]:
        ranges, _notes = self._current()
        return hold_rows(ranges, self.chosen_tools(), self.tension.value(), self.kp_range)

    def _update(self, *_args) -> None:
        ranges, notes = self._current()
        rows = self.rows()
        if not self.sources:
            text = "No source of burial sections: see below."
        elif not rows:
            other = sorted({TOOL_LABELS[r.tool] for r in ranges if r.tool not in self.chosen_tools()})
            text = "No section of the tools ticked" + (" in the analysed KPs" if self.kp_range else "") + "."
            if other:
                text += f" Found: {', '.join(other)} (not held during lay unless ticked)."
        else:
            shown = ", ".join(f"{row['name']} KP {row['kp0']:.3f}–{row['kp1']:.3f}" for row in rows[:6])
            text = f"{len(rows)} section(s) to hold: {shown}" + (" …" if len(rows) > 6 else "") + "."
        self.found.setText(" ".join([text] + list(notes)))
        ok = self.buttons.button(BUTTON_BOX_OK)
        if ok is not None:
            ok.setEnabled(bool(rows))

# -*- coding: utf-8 -*-
"""Shared form rows for a KP-range table input.

Used by the Exclusions "KP range table" criterion and the Risk Profile
"KP-range table" check: start/end KP fields, the KP unit, and the RPL the
table's KPs are quoted against (stored with the config so the ranges are
translated onto the plan's route, and stay tied to that RPL when the plan
moves to another revision).
"""
from __future__ import annotations

from typing import Dict, List

from qgis.PyQt.QtWidgets import QComboBox, QFormLayout, QLabel

from .. import kp_table, ui_helpers
from .attribute_widgets import FieldCombo, layer_field_names


class KpTableForm:
    """Adds the KP-table rows to ``form``; ``model`` enables the RPL picker."""

    def __init__(self, form: QFormLayout, config: Dict, model=None):
        self.config = config
        self.model = model
        start, end = kp_table.fields(config)
        self.start_field = FieldCombo(start, "field holding the start KP")
        self.end_field = FieldCombo(end, "field holding the end KP")
        form.addRow("Start KP field:", self.start_field)
        form.addRow("End KP field:", self.end_field)
        self.unit_combo = QComboBox()
        self.unit_combo.addItem("km", kp_table.KP_UNIT_KM)
        self.unit_combo.addItem("m", kp_table.KP_UNIT_M)
        self.unit_combo.setCurrentIndex(
            max(0, self.unit_combo.findData(kp_table.unit(config))))
        self.unit_combo.setToolTip("Unit of the KP values in the table.")
        form.addRow("KP unit:", self.unit_combo)
        self.picker = None
        if model is not None and getattr(model, "plan", None):
            from ..rpl_reference import make_table_picker
            self.picker = make_table_picker(model, config)
            self.picker.combo.setToolTip(
                "The RPL the table's KPs were quoted against. KPs on another "
                "RPL are translated to this plan's route by seabed position "
                "each time the table is read, and stretches where the routes "
                "diverge are reported.")
            form.addRow(self.picker)
        else:
            form.addRow("KP reference:", QLabel(kp_table.reference_text(config)))
        if kp_table.KP_REF_KEY not in config and self.picker is not None:
            note = QLabel("No KP reference RPL was recorded for this table — "
                          "confirm the RPL above and save.")
            note.setWordWrap(True)
            note.setStyleSheet(ui_helpers.hint_style())
            form.addRow(note)

    def widgets(self) -> List:
        """Field pickers that follow the selected input layer."""
        return [self.start_field, self.end_field]

    def problems(self, layer) -> List[str]:
        """Reasons the table could not be read as configured."""
        out = []
        start, end = self.start_field.text(), self.end_field.text()
        if not start or not end:
            out.append("Choose the start and end KP fields.")
        elif layer is not None:
            names = layer_field_names(layer)
            missing = [n for n in (start, end) if n not in names]
            if names and missing:
                out.append("The input has no field named "
                           + " or ".join(f"'{n}'" for n in missing)
                           + ". Pick the start and end KP fields from the list.")
        return out

    def apply(self, config: Dict) -> None:
        config["start_field"] = self.start_field.text() or kp_table.DEFAULT_START_FIELD
        config["end_field"] = self.end_field.text() or kp_table.DEFAULT_END_FIELD
        config[kp_table.KP_UNIT_KEY] = self.unit_combo.currentData() or kp_table.KP_UNIT_KM
        if self.picker is None:
            # No plan to pick from: keep whatever reference was recorded.
            for key in (kp_table.KP_REF_KEY, kp_table.KP_REF_LABEL_KEY,
                        kp_table.KP_REF_START_KEY):
                if key in self.config:
                    config[key] = self.config[key]
        else:
            from ..rpl_reference import table_reference
            rpl_id, label, start = table_reference(self.model, self.picker.rpl_id())
            config[kp_table.KP_REF_KEY] = rpl_id
            config[kp_table.KP_REF_LABEL_KEY] = label
            # Re-saving with the same RPL keeps the start KP recorded when
            # the table was first referenced, so a renumbering since then
            # is still corrected for.
            recorded = self.config.get(kp_table.KP_REF_START_KEY)
            if self.config.get(kp_table.KP_REF_KEY, None) == rpl_id \
                    and recorded is not None:
                config[kp_table.KP_REF_START_KEY] = recorded
            elif start is None:
                config.pop(kp_table.KP_REF_START_KEY, None)
            else:
                config[kp_table.KP_REF_START_KEY] = round(start, 6)

# -*- coding: utf-8 -*-
"""Ground Model ▸ CPTs: import, list, check and show cone penetration tests.

CPT imports are project-scoped (shared by every plan, like soil classes and
numeric datasets) and are not part of a plan's history. Each plan measures
the CPTs' KP and cross-course offset on its own route whenever they are
shown, and keeps its own display choices (traces on/off, channel, how far
from the route a CPT may be, how far along the route it represents).
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional

from qgis.core import QgsProject
from qgis.PyQt.QtCore import QSettings, Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..geotech import cpt as cpt_mod
from ..qgis_compat import (
    BUTTON_BOX_CANCEL,
    BUTTON_BOX_OK,
    DIALOG_ACCEPTED,
    EDIT_TRIGGER_NONE,
    HEADER_RESIZE_MODE_INTERACTIVE,
    ITEM_DATA_USER_ROLE,
    MESSAGEBOX_NO,
    MESSAGEBOX_YES,
    SELECTION_BEHAVIOR_SELECT_ROWS,
    SELECTION_MODE_EXTENDED,
    qt_exec,
)
from . import cpt_register, schema, ui_helpers
from .cpt_plot import build_entries

SORT_ASCENDING = getattr(getattr(Qt, "SortOrder", Qt), "AscendingOrder")

_SETTINGS_ROOT = "SubseaCableTools/BurialPlanner"
_COLUMNS = ["CPT", "KP", "Offset", "On this plan", "Water depth (m)", "Penetration (m)",
            "Readings", "qc max (MPa)", "Tested", "Import", "Summary"]
_ALL = "__all__"


def _num(value, digits=2) -> str:
    return "" if value is None else f"{float(value):.{digits}f}"


class _SortItem(QTableWidgetItem):
    """Numbers sort numerically; blanks last."""

    def __init__(self, text: str, key=None):
        super().__init__(text)
        self._key = key

    def __lt__(self, other):
        a, b = self._key, getattr(other, "_key", None)
        if a is None or b is None:
            return (a is None) < (b is None) if (a is None) != (b is None) else self.text() < other.text()
        return a < b


class CptSettingsDialog(QDialog):
    """Placement (this plan) and processing (the project) settings."""

    def __init__(self, view: Dict, values: Dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("CPT settings")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        place = QGroupBox("Along this plan's route")
        form = QFormLayout(place)
        self.max_offset = self._spin(view["max_offset_m"], 0, 1e5, 1, " m")
        self.max_offset.setToolTip("CPTs farther than this from the route are listed but not drawn "
                                   "or used by CPT datasets.")
        self.half_range = self._spin(view["half_range_m"], 0, 1e5, 1, " m")
        self.half_range.setToolTip("How far along the route a CPT is taken to represent, each side of "
                                   "its KP; never past halfway to the next CPT. Used for the trace "
                                   "hover and for CPT colour sections.")
        self.full_scale = self._spin(view["full_scale"], 0, 1e6, 3, "")
        self.full_scale.setSpecialValueText("automatic")
        self.full_scale.setToolTip("Value drawn at the full width of each trace; automatic uses the "
                                   "largest value shown, rounded up.")
        self.strip = QCheckBox("SBTn colour strip beside each trace")
        self.strip.setChecked(bool(view.get("strip", True)))
        form.addRow("Use CPTs within", self.max_offset)
        form.addRow("Each CPT represents up to", self.half_range)
        form.addRow("Trace full scale", self.full_scale)
        form.addRow(self.strip)
        layout.addWidget(place)
        proc = QGroupBox("Derived parameters (every plan in this project)")
        pform = QFormLayout(proc)
        self.area = self._spin(values["area_ratio"], 0.01, 1.0, 3, "")
        self.area.setToolTip("Cone net area ratio a for qt = qc + u2 (1 − a), used when the AGS file "
                             "gives none (SCPG_CAR / CPTG_CAR). Typically 0.70–0.85.")
        self.pore = QComboBox()
        for key, label in (("auto", "u2 when delivered (recommended)"), ("u2", "u2"), ("u1", "u1"),
                           ("u3", "u3"), ("none", "none — qt = qc")):
            self.pore.addItem(label, key)
        self.pore.setCurrentIndex(max(0, self.pore.findData(values["pore_channel"])))
        self.zeroed = QCheckBox("Readings are zeroed at the seabed (unless the file says otherwise)")
        self.zeroed.setChecked(values["zeroed_at_seabed"])
        self.zeroed.setToolTip("Offshore seabed CPT readings are normally zeroed at the seabed, so the "
                               "water column is not in qc or u. Untick for readings zeroed on deck "
                               "(the water depth is then added to σv0 and u0).")
        self.weight_mode = QComboBox()
        self.weight_mode.addItem("constant", "constant")
        self.weight_mode.addItem("estimated from the CPT (Robertson & Cabal 2010), else constant", "estimated")
        self.weight_mode.setCurrentIndex(max(0, self.weight_mode.findData(values["unit_weight_mode"])))
        self.weight = self._spin(values["unit_weight"], 10, 25, 2, " kN/m³")
        self.water = self._spin(values["water_unit_weight"], 9.5, 10.5, 3, " kN/m³")
        self.nkt = self._spin(values["nkt"], 1, 50, 1, "")
        self.nkt.setToolTip("Su = qnet / Nkt. Typically 10–20; 14–16 where there is little experience.")
        self.nkt_high = self._spin(values["nkt_high"], 1, 50, 1, "")
        self.nkt_high.setToolTip("A second, higher Nkt gives the lower Su estimate drawn dashed.")
        self.su_fine = QCheckBox("No Su where readings are coarse-grained (Ic < 2.6)")
        self.su_fine.setToolTip("Su is still given where Ic cannot be found (fs = 0 or σ'v0 = 0).")
        self.su_fine.setChecked(values["su_fine_only"])
        self.dr_coarse = QCheckBox("Dr only for coarse-grained readings (Ic < 2.6)")
        self.dr_coarse.setChecked(values["dr_coarse_only"])
        for label, widget in (("Cone area ratio a", self.area), ("Pore pressure for qt and Bq", self.pore),
                              ("", self.zeroed), ("Soil unit weight", self.weight_mode),
                              ("Constant unit weight", self.weight), ("Sea water unit weight", self.water),
                              ("Nkt", self.nkt), ("Nkt for the lower Su", self.nkt_high),
                              ("", self.su_fine), ("", self.dr_coarse)):
            pform.addRow(label, widget)
        note = QLabel("Robertson & Cabal, Guide to CPT (2022): qt, Rf, qnet, Bq, Qtn, Fr, Ic (iterated n), "
                      "SBTn zones 2–7, Su = qnet/Nkt, Dr = √(Qtn/350). Screening values: the contractor's "
                      "interpretation and laboratory data take precedence.")
        note.setWordWrap(True)
        note.setStyleSheet(ui_helpers.hint_style())
        pform.addRow(note)
        layout.addWidget(proc)
        box = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    @staticmethod
    def _spin(value, low, high, decimals, suffix):
        spin = QDoubleSpinBox()
        spin.setDecimals(decimals)
        spin.setRange(low, high)
        spin.setValue(float(value or 0.0))
        spin.setSuffix(suffix)
        return spin

    def view(self) -> Dict:
        return {"max_offset_m": self.max_offset.value(), "half_range_m": self.half_range.value(),
                "full_scale": self.full_scale.value(), "strip": self.strip.isChecked()}

    def values(self) -> Dict:
        return {"area_ratio": self.area.value(), "pore_channel": self.pore.currentData(),
                "zeroed_at_seabed": self.zeroed.isChecked(), "unit_weight_mode": self.weight_mode.currentData(),
                "unit_weight": self.weight.value(), "water_unit_weight": self.water.value(),
                "nkt": self.nkt.value(), "nkt_high": self.nkt_high.value(),
                "su_fine_only": self.su_fine.isChecked(), "dr_coarse_only": self.dr_coarse.isChecked()}


class CptPanel(QWidget):
    def __init__(self, model, dock, plot, parent=None):
        super().__init__(parent)
        self.model, self.dock, self.plot = model, dock, plot
        self.placements: Dict[str, Dict] = {}
        self.place_notes: List[str] = []
        self._log_window = None
        self._loading = False
        self._watched_layer_id = ""

        # Overlay controls: shown in the Ground Model display row in every mode.
        self.overlay_controls = QWidget()
        overlay = QHBoxLayout(self.overlay_controls)
        overlay.setContentsMargins(12, 0, 0, 0)
        self.show_traces = QCheckBox("CPT traces")
        self.show_traces.setToolTip("Draw each CPT near the route at its KP: a trace of the chosen value "
                                    "down to the deepest reading, with an SBTn colour strip. Click a "
                                    "trace to open its log.")
        self.channel = QComboBox()
        for key in cpt_mod.TRACE_CHANNELS:
            self.channel.addItem(cpt_mod.channel_label(key), key)
        self.channel.setToolTip("The value drawn as each CPT's trace.")
        overlay.addWidget(self.show_traces)
        overlay.addWidget(self.channel)
        self.show_traces.toggled.connect(self._overlay_changed)
        self.channel.currentIndexChanged.connect(self._overlay_changed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.campaign = QComboBox()
        self.campaign.setMinimumWidth(200)
        self.campaign.setToolTip("CPT imports in this project (shared by every plan).")
        self.campaign.currentIndexChanged.connect(lambda *_a: self._fill_table())
        row.addWidget(QLabel("Import"))
        row.addWidget(self.campaign, 1)
        self.buttons: Dict[str, QPushButton] = {}
        for key, label, tip, callback in (
                ("import", "Import AGS…", "Import CPTs from AGS 3 / AGS 4 files, optionally joined to a "
                 "position and summary table by CPT name.", self._import),
                ("edit", "Edit…", "Change the files, the position table or the register details of the "
                 "selected import.", self._edit),
                ("reload", "Reload", "Read the selected import's files again with the same choices.",
                 self._reload),
                ("remove", "Remove…", "Delete the selected import and its CPTs from this project.", self._remove),
                ("logs", "Logs…", "Open the selected CPTs (or the first) as logs: tracks on one depth axis.",
                 lambda: self.open_logs()),
                ("section", "Colour section…", "Add a numeric dataset of the trace value (changeable in the "
                 "dialog), placed at the CPT positions, to colour the plot by KP and depth.",
                 self._colour_section),
                ("settings", "Settings…", "How far from the route CPTs are used, and the derived-parameter "
                 "assumptions.", self._settings)):
            button = QPushButton(label)
            button.setToolTip(tip)
            button.clicked.connect(callback)
            self.buttons[key] = button
            row.addWidget(button)
        self.show_map = QCheckBox("Show on map")
        self.show_map.setToolTip("The CPT locations layer (Burial Planner group), labelled by name.")
        self.show_map.setChecked(bool(QSettings().value(f"{_SETTINGS_ROOT}/cpt_layer_visible", True, type=bool)))
        self.show_map.toggled.connect(self._map_toggled)
        row.addWidget(self.show_map)
        layout.addLayout(row)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(HEADER_RESIZE_MODE_INTERACTIVE)
        header.setStretchLastSection(True)
        self.table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.table.setSelectionMode(SELECTION_MODE_EXTENDED)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(EDIT_TRIGGER_NONE)
        self.table.setToolTip("Select a CPT to find it on the map and the plots; double-click for its log.")
        # Route order by default (the user may sort by any column).
        self.table.horizontalHeader().setSortIndicator(1, SORT_ASCENDING)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.cellDoubleClicked.connect(lambda *_a: self.open_logs())
        ui_helpers.enable_column_menu(self.table, f"{_SETTINGS_ROOT}/cpt_table_columns",
                                      always_visible=(0,), default_hidden=("Tested",))
        layout.addWidget(self.table, 1)
        plot.cptClicked.connect(self._trace_clicked)

    # -- state -----------------------------------------------------------------------
    @property
    def register(self):
        return self.model.cpts

    def view(self) -> Dict:
        return self.model.cpt_view() if self.model.plan else dict(self.model.CPT_VIEW_DEFAULTS)

    def refresh(self) -> None:
        """Re-read the register when it changed and re-place on the route."""
        try:
            self.register.refresh()
        except Exception as exc:
            self.status.setText(f"✗ The CPTs could not be read: {exc}")
            return
        self.setEnabled(True)
        view = self.view()
        self._loading = True
        self.show_traces.setChecked(bool(view["show"]))
        self.channel.setCurrentIndex(max(0, self.channel.findData(view["channel"])))
        self._loading = False
        self._fill_campaigns()
        self._place()
        self._fill_table()
        self.render()
        if self._log_window is not None:
            self._log_window.set_order(self.ordered_ids(), self.placements)
            self._log_window.sync()

    def _fill_campaigns(self) -> None:
        current = self.campaign.currentData()
        self.campaign.blockSignals(True)
        self.campaign.clear()
        campaigns = self.register.campaigns
        self.campaign.addItem(f"All imports ({len(self.register.cpts)} CPTs)", _ALL)
        counts: Dict[str, int] = {}
        for cpt in self.register.cpts:
            counts[cpt.get("campaign_id")] = counts.get(cpt.get("campaign_id"), 0) + 1
        for campaign in campaigns:
            label = f"{campaign.get('name') or 'CPTs'} — {counts.get(campaign['campaign_id'], 0)} CPT(s)"
            details = " ".join(v for v in (campaign.get("originator"), campaign.get("revision")) if v)
            if details:
                label += f" ({details})"
            if campaign.get("status") == "superseded":
                label += " [superseded]"
            self.campaign.addItem(label, campaign["campaign_id"])
        self.campaign.setCurrentIndex(max(0, self.campaign.findData(current)))
        self.campaign.blockSignals(False)

    def selected_campaign(self) -> Optional[Dict]:
        """The chosen import, or the only one when "All" is chosen."""
        data = self.campaign.currentData()
        if data and data != _ALL:
            return self.register.campaign(data)
        campaigns = self.register.campaigns
        return campaigns[0] if len(campaigns) == 1 else None

    def _place(self) -> None:
        """Place the CPTs of current imports; superseded imports are listed
        but neither placed nor drawn."""
        view = self.view()
        rows, notes = cpt_register.place(self.model.route, self.register.active_cpts(),
                                         float(view["max_offset_m"]), float(view["half_range_m"])) \
            if self.model.route is not None else ([], [])
        self.placements = {r["cpt_id"]: r for r in rows}
        superseded = self.register.superseded_ids()
        for cpt in self.register.cpts:
            if cpt.get("campaign_id") in superseded:
                self.placements[cpt["cpt_id"]] = {"cpt_id": cpt["cpt_id"], "name": cpt.get("name"),
                                                  "kp": None, "offset_m": None, "start_kp": None,
                                                  "end_kp": None, "status": "superseded import"}
        self.place_notes = notes

    def ordered_ids(self, cpts: Optional[List[Dict]] = None) -> List[str]:
        """CPT ids: placed by KP, then the rest by name."""
        cpts = self.register.cpts if cpts is None else cpts

        def key(cpt):
            placement = self.placements.get(cpt["cpt_id"]) or {}
            kp = placement.get("kp")
            return (0 if placement.get("status") == "placed" else 1 if kp is not None else 2,
                    kp if kp is not None else 0.0, str(cpt.get("name") or "").casefold())
        return [c["cpt_id"] for c in sorted(cpts, key=key)]

    def _fill_table(self) -> None:
        wanted = self.campaign.currentData()
        cpts = [c for c in self.register.cpts if wanted in (None, _ALL) or c.get("campaign_id") == wanted]
        names = {c.get("campaign_id"): c.get("name") or "" for c in self.register.campaigns}
        selected = set(self.selected_ids())
        self.table.setSortingEnabled(False)
        with ui_helpers.silent_rebuild(self.table):
            self.table.setRowCount(0)
            for cpt_id in self.ordered_ids(cpts):
                cpt = self.register.by_id(cpt_id)
                placement = self.placements.get(cpt_id) or {}
                row = self.table.rowCount()
                self.table.insertRow(row)
                status = placement.get("status", "no route") if self.model.route is not None else "no route"
                values = [
                    (cpt.get("name") or "", None),
                    (schema.format_kp(placement["kp"]) if placement.get("kp") is not None else "", placement.get("kp")),
                    (cpt_register.side_text(placement.get("offset_m")),
                     abs(placement["offset_m"]) if placement.get("offset_m") is not None else None),
                    (status, None),
                    (_num(cpt.get("water_depth_m"), 1), cpt.get("water_depth_m")),
                    (_num(cpt.get("final_depth_m") or cpt.get("max_depth_m")),
                     cpt.get("final_depth_m") or cpt.get("max_depth_m")),
                    (str(cpt.get("readings") or 0), cpt.get("readings")),
                    (_num(cpt.get("max_qc_mpa"), 3), cpt.get("max_qc_mpa")),
                    (cpt.get("tested") or "", None),
                    (names.get(cpt.get("campaign_id"), ""), None),
                    (cpt.get("summary") or "", None),
                ]
                for column, (text, sort_key) in enumerate(values):
                    item = _SortItem(text, sort_key)
                    if column == 0:
                        item.setData(ITEM_DATA_USER_ROLE, cpt_id)
                        warnings = (cpt.get("meta") or {}).get("warnings") or []
                        item.setToolTip(f"{cpt.get('source_file')} ({cpt.get('ags_version')})"
                                        + (("\n⚠ " + "\n⚠ ".join(warnings[:6])) if warnings else ""))
                    if column == 3 and status != "placed":
                        item.setForeground(ui_helpers.qcolor("warn"))
                    if column == 10:
                        item.setToolTip(text)
                    self.table.setItem(row, column, item)
        self.table.setSortingEnabled(True)
        if selected:
            # One selection for every previously selected row (selectRow per
            # row would keep only the last in extended selection mode).
            from qgis.PyQt.QtCore import QItemSelection, QItemSelectionModel
            selection = QItemSelection()
            for row in range(self.table.rowCount()):
                item = self.table.item(row, 0)
                if item is not None and item.data(ITEM_DATA_USER_ROLE) in selected:
                    index = self.table.model().index(row, 0)
                    selection.select(index, index)
            flags = getattr(QItemSelectionModel, "SelectionFlag", QItemSelectionModel)
            self.table.selectionModel().select(selection, flags.Select | flags.Rows)
        self.table.resizeColumnsToContents()
        self._update_status()

    def _update_status(self) -> None:
        cpts = self.register.cpts
        has = bool(cpts)
        for key in ("edit", "reload", "remove"):
            self.buttons[key].setEnabled(self.selected_campaign() is not None)
        for key in ("logs", "section"):
            self.buttons[key].setEnabled(has)
        self.buttons["section"].setEnabled(has and bool(self.model.plan))
        if not has:
            self.status.setText("No CPTs in this project yet. Import AGS… reads AGS 3 or AGS 4 files and "
                                "an optional table of positions, water depth and horizons per CPT.")
            self.status.setStyleSheet(ui_helpers.hint_style())
            return
        placed = sum(1 for p in self.placements.values() if p["status"] == "placed")
        unpositioned = sum(1 for c in cpts if c.get("lat") is None)
        view = self.view()
        text = f"{len(cpts)} CPT(s) in {len(self.register.campaigns)} import(s)"
        if self.model.route is None:
            text += "; set the plan's route to place them by KP."
        else:
            text += f"; {placed} within {float(view['max_offset_m']):g} m of this plan's route."
        problems = list(self.place_notes)
        if unpositioned:
            problems.append(f"{unpositioned} without a position (Edit… to add a position table)")
        for campaign in self.register.campaigns:
            changed = cpt_register.changed_sources(campaign)
            if changed:
                problems.append(f"{campaign.get('name')}: " + "; ".join(changed[:3]) + " — Reload")
        if problems:
            text += " ⚠ " + "; ".join(problems[:4])
        self.status.setText(text)
        self.status.setStyleSheet(ui_helpers.status_style("warn") if problems else "")

    def selected_ids(self) -> List[str]:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()}) \
            if self.table.selectionModel() is not None else []
        out = []
        for row in rows:
            item = self.table.item(row, 0)
            if item is not None and item.data(ITEM_DATA_USER_ROLE):
                out.append(item.data(ITEM_DATA_USER_ROLE))
        return out

    # -- overlay ---------------------------------------------------------------------
    def render(self) -> None:
        """Push the CPT traces (or nothing) to the Ground Model plot."""
        view = self.view()
        profile = getattr(self.dock, "profile", None)
        if profile is not None and hasattr(profile, "set_cpts"):
            profile.set_cpts([(p["kp"], f"{p['name']} ({cpt_register.side_text(p.get('offset_m'))})")
                              for p in self.placements.values() if p.get("status") == "placed"]
                             if self.model.plan else [])
        if not view["show"] or not self.model.plan or not self.placements:
            self.plot.set_cpts(None)
            return
        settings = self.model.cpt_settings()
        depth = None
        numeric_state = {}
        try:
            numeric_state = json.loads(self.model.plan.get("params_json") or "{}").get("numeric_ground") or {}
        except (TypeError, ValueError, AttributeError):
            pass
        if numeric_state.get("mode") == 1:
            depth = (float(numeric_state.get("depth_min", 0)), float(numeric_state.get("depth_max", 3)))
        entries, scale = build_entries(self.register, list(self.placements.values()),
                                       view["channel"], settings, depth)
        full = float(view.get("full_scale") or 0.0) or scale
        self.plot.set_cpts(entries, full, view["channel"], bool(view.get("strip", True)))
        selected = self.selected_ids()
        self.plot.set_selected_cpt(selected[0] if selected else "")

    def _overlay_changed(self, *_args) -> None:
        if self._loading or not self.model.plan:
            return
        self.model.save_cpt_view({"show": self.show_traces.isChecked(),
                                  "channel": self.channel.currentData() or "qc"}, reason="CPT traces")

    # -- selection / map sync ------------------------------------------------------------
    def _selection_changed(self) -> None:
        ids = self.selected_ids()
        self.plot.set_selected_cpt(ids[0] if ids else "")
        if len(ids) != 1:
            return
        self.highlight(ids[0])
        if self._log_window is not None and self._log_window.isVisible():
            self._log_window.show_cpts([ids[0]] + [i for i in self._log_window.compare if i != ids[0]])

    def highlight(self, cpt_id: str) -> None:
        """Mark the CPT on the map and the profile (its KP when placed)."""
        placement = self.placements.get(cpt_id) or {}
        cpt = self.register.by_id(cpt_id) or {}
        if self.dock is None:
            return
        if placement.get("kp") is not None:
            self.dock.highlight_kp(placement["kp"])
            self.plot.show_kp(placement["kp"], 0.0)
        if cpt.get("lat") is not None and hasattr(self.dock, "flash_point"):
            self.dock.flash_point(cpt["lon"], cpt["lat"])

    def select(self, cpt_id: str) -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.data(ITEM_DATA_USER_ROLE) == cpt_id:
                self.table.selectRow(row)
                self.table.scrollToItem(item)
                return

    def _trace_clicked(self, cpt_id: str) -> None:
        self.select(cpt_id)
        self.open_logs([cpt_id])

    # -- logs ------------------------------------------------------------------------------
    def open_logs(self, cpt_ids: Optional[List[str]] = None) -> None:
        ids = list(cpt_ids or self.selected_ids() or self.ordered_ids()[:1])
        if not ids:
            return
        from .cpt_log_view import CptLogWindow
        if self._log_window is None:
            # Owned by the dock so it closes with the Burial Planner.
            self._log_window = CptLogWindow(self.model, self.dock if isinstance(self.dock, QWidget)
                                            else self.window())
            self._log_window.cptChosen.connect(self._log_chosen)
        self._log_window.set_order(self.ordered_ids(), self.placements)
        self._log_window.show_cpts(ids)
        self._log_window.show()
        self._log_window.raise_()

    def _log_chosen(self, cpt_id: str) -> None:
        self.plot.set_selected_cpt(cpt_id)
        self.highlight(cpt_id)

    # -- actions --------------------------------------------------------------------------
    def _import(self) -> None:
        self._open_import(None)

    def _edit(self) -> None:
        campaign = self.selected_campaign()
        if campaign is None:
            QMessageBox.information(self, "CPTs", "Choose the import to edit in the Import list.")
            return
        self._open_import(campaign)

    def _open_import(self, campaign: Optional[Dict]) -> None:
        from .cpt_import_dialog import CptImportDialog
        dialog = CptImportDialog(self.model, campaign, self)
        if qt_exec(dialog) != DIALOG_ACCEPTED or dialog.result() is None:
            return
        self.save(*dialog.result())

    def save(self, campaign: Dict, cpts: List[Dict], readings: Dict[str, Dict]) -> Optional[str]:
        if not campaign.get("seq") and self.register.campaign(campaign.get("campaign_id") or "") is None:
            campaign = dict(campaign, seq=len(self.register.campaigns))
        campaign_id = self.model.save_cpt_campaign(campaign, cpts, readings)
        if campaign_id:
            self._ensure_layer()
            self.refresh()
            index = self.campaign.findData(campaign_id)
            if index >= 0:
                self.campaign.setCurrentIndex(index)
        return campaign_id

    def _reload(self) -> None:
        campaign = self.selected_campaign()
        if campaign is None:
            QMessageBox.information(self, "CPTs", "Choose the import to reload in the Import list.")
            return
        try:
            built = cpt_register.build(campaign.get("config") or {}, campaign_id=campaign["campaign_id"])
        except Exception as exc:  # noqa: BLE001 - unreadable workbooks raise zipfile/openpyxl errors
            QMessageBox.warning(self, "Reload CPTs", f"The import could not be read again: {exc}")
            return
        if not built["cpts"]:
            QMessageBox.warning(self, "Reload CPTs", "No CPTs could be read:\n" + "\n".join(built["notes"][:10]))
            return
        before = len([c for c in self.register.cpts if c.get("campaign_id") == campaign["campaign_id"]])
        updated = dict(campaign, config=cpt_register.stamp_fingerprints(campaign.get("config") or {}),
                       imported_utc=schema.utc_now_iso())
        if self.save(updated, built["cpts"], built["readings"]):
            QMessageBox.information(self, "Reload CPTs",
                                    f"Before: {before} CPT(s). Now: {len(built['cpts'])} CPT(s)."
                                    + ("\n\n" + "\n".join(built["notes"][:12]) if built["notes"] else ""))

    def _remove(self) -> None:
        campaign = self.selected_campaign()
        if campaign is None:
            QMessageBox.information(self, "CPTs", "Choose the import to remove in the Import list.")
            return
        count = len([c for c in self.register.cpts if c.get("campaign_id") == campaign["campaign_id"]])
        text = (f"Delete the import '{campaign.get('name')}' and its {count} CPT(s) from this project? "
                "The AGS files and the position table are not touched.")
        if QMessageBox.question(self, "Remove CPTs", text, MESSAGEBOX_YES | MESSAGEBOX_NO) != MESSAGEBOX_YES:
            return
        if self.model.delete_cpt_campaign(campaign["campaign_id"]):
            self._ensure_layer()
            self.refresh()

    def _settings(self) -> None:
        dialog = CptSettingsDialog(self.view(), self.model.cpt_settings(), self)
        if qt_exec(dialog) != DIALOG_ACCEPTED:
            return
        if self.model.plan:
            self.model.save_cpt_view(dialog.view(), reason="CPT placement")
        self.model.save_cpt_settings(dialog.values())
        self.refresh()

    def _colour_section(self) -> None:
        tab = self._ground_tab()
        if tab is None:
            return
        tab.add_cpt_dataset(self.channel.currentData() or "su")

    def _ground_tab(self):
        widget = self.parent()
        while widget is not None and not hasattr(widget, "add_cpt_dataset"):
            widget = widget.parent()
        return widget

    # -- map layer -------------------------------------------------------------------------
    def _ensure_layer(self) -> None:
        try:
            from . import map_layers
            layer = map_layers.ensure_cpt_layer(QgsProject.instance(), self.model.store.gpkg_path,
                                                self.show_map.isChecked())
        except Exception:
            from ..plugin_log import log_exception
            log_exception("Burial Planner: the CPT locations layer could not be shown")
            return
        self._watch_layer(layer)

    def _watch_layer(self, layer) -> None:
        """Selecting CPTs on the map selects them in the table (two-way)."""
        if layer is None or layer.id() == self._watched_layer_id:
            return
        layer.selectionChanged.connect(self._map_selection_changed)
        self._watched_layer_id = layer.id()

    def _map_selection_changed(self, selected, _deselected, _clear) -> None:
        layer = QgsProject.instance().mapLayer(self._watched_layer_id or "")
        if layer is None or not selected:
            return
        try:
            feature = layer.getFeature(selected[0])
            cpt_id = feature["cpt_id"]
        except (KeyError, RuntimeError):
            return
        if cpt_id:
            self.select(str(cpt_id))

    def _map_toggled(self, checked: bool) -> None:
        QSettings().setValue(f"{_SETTINGS_ROOT}/cpt_layer_visible", bool(checked))
        try:
            from . import map_layers
            map_layers.set_cpt_layer_visible(QgsProject.instance(), self.model.store.gpkg_path,
                                             bool(checked))
        except Exception:
            from ..plugin_log import log_exception
            log_exception("Burial Planner: the CPT locations layer visibility was not changed")

    def ensure_map_layer(self) -> None:
        """Called when CPT mode is shown: the layer is added once CPTs exist;
        an existing layer is left as the user set it in the Layers panel."""
        from . import map_layers
        layer = map_layers.find_layer(QgsProject.instance(), self.model.store.gpkg_path,
                                      schema.CPT_LAYER_NAME)
        if layer is None and self.register.cpts and self.show_map.isChecked():
            self._ensure_layer()
            return
        self._watch_layer(layer)

# -*- coding: utf-8 -*-
"""Bathymetry Profile tab — prepare reusable depth and terrain-slope data."""

from __future__ import annotations

import math
from typing import Optional

from qgis.core import QgsProject
from qgis.PyQt.QtGui import QFont
from qgis.PyQt.QtWidgets import (
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import ui_helpers

# Plain-language guide to the slope panel's four series (the question
# engineers kept asking: "which slope do I look at?").
SLOPE_GUIDE_HTML = (
    "<b>Which slope should I look at?</b>"
    "<ul style='margin-left:-20px'>"
    "<li><b>Longitudinal</b> (orange) — pitch along the route, signed: "
    "+ = up-slope with increasing KP. Use it for up/down-slope limits of the "
    "burial tool.</li>"
    "<li><b>Cross tilt</b> (purple) — average side-to-side tilt between the "
    "port and starboard samples (± the cross offset), signed: + = deeper to "
    "starboard of travel. Use it for vehicle roll across the track width; a "
    "symmetric trench reads ~0.</li>"
    "<li><b>Max local cross</b> (red dotted) — the steepest local side "
    "slope found between those two points. Only differs from cross tilt "
    "when the cross offset is several cells wide (set it to the vehicle "
    "half-track); use it to catch a steep wall inside the track.</li>"
    "<li><b>Absolute</b> (brown dashed) — the combined gradient magnitude "
    "(longitudinal and cross together). Use it for 'maximum slope in any "
    "direction' criteria. It is blank where cross slope has no data.</li>"
    "</ul>"
    "Gaps are honest: a slope is left blank rather than bridged across "
    "missing data or between different surveys.")


class ProfileTab(QWidget):
    """Profile sampling controls kept separate from exclusion resolution."""

    def __init__(self, model, dock, parent=None):
        super().__init__(parent)
        self.model = model
        self.dock = dock
        self._loading = False
        self._dirty = False
        self._loaded_plan_id = ""

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addStretch(1)
        content = QWidget()
        content.setMaximumWidth(850)
        layout = QVBoxLayout(content)
        outer.addWidget(content, 4)
        outer.addStretch(1)

        intro = QLabel(
            "Build the reusable bathymetry profile before evaluating exclusions. "
            "Depth is sampled once along the scoped route; longitudinal, cross "
            "and absolute terrain slopes are then derived from those stored "
            "samples without rereading the bathymetry for every rule.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        source_box = QGroupBox("Selected inputs")
        source_form = QFormLayout(source_box)
        self.route_label = QLabel("—")
        self.route_label.setWordWrap(True)
        self.source_label = QLabel("—")
        self.source_label.setWordWrap(True)
        source_form.addRow("Route and scope:", self.route_label)
        source_form.addRow("Bathymetry source:", self.source_label)
        source_note = QLabel(
            "Route, scope and source layers are selected on Inputs. Return to "
            "Inputs whenever the source data or reviewed KP range changes.")
        source_note.setWordWrap(True)
        source_note.setStyleSheet(ui_helpers.hint_style())
        source_form.addRow(source_note)
        from ...bathymetry_sampling import configure_layers
        options_btn = QPushButton("Bathymetry source conventions…")
        options_btn.clicked.connect(lambda: configure_layers(self))
        source_form.addRow(options_btn)
        layout.addWidget(source_box)

        settings_box = QGroupBox("Profile sampling and terrain-slope resolution")
        settings_form = QFormLayout(settings_box)
        self.profile_step_spin = QDoubleSpinBox()
        self.profile_step_spin.setRange(0.0, 5000.0)
        self.profile_step_spin.setDecimals(1)
        self.profile_step_spin.setSuffix(" m")
        self.profile_step_spin.setSpecialValueText("Auto (bathy cell)")
        self.profile_step_spin.setToolTip(
            "Station spacing for the stored depth profile. Auto follows the "
            "smallest raster cell (or 5 m for contours), bounded to at least "
            "2 m, no coarser than the exclusion Sample step, and about 500,000 "
            "stations over the scope.")
        settings_form.addRow("Profile step:", self.profile_step_spin)

        self.cross_offset_spin = QDoubleSpinBox()
        self.cross_offset_spin.setRange(0.0, 10000.0)
        self.cross_offset_spin.setDecimals(1)
        self.cross_offset_spin.setSuffix(" m")
        self.cross_offset_spin.setSpecialValueText("Auto (profile step)")
        self.cross_offset_spin.setToolTip(
            "Depth is sampled this distance to port and starboard. Auto gives "
            "local cross-terrain slope; enter the plough half-track width to "
            "approximate roll across the physical vehicle span.")
        settings_form.addRow("Cross offset (each side):", self.cross_offset_spin)

        self.resolution_label = QLabel("—")
        self.resolution_label.setWordWrap(True)
        settings_form.addRow("Resolved sampling:", self.resolution_label)
        self.cross_hint = QLabel("")
        self.cross_hint.setWordWrap(True)
        self.cross_hint.setStyleSheet(ui_helpers.status_style("warn"))
        self.cross_hint.setVisible(False)
        settings_form.addRow(self.cross_hint)
        layout.addWidget(settings_box)

        build_box = QGroupBox("Stored plan profile")
        build_layout = QVBoxLayout(build_box)
        self.profile_state_label = QLabel("—")
        self.profile_state_label.setWordWrap(True)
        build_layout.addWidget(self.profile_state_label)
        button_row = QHBoxLayout()
        self.apply_button = QPushButton("Apply settings")
        self.apply_button.setToolTip(
            "Save the profile settings without sampling. The existing profile "
            "will be marked stale when its resolution or cross span differs.")
        self.apply_button.clicked.connect(self._apply_settings)
        button_row.addWidget(self.apply_button)
        self.rebuild_button = QPushButton("Apply && rebuild profile")
        self.rebuild_button.setToolTip(
            "Save these settings, then rebuild and persist depth plus "
            "cross-offset samples in the background.")
        # The rebuild is the primary action of this tab — make it read so.
        bold = QFont(self.rebuild_button.font())
        bold.setBold(True)
        self.rebuild_button.setFont(bold)
        self.rebuild_button.clicked.connect(self._apply_and_rebuild)
        button_row.addWidget(self.rebuild_button)
        self.recheck_button = QPushButton("Re-check")
        self.recheck_button.setToolTip(
            "Check again whether the stored profile still matches the route "
            "and bathymetry (e.g. after replacing a file on disk).")
        self.recheck_button.clicked.connect(self._recheck)
        button_row.addWidget(self.recheck_button)
        button_row.addStretch(1)
        build_layout.addLayout(button_row)
        self.apply_feedback = QLabel("")
        self.apply_feedback.setWordWrap(True)
        self.apply_feedback.setStyleSheet(ui_helpers.hint_style())
        build_layout.addWidget(self.apply_feedback)
        layout.addWidget(build_box)

        guide_box = QGroupBox("Reading the slope panel")
        guide_layout = QVBoxLayout(guide_box)
        guide = QLabel(SLOPE_GUIDE_HTML)
        guide.setWordWrap(True)
        guide_layout.addWidget(guide)
        layout.addWidget(guide_box)
        layout.addStretch(1)

        # Unapplied spin edits mark the buttons and survive background
        # refreshes of the same plan.
        self.profile_step_spin.valueChanged.connect(self._mark_dirty)
        self.cross_offset_spin.valueChanged.connect(self._mark_dirty)

        model.planChanged.connect(self.refresh)
        model.inputsChanged.connect(self.refresh)
        self.refresh()

    def _mark_dirty(self, *_args) -> None:
        if self._loading:
            return
        self._set_dirty(True)

    def _set_dirty(self, dirty: bool) -> None:
        self._dirty = bool(dirty)
        self.apply_button.setText("Apply settings *" if dirty
                                  else "Apply settings")
        self.rebuild_button.setText("Apply && rebuild profile *" if dirty
                                    else "Apply && rebuild profile")

    def _source_text(self) -> str:
        config = self.model.depth_config()
        if not config.is_configured():
            return "No bathymetry source configured. Select one on Inputs."
        project = QgsProject.instance()
        if config.raster_layer_ids:
            names = []
            for layer_id in config.raster_layer_ids:
                layer = project.mapLayer(layer_id)
                names.append(layer.name() if layer is not None
                              else "⚠ raster not in the project")
            text = f"Raster band {config.raster_band}: " + ", ".join(names)
        else:
            names = []
            for entry in config.contour_layers:
                layer = project.mapLayer(entry.get("layer_id") or "")
                name = layer.name() if layer is not None \
                    else "⚠ contour layer not in the project"
                field = entry.get("depth_field") or "first field"
                names.append(f"{name} [{field}]")
            text = "Contours: " + ", ".join(names)
        if self.model.depth_relinks:
            text += (" — relinked by source to " + ", ".join(
                self.model.depth_relinks) + " (the saved layer id was not "
                "in the project; Apply source on Inputs to keep it)")
        return text

    def refresh(self) -> None:
        self._loading = True
        try:
            plan = self.model.plan
            plan_id = str(plan.get("plan_id") or "")
            same_plan = plan_id == self._loaded_plan_id
            params = self.model.gen_params()
            # Never clobber edited-but-unapplied values on a background
            # refresh of the same plan.
            if not (same_plan and self._dirty):
                self.profile_step_spin.setValue(params.profile_step_m)
                self.cross_offset_spin.setValue(params.cross_offset_m)
                self._set_dirty(False)
            if not same_plan:
                self.apply_feedback.setText("")
            self._loaded_plan_id = plan_id
            enabled = bool(plan)
            self.apply_button.setEnabled(enabled)

            route_name = plan.get("rpl_name") or "No route selected"
            if self.model.route is not None:
                self.route_label.setText(
                    f"{route_name}; KP {params.scope.start_km:.3f}–"
                    f"{params.scope.end_km:.3f} "
                    f"({params.scope.length_km:.3f} km)")
            else:
                self.route_label.setText(route_name)
            self.source_label.setText(self._source_text())

            resolved_step = self.model.resolve_profile_step_m(params)
            resolved_cross = self.model.resolve_cross_offset_m(params)
            stations = (int(math.ceil(
                params.scope.length_km * 1000.0 / max(resolved_step, 1.0))) + 1
                if params.scope.length_km > 0 else 0)
            self.resolution_label.setText(
                f"Approximately {stations:,} route stations at {resolved_step:g} m; "
                f"local longitudinal slope baseline {2.0 * resolved_step:g} m; "
                f"cross-slope span {2.0 * resolved_cross:g} m "
                f"(±{resolved_cross:g} m).")
            config = self.model.depth_config()
            contours_only = bool(config.contour_layers) and \
                not config.raster_layer_ids
            if contours_only and params.cross_offset_m <= 0:
                # Auto = the 5 m contour step: a ±5 m transverse line needs a
                # contour crossing on each side, which flat seabed never has.
                self.cross_hint.setText(
                    f"Contour-only bathymetry with Auto cross offset samples "
                    f"only ±{resolved_cross:g} m either side: unless the "
                    "seabed is steep, no contour falls that close and cross/"
                    "absolute slope come out as no data (Insufficient "
                    "Information). Enter the burial tool's half-track width "
                    "(typically 5–25 m) — or hide that criterion's no-data "
                    "ranges from the profile's Overlays menu.")
                self.cross_hint.setVisible(True)
            else:
                self.cross_hint.setVisible(False)

            reasons = self.model.profile_stale_reasons()
            profile = self.model.bathy_profile
            state = ("missing" if profile is None or not profile.kps
                     else ("stale" if reasons else "current"))
            if state == "missing":
                state_text = "No stored profile. Apply settings and rebuild it."
                style = ui_helpers.status_style("error")
            elif state == "stale":
                state_text = (
                    "The stored profile is out of date — rebuild before "
                    "generating exclusions:\n• " + "\n• ".join(reasons))
                style = ui_helpers.status_style("warn")
            else:
                state_text = (
                    f"Current: {profile.sample_count:,} stations at "
                    f"{profile.step_m:g} m, cross ±{profile.cross_offset_m:g} m."
                    if profile is not None else "Current profile.")
                style = ui_helpers.status_style("ok")
            self.profile_state_label.setText(state_text)
            self.profile_state_label.setStyleSheet(style)
            blockers = []
            if not enabled:
                blockers.append("no plan is open")
            else:
                if self.model.route is None:
                    blockers.append("the route is not set (Inputs)")
                if params.scope.length_km <= 0:
                    blockers.append("the scope is not set (Inputs)")
                if not self.model.depth_config().is_configured():
                    blockers.append("no bathymetry source is configured "
                                    "(Inputs)")
            self.rebuild_button.setEnabled(not blockers)
            if blockers:
                self.rebuild_button.setToolTip(
                    "Cannot rebuild yet: " + "; ".join(blockers) + ".")
            else:
                self.rebuild_button.setToolTip(
                    "Save these settings, then rebuild and persist depth "
                    "plus cross-offset samples in the background.")
        finally:
            self._loading = False

    def set_runtime_status(self, text: str) -> None:
        """Mirror background sampling status while this tab is visible."""
        if text:
            self.profile_state_label.setText(text)
            self.profile_state_label.setStyleSheet("")

    def _save_settings(self) -> Optional[bool]:
        """True = saved, False = write failed, None = nothing to save."""
        if self._loading or not self.model.plan:
            return False
        profile_step = self.profile_step_spin.value()
        cross_offset = self.cross_offset_spin.value()
        params = self.model.gen_params()
        if (abs(params.profile_step_m - profile_step) <= 1e-9
                and abs(params.cross_offset_m - cross_offset) <= 1e-9):
            self._set_dirty(False)
            return None
        if self.model.update_gen_params({
            "profile_step_m": profile_step,
            "cross_offset_m": cross_offset,
        }, reason="bathymetry profile parameters"):
            # Cleared only after the write succeeded: on a store failure
            # the edits stay dirty-protected instead of silently reverting
            # on the next refresh.
            self._set_dirty(False)
            return True
        return False

    def _apply_settings(self) -> None:
        saved = self._save_settings()
        if saved is None:
            self.apply_feedback.setText(
                "No changes to apply — the stored settings already match.")
        elif saved:
            self.apply_feedback.setText("Settings applied.")
        self.refresh()

    def _recheck(self) -> None:
        self.model.invalidate_depth_cache()
        self.apply_feedback.setText("Checked against the current route and "
                                    "bathymetry.")
        self.refresh()
        try:
            self.dock._refresh_profile()
        except (AttributeError, RuntimeError):
            pass

    def _apply_and_rebuild(self) -> None:
        if self._save_settings() is not False:
            self.apply_feedback.setText("")
            self.dock.request_profile_resample()


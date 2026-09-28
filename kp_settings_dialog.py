"""KP distance settings: Geodesic (WGS84, default) or Cartesian (grid)."""
from __future__ import annotations

from typing import Optional, Tuple

from qgis.core import QgsCoordinateReferenceSystem
from qgis.PyQt.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QGroupBox,
                                 QLabel, QRadioButton, QVBoxLayout)

from .kp_range_utils import (KP_MODE_CARTESIAN, KP_MODE_GEODESIC, describe_kp_mode,
                             kp_distance_mode, kp_grid_crs_setting,
                             set_kp_distance_settings)
from .qgis_compat import BUTTON_BOX_CANCEL, BUTTON_BOX_OK, qt_exec


class KpSettingsDialog(QDialog):
    """Choose how KP is measured.

    ``mode`` / ``grid_crs`` preset the choice; ``note`` explains the scope
    (plugin-wide, or one burial plan).
    """

    def __init__(self, parent=None, mode: Optional[str] = None,
                 grid_crs: Optional[str] = None, title: str = "KP settings",
                 note: str = ""):
        super().__init__(parent)
        self.setWindowTitle(title)
        mode = mode or kp_distance_mode()
        grid_crs = kp_grid_crs_setting() if grid_crs is None else grid_crs
        layout = QVBoxLayout(self)
        box = QGroupBox("KP distance")
        inner = QVBoxLayout(box)
        self.geodesic = QRadioButton("Geodesic — WGS84 ellipsoid (recommended)")
        self.cartesian = QRadioButton("Cartesian — planar grid distances")
        self.geodesic.setChecked(mode != KP_MODE_CARTESIAN)
        self.cartesian.setChecked(mode == KP_MODE_CARTESIAN)
        inner.addWidget(self.geodesic)
        inner.addWidget(self.cartesian)
        self.fixed_crs = QCheckBox("Use a specific grid CRS")
        self.fixed_crs.setToolTip(
            "Unticked: the project CRS when it is projected, otherwise the UTM "
            "zone at the start of each route.")
        inner.addWidget(self.fixed_crs)
        try:
            from qgis.gui import QgsProjectionSelectionWidget
            self.crs_widget = QgsProjectionSelectionWidget()
        except Exception:  # pragma: no cover - GUI unavailable
            self.crs_widget = None
        if self.crs_widget is not None:
            inner.addWidget(self.crs_widget)
            if grid_crs:
                self.crs_widget.setCrs(QgsCoordinateReferenceSystem(grid_crs))
        self.fixed_crs.setChecked(bool(grid_crs))
        layout.addWidget(box)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        if note:
            hint = QLabel(note)
            hint.setWordWrap(True)
            layout.addWidget(hint)
        buttons = QDialogButtonBox()
        buttons.setStandardButtons(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        for widget in (self.geodesic, self.cartesian, self.fixed_crs):
            widget.toggled.connect(self._sync)
        if self.crs_widget is not None:
            self.crs_widget.crsChanged.connect(self._sync)
        self._sync()

    def _grid(self) -> str:
        if not (self.fixed_crs.isChecked() and self.crs_widget is not None):
            return ""
        crs = self.crs_widget.crs()
        return crs.authid() if crs.isValid() else ""

    def _sync(self, *_):
        cartesian = self.cartesian.isChecked()
        self.fixed_crs.setEnabled(cartesian)
        if self.crs_widget is not None:
            self.crs_widget.setEnabled(cartesian and self.fixed_crs.isChecked())
        self.summary.setText("KP will be measured as: " + describe_kp_mode(*self.values()))

    def _accept(self):
        if self.cartesian.isChecked() and self.fixed_crs.isChecked():
            crs = self.crs_widget.crs() if self.crs_widget is not None else None
            if crs is None or not crs.isValid() or crs.isGeographic():
                self.summary.setText("Choose a projected CRS (metres) for grid distances.")
                return
        self.accept()

    def values(self) -> Tuple[str, str]:
        mode = KP_MODE_CARTESIAN if self.cartesian.isChecked() else KP_MODE_GEODESIC
        return mode, (self._grid() if mode == KP_MODE_CARTESIAN else "")


def edit_global_kp_settings(parent=None) -> bool:
    """Plugin menu: edit the plugin-wide KP distance setting."""
    dialog = KpSettingsDialog(
        parent, note=(
            "Applies to the KP Mouse Tool, Depth Profile, KP Plotter, KP processing "
            "tools (their Distance mode default), the Planner and new Burial Planner "
            "plans. Existing burial plans keep their own setting (Burial Planner ▸ "
            "Inputs ▸ KP distance)."))
    if qt_exec(dialog):
        set_kp_distance_settings(*dialog.values())
        return True
    return False

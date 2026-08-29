# -*- coding: utf-8 -*-
"""Import a path file (``.pthmdb``) as a workbench RPL revision.

Path databases (.pthmdb) carry a real position list — KP, cable distance,
labels, per-segment bearing/cable type/burial — so unlike the bare
route-line import this module preserves the stated numbers and only derives
what the file leaves blank (via ``reconcile_model``, never ``recompute``).

Mapping:

* ``PathPoints`` rows become :class:`RplPoint` in ``Index`` order. The
  source ``Label`` becomes the RPL ``Event`` text, so the existing event
  rules classify branching units, joints, BMHs and transitions and the
  assembly extraction works unchanged. ``KP``/``CableDist`` (metres or
  kilometres, auto-detected by the reader) become the cumulative route and
  cable distances.
* ``PathLines`` rows become :class:`RplSegment` with the stated bearing,
  span distance and span cable distance; slack is left for
  ``reconcile_model`` to imply from route vs cable distance. ``CableType``
  and the ``Buried`` flag ride along as segment attributes.
* ``AssemblyPoints`` labels fill in the event of the nearest path point (by
  KP) when that point has no label of its own.
* A populated ``Profile`` table (a KP/depth series sampled from a
  bathymetry) is interpolated onto the path points as ``depth_m`` →
  ``ApproxDepth``, and summarised in the import audit.

The pure builder (:func:`model_from_path_data`) is separate from the dialog
for headless testing.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from ..processing.pthmdb_reader import (
    PathFileData,
    PathFileError,
    kp_to_km,
    read_path_file,
)
from . import schema
from .rpl_engine import RplModel, RplPoint, RplSegment
from .rpl_import_service import (
    CommitError,
    CommitRequest,
    commit_import,
    make_wgs84_distance_area,
    reconcile_model,
)

PATH_FILE_FILTER = (
    "Path files (*.pthmdb);;Access databases (*.mdb *.accdb);;"
    "All files (*.*)"
)

#: An AssemblyPoint label is applied to a path point at most this far away
#: along the route (matches the route-line event matching tolerance).
ASSEMBLY_MATCH_MAX_KM = 0.1

#: Cap on profile samples copied into the wb_meta import-audit blob.
_AUDIT_PROFILE_MAX_ROWS = 5000

_PLACEHOLDER_TEXT = {"", "undef", "n/a", "none"}


def _clean(value) -> str:
    text = "" if value is None else str(value).strip()
    return "" if text.lower() in _PLACEHOLDER_TEXT else text


def _row_get(row: Dict, name: str, default=None):
    for key, value in row.items():
        if str(key).upper() == name.upper():
            return value
    return default


def _number(value) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _interp_profile_depth(profile_km: List[Tuple[float, float]],
                          kp_km: Optional[float]) -> Optional[float]:
    """Linear interpolation on a KP-sorted (kp_km, depth) series, no
    extrapolation."""
    if kp_km is None or not profile_km:
        return None
    if kp_km < profile_km[0][0] or kp_km > profile_km[-1][0]:
        return None
    previous = profile_km[0]
    for station in profile_km:
        if station[0] >= kp_km:
            if station[0] == previous[0]:
                return station[1]
            frac = (kp_km - previous[0]) / (station[0] - previous[0])
            return previous[1] + frac * (station[1] - previous[1])
        previous = station
    return None


def model_from_path_data(data: PathFileData,
                         da=None) -> Tuple[RplModel, Dict, List[str]]:
    """Build ``(model, audit, warnings)`` from decoded path-file content.

    ``da`` is the :class:`QgsDistanceArea` used by ``reconcile_model`` to
    derive whatever the file left blank; pass ``None`` in headless tests to
    skip reconciliation.
    """
    warnings: List[str] = list(data.warnings)
    kp_unit = data.kp_unit
    if kp_unit is None:
        warnings.append(
            "KP unit could not be verified against the route geometry; "
            "cumulative distances are re-derived from the coordinates")

    def _km(value) -> Optional[float]:
        number = _number(value)
        if number is None or kp_unit is None:
            return None
        return kp_to_km(number, kp_unit)

    points: List[RplPoint] = []
    for i, row in enumerate(data.path_points):
        event = _clean(_row_get(row, "Label"))
        attrs: Dict = {}
        remarks = _clean(_row_get(row, "Comment"))
        if remarks:
            attrs["Remarks"] = remarks
        points.append(RplPoint(
            seq=i,
            pos_no=i + 1,
            event=event,
            lat=float(row["y"]),
            lon=float(row["x"]),
            dist_cum_km=_km(_row_get(row, "KP")),
            cable_dist_cum_km=_km(_row_get(row, "CableDist")),
            depth_m=None,
            attrs=attrs,
        ))
    if points and not points[0].event:
        points[0].event = "A End"
    if len(points) > 1 and not points[-1].event:
        points[-1].event = "B End"

    # Path files write Depth=0 for every point until a profile is attached;
    # only trust the column when the file actually carries depths.
    depths = [_number(_row_get(row, "Depth")) for row in data.path_points]
    if any(d for d in depths):
        for point, depth in zip(points, depths):
            point.depth_m = depth

    segments: List[RplSegment] = []
    if len(data.path_lines) == len(points) - 1:
        for i, row in enumerate(data.path_lines):
            attrs = {}
            cable_type = _clean(_row_get(row, "CableType"))
            if cable_type:
                attrs["CableType"] = cable_type
            buried = _clean(_row_get(row, "Buried"))
            if buried:
                attrs["Buried"] = buried
            segments.append(RplSegment(
                seq=i,
                bearing_deg=_number(_row_get(row, "Bearing")),
                dist_km=_km(_row_get(row, "dKP")),
                slack_pct=None,
                cable_dist_km=_km(_row_get(row, "SegCableDist")),
                attrs=attrs,
            ))
    else:
        if data.path_lines:
            warnings.append(
                f"PathLines count ({len(data.path_lines)}) does not match "
                f"PathPoints ({len(points)}); segment attributes dropped and "
                "segments re-derived from the points")
        segments = [RplSegment(seq=i) for i in range(max(0, len(points) - 1))]

    # AssemblyPoints: label otherwise-unlabelled path points so joints and
    # branching units survive as RPL events (and assembly extraction sees
    # them).
    assembly_audit: List[Dict] = []
    for row in data.assembly_points:
        label = _clean(_row_get(row, "Label")) or _clean(_row_get(row, "TypeID"))
        kp_km = _km(_row_get(row, "KP"))
        entry = {
            "label": label,
            "type": _clean(_row_get(row, "Type")),
            "kp_km": kp_km,
            "matched_pos_no": None,
        }
        if label and kp_km is not None:
            candidates = [
                (abs(point.dist_cum_km - kp_km), point)
                for point in points if point.dist_cum_km is not None]
            if candidates:
                distance, point = min(candidates, key=lambda pair: pair[0])
                if distance <= ASSEMBLY_MATCH_MAX_KM:
                    entry["matched_pos_no"] = point.pos_no
                    if not point.event:
                        point.event = label
                elif label:
                    warnings.append(
                        f"AssemblyPoint '{label}' at KP {kp_km:.3f} km is "
                        f"{distance:.3f} km from the nearest path point; "
                        "not applied")
        assembly_audit.append(entry)

    # Depth profile: interpolate onto the positions; keep the (capped)
    # series in the audit so nothing curated in the source file is lost silently.
    profile_audit: Optional[Dict] = None
    profile_km = []
    for row in data.profile:
        kp_km = _km(_row_get(row, "Kp"))
        depth = _number(_row_get(row, "Depth"))
        if kp_km is not None and depth is not None:
            profile_km.append((kp_km, depth))
    profile_km.sort(key=lambda pair: pair[0])
    if profile_km:
        applied = 0
        for point in points:
            if point.depth_m is None:
                depth = _interp_profile_depth(profile_km, point.dist_cum_km)
                if depth is not None:
                    point.depth_m = depth
                    applied += 1
        bathy_names = sorted({
            _clean(_row_get(row, "BathyName")) for row in data.profile
            if _clean(_row_get(row, "BathyName"))})
        profile_audit = {
            "sample_count": len(profile_km),
            "applied_to_points": applied,
            "bathy_names": bathy_names,
            "kp_depth_km": profile_km[:_AUDIT_PROFILE_MAX_ROWS],
        }
        if len(profile_km) > _AUDIT_PROFILE_MAX_ROWS:
            profile_audit["truncated"] = True
            warnings.append(
                f"depth profile has {len(profile_km)} samples; only the "
                f"first {_AUDIT_PROFILE_MAX_ROWS} are kept in the import "
                "audit")

    model = RplModel(points=points, segments=segments)

    audit: Dict = {
        "method": "pthmdb",
        "source_file": data.source_file,
        "point_count": len(points),
        "segment_count": len(segments),
        "kp_unit": kp_unit or "underived",
        "crs": data.crs_auth_id or "",
        "crs_note": data.crs_note,
        "assembly_points": assembly_audit,
    }
    if data.user_notes:
        audit["path_user_notes"] = data.user_notes
    if profile_audit is not None:
        audit["depth_profile"] = profile_audit
    if warnings:
        audit["warnings"] = list(warnings)

    if da is not None:
        report = reconcile_model(model, da, derive_missing=True)
        audit["derivation"] = report.to_dict()

    return model, audit, warnings


class PthmdbImportDialog(QDialog):
    """Pick a ``.pthmdb`` file + cable segment, register an RPL revision."""

    def __init__(self, store, parent=None, route_name: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Import path file")
        self.store = store
        self.rpl_id: Optional[str] = None
        self.extract_assembly = False
        self._data: Optional[PathFileData] = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Imports a path file (.pthmdb) as a new RPL revision "
            "of a cable segment. Positions, KP, cable distances, labels, "
            "cable types and burial flags are taken from the file; slack is "
            "implied from route vs cable distance."))
        form = QFormLayout()

        self.file_edit = QLineEdit()
        self.file_edit.setReadOnly(True)
        browse = QPushButton("Path file (.pthmdb)...")
        browse.clicked.connect(self._browse)
        form.addRow("Path file", self.file_edit)
        form.addRow("", browse)

        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        form.addRow("Contents", self.summary_label)

        self.route_combo = QComboBox()
        self.route_combo.setEditable(True)
        for route in self.store.list_routes() if self.store else []:
            self.route_combo.addItem(route.get("name") or "")
        self.route_combo.setEditText(route_name or "")
        self.route_combo.editTextChanged.connect(self._update_rev_default)
        form.addRow("Cable segment", self.route_combo)

        self.rev_edit = QLineEdit()
        form.addRow("Revision label", self.rev_edit)

        self.kind_combo = QComboBox()
        self.kind_combo.addItem("Planned", "planned")
        self.kind_combo.addItem("As-laid", "as_laid")
        form.addRow("RPL kind", self.kind_combo)

        self.extract_check = QCheckBox(
            "Extract an assembly from the imported events afterwards")
        form.addRow("", self.extract_check)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._commit)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._update_rev_default()

    # -- helpers ---------------------------------------------------------------
    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Path file (.pthmdb)", "", PATH_FILE_FILTER)
        if not path:
            return
        try:
            data = read_path_file(path)
        except PathFileError as exc:
            QMessageBox.warning(self, "Import path file", str(exc))
            return
        self._data = data
        self.file_edit.setText(path)
        self._show_summary(data)
        if not (self.route_combo.currentText() or "").strip():
            self.route_combo.setEditText(
                os.path.splitext(os.path.basename(path))[0])
            self._update_rev_default()

    def _show_summary(self, data: PathFileData):
        parts = [f"{len(data.path_points)} positions"]
        first = _clean(_row_get(data.path_points[0], "Label"))
        last = _clean(_row_get(data.path_points[-1], "Label"))
        if first or last:
            parts.append(f"{first or '?'} → {last or '?'}")
        kp_span = _number(_row_get(data.path_points[-1], "KP"))
        if kp_span is not None and data.kp_unit:
            parts.append(f"{kp_to_km(kp_span, data.kp_unit):.1f} km")
        parts.append(data.crs_auth_id or "CRS not detected")
        if data.profile:
            parts.append(f"depth profile ({len(data.profile)} samples)")
        if data.assembly_points:
            parts.append(f"{len(data.assembly_points)} assembly points")
        text = ", ".join(parts)
        if data.warnings:
            text += "\nWarnings: " + "; ".join(data.warnings)
        self.summary_label.setText(text)

    def _update_rev_default(self):
        if not self.store:
            return
        name = (self.route_combo.currentText() or "").strip().lower()
        route = next(
            (r for r in self.store.list_routes()
             if (r.get("name") or "").strip().lower() == name), None)
        if route:
            revisions = self.store.revisions_of_route(route["route_id"])
            self.rev_edit.setText(schema.next_rev_label(revisions))
        else:
            self.rev_edit.setText("Rev 1")

    # -- commit ----------------------------------------------------------------
    def _commit(self):
        if self._data is None:
            QMessageBox.information(
                self, "Import path file",
                "Browse to a .pthmdb path file first.")
            return
        route_name = (self.route_combo.currentText() or "").strip()
        if not route_name:
            QMessageBox.information(
                self, "Import path file",
                "Enter a cable segment name.")
            return
        if self._data.crs_auth_id != "EPSG:4326":
            answer = QMessageBox.question(
                self, "Import path file",
                "The file's coordinate system could not be confirmed as "
                f"WGS84 ({self._data.crs_note}). The workbench stores "
                "positions as WGS84 latitude/longitude.\n\n"
                "Import the coordinates as WGS84 degrees anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return

        try:
            da = make_wgs84_distance_area(None)
            model, audit, warnings = model_from_path_data(self._data, da=da)
        except (PathFileError, ValueError) as exc:
            QMessageBox.warning(self, "Import path file", str(exc))
            return
        if warnings:
            shown = "\n".join(warnings[:8])
            if len(warnings) > 8:
                shown += f"\n... and {len(warnings) - 8} more."
            answer = QMessageBox.question(
                self, "Import path file",
                f"The file imported with warnings:\n\n{shown}\n\n"
                "Register the revision anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            result = commit_import(self.store, model, CommitRequest(
                route_name=route_name,
                kind=self.kind_combo.currentData() or "planned",
                rev_label=self.rev_edit.text().strip(),
                source_file=self._data.source_file,
                audit=audit,
            ))
        except CommitError as exc:
            QMessageBox.warning(self, "Import path file", str(exc))
            return
        self.rpl_id = result.rpl_id
        self.extract_assembly = self.extract_check.isChecked()
        self.accept()

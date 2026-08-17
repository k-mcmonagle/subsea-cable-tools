# -*- coding: utf-8 -*-
"""Create an RPL revision from a plain route line (KML, SHP, GPX, GeoJSON...).

A received route often arrives as bare line geometry with no RPL attributes.
This module turns one line feature into a minimal but consistent RplModel —
one position per vertex, zero slack, distances/bearings/cumulatives computed
by the engine — and registers it through the same commit path as the file
import wizard, so revision labels, supersedes lineage, layer naming and
topology endpoints behave identically to every other import route.

The pure builders (:func:`vertices_lonlat`, :func:`model_from_lonlat`) are
kept separate from the dialog for headless testing.
"""

from __future__ import annotations

import os
from typing import List, Optional, Tuple

from qgis.PyQt.QtWidgets import (
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
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsDistanceArea,
    QgsProject,
    QgsVectorLayer,
)

from ..qgis_compat import GEOMETRY_LINE
from . import schema
from .rpl_engine import RplModel, RplPoint, RplSegment, SlackMode, recompute
from .rpl_import_service import CommitError, CommitRequest, commit_import

LINE_FILE_FILTER = (
    "Route line files (*.kml *.kmz *.gpx *.geojson *.json *.shp *.gpkg *.tab *.mif);;"
    "All files (*.*)"
)


class RouteLineError(ValueError):
    """User-facing problem with the chosen line source."""


def _pick_line_feature(layer: QgsVectorLayer):
    """The single line feature to convert: the selection if it is exactly one,
    else the layer's only line feature. Anything ambiguous is refused rather
    than guessed."""
    selected = list(layer.selectedFeatures())
    candidates = [f for f in selected if f.hasGeometry()]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise RouteLineError(
            "More than one feature is selected on the layer. "
            "Select exactly the route line to import.")
    features = [f for f in layer.getFeatures() if f.hasGeometry()]
    if not features:
        raise RouteLineError("The layer has no line features.")
    if len(features) > 1:
        raise RouteLineError(
            f"The layer has {len(features)} line features. "
            "Select the one route line on the layer (or split the file) and retry.")
    return features[0]


def vertices_lonlat(layer: QgsVectorLayer,
                    transform_context=None) -> List[Tuple[float, float]]:
    """(lon, lat) vertices of the layer's single/selected line feature, WGS84.

    Refuses multi-part geometries with more than one part and lines with
    fewer than two distinct vertices; never resamples or simplifies.
    """
    feature = _pick_line_feature(layer)
    geometry = feature.geometry()
    parts = geometry.asMultiPolyline() if geometry.isMultipart() else [geometry.asPolyline()]
    parts = [p for p in parts if len(p) >= 2]
    if not parts:
        raise RouteLineError("The feature's geometry is not a usable line.")
    if len(parts) > 1:
        raise RouteLineError(
            "The route line is multi-part (broken into "
            f"{len(parts)} pieces). Merge it into one continuous line first.")
    points = parts[0]

    crs = layer.crs()
    wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    if crs.isValid() and crs != wgs84:
        if transform_context is None:
            transform_context = QgsProject.instance().transformContext()
        transform = QgsCoordinateTransform(crs, wgs84, transform_context)
        points = [transform.transform(p) for p in points]

    coords: List[Tuple[float, float]] = []
    for p in points:
        lonlat = (float(p.x()), float(p.y()))
        if not coords or coords[-1] != lonlat:
            coords.append(lonlat)
    if len(coords) < 2:
        raise RouteLineError("The line needs at least two distinct vertices.")
    return coords


def model_from_lonlat(coords: List[Tuple[float, float]],
                      da: Optional[QgsDistanceArea] = None) -> RplModel:
    """A minimal consistent RplModel: one position per vertex, zero slack.

    Distances, bearings and cumulatives come from the engine's recompute so
    they match every other tool; the route ends are labelled so derived RPL
    sections read naturally.
    """
    if da is None:
        da = QgsDistanceArea()
        da.setEllipsoid("WGS84")
    last = len(coords) - 1
    points = [
        RplPoint(
            seq=i, pos_no=i + 1,
            event="A End" if i == 0 else ("B End" if i == last else ""),
            lat=lat, lon=lon,
        )
        for i, (lon, lat) in enumerate(coords)
    ]
    segments = [RplSegment(seq=i, slack_pct=0.0) for i in range(last)]
    model = RplModel(points=points, segments=segments)
    recompute(model, da, slack_mode=SlackMode.HOLD_SLACK)
    return model


def load_line_file(path: str) -> QgsVectorLayer:
    """Open a route-line file (KML etc.) as a temporary OGR layer."""
    layer = QgsVectorLayer(path, os.path.basename(path), "ogr")
    if not layer.isValid():
        raise RouteLineError(f"Could not open '{path}' as a vector layer.")
    if layer.geometryType() != GEOMETRY_LINE:
        raise RouteLineError(
            f"'{os.path.basename(path)}' does not contain line geometry.")
    return layer


class RplFromLineDialog(QDialog):
    """Pick a line source + cable segment, register the result as a revision."""

    def __init__(self, store, parent=None, route_name: str = ""):
        super().__init__(parent)
        self.setWindowTitle("New RPL from route line")
        self.store = store
        self.rpl_id: Optional[str] = None
        self._file_layer: Optional[QgsVectorLayer] = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Registers a bare route line (a KML, or any line layer) as a new "
            "RPL revision of a cable segment. One position per vertex, zero "
            "slack; distances and bearings are computed."))
        form = QFormLayout()

        self.layer_combo = QComboBox()
        self._reload_layers()
        browse = QPushButton("Route line file (KML...)...")
        browse.clicked.connect(self._browse)
        form.addRow("Line layer", self.layer_combo)
        form.addRow("", browse)

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
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._commit)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._update_rev_default()

    # -- helpers ---------------------------------------------------------------
    def _reload_layers(self):
        self.layer_combo.clear()
        for layer in QgsProject.instance().mapLayers().values():
            if isinstance(layer, QgsVectorLayer) \
                    and layer.geometryType() == GEOMETRY_LINE:
                self.layer_combo.addItem(layer.name(), layer.id())

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Route line file", "", LINE_FILE_FILTER)
        if not path:
            return
        try:
            self._file_layer = load_line_file(path)
        except RouteLineError as exc:
            QMessageBox.warning(self, "Route line file", str(exc))
            return
        label = f"[file] {os.path.basename(path)}"
        index = self.layer_combo.findText(label)
        if index < 0:
            self.layer_combo.addItem(label, "__file__")
            index = self.layer_combo.count() - 1
        self.layer_combo.setCurrentIndex(index)

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

    def _current_layer(self) -> Optional[QgsVectorLayer]:
        if self.layer_combo.currentData() == "__file__":
            return self._file_layer
        layer_id = self.layer_combo.currentData()
        layer = QgsProject.instance().mapLayer(layer_id) if layer_id else None
        return layer if isinstance(layer, QgsVectorLayer) else None

    # -- commit ----------------------------------------------------------------
    def _commit(self):
        layer = self._current_layer()
        if layer is None:
            QMessageBox.information(
                self, "New RPL from route line",
                "Choose a line layer or browse to a route line file.")
            return
        route_name = (self.route_combo.currentText() or "").strip()
        if not route_name:
            QMessageBox.information(
                self, "New RPL from route line", "Enter a cable segment name.")
            return
        try:
            coords = vertices_lonlat(layer)
            model = model_from_lonlat(coords)
            source = layer.source().split("|")[0]
            result = commit_import(self.store, model, CommitRequest(
                route_name=route_name,
                kind=self.kind_combo.currentData() or "planned",
                rev_label=self.rev_edit.text().strip(),
                source_file=source,
                audit={"method": "route_line", "source_layer": layer.name(),
                       "vertex_count": len(coords)},
            ))
        except (RouteLineError, CommitError) as exc:
            QMessageBox.warning(self, "New RPL from route line", str(exc))
            return
        self.rpl_id = result.rpl_id
        self.accept()

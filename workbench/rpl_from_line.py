# -*- coding: utf-8 -*-
"""Create an RPL revision from a plain route line or point sequence.

A received route often arrives as bare geometry with few or no RPL
attributes — a KML route line, a point export with only an event-label
column, or a SHP/GPX/GeoJSON line. This module turns one line feature (or
an ordered point layer) into a minimal but consistent RplModel — one
position per vertex, zero slack, distances/bearings/cumulatives computed
by the engine — and registers it through the same commit path as the file
import wizard, so revision labels, supersedes lineage, layer naming and
topology endpoints behave identically to every other import route.

Event labels can be carried in from the source itself (a point source with
a label field) or from a separate point layer whose features are matched
to the nearest route vertex. Everything else in the standard RPL schema is
created blank for the user to populate later in the RPL Manager.

The pure builders (:func:`vertices_lonlat`, :func:`route_from_points`,
:func:`match_events_to_vertices`, :func:`model_from_lonlat`) are kept
separate from the dialog for headless testing.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

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
    QgsPointXY,
    QgsProject,
    QgsVectorLayer,
)

from ..qgis_compat import GEOMETRY_LINE, GEOMETRY_POINT
from . import schema
from .rpl_engine import RplModel, RplPoint, RplSegment, SlackMode, recompute
from .rpl_import_service import CommitError, CommitRequest, commit_import

LINE_FILE_FILTER = (
    "Route files (*.kml *.kmz *.gpx *.geojson *.json *.shp *.gpkg *.tab *.mif *.csv);;"
    "All files (*.*)"
)

# Events farther than this from every route vertex are reported, not guessed.
EVENT_MATCH_MAX_M = 100.0

_LIKELY_LABEL_FIELDS = ("event", "label", "name", "description", "remarks")


class RouteLineError(ValueError):
    """User-facing problem with the chosen route source."""


def _wgs84_transform(layer: QgsVectorLayer, transform_context=None):
    crs = layer.crs()
    wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    if not crs.isValid() or crs == wgs84:
        return None
    if transform_context is None:
        transform_context = QgsProject.instance().transformContext()
    return QgsCoordinateTransform(crs, wgs84, transform_context)


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

    transform = _wgs84_transform(layer, transform_context)
    if transform is not None:
        points = [transform.transform(p) for p in points]

    coords: List[Tuple[float, float]] = []
    for p in points:
        lonlat = (float(p.x()), float(p.y()))
        if not coords or coords[-1] != lonlat:
            coords.append(lonlat)
    if len(coords) < 2:
        raise RouteLineError("The line needs at least two distinct vertices.")
    return coords


def point_records(layer: QgsVectorLayer, label_field: Optional[str] = None,
                  transform_context=None) -> List[Tuple[float, float, str]]:
    """(lon, lat, label) for each point feature in layer order, WGS84.

    Multi-point features contribute each of their parts in order. Features
    with no geometry are skipped. ``label`` is the stripped text of
    ``label_field`` ("" when the field is unset or empty).
    """
    transform = _wgs84_transform(layer, transform_context)
    # lookupField falls back to a case-insensitive match ("name" vs "Name")
    field_index = layer.fields().lookupField(label_field) if label_field else -1
    records: List[Tuple[float, float, str]] = []
    for feature in layer.getFeatures():
        if not feature.hasGeometry():
            continue
        label = ""
        if field_index >= 0:
            value = feature.attribute(field_index)
            label = "" if value is None else str(value).strip()
        geometry = feature.geometry()
        parts = geometry.asMultiPoint() if geometry.isMultipart() \
            else [geometry.asPoint()]
        for p in parts:
            if transform is not None:
                p = transform.transform(p)
            records.append((float(p.x()), float(p.y()), label))
    return records


def route_from_points(layer: QgsVectorLayer, label_field: Optional[str] = None,
                      transform_context=None,
                      ) -> Tuple[List[Tuple[float, float]], Dict[int, str]]:
    """Route vertices (and any event labels) from an ordered point layer.

    Points are taken in layer order — the order they appear in the file.
    Consecutive duplicate positions collapse (a label on the duplicate is
    kept if the retained position has none).
    """
    coords: List[Tuple[float, float]] = []
    events: Dict[int, str] = {}
    for lon, lat, label in point_records(layer, label_field, transform_context):
        if coords and coords[-1] == (lon, lat):
            if label and not events.get(len(coords) - 1):
                events[len(coords) - 1] = label
            continue
        coords.append((lon, lat))
        if label:
            events[len(coords) - 1] = label
    if len(coords) < 2:
        raise RouteLineError(
            "The point layer needs at least two distinct positions.")
    return coords, events


def match_events_to_vertices(coords: List[Tuple[float, float]],
                             events: List[Tuple[float, float, str]],
                             da: Optional[QgsDistanceArea] = None,
                             max_offset_m: float = EVENT_MATCH_MAX_M,
                             ) -> Tuple[Dict[int, str], List[str]]:
    """Assign labelled event points to their nearest route vertex.

    Returns ``(vertex index -> label, warnings)``. An event farther than
    ``max_offset_m`` from every vertex is skipped with a warning rather
    than snapped to a vertex it does not belong to; when two events claim
    the same vertex the closer one wins and the other is reported.
    """
    if da is None:
        da = QgsDistanceArea()
        da.setEllipsoid("WGS84")
    assigned: Dict[int, Tuple[str, float]] = {}
    warnings: List[str] = []
    vertex_points = [QgsPointXY(lon, lat) for lon, lat in coords]
    for lon, lat, label in events:
        if not label:
            continue
        event_point = QgsPointXY(lon, lat)
        best_index, best_dist = -1, float("inf")
        for index, vertex in enumerate(vertex_points):
            dist = da.measureLine(event_point, vertex)
            if dist < best_dist:
                best_index, best_dist = index, dist
        if best_index < 0 or best_dist > max_offset_m:
            warnings.append(
                f"'{label}' is {best_dist:.0f} m from the nearest route "
                "vertex — not assigned.")
            continue
        current = assigned.get(best_index)
        if current is not None and current[1] <= best_dist:
            warnings.append(
                f"'{label}' and '{current[0]}' both map to position "
                f"{best_index + 1}; kept the closer '{current[0]}'.")
            continue
        if current is not None:
            warnings.append(
                f"'{current[0]}' and '{label}' both map to position "
                f"{best_index + 1}; kept the closer '{label}'.")
        assigned[best_index] = (label, best_dist)
    return {index: label for index, (label, _dist) in assigned.items()}, warnings


def model_from_lonlat(coords: List[Tuple[float, float]],
                      da: Optional[QgsDistanceArea] = None,
                      events: Optional[Dict[int, str]] = None) -> RplModel:
    """A minimal consistent RplModel: one position per vertex, zero slack.

    Distances, bearings and cumulatives come from the engine's recompute so
    they match every other tool. ``events`` (vertex index -> label) fills
    the Event column; ends without a supplied label read "A End"/"B End" so
    derived RPL sections still read naturally.
    """
    if da is None:
        da = QgsDistanceArea()
        da.setEllipsoid("WGS84")
    events = events or {}
    last = len(coords) - 1

    def _event(index: int) -> str:
        label = events.get(index) or ""
        if label:
            return label
        if index == 0:
            return "A End"
        if index == last:
            return "B End"
        return ""

    points = [
        RplPoint(
            seq=i, pos_no=i + 1,
            event=_event(i),
            lat=lat, lon=lon,
        )
        for i, (lon, lat) in enumerate(coords)
    ]
    segments = [RplSegment(seq=i, slack_pct=0.0) for i in range(last)]
    model = RplModel(points=points, segments=segments)
    recompute(model, da, slack_mode=SlackMode.HOLD_SLACK)
    return model


def _try_ogr_layer(uri: str, name: str, geometry_type) -> Optional[QgsVectorLayer]:
    layer = QgsVectorLayer(uri, name, "ogr")
    if not layer.isValid() or layer.geometryType() != geometry_type:
        return None
    try:
        if layer.featureCount() == 0:
            return None
    except Exception:
        pass
    return layer


def load_route_file(path: str) -> Tuple[Optional[QgsVectorLayer],
                                        Optional[QgsVectorLayer]]:
    """Open a route file as ``(line layer, point layer)`` — either may be None.

    Mixed-geometry files (a KML with the route line plus event placemarks)
    yield both, via OGR's geometry-type filter; single-geometry files yield
    whichever half they contain.
    """
    base = os.path.basename(path)
    line = _try_ogr_layer(f"{path}|geometrytype=LineString", base, GEOMETRY_LINE)
    points = _try_ogr_layer(f"{path}|geometrytype=Point", base, GEOMETRY_POINT)
    if line is None and points is None:
        direct = QgsVectorLayer(path, base, "ogr")
        if not direct.isValid():
            raise RouteLineError(f"Could not open '{path}' as a vector layer.")
        if direct.geometryType() == GEOMETRY_LINE:
            line = direct
        elif direct.geometryType() == GEOMETRY_POINT:
            points = direct
        else:
            raise RouteLineError(
                f"'{base}' contains neither line nor point geometry.")
    return line, points


def load_line_file(path: str) -> QgsVectorLayer:
    """Open a route-line file (KML etc.) as a temporary OGR layer."""
    line, _points = load_route_file(path)
    if line is None:
        raise RouteLineError(
            f"'{os.path.basename(path)}' does not contain line geometry.")
    return line


def _likely_label_field(layer: QgsVectorLayer) -> str:
    names = [field.name() for field in layer.fields()]
    for wanted in _LIKELY_LABEL_FIELDS:
        for name in names:
            if wanted in name.lower():
                return name
    return ""


class RplFromLineDialog(QDialog):
    """Pick a route source + cable segment, register the result as a revision."""

    _FILE_ROUTE = "__file__"
    _FILE_EVENTS = "__file_points__"

    def __init__(self, store, parent=None, route_name: str = ""):
        super().__init__(parent)
        self.setWindowTitle("New RPL from route line")
        self.store = store
        self.rpl_id: Optional[str] = None
        self._file_layer: Optional[QgsVectorLayer] = None
        self._file_events_layer: Optional[QgsVectorLayer] = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Registers a bare route (a KML, any line layer, or an ordered "
            "point layer) as a new RPL revision of a cable segment. One "
            "position per vertex, zero slack; distances and bearings are "
            "computed and the other RPL fields are created blank to fill in "
            "later. Event labels can be taken from a point layer's field."))
        form = QFormLayout()

        self.layer_combo = QComboBox()
        self.layer_combo.currentIndexChanged.connect(self._source_changed)
        browse = QPushButton("Route file (KML...)...")
        browse.clicked.connect(self._browse)
        form.addRow("Route source", self.layer_combo)
        form.addRow("", browse)

        self.events_combo = QComboBox()
        self.events_combo.currentIndexChanged.connect(self._events_layer_changed)
        form.addRow("Event labels from", self.events_combo)
        self.label_field_combo = QComboBox()
        form.addRow("Label field", self.label_field_combo)

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
        self._reload_layers()
        self._update_rev_default()

    # -- helpers ---------------------------------------------------------------
    def _reload_layers(self):
        self.layer_combo.blockSignals(True)
        self.layer_combo.clear()
        for layer in QgsProject.instance().mapLayers().values():
            if not isinstance(layer, QgsVectorLayer):
                continue
            if layer.geometryType() == GEOMETRY_LINE:
                self.layer_combo.addItem(layer.name(), layer.id())
            elif layer.geometryType() == GEOMETRY_POINT:
                self.layer_combo.addItem(f"{layer.name()} [points]", layer.id())
        self.layer_combo.blockSignals(False)
        self._reload_events_layers()
        self._source_changed()

    def _reload_events_layers(self):
        previous = self.events_combo.currentData()
        self.events_combo.blockSignals(True)
        self.events_combo.clear()
        self.events_combo.addItem("(no event labels)", "")
        for layer in QgsProject.instance().mapLayers().values():
            if isinstance(layer, QgsVectorLayer) \
                    and layer.geometryType() == GEOMETRY_POINT:
                self.events_combo.addItem(layer.name(), layer.id())
        if self._file_events_layer is not None:
            self.events_combo.addItem(
                f"[file] {self._file_events_layer.name()} points",
                self._FILE_EVENTS)
        index = self.events_combo.findData(previous) if previous else -1
        self.events_combo.setCurrentIndex(index if index >= 0 else 0)
        self.events_combo.blockSignals(False)
        self._events_layer_changed()

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Route file", "", LINE_FILE_FILTER)
        if not path:
            return
        try:
            line, points = load_route_file(path)
        except RouteLineError as exc:
            QMessageBox.warning(self, "Route file", str(exc))
            return
        self._file_layer = line if line is not None else points
        self._file_events_layer = points if line is not None else None
        suffix = "" if line is not None else " [points]"
        label = f"[file] {os.path.basename(path)}{suffix}"
        index = self.layer_combo.findText(label)
        if index < 0:
            self.layer_combo.addItem(label, self._FILE_ROUTE)
            index = self.layer_combo.count() - 1
        self._reload_events_layers()
        self.layer_combo.setCurrentIndex(index)
        if self._file_events_layer is not None:
            file_index = self.events_combo.findData(self._FILE_EVENTS)
            if file_index >= 0:
                self.events_combo.setCurrentIndex(file_index)

    def _source_changed(self, _index=None):
        # A point source carries its own labels; offer it as the default
        # events source so its label field is one click away.
        layer = self._current_layer()
        if layer is not None and layer.geometryType() == GEOMETRY_POINT \
                and self.events_combo.currentIndex() <= 0:
            data = (self._FILE_ROUTE if self.layer_combo.currentData() == self._FILE_ROUTE
                    else layer.id())
            if data == self._FILE_ROUTE:
                # browsed point file: route source doubles as events source
                index = self.events_combo.findData(self._FILE_EVENTS)
                if index < 0:
                    self.events_combo.addItem(
                        f"[file] {layer.name()} points", self._FILE_EVENTS)
                    self._file_events_layer = layer
                    index = self.events_combo.count() - 1
                self.events_combo.setCurrentIndex(index)
            else:
                index = self.events_combo.findData(data)
                if index >= 0:
                    self.events_combo.setCurrentIndex(index)

    def _events_layer_changed(self, _index=None):
        layer = self._current_events_layer()
        self.label_field_combo.clear()
        self.label_field_combo.addItem("(none)", "")
        if layer is None:
            return
        for field in layer.fields():
            self.label_field_combo.addItem(field.name(), field.name())
        likely = _likely_label_field(layer)
        if likely:
            index = self.label_field_combo.findData(likely)
            if index >= 0:
                self.label_field_combo.setCurrentIndex(index)

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
        if self.layer_combo.currentData() == self._FILE_ROUTE:
            return self._file_layer
        layer_id = self.layer_combo.currentData()
        layer = QgsProject.instance().mapLayer(layer_id) if layer_id else None
        return layer if isinstance(layer, QgsVectorLayer) else None

    def _current_events_layer(self) -> Optional[QgsVectorLayer]:
        data = self.events_combo.currentData()
        if not data:
            return None
        if data == self._FILE_EVENTS:
            return self._file_events_layer
        layer = QgsProject.instance().mapLayer(data)
        return layer if isinstance(layer, QgsVectorLayer) else None

    # -- commit ----------------------------------------------------------------
    def _commit(self):
        layer = self._current_layer()
        if layer is None:
            QMessageBox.information(
                self, "New RPL from route line",
                "Choose a route layer or browse to a route file.")
            return
        route_name = (self.route_combo.currentText() or "").strip()
        if not route_name:
            QMessageBox.information(
                self, "New RPL from route line", "Enter a cable segment name.")
            return
        events_layer = self._current_events_layer()
        label_field = self.label_field_combo.currentData() or ""
        try:
            same_source = events_layer is not None and (
                events_layer is layer or events_layer.id() == layer.id())
            if layer.geometryType() == GEOMETRY_POINT:
                coords, events = route_from_points(
                    layer, label_field if same_source else None)
            else:
                coords = vertices_lonlat(layer)
                events = {}
            warnings: List[str] = []
            if events_layer is not None and not same_source and label_field:
                records = [r for r in point_records(events_layer, label_field)
                           if r[2]]
                matched, warnings = match_events_to_vertices(coords, records)
                # matched labels win over any labels the source carried
                events.update(matched)
        except RouteLineError as exc:
            QMessageBox.warning(self, "New RPL from route line", str(exc))
            return
        if warnings:
            shown = "\n".join(warnings[:8])
            if len(warnings) > 8:
                shown += f"\n... and {len(warnings) - 8} more."
            answer = QMessageBox.question(
                self, "Event label matching",
                "Some event labels could not be assigned cleanly:\n\n"
                f"{shown}\n\nRegister the revision anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            model = model_from_lonlat(coords, events=events)
            source = layer.source().split("|")[0]
            method = ("route_points"
                      if layer.geometryType() == GEOMETRY_POINT else "route_line")
            audit = {"method": method, "source_layer": layer.name(),
                     "vertex_count": len(coords)}
            if events:
                audit["event_label_count"] = len(events)
            if events_layer is not None and label_field:
                audit["event_source_layer"] = events_layer.name()
                audit["event_label_field"] = label_field
            result = commit_import(self.store, model, CommitRequest(
                route_name=route_name,
                kind=self.kind_combo.currentData() or "planned",
                rev_label=self.rev_edit.text().strip(),
                source_file=source,
                audit=audit,
            ))
        except (RouteLineError, CommitError) as exc:
            QMessageBox.warning(self, "New RPL from route line", str(exc))
            return
        self.rpl_id = result.rpl_id
        self.accept()

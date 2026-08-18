# -*- coding: utf-8 -*-
"""RplModel <-> QgsVectorLayer synchronisation with lockstep undo.

An RPL is stored as a pair of GeoPackage layers (points + lines). This module
loads that pair into an in-memory rpl_engine.RplModel and writes engine
ChangeSets back through the layers' *edit buffers* (never the data provider
directly), so every engine operation becomes exactly one undo command on each
layer. The workbench UI then drives both layers' undo stacks in lockstep.

Field mapping follows workbench.schema.RPL_POINT_FIELDS / RPL_LINE_FIELDS;
any extra attributes ride along in the model's ``attrs`` dicts untouched.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsVectorLayer

from .rpl_engine import ChangeSet, RplModel, RplPoint, RplSegment

# Point-layer fields the engine owns (recomputed); everything else is attrs.
_POINT_ENGINE_FIELDS = {
    "rpl_id", "SeqNo", "PosNo", "Event", "DistCumulative", "CableDistCumulative",
    "ApproxDepth", "Latitude", "Longitude",
}
_LINE_ENGINE_FIELDS = {
    "rpl_id", "SeqNo", "FromPos", "ToPos", "Bearing", "DistBetweenPos",
    "Slack", "CableDistBetweenPos",
}


def _attr(feature: QgsFeature, name: str):
    try:
        value = feature[name]
    except KeyError:
        return None
    if value is None:
        return None
    if type(value).__name__ == "QVariant":
        return None if not value.isValid() or value.isNull() else value.value()
    return value


def _float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _layer_alive(layer) -> bool:
    """True if the QgsVectorLayer's C++ object still exists.

    Removing a layer from the project (or closing the project) deletes the
    underlying C++ object while Python references linger; touching one then
    raises ``RuntimeError: wrapped C/C++ object ... has been deleted``.
    """
    if layer is None:
        return False
    try:
        from qgis.PyQt import sip
        if sip.isdeleted(layer):
            return False
    except ImportError:
        pass
    try:
        layer.id()
    except RuntimeError:
        return False
    return True


class RplLayerSync:
    """Binds one RPL's points + lines layers to an in-memory model."""

    def __init__(self, points_layer: QgsVectorLayer, lines_layer: QgsVectorLayer, rpl_id: str = ""):
        self.points_layer = points_layer
        self.lines_layer = lines_layer
        self.rpl_id = rpl_id
        self._point_fids: List[int] = []   # index = point seq
        self._line_fids: List[int] = []    # index = segment seq

    # -- load -----------------------------------------------------------------
    def load_model(self) -> RplModel:
        point_rows = []
        for feature in self.points_layer.getFeatures():
            geom = feature.geometry()
            if geom is None or geom.isEmpty():
                continue
            pt = geom.asPoint()
            seq = _int(_attr(feature, "SeqNo"))
            attrs = {
                f.name(): _attr(feature, f.name())
                for f in self.points_layer.fields()
                if f.name() not in _POINT_ENGINE_FIELDS and f.name().lower() != "fid"
            }
            point_rows.append((
                seq if seq is not None else len(point_rows),
                feature.id(),
                RplPoint(
                    seq=0,
                    pos_no=_int(_attr(feature, "PosNo")),
                    event=str(_attr(feature, "Event") or ""),
                    lat=float(pt.y()),
                    lon=float(pt.x()),
                    dist_cum_km=_float(_attr(feature, "DistCumulative")),
                    cable_dist_cum_km=_float(_attr(feature, "CableDistCumulative")),
                    depth_m=_float(_attr(feature, "ApproxDepth")),
                    attrs=attrs,
                ),
            ))
        point_rows.sort(key=lambda r: r[0])

        line_rows = []
        for feature in self.lines_layer.getFeatures():
            seq = _int(_attr(feature, "SeqNo"))
            attrs = {
                f.name(): _attr(feature, f.name())
                for f in self.lines_layer.fields()
                if f.name() not in _LINE_ENGINE_FIELDS and f.name().lower() != "fid"
            }
            line_rows.append((
                seq if seq is not None else len(line_rows),
                feature.id(),
                RplSegment(
                    seq=0,
                    bearing_deg=_float(_attr(feature, "Bearing")),
                    dist_km=_float(_attr(feature, "DistBetweenPos")),
                    slack_pct=_float(_attr(feature, "Slack")),
                    cable_dist_km=_float(_attr(feature, "CableDistBetweenPos")),
                    attrs=attrs,
                ),
            ))
        line_rows.sort(key=lambda r: r[0])

        points = [row[2] for row in point_rows]
        segments = [row[2] for row in line_rows]
        for i, point in enumerate(points):
            point.seq = i
        for i, seg in enumerate(segments):
            seg.seq = i
        self._point_fids = [row[1] for row in point_rows]
        self._line_fids = [row[1] for row in line_rows]
        return RplModel(points=points, segments=segments)

    # -- feature lookup ---------------------------------------------------------
    def point_fids(self, indices) -> List[int]:
        """Layer feature ids for the given model point indices (order kept)."""
        return [self._point_fids[i] for i in indices
                if 0 <= i < len(self._point_fids)]

    def line_fids(self, indices) -> List[int]:
        """Layer feature ids for the given model segment indices (order kept)."""
        return [self._line_fids[i] for i in indices
                if 0 <= i < len(self._line_fids)]

    # -- edit session -----------------------------------------------------------
    def is_valid(self) -> bool:
        """Both bound layers still exist as C++ objects."""
        return _layer_alive(self.points_layer) and _layer_alive(self.lines_layer)

    def begin_session(self) -> None:
        for layer in (self.points_layer, self.lines_layer):
            if _layer_alive(layer) and not layer.isEditable():
                layer.startEditing()

    def is_dirty(self) -> bool:
        return any(
            _layer_alive(layer) and layer.isEditable() and layer.isModified()
            for layer in (self.points_layer, self.lines_layer)
        )

    def commit(self) -> bool:
        ok = True
        for layer in (self.points_layer, self.lines_layer):
            if _layer_alive(layer) and layer.isEditable():
                ok = layer.commitChanges() and ok
        return ok

    def rollback(self) -> None:
        for layer in (self.points_layer, self.lines_layer):
            if _layer_alive(layer) and layer.isEditable():
                layer.rollBack()

    def undo(self) -> None:
        if not self.is_valid():
            return
        for layer in (self.points_layer, self.lines_layer):
            stack = layer.undoStack()
            if stack is not None and stack.canUndo():
                stack.undo()
        for layer in (self.points_layer, self.lines_layer):
            layer.triggerRepaint()

    def redo(self) -> None:
        if not self.is_valid():
            return
        for layer in (self.points_layer, self.lines_layer):
            stack = layer.undoStack()
            if stack is not None and stack.canRedo():
                stack.redo()
        for layer in (self.points_layer, self.lines_layer):
            layer.triggerRepaint()

    def can_undo(self) -> bool:
        if not _layer_alive(self.points_layer):
            return False
        stack = self.points_layer.undoStack()
        return stack is not None and stack.canUndo()

    def can_redo(self) -> bool:
        if not _layer_alive(self.points_layer):
            return False
        stack = self.points_layer.undoStack()
        return stack is not None and stack.canRedo()

    # -- apply --------------------------------------------------------------------
    def apply(self, model: RplModel, changeset: ChangeSet, label: str = "") -> None:
        """Write a ChangeSet to both layers as one edit command each."""
        label = label or changeset.label or "Edit RPL"
        self.begin_session()
        self.points_layer.beginEditCommand(label)
        self.lines_layer.beginEditCommand(label)
        try:
            if changeset.structural or len(self._point_fids) != len(model.points):
                self._rewrite_all(model)
            else:
                self._patch(model, changeset)
        except Exception:
            self.points_layer.destroyEditCommand()
            self.lines_layer.destroyEditCommand()
            raise
        self.points_layer.endEditCommand()
        self.lines_layer.endEditCommand()
        for layer in (self.points_layer, self.lines_layer):
            layer.triggerRepaint()

    # -- write helpers --------------------------------------------------------------
    def _point_attr_map(self, model: RplModel, idx: int) -> Dict[str, object]:
        point = model.points[idx]
        values = {
            "rpl_id": self.rpl_id,
            "SeqNo": idx,
            "PosNo": point.pos_no,
            "Event": point.event,
            "DistCumulative": point.dist_cum_km,
            "CableDistCumulative": point.cable_dist_cum_km,
            "ApproxDepth": point.depth_m,
            "Latitude": point.lat,
            "Longitude": point.lon,
        }
        values.update(point.attrs)
        return values

    def _line_attr_map(self, model: RplModel, idx: int) -> Dict[str, object]:
        seg = model.segments[idx]
        a, b = model.points[idx], model.points[idx + 1]
        values = {
            "rpl_id": self.rpl_id,
            "SeqNo": idx,
            "FromPos": a.pos_no,
            "ToPos": b.pos_no,
            "Bearing": seg.bearing_deg,
            "DistBetweenPos": seg.dist_km,
            "Slack": seg.slack_pct,
            "CableDistBetweenPos": seg.cable_dist_km,
        }
        values.update(seg.attrs)
        return values

    def _point_geometry(self, model: RplModel, idx: int) -> QgsGeometry:
        point = model.points[idx]
        return QgsGeometry.fromPointXY(QgsPointXY(point.lon, point.lat))

    def _line_geometry(self, model: RplModel, idx: int) -> QgsGeometry:
        a, b = model.points[idx], model.points[idx + 1]
        return QgsGeometry.fromPolylineXY([QgsPointXY(a.lon, a.lat), QgsPointXY(b.lon, b.lat)])

    def _set_attrs(self, layer: QgsVectorLayer, fid: int, values: Dict[str, object]) -> None:
        fields = layer.fields()
        for name, value in values.items():
            field_idx = fields.indexOf(name)
            if field_idx >= 0:
                layer.changeAttributeValue(fid, field_idx, value)

    def _patch(self, model: RplModel, changeset: ChangeSet) -> None:
        for idx in sorted(changeset.point_indices):
            if not (0 <= idx < len(self._point_fids)):
                continue
            fid = self._point_fids[idx]
            self.points_layer.changeGeometry(fid, self._point_geometry(model, idx))
            self._set_attrs(self.points_layer, fid, self._point_attr_map(model, idx))
        for idx in sorted(changeset.segment_indices):
            if not (0 <= idx < len(self._line_fids)):
                continue
            fid = self._line_fids[idx]
            self.lines_layer.changeGeometry(fid, self._line_geometry(model, idx))
            self._set_attrs(self.lines_layer, fid, self._line_attr_map(model, idx))

    def _rewrite_all(self, model: RplModel) -> None:
        """Structural change: replace every feature (still one undo command)."""
        self.points_layer.deleteFeatures(list(self._point_fids))
        self.lines_layer.deleteFeatures(list(self._line_fids))

        new_point_fids: List[int] = []
        point_fields = self.points_layer.fields()
        for idx in range(len(model.points)):
            feature = QgsFeature(point_fields)
            feature.setGeometry(self._point_geometry(model, idx))
            for name, value in self._point_attr_map(model, idx).items():
                field_idx = point_fields.indexOf(name)
                if field_idx >= 0:
                    feature.setAttribute(field_idx, value)
            self.points_layer.addFeature(feature)
            new_point_fids.append(feature.id())

        new_line_fids: List[int] = []
        line_fields = self.lines_layer.fields()
        for idx in range(len(model.segments)):
            feature = QgsFeature(line_fields)
            feature.setGeometry(self._line_geometry(model, idx))
            for name, value in self._line_attr_map(model, idx).items():
                field_idx = line_fields.indexOf(name)
                if field_idx >= 0:
                    feature.setAttribute(field_idx, value)
            self.lines_layer.addFeature(feature)
            new_line_fids.append(feature.id())

        self._point_fids = new_point_fids
        self._line_fids = new_line_fids


def model_rows_for_layers(model: RplModel, rpl_id: str, source_file: str = "") -> Dict[str, List[Dict]]:
    """Row dicts (with WKT geometry) for creating the gpkg layers from a model.

    Used at registration time (initial write via write_layer_to_gpkg).
    """
    from ..processing.cable_lay_parsers import WKT_KEY

    point_rows: List[Dict] = []
    for idx, point in enumerate(model.points):
        row = {
            "rpl_id": rpl_id,
            "SeqNo": idx,
            "PosNo": point.pos_no,
            "Event": point.event,
            "DistCumulative": point.dist_cum_km,
            "CableDistCumulative": point.cable_dist_cum_km,
            "ApproxDepth": point.depth_m,
            "Latitude": point.lat,
            "Longitude": point.lon,
        }
        row.update(point.attrs)
        row.setdefault("SourceFile", source_file)
        row[WKT_KEY] = f"POINT ({point.lon} {point.lat})"
        point_rows.append(row)

    line_rows: List[Dict] = []
    for idx, seg in enumerate(model.segments):
        a, b = model.points[idx], model.points[idx + 1]
        row = {
            "rpl_id": rpl_id,
            "SeqNo": idx,
            "FromPos": a.pos_no,
            "ToPos": b.pos_no,
            "Bearing": seg.bearing_deg,
            "DistBetweenPos": seg.dist_km,
            "Slack": seg.slack_pct,
            "CableDistBetweenPos": seg.cable_dist_km,
        }
        row.update(seg.attrs)
        row.setdefault("SourceFile", source_file)
        row[WKT_KEY] = f"LINESTRING ({a.lon} {a.lat}, {b.lon} {b.lat})"
        line_rows.append(row)

    return {"points": point_rows, "lines": line_rows}

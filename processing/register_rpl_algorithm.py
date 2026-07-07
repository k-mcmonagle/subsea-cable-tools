# -*- coding: utf-8 -*-
"""RegisterRPLAlgorithm

Registers an imported RPL (a point layer + line layer pair, e.g. from
"Import Excel RPL") into the Cable Route Workbench GeoPackage:

- validates the pairing (n points = n lines + 1; FromPos/ToPos chain),
- assigns a stable rpl_id and SeqNo ordering (PosNo is never renumbered),
- derives per-segment Slack from CableDistBetweenPos / DistBetweenPos where
  missing,
- copies both layers into the workbench GeoPackage,
- inserts the wb_rpl registry row and the RPL's topology component with
  ports A and B,
- optionally loads the new layers into a "Cable Route Workbench" group.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterString,
    QgsProject,
    QgsWkbTypes,
)

from ..workbench import schema
from ..workbench.rpl_engine import RplModel, RplPoint, RplSegment, derive_slack, validate
from ..workbench.rpl_layer_io import model_rows_for_layers
from ..workbench.store import (
    WorkbenchStore,
    default_project_gpkg_path,
    project_gpkg_path,
    set_project_gpkg_path,
)

WORKBENCH_GROUP = "Cable Route Workbench"


def _value(feature, name: str):
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
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _int(value) -> Optional[int]:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


class RegisterRPLAlgorithm(QgsProcessingAlgorithm):
    INPUT_POINTS = "INPUT_POINTS"
    INPUT_LINES = "INPUT_LINES"
    RPL_NAME = "RPL_NAME"
    RPL_KIND = "RPL_KIND"
    GPKG_PATH = "GPKG_PATH"
    LOAD_LAYERS = "LOAD_LAYERS"

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT_POINTS, self.tr("RPL point layer"), [QgsProcessing.TypeVectorPoint]))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT_LINES, self.tr("RPL line layer"), [QgsProcessing.TypeVectorLine]))
        self.addParameter(QgsProcessingParameterString(
            self.RPL_NAME, self.tr("RPL name"), defaultValue=""))
        self.addParameter(QgsProcessingParameterEnum(
            self.RPL_KIND, self.tr("RPL kind"),
            options=[self.tr("Planned"), self.tr("As-laid")], defaultValue=0))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.GPKG_PATH, self.tr("Workbench GeoPackage (blank = project default)"),
            fileFilter="GeoPackage (*.gpkg)", optional=True, createByDefault=False))
        self.addParameter(QgsProcessingParameterBoolean(
            self.LOAD_LAYERS, self.tr("Load registered layers into the project"),
            defaultValue=True))

    # -- model building --------------------------------------------------------
    @staticmethod
    def build_model(point_features: List, line_features: List) -> RplModel:
        """Build an RplModel from importer-schema features (sorted by PosNo/SeqNo)."""
        point_engine = {"PosNo", "SeqNo", "rpl_id", "Event", "DistCumulative",
                        "CableDistCumulative", "ApproxDepth", "Latitude", "Longitude"}
        line_engine = {"SeqNo", "rpl_id", "FromPos", "ToPos", "Bearing",
                       "DistBetweenPos", "Slack", "CableDistBetweenPos"}

        def point_sort_key(item):
            index, feature = item
            pos_no = _int(_value(feature, "PosNo"))
            seq = _int(_value(feature, "SeqNo"))
            if seq is not None:
                return (0, seq)
            if pos_no is not None:
                return (1, pos_no)
            return (2, index)

        def line_sort_key(item):
            index, feature = item
            seq = _int(_value(feature, "SeqNo"))
            from_pos = _int(_value(feature, "FromPos"))
            if seq is not None:
                return (0, seq)
            if from_pos is not None:
                return (1, from_pos)
            return (2, index)

        points: List[RplPoint] = []
        for i, (_, feature) in enumerate(sorted(enumerate(point_features), key=point_sort_key)):
            geom = feature.geometry()
            if geom is None or geom.isEmpty():
                continue
            pt = geom.asPoint()
            attrs = {
                f.name(): _value(feature, f.name())
                for f in feature.fields()
                if f.name() not in point_engine and f.name().lower() != "fid"
            }
            points.append(RplPoint(
                seq=len(points),
                pos_no=_int(_value(feature, "PosNo")),
                event=str(_value(feature, "Event") or ""),
                lat=float(pt.y()),
                lon=float(pt.x()),
                dist_cum_km=_float(_value(feature, "DistCumulative")),
                cable_dist_cum_km=_float(_value(feature, "CableDistCumulative")),
                depth_m=_float(_value(feature, "ApproxDepth")),
                attrs=attrs,
            ))

        segments: List[RplSegment] = []
        for i, (_, feature) in enumerate(sorted(enumerate(line_features), key=line_sort_key)):
            attrs = {
                f.name(): _value(feature, f.name())
                for f in feature.fields()
                if f.name() not in line_engine and f.name().lower() != "fid"
            }
            attrs["FromPos"] = _int(_value(feature, "FromPos"))
            attrs["ToPos"] = _int(_value(feature, "ToPos"))
            segments.append(RplSegment(
                seq=len(segments),
                bearing_deg=_float(_value(feature, "Bearing")),
                dist_km=_float(_value(feature, "DistBetweenPos")),
                slack_pct=_float(_value(feature, "Slack")),
                cable_dist_km=_float(_value(feature, "CableDistBetweenPos")),
                attrs=attrs,
            ))
        # FromPos/ToPos are recomputed from point order on write; drop from attrs
        for seg in segments:
            seg.attrs.pop("FromPos", None)
            seg.attrs.pop("ToPos", None)
        return RplModel(points=points, segments=segments)

    @staticmethod
    def check_pairing(model: RplModel, line_features: List) -> List[str]:
        warnings: List[str] = []
        n_points, n_lines = len(model.points), len(model.segments)
        if n_points != n_lines + 1:
            warnings.append(
                f"Expected {n_points - 1} line segments for {n_points} points, found {n_lines}."
            )
        pos_nos = [p.pos_no for p in model.points]
        chain: List[str] = []
        for i, feature in enumerate(sorted(line_features, key=lambda f: _int(_value(f, "FromPos")) or 0)):
            from_pos = _int(_value(feature, "FromPos"))
            to_pos = _int(_value(feature, "ToPos"))
            if i + 1 < len(pos_nos):
                if from_pos is not None and pos_nos[i] is not None and from_pos != pos_nos[i]:
                    chain.append(f"segment {i}: FromPos {from_pos} != PosNo {pos_nos[i]}")
                if to_pos is not None and pos_nos[i + 1] is not None and to_pos != pos_nos[i + 1]:
                    chain.append(f"segment {i}: ToPos {to_pos} != PosNo {pos_nos[i + 1]}")
        if chain:
            warnings.append("FromPos/ToPos chain mismatches: " + "; ".join(chain[:5]))
        return warnings

    # -- processing -------------------------------------------------------------
    def processAlgorithm(self, parameters, context, feedback):
        points_source = self.parameterAsSource(parameters, self.INPUT_POINTS, context)
        lines_source = self.parameterAsSource(parameters, self.INPUT_LINES, context)
        if points_source is None or lines_source is None:
            raise QgsProcessingException(self.tr("Both point and line layers are required."))

        name = (self.parameterAsString(parameters, self.RPL_NAME, context) or "").strip()
        if not name:
            name = points_source.sourceName() or "RPL"
        kind = ["planned", "as_laid"][self.parameterAsEnum(parameters, self.RPL_KIND, context)]

        gpkg_path = (self.parameterAsString(parameters, self.GPKG_PATH, context) or "").strip()
        if not gpkg_path or gpkg_path.lower() in ("temporary_output", "temporary output"):
            gpkg_path = project_gpkg_path(context.project()) or default_project_gpkg_path(context.project())

        point_features = list(points_source.getFeatures())
        line_features = list(lines_source.getFeatures())
        model = self.build_model(point_features, line_features)

        for warning in self.check_pairing(model, line_features):
            feedback.pushWarning(warning)
        for finding in validate(model):
            if finding["severity"] == "error":
                raise QgsProcessingException(self.tr(finding["message"]))
            feedback.pushWarning(finding["message"])

        derived = derive_slack(model)
        if derived:
            feedback.pushInfo(self.tr(f"Derived slack for {derived} segments from cable distances."))

        store = WorkbenchStore(gpkg_path, context.transformContext())
        store.migrate()

        rpl_id = schema.new_id()
        points_layer_name = schema.rpl_points_layer_name(name)
        lines_layer_name = schema.rpl_lines_layer_name(name)

        # refuse to clobber another registered RPL's layers
        for row in store.list_rpls():
            if row.get("points_layer") == points_layer_name:
                raise QgsProcessingException(self.tr(
                    f"An RPL named '{name}' is already registered in this workbench. "
                    "Pick a different name."))

        source_file = ""
        if model.points and model.points[0].attrs.get("SourceFile"):
            source_file = str(model.points[0].attrs["SourceFile"])

        rows = model_rows_for_layers(model, rpl_id, source_file)
        point_specs = self._specs_with_extras(schema.RPL_POINT_FIELDS, rows["points"])
        line_specs = self._specs_with_extras(schema.RPL_LINE_FIELDS, rows["lines"])
        store.write_spatial_layer(points_layer_name, point_specs, QgsWkbTypes.Point, rows["points"])
        store.write_spatial_layer(lines_layer_name, line_specs, QgsWkbTypes.LineString, rows["lines"])

        store.save_rpl({
            "rpl_id": rpl_id,
            "name": name,
            "kind": kind,
            "points_layer": points_layer_name,
            "lines_layer": lines_layer_name,
            "source_file": source_file,
            "slack_mode": "hold_cable" if kind == "as_laid" else "hold_slack",
            "depth_source_config": "",
            "notes": "",
        })
        store.save_component(
            {"component_id": schema.new_id(), "kind": "rpl", "subject_id": rpl_id, "name": name},
            port_labels=["A", "B"],
        )

        self._gpkg_path = gpkg_path
        self._layer_names = [points_layer_name, lines_layer_name]
        self._load_layers = self.parameterAsBool(parameters, self.LOAD_LAYERS, context)

        feedback.pushInfo(self.tr(
            f"Registered RPL '{name}' ({len(model.points)} positions, "
            f"{len(model.segments)} segments) into {os.path.basename(gpkg_path)}."))
        return {"RPL_ID": rpl_id, "GPKG_PATH": gpkg_path,
                "POINTS_LAYER": points_layer_name, "LINES_LAYER": lines_layer_name}

    @staticmethod
    def _specs_with_extras(base_specs, rows: List[Dict]):
        """Base field specs plus any extra attribute keys found in the rows."""
        from .cable_lay_parsers import WKT_KEY

        specs = list(base_specs)
        known = {name for name, _ in specs}
        for row in rows:
            for key, value in row.items():
                if key in known or key == WKT_KEY:
                    continue
                if isinstance(value, bool):
                    type_str = "str"
                elif isinstance(value, int):
                    type_str = "int"
                elif isinstance(value, float):
                    type_str = "float"
                else:
                    type_str = "str"
                specs.append((key, type_str))
                known.add(key)
        return specs

    def postProcessAlgorithm(self, context, feedback):
        """Load the registered layers into a workbench group (main thread)."""
        if not getattr(self, "_load_layers", False):
            return {}
        project = context.project() or QgsProject.instance()
        set_project_gpkg_path(self._gpkg_path, project)
        root = project.layerTreeRoot()
        group = root.findGroup(WORKBENCH_GROUP) or root.insertGroup(0, WORKBENCH_GROUP)
        from .cable_lay_parsers import gpkg_layer_uri
        from qgis.core import QgsVectorLayer

        for layer_name in getattr(self, "_layer_names", []):
            layer = QgsVectorLayer(gpkg_layer_uri(self._gpkg_path, layer_name), layer_name, "ogr")
            if layer.isValid():
                project.addMapLayer(layer, False)
                group.addLayer(layer)
        return {}

    def name(self):
        return "register_rpl"

    def displayName(self):
        return self.tr("Register RPL into Workbench")

    def group(self):
        return self.tr("RPL Tools")

    def groupId(self):
        return "rpl_tools"

    def shortHelpString(self):
        return self.tr(
            """
Registers an RPL point + line layer pair (for example the output of Import Excel RPL) into the Cable Route Workbench GeoPackage, making it a managed RPL entity.

- Validates that the layers pair up (n points = n segments + 1, FromPos/ToPos chain).
- Derives per-segment Slack (%) from CableDistBetweenPos / DistBetweenPos where missing.
- Copies both layers into the workbench GeoPackage (default: <project>_workbench.gpkg beside the project file) and records the RPL in the registry.
- Creates the RPL's system-topology component with ports A and B so it can later be connected to other RPLs via joints or branching units.

Use the RPL Manager dock to browse, edit, and fit assemblies onto registered RPLs.
"""
        )

    def tr(self, string):
        return QCoreApplication.translate("RegisterRPLAlgorithm", string)

    def createInstance(self):
        return RegisterRPLAlgorithm()

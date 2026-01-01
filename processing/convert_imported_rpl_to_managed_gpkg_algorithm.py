# -*- coding: utf-8 -*-
"""ConvertImportedRPLToManagedGpkgAlgorithm

Takes a typical RPL import output (points + lines) and creates a *managed* RPL
container in a single GeoPackage.

Primary goals (MVP):
- Assign stable IDs + ordering (`node_id`, `seg_id`, `seq`)
- Rebuild segment geometries from ordered points to guarantee sync
- Preserve imported attributes where possible
- Write both layers into one GPKG so the RPL is a single portable artifact

This is intentionally a foundation step for an interactive editor/map tool.
"""

from __future__ import annotations

import os
import uuid
from typing import Dict, List, Optional, Tuple

from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsPointXY,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingOutputMultipleLayers,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterString,
    QgsProject,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)


class ConvertImportedRplToManagedGpkgAlgorithm(QgsProcessingAlgorithm):
    INPUT_POINTS = "INPUT_POINTS"
    INPUT_LINES = "INPUT_LINES"
    OUTPUT_GPKG = "OUTPUT_GPKG"

    OUTPUT_PREFIX = "OUTPUT_PREFIX"
    RPL_ID = "RPL_ID"

    OUTPUT_GROUP = "OUTPUT_GROUP"

    ORDER_MODE = "ORDER_MODE"
    REBUILD_SEGMENTS = "REBUILD_SEGMENTS"

    OUTPUT_MODE = "OUTPUT_MODE"

    OUTPUT_LAYERS = "OUTPUT_LAYERS"

    _ORDER_CHOICES = [
        "PosNo (ascending)",
        "DistCumulative (ascending)",
        "Feature id (ascending)",
    ]

    _OUTPUT_MODE_CHOICES = [
        "Overwrite layers in existing GeoPackage (keep other layers)",
        "Overwrite GeoPackage file (delete and recreate)",
        "Create unique layer names in GeoPackage",
    ]

    def tr(self, string: str) -> str:
        return QCoreApplication.translate("Processing", string)

    def createInstance(self):
        return ConvertImportedRplToManagedGpkgAlgorithm()

    def name(self):
        return "convertimportedrpltomanagedgpkg"

    def displayName(self):
        return self.tr("Convert Imported RPL → Managed RPL (GeoPackage)")

    def group(self):
        return self.tr("RPL Tools")

    def groupId(self):
        return "rpl_tools"

    def shortHelpString(self):
        return self.tr(
            """
<h3>Convert Imported RPL → Managed RPL (GeoPackage)</h3>

<p>Converts an imported RPL (typically produced by <b>Import Excel RPL</b>) into a managed RPL stored in a single GeoPackage.</p>

<p><b>What it does</b></p>
<ul>
  <li>Orders points into a route sequence (by PosNo or DistCumulative).</li>
  <li>Creates a new points layer with <code>node_id</code> and <code>seq</code>.</li>
  <li>Creates a new segments layer with <code>seg_id</code>, <code>seq</code>, <code>from_node_id</code>, <code>to_node_id</code>, and <code>length_mode</code>.</li>
  <li>Rebuilds segment geometries from the ordered point coordinates to ensure points/segments are synchronized.</li>
  <li>Writes both layers into a single <code>.gpkg</code> file.</li>
</ul>

<p><b>Notes</b></p>
<ul>
  <li>This is a foundation step for an interactive RPL manager/editor.</li>
  <li>Segment attributes are copied from the input lines layer when possible (matched by FromPos/ToPos), otherwise left null/empty.</li>
</ul>
"""
        )

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT_POINTS,
                self.tr("Input RPL Points Layer"),
                [QgsProcessing.TypeVectorPoint],
            )
        )

        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT_LINES,
                self.tr("Input RPL Lines Layer"),
                [QgsProcessing.TypeVectorLine],
                optional=True,
            )
        )

        self.addParameter(
            QgsProcessingParameterFileDestination(
                self.OUTPUT_GPKG,
                self.tr("Output GeoPackage"),
                fileFilter="GeoPackage (*.gpkg)",
            )
        )

        self.addParameter(
            QgsProcessingParameterString(
                self.OUTPUT_PREFIX,
                self.tr("Layer name prefix"),
                defaultValue="RPL",
            )
        )

        self.addParameter(
            QgsProcessingParameterString(
                self.OUTPUT_GROUP,
                self.tr("Project group for outputs"),
                defaultValue="Managed RPL",
                optional=True,
            )
        )

        self.addParameter(
            QgsProcessingParameterString(
                self.RPL_ID,
                self.tr("RPL ID (optional, stored as attribute)"),
                defaultValue="",
                optional=True,
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                self.ORDER_MODE,
                self.tr("Point ordering"),
                options=self._ORDER_CHOICES,
                defaultValue=0,
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                self.OUTPUT_MODE,
                self.tr("GeoPackage write mode"),
                options=self._OUTPUT_MODE_CHOICES,
                defaultValue=0,
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.REBUILD_SEGMENTS,
                self.tr("Rebuild segments (ignore input line geometries)"),
                defaultValue=True,
            )
        )

        self.addOutput(QgsProcessingOutputMultipleLayers(self.OUTPUT_LAYERS, self.tr("Output Layers")))

    @staticmethod
    def _uuid() -> str:
        return str(uuid.uuid4())

    @staticmethod
    def _field_index(fields: QgsFields, name: str) -> int:
        try:
            return fields.lookupField(name)
        except Exception:
            return -1

    def _order_points(
        self,
        point_features: List[QgsFeature],
        point_fields: QgsFields,
        order_mode: int,
        feedback,
    ) -> List[QgsFeature]:
        """Return ordered list of point features."""

        idx_posno = self._field_index(point_fields, "PosNo")
        idx_dist = self._field_index(point_fields, "DistCumulative")

        def safe_float(v):
            try:
                return float(v)
            except Exception:
                return None

        def safe_int(v):
            try:
                return int(v)
            except Exception:
                return None

        if order_mode == 0 and idx_posno >= 0:
            def posno_sort_key(f: QgsFeature):
                pos = safe_int(f[idx_posno])
                return (pos if pos is not None else 10**18, int(f.id()))

            ordered = sorted(point_features, key=posno_sort_key)
            # Warn if many PosNo are missing
            missing = sum(1 for f in ordered if safe_int(f[idx_posno]) is None)
            if missing:
                feedback.pushWarning(self.tr(f"{missing} point(s) missing valid PosNo; ordering falls back to feature id for those."))
            return ordered

        if order_mode == 1 and idx_dist >= 0:
            def dist_sort_key(f: QgsFeature):
                d = safe_float(f[idx_dist])
                return (d if d is not None else 10**18, int(f.id()))

            ordered = sorted(point_features, key=dist_sort_key)
            missing = sum(1 for f in ordered if safe_float(f[idx_dist]) is None)
            if missing:
                feedback.pushWarning(self.tr(f"{missing} point(s) missing valid DistCumulative; ordering falls back to feature id for those."))
            return ordered

        # Fallback
        feedback.pushWarning(self.tr("Could not apply requested ordering (missing field). Falling back to feature id ordering."))
        return sorted(point_features, key=lambda f: int(f.id()))

    def _match_line_by_from_to(
        self,
        lines: List[QgsFeature],
        line_fields: QgsFields,
        from_pos: Optional[int],
        to_pos: Optional[int],
        used_fids: set[int],
    ) -> Optional[QgsFeature]:
        if from_pos is None or to_pos is None:
            return None

        idx_from = self._field_index(line_fields, "FromPos")
        idx_to = self._field_index(line_fields, "ToPos")
        if idx_from < 0 or idx_to < 0:
            return None

        def safe_int(v):
            try:
                return int(v)
            except Exception:
                return None

        # Prefer exact direction
        for f in lines:
            if int(f.id()) in used_fids:
                continue
            if safe_int(f[idx_from]) == from_pos and safe_int(f[idx_to]) == to_pos:
                return f

        # Fall back: reversed direction (some datasets may not respect direction)
        for f in lines:
            if int(f.id()) in used_fids:
                continue
            if safe_int(f[idx_from]) == to_pos and safe_int(f[idx_to]) == from_pos:
                return f

        return None

    def _infer_prefix_from_inputs(self, points_layer: QgsVectorLayer, points_source_fields: QgsFields) -> str:
        """Try to derive a meaningful prefix from layer name or SourceFile field."""

        # 1) Prefer SourceFile attribute value (often Excel filename) if present.
        src_idx = self._field_index(points_source_fields, "SourceFile")
        if src_idx >= 0:
            try:
                for f in points_layer.getFeatures():
                    v = f[src_idx]
                    if v:
                        base = os.path.splitext(os.path.basename(str(v)))[0].strip()
                        if base:
                            return base
                    break
            except Exception:
                pass

        # 2) Fall back to layer name, stripping common suffixes.
        name = (points_layer.name() or "").strip()
        for suffix in ["_Points", " Points", "_points", " points"]:
            if name.endswith(suffix):
                name = name[: -len(suffix)].strip()
                break
        return name or "RPL"

    def _layer_exists(self, gpkg_path: str, layer_name: str) -> bool:
        try:
            uri = f"{gpkg_path}|layername={layer_name}"
            lyr = QgsVectorLayer(uri, layer_name, "ogr")
            return bool(lyr.isValid())
        except Exception:
            return False

    def _unique_layer_name(self, gpkg_path: str, desired: str) -> str:
        if not os.path.exists(gpkg_path):
            return desired
        if not self._layer_exists(gpkg_path, desired):
            return desired

        i = 2
        while True:
            candidate = f"{desired}_{i}"
            if not self._layer_exists(gpkg_path, candidate):
                return candidate
            i += 1

    def processAlgorithm(self, parameters, context, feedback):
        point_source = self.parameterAsSource(parameters, self.INPUT_POINTS, context)
        line_source = self.parameterAsSource(parameters, self.INPUT_LINES, context)

        if point_source is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT_POINTS))

        gpkg_path = self.parameterAsFileOutput(parameters, self.OUTPUT_GPKG, context)
        if not gpkg_path:
            raise QgsProcessingException(self.tr("Output GeoPackage path is required. In the algorithm dialog, set 'Output GeoPackage' to 'Save to file…' and choose a .gpkg path."))

        prefix = (self.parameterAsString(parameters, self.OUTPUT_PREFIX, context) or "").strip()
        output_group = (self.parameterAsString(parameters, self.OUTPUT_GROUP, context) or "").strip()

        rpl_id = (self.parameterAsString(parameters, self.RPL_ID, context) or "").strip()
        order_mode = int(self.parameterAsEnum(parameters, self.ORDER_MODE, context))
        rebuild_segments = bool(self.parameterAsBool(parameters, self.REBUILD_SEGMENTS, context))
        output_mode = int(self.parameterAsEnum(parameters, self.OUTPUT_MODE, context))

        # Resolve input layers (for nicer defaults / prefix inference)
        points_layer = self.parameterAsVectorLayer(parameters, self.INPUT_POINTS, context)
        if points_layer is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT_POINTS))

        # If user left the default "RPL" (or blank), auto-infer a more meaningful prefix.
        if (not prefix) or prefix.strip().lower() == "rpl":
            prefix = self._infer_prefix_from_inputs(points_layer, point_source.fields())

        # Decide write behavior
        # 0: overwrite layers in existing GPKG, keep other layers
        # 1: overwrite entire file
        # 2: unique layer names
        if output_mode == 1 and os.path.exists(gpkg_path):
            try:
                os.remove(gpkg_path)
            except Exception as e:
                raise QgsProcessingException(self.tr(f"Could not overwrite existing GeoPackage: {e}"))

        # Read points
        point_fields_in = point_source.fields()
        points: List[QgsFeature] = [f for f in point_source.getFeatures()]
        if len(points) < 2:
            raise QgsProcessingException(self.tr("At least 2 point features are required."))

        ordered_points = self._order_points(points, point_fields_in, order_mode, feedback)

        # Build node_id mapping
        idx_posno = self._field_index(point_fields_in, "PosNo")

        def safe_int(v):
            try:
                return int(v)
            except Exception:
                return None

        node_ids: List[str] = []
        node_by_fid: Dict[int, Tuple[int, str, Optional[int], QgsPointXY]] = {}

        for seq, f in enumerate(ordered_points, start=1):
            geom = f.geometry()
            if geom is None or geom.isEmpty() or geom.type() != QgsWkbTypes.PointGeometry:
                raise QgsProcessingException(self.tr(f"Point feature {f.id()} has invalid geometry."))
            pt = QgsPointXY(geom.asPoint())
            node_id = self._uuid()
            posno = safe_int(f[idx_posno]) if idx_posno >= 0 else None
            node_by_fid[int(f.id())] = (seq, node_id, posno, pt)
            node_ids.append(node_id)

        # Create memory nodes layer
        crs: QgsCoordinateReferenceSystem = point_source.sourceCrs()
        if not crs or not crs.isValid():
            crs = QgsCoordinateReferenceSystem("EPSG:4326")

        nodes_layer_name = f"{prefix}_nodes"
        segs_layer_name = f"{prefix}_segments"

        if output_mode == 2:
            nodes_layer_name = self._unique_layer_name(gpkg_path, nodes_layer_name)
            segs_layer_name = self._unique_layer_name(gpkg_path, segs_layer_name)

        nodes_mem = QgsVectorLayer(f"Point?crs={crs.authid()}", nodes_layer_name, "memory")
        nodes_dp = nodes_mem.dataProvider()

        nodes_fields = QgsFields()
        for fld in point_fields_in:
            nodes_fields.append(QgsField(fld.name(), fld.type(), fld.typeName(), fld.length(), fld.precision()))
        nodes_fields.append(QgsField("node_id", QVariant.String))
        nodes_fields.append(QgsField("seq", QVariant.Int))
        nodes_fields.append(QgsField("source_ref", QVariant.String))
        if rpl_id:
            nodes_fields.append(QgsField("rpl_id", QVariant.String))

        nodes_dp.addAttributes(list(nodes_fields))
        nodes_mem.updateFields()

        # Add node features
        out_nodes: List[QgsFeature] = []
        for seq, f in enumerate(ordered_points, start=1):
            geom = QgsGeometry(f.geometry())
            node_id = node_by_fid[int(f.id())][1]

            out_f = QgsFeature(nodes_fields)
            out_f.setGeometry(geom)
            attrs = list(f.attributes())
            attrs.append(node_id)
            attrs.append(seq)
            # Prefer SourceFile attribute when present, else layer name
            try:
                src_idx = self._field_index(point_fields_in, "SourceFile")
                src_val = f[src_idx] if src_idx >= 0 else None
                src_text = str(src_val) if src_val else (points_layer.name() or "")
            except Exception:
                src_text = points_layer.name() or ""
            attrs.append(src_text)
            if rpl_id:
                attrs.append(rpl_id)
            out_f.setAttributes(attrs)
            out_nodes.append(out_f)

        nodes_dp.addFeatures(out_nodes)
        nodes_mem.updateExtents()

        # Prepare line lookup (optional)
        lines: List[QgsFeature] = []
        line_fields_in: Optional[QgsFields] = None
        if line_source is not None:
            line_fields_in = line_source.fields()
            lines = [f for f in line_source.getFeatures()]

        # Stable iteration order for fallback indexing
        lines_by_fid: List[QgsFeature] = sorted(lines, key=lambda f: int(f.id()))

        # Create memory segments layer
        segs_mem = QgsVectorLayer(f"LineString?crs={crs.authid()}", segs_layer_name, "memory")
        segs_dp = segs_mem.dataProvider()

        segs_fields = QgsFields()
        # Preserve input line fields if provided; otherwise provide a minimal schema compatible with import.
        if line_fields_in is not None and len(line_fields_in) > 0:
            for fld in line_fields_in:
                segs_fields.append(QgsField(fld.name(), fld.type(), fld.typeName(), fld.length(), fld.precision()))
        else:
            # Minimal import-compatible set
            segs_fields.append(QgsField("FromPos", QVariant.Int))
            segs_fields.append(QgsField("ToPos", QVariant.Int))
            segs_fields.append(QgsField("Bearing", QVariant.Double))
            segs_fields.append(QgsField("DistBetweenPos", QVariant.Double))
            segs_fields.append(QgsField("Slack", QVariant.Double))
            segs_fields.append(QgsField("CableDistBetweenPos", QVariant.Double))
            segs_fields.append(QgsField("CableCode", QVariant.String))
            segs_fields.append(QgsField("CableType", QVariant.String))

        segs_fields.append(QgsField("seg_id", QVariant.String))
        segs_fields.append(QgsField("seq", QVariant.Int))
        segs_fields.append(QgsField("from_node_id", QVariant.String))
        segs_fields.append(QgsField("to_node_id", QVariant.String))
        segs_fields.append(QgsField("length_mode", QVariant.String))
        segs_fields.append(QgsField("source_ref", QVariant.String))
        if rpl_id:
            segs_fields.append(QgsField("rpl_id", QVariant.String))

        segs_dp.addAttributes(list(segs_fields))
        segs_mem.updateFields()

        # Build segments from ordered nodes
        used_line_fids: set[int] = set()
        idx_line_from = self._field_index(line_fields_in, "FromPos") if line_fields_in is not None else -1
        idx_line_to = self._field_index(line_fields_in, "ToPos") if line_fields_in is not None else -1

        out_segs: List[QgsFeature] = []
        for i in range(len(ordered_points) - 1):
            a = ordered_points[i]
            b = ordered_points[i + 1]

            a_seq, a_node_id, a_posno, a_pt = node_by_fid[int(a.id())]
            b_seq, b_node_id, b_posno, b_pt = node_by_fid[int(b.id())]

            seg_geom = QgsGeometry.fromPolylineXY([a_pt, b_pt])

            # Always attempt to match an input line feature to copy attributes.
            # Geometry is always rebuilt from points (sync guarantee); rebuild_segments controls whether
            # we *trust* input line geometries (currently we never do for MVP).
            matched_line: Optional[QgsFeature] = None
            if lines and line_fields_in is not None:
                matched_line = self._match_line_by_from_to(lines, line_fields_in, a_posno, b_posno, used_line_fids)
                if matched_line is not None:
                    ml = matched_line
                    used_line_fids.add(int(ml.id()))
                elif i < len(lines_by_fid):
                    # Fallback: assume line features are already in segment order.
                    ml2 = lines_by_fid[i]
                    matched_line = ml2
                    used_line_fids.add(int(ml2.id()))

            seg_id = self._uuid()

            out_seg = QgsFeature(segs_fields)
            out_seg.setGeometry(seg_geom)

            base_attrs: List = []
            if line_fields_in is not None and len(line_fields_in) > 0:
                if matched_line is not None:
                    base_attrs = list(matched_line.attributes())
                else:
                    base_attrs = [None] * len(line_fields_in)
                    # If the schema has FromPos/ToPos, fill them when possible
                    if idx_line_from >= 0 and a_posno is not None:
                        base_attrs[idx_line_from] = a_posno
                    if idx_line_to >= 0 and b_posno is not None:
                        base_attrs[idx_line_to] = b_posno
            else:
                base_attrs = [
                    a_posno,
                    b_posno,
                    None,
                    None,
                    None,
                    None,
                    "",
                    "",
                ]

            base_attrs.append(seg_id)
            base_attrs.append(i + 1)
            base_attrs.append(a_node_id)
            base_attrs.append(b_node_id)
            base_attrs.append("SLACK_LOCKED")
            # Prefer line SourceFile when present, else points layer name
            try:
                if matched_line is not None and line_fields_in is not None:
                    src_idx = self._field_index(line_fields_in, "SourceFile")
                    src_val = matched_line[src_idx] if src_idx >= 0 else None
                    src_text = str(src_val) if src_val else (points_layer.name() or "")
                else:
                    src_text = points_layer.name() or ""
            except Exception:
                src_text = points_layer.name() or ""
            base_attrs.append(src_text)
            if rpl_id:
                base_attrs.append(rpl_id)

            out_seg.setAttributes(base_attrs)
            out_segs.append(out_seg)

        segs_dp.addFeatures(out_segs)
        segs_mem.updateExtents()

        # Write to GeoPackage
        transform_context = context.transformContext()

        save_opts_nodes = QgsVectorFileWriter.SaveVectorOptions()
        save_opts_nodes.driverName = "GPKG"
        save_opts_nodes.fileEncoding = "UTF-8"
        save_opts_nodes.layerName = nodes_layer_name
        if os.path.exists(gpkg_path):
            save_opts_nodes.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteLayer
        else:
            save_opts_nodes.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteFile

        err, _, _, err_msg = QgsVectorFileWriter.writeAsVectorFormatV3(
            nodes_mem, gpkg_path, transform_context, save_opts_nodes
        )
        if err != QgsVectorFileWriter.NoError:
            raise QgsProcessingException(self.tr(f"Failed to write nodes layer: {err_msg}"))

        save_opts_segs = QgsVectorFileWriter.SaveVectorOptions()
        save_opts_segs.driverName = "GPKG"
        save_opts_segs.fileEncoding = "UTF-8"
        save_opts_segs.layerName = segs_layer_name
        save_opts_segs.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteLayer

        err, _, _, err_msg = QgsVectorFileWriter.writeAsVectorFormatV3(
            segs_mem, gpkg_path, transform_context, save_opts_segs
        )
        if err != QgsVectorFileWriter.NoError:
            raise QgsProcessingException(self.tr(f"Failed to write segments layer: {err_msg}"))

        # Load layers to project and return their IDs
        out_layers: Dict[str, str] = {}
        nodes_uri = f"{gpkg_path}|layername={nodes_layer_name}"
        segs_uri = f"{gpkg_path}|layername={segs_layer_name}"

        nodes_out = QgsVectorLayer(nodes_uri, nodes_layer_name, "ogr")
        segs_out = QgsVectorLayer(segs_uri, segs_layer_name, "ogr")

        if not nodes_out.isValid():
            feedback.pushWarning(self.tr("Nodes layer written but could not be loaded into the project."))
        else:
            if output_group:
                root = QgsProject.instance().layerTreeRoot()
                group = root.findGroup(output_group) or root.addGroup(output_group)
                QgsProject.instance().addMapLayer(nodes_out, False)
                group.addLayer(nodes_out)
            else:
                QgsProject.instance().addMapLayer(nodes_out)
            out_layers[nodes_layer_name] = nodes_out.id()

        if not segs_out.isValid():
            feedback.pushWarning(self.tr("Segments layer written but could not be loaded into the project."))
        else:
            if output_group:
                root = QgsProject.instance().layerTreeRoot()
                group = root.findGroup(output_group) or root.addGroup(output_group)
                QgsProject.instance().addMapLayer(segs_out, False)
                group.addLayer(segs_out)
            else:
                QgsProject.instance().addMapLayer(segs_out)
            out_layers[segs_layer_name] = segs_out.id()

        feedback.pushInfo(self.tr(f"Managed RPL written to: {gpkg_path}"))
        feedback.pushInfo(self.tr(f"Nodes: {len(out_nodes)}"))
        feedback.pushInfo(self.tr(f"Segments: {len(out_segs)}"))

        return {
            self.OUTPUT_LAYERS: out_layers,
            self.OUTPUT_GPKG: gpkg_path,
        }

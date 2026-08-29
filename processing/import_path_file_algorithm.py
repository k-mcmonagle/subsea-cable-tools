# -*- coding: utf-8 -*-
"""ImportPathFileAlgorithm: Imports .pthmdb path files into QGIS.

A ``.pthmdb`` file is a GeoMedia-format Access database describing one cable
path (route position list). This tool loads each selected file as a set of
layers: the route line, the path points (KP, slack, labels, cable types),
the per-segment lines (bearing, dKP, burial flag), the assembly points and —
when the file carries a bathymetry profile — the KP/depth profile.

Unlike the generic MDB import, the coordinate system is auto-detected from
the file's GCoordSystem record (path files store geographic degrees on
WGS84); a CRS parameter is only needed for unusual files.

To register a path file as a workbench RPL revision instead of plain
layers, use the Cable Route Workbench: New > Import path file.
"""

import os

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingOutputMultipleLayers,
    QgsProcessingParameterCrs,
    QgsProcessingParameterMultipleLayers,
    QgsVectorLayer,
)

from ..qgis_compat import (
    FIELD_TYPE_DOUBLE,
    FIELD_TYPE_INT,
    FIELD_TYPE_STRING,
)
from .import_mdb_algorithm import (
    ImportMdbAlgorithm,
    _write_to_temporary_gpkg,
)
from .pthmdb_reader import PathFileError, kp_to_km, read_path_file


def _field_type_for(values):
    """Pick a QGIS field type from a column's Python values."""
    kinds = {type(v) for v in values if v is not None}
    if kinds <= {bool, int}:
        return FIELD_TYPE_INT
    if kinds <= {bool, int, float}:
        return FIELD_TYPE_DOUBLE
    return FIELD_TYPE_STRING


def _coerce(value, field_type):
    if value is None:
        return None
    if field_type == FIELD_TYPE_INT:
        return int(value)
    if field_type == FIELD_TYPE_DOUBLE:
        return float(value)
    return str(value)


def _build_layer(geometry_def, layer_name, rows, columns, geometry_fn):
    """Create a memory layer from row dicts.

    ``columns`` is ``[(name, values_iterable), ...]`` used for type
    inference; ``geometry_fn(row)`` returns a QgsGeometry or None.
    """
    layer = QgsVectorLayer(geometry_def, layer_name, "memory")
    provider = layer.dataProvider()
    typed = [(name, _field_type_for(values)) for name, values in columns]
    provider.addAttributes([QgsField(name, ftype) for name, ftype in typed])
    layer.updateFields()

    features = []
    for row in rows:
        feature = QgsFeature(layer.fields())
        geometry = geometry_fn(row)
        if geometry is not None:
            feature.setGeometry(geometry)
        feature.setAttributes(
            [_coerce(row.get(name), ftype) for name, ftype in typed])
        features.append(feature)
    provider.addFeatures(features)
    layer.updateExtents()
    return layer


def _attribute_columns(rows, skip=("x", "y", "z", "vertices")):
    """Stable ``[(name, values), ...]`` across row dicts, minus geometry keys."""
    names = []
    for row in rows:
        for name in row:
            if name not in names and name not in skip:
                names.append(name)
    return [(name, [row.get(name) for row in rows]) for name in names]


class ImportPathFileAlgorithm(QgsProcessingAlgorithm):
    INPUT_FILES = 'INPUT_FILES'
    CRS_OVERRIDE = 'CRS_OVERRIDE'
    OUTPUT_LAYERS = 'OUTPUT_LAYERS'

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterMultipleLayers(
            self.INPUT_FILES,
            self.tr('Input path file(s) (.pthmdb)'),
            QgsProcessing.TypeFile,
        ))
        self.addParameter(QgsProcessingParameterCrs(
            self.CRS_OVERRIDE,
            self.tr('CRS override (only if auto-detection fails)'),
            optional=True,
        ))
        self.addOutput(QgsProcessingOutputMultipleLayers(
            self.OUTPUT_LAYERS, self.tr('Imported Layers')))

    def processAlgorithm(self, parameters, context, feedback):
        files = self.parameterAsFileList(parameters, self.INPUT_FILES, context)
        crs_override = self.parameterAsCrs(parameters, self.CRS_OVERRIDE, context)

        normalized_files = []
        seen = set()
        for path in files:
            normalized = os.path.abspath(os.path.normpath(os.fspath(path)))
            key = os.path.normcase(normalized)
            if key in seen:
                continue
            seen.add(key)
            if not os.path.isfile(normalized):
                raise QgsProcessingException(f"Path file not found: {normalized}")
            if os.path.splitext(normalized)[1].lower() not in {'.pthmdb', '.mdb', '.accdb'}:
                raise QgsProcessingException(
                    f"Input must be a .pthmdb path file: {normalized}")
            normalized_files.append(normalized)
        if not normalized_files:
            raise QgsProcessingException("Select at least one .pthmdb path file.")

        output_layers = []
        for file_index, path in enumerate(normalized_files):
            if feedback.isCanceled():
                break
            stem = os.path.splitext(os.path.basename(path))[0]
            feedback.pushInfo(f"Reading {os.path.basename(path)}...")
            try:
                data = read_path_file(path)
            except PathFileError as exc:
                raise QgsProcessingException(str(exc))

            for warning in data.warnings:
                feedback.pushWarning(f"  {warning}")

            if data.crs_auth_id:
                crs = QgsCoordinateReferenceSystem(data.crs_auth_id)
                feedback.pushInfo(f"  CRS auto-detected: {data.crs_auth_id} "
                                  f"({data.crs_note})")
            elif crs_override and crs_override.isValid():
                crs = crs_override
                feedback.pushWarning(
                    f"  {data.crs_note}; using the CRS override "
                    f"({crs_override.authid()})")
            else:
                raise QgsProcessingException(
                    f"{os.path.basename(path)}: {data.crs_note}. "
                    "Set the CRS override parameter.")

            kp_unit = data.kp_unit
            feedback.pushInfo(
                f"  {len(data.path_points)} path points, "
                f"{len(data.path_lines)} segments, "
                f"{len(data.assembly_points)} assembly points, "
                f"{len(data.profile)} profile samples, "
                f"{len(data.path_width)} path-width rows, "
                f"{len(data.side_slopes)} side-slope rows; "
                f"KP unit: {kp_unit or 'undetermined'}")

            def _point_geometry(row):
                return QgsGeometry.fromPointXY(QgsPointXY(row["x"], row["y"]))

            def _add(layer, name):
                disk = _write_to_temporary_gpkg(layer, name, crs, context, feedback)
                if disk is None:
                    return
                ImportMdbAlgorithm._register_output_layer(context, disk, name, stem)
                output_layers.append(disk.id())

            def _with_kp_km(rows, kp_field):
                enriched = []
                for row in rows:
                    row = dict(row)
                    value = row.get(kp_field)
                    if isinstance(value, (int, float)) and kp_unit:
                        row["kp_km"] = kp_to_km(value, kp_unit)
                    enriched.append(row)
                return enriched

            crs_def = crs.authid() or f"EPSG:{crs.srsid()}"

            # Route line: one feature through the ordered path points.
            route_rows = [{
                "source": os.path.basename(path),
                "points": len(data.path_points),
                "length_km": (kp_to_km(
                    max(p.get("KP") for p in data.path_points)
                    - min(p.get("KP") for p in data.path_points), kp_unit)
                    if kp_unit and all(
                        isinstance(p.get("KP"), (int, float))
                        for p in data.path_points) else None),
                "kp_unit": kp_unit or "",
                "notes": data.user_notes,
            }]
            route_layer = _build_layer(
                f"LineString?crs={crs_def}", f"{stem} - Route", route_rows,
                _attribute_columns(route_rows),
                lambda _row: QgsGeometry.fromPolylineXY(
                    [QgsPointXY(x, y) for x, y, _z in data.route_vertices]))
            _add(route_layer, f"{stem} - Route")

            # Path points: the full position list with every attribute.
            point_rows = _with_kp_km(data.path_points, "KP")
            points_layer = _build_layer(
                f"Point?crs={crs_def}", f"{stem} - Path Points", point_rows,
                _attribute_columns(point_rows), _point_geometry)
            _add(points_layer, f"{stem} - Path Points")

            # Per-segment lines with bearing / dKP / cable type / burial.
            if data.path_lines:
                segment_rows = data.path_lines
                segments_layer = _build_layer(
                    f"LineString?crs={crs_def}", f"{stem} - Segments",
                    segment_rows, _attribute_columns(segment_rows),
                    lambda row: QgsGeometry.fromPolylineXY(
                        [QgsPointXY(x, y) for x, y, _z in row["vertices"]]))
                _add(segments_layer, f"{stem} - Segments")

            if data.assembly_points:
                assembly_rows = _with_kp_km(data.assembly_points, "KP")
                assembly_layer = _build_layer(
                    f"Point?crs={crs_def}", f"{stem} - Assembly Points",
                    assembly_rows, _attribute_columns(assembly_rows),
                    _point_geometry)
                _add(assembly_layer, f"{stem} - Assembly Points")

            # Path width (route corridor offsets), when the file carries it.
            if data.path_width:
                width_rows = _with_kp_km(data.path_width, "KP")
                if any("vertices" in row for row in data.path_width):
                    width_layer = _build_layer(
                        f"LineString?crs={crs_def}", f"{stem} - Path Width",
                        width_rows, _attribute_columns(width_rows),
                        lambda row: (QgsGeometry.fromPolylineXY(
                            [QgsPointXY(x, y) for x, y, _z in row["vertices"]])
                            if row.get("vertices") else None))
                elif any(isinstance(row.get("x"), (int, float))
                         for row in data.path_width):
                    width_layer = _build_layer(
                        f"Point?crs={crs_def}", f"{stem} - Path Width",
                        width_rows, _attribute_columns(width_rows),
                        lambda row: (_point_geometry(row)
                                     if isinstance(row.get("x"), (int, float))
                                     else None))
                else:
                    width_layer = _build_layer(
                        "None", f"{stem} - Path Width", width_rows,
                        _attribute_columns(width_rows), lambda _row: None)
                _add(width_layer, f"{stem} - Path Width")

            # Side slopes (KP/slope table, no geometry), when present.
            if data.side_slopes:
                slope_rows = _with_kp_km(data.side_slopes, "kp")
                slopes_layer = _build_layer(
                    "None", f"{stem} - Side Slopes", slope_rows,
                    _attribute_columns(slope_rows), lambda _row: None)
                _add(slopes_layer, f"{stem} - Side Slopes")

            # Depth profile (present once a bathymetry was attached in the source application).
            if data.profile:
                profile_rows = _with_kp_km(data.profile, "Kp")
                has_geometry = all(
                    isinstance(row.get("x"), (int, float))
                    and isinstance(row.get("y"), (int, float))
                    for row in data.profile)
                if has_geometry:
                    profile_layer = _build_layer(
                        f"Point?crs={crs_def}", f"{stem} - Depth Profile",
                        profile_rows, _attribute_columns(profile_rows),
                        _point_geometry)
                else:
                    profile_layer = _build_layer(
                        "None", f"{stem} - Depth Profile", profile_rows,
                        _attribute_columns(profile_rows), lambda _row: None)
                _add(profile_layer, f"{stem} - Depth Profile")

            feedback.setProgress(int(100 * (file_index + 1) / len(normalized_files)))

        return {self.OUTPUT_LAYERS: output_layers}

    def name(self):
        return 'import_path_file'

    def displayName(self):
        return self.tr('Import Path File (.pthmdb)')

    def group(self):
        return self.tr('MDB Tools')

    def groupId(self):
        return 'mdb_tools'

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return ImportPathFileAlgorithm()

    def shortHelpString(self):
        return self.tr("""<h3>Import Path File (.pthmdb)</h3>
<p>Imports one or more path files (.pthmdb) &mdash; cable route position lists stored as GeoMedia-format Access databases &mdash; as QGIS layers.</p>

<h4>Layers created per file</h4>
<ul>
  <li><b>Route:</b> the full route as a single line feature.</li>
  <li><b>Path Points:</b> every route position with KP, cable distance, slack, depth, label, comment, cable type and bearing. A <code>kp_km</code> field is added with KP converted to kilometres.</li>
  <li><b>Segments:</b> one line per point-to-point leg with bearing, dKP, span cable distance, cable type and the burial flag.</li>
  <li><b>Assembly Points:</b> cable assembly positions (joints, branching units, landings), when present.</li>
  <li><b>Depth Profile:</b> the KP/depth profile sampled from a bathymetry in the source application, when present.</li>
  <li><b>Path Width / Side Slopes:</b> the route corridor offsets and the KP/side-slope table, when the file carries them (Side Slopes imports as a geometryless attribute table).</li>
</ul>
<p>Each file's layers are placed in a group named after the file, stored as indexed temporary GeoPackages. Every table in the file is accounted for: any populated table the tool does not recognise is named in the log with its row count, so nothing is dropped silently.</p>

<h4>Coordinate system and units</h4>
<p>The CRS is auto-detected from the file's GeoMedia <code>GCoordSystem</code> record; path files normally store geographic degrees on WGS84 (EPSG:4326). The <b>CRS override</b> parameter is only used when auto-detection fails. The KP unit (metres or kilometres) is verified against the geodesic length of the route.</p>

<h4>Prerequisites</h4>
<p>None for typical files: the plugin's bundled pure-Python MDB reader is used, with an automatic ODBC fallback (Windows + Microsoft Access Database Engine + pyodbc) for unusual files.</p>

<h4>Workbench integration</h4>
<p>To register a path file as a Cable Route Workbench RPL revision (with events, cable types, slack and depth carried into the RPL, and optional assembly extraction), use the Workbench instead: <i>New &gt; Import path file (.pthmdb)...</i></p>""")

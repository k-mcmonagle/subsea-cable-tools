# import_mdb_algorithm.py
# -*- coding: utf-8 -*-
"""
ImportMdbAlgorithm: Imports GeoMedia MDB feature tables into QGIS.
This tool imports any GeoMedia MDB feature class (Point, LineString, Polygon,
MultiPoint, 2D and 3D), not just bathymetric data. It relies on a user-provided
CRS and adds 'depth' and 'source' attributes.
"""

import os
import math
import traceback
import json
import tempfile
import shutil
import subprocess
import sys
import hashlib
import time
try:
    import pyodbc
except Exception:  # pragma: no cover
    pyodbc = None
from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterCrs,
    QgsProcessingOutputMultipleLayers,
    QgsVectorLayer,
    QgsField,
    QgsFeature,
    QgsGeometry,
    QgsProcessingException,
    QgsCoordinateReferenceSystem,
    QgsProcessingContext,
    QgsVectorFileWriter,
)
from ..qgis_compat import (
    FIELD_TYPE_DOUBLE,
    FIELD_TYPE_INT,
    FIELD_TYPE_STRING,
    processing_generate_temp_filename,
    processing_temp_folder,
)
from .geomedia_blob import decode_geometry_blob, parse_blob  # noqa: F401 - re-exported for callers/tests
from .mdb_odbc_worker import GRAPHIC_TYPE_CODE, find_candidate_geometry_fields


ACCESS_ODBC_DRIVER_NAME = "Microsoft Access Driver (*.mdb, *.accdb)"


def _odbc_braced_value(value):
    """Return an ODBC connection string value enclosed in braces."""
    return "{" + os.fspath(value).replace("}", "}}") + "}"


def _normalize_mdb_path(mdb_file):
    path = os.path.abspath(os.path.normpath(os.fspath(mdb_file).strip().strip('"')))
    return path.replace("/", "\\")


def _access_connection_string(mdb_file):
    normalized = _normalize_mdb_path(mdb_file)
    return (
        "Driver="
        + _odbc_braced_value(ACCESS_ODBC_DRIVER_NAME)
        + ";DBQ="
        + normalized
        + ";"
    )


def _quote_access_identifier(identifier):
    """Return a bracket-quoted Access identifier after rejecting unsafe names."""
    text = str(identifier)
    if not text:
        raise ValueError("Access identifier is empty")
    if any(ch in text for ch in "[]"):
        raise ValueError(f"Access identifier contains brackets: {text!r}")
    if any(ord(ch) < 32 for ch in text):
        raise ValueError(f"Access identifier contains control characters: {text!r}")
    return "[" + text + "]"


def _get_column_names(cursor, table_name):
    sql = "SELECT * FROM " + _quote_access_identifier(table_name) + " WHERE 1=0"  # nosec B608
    cursor.execute(sql)
    return [desc[0] for desc in cursor.description]


def _safe_temp_stem(name):
    text = str(name)
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in text)
    cleaned = cleaned.strip("._") or "table"
    digest = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:10]
    return f"{cleaned[:80]}_{digest}"


def _require_access_odbc_driver(feedback=None):
    """Fail fast if the Access ODBC driver is not available in this Python/ODBC environment."""
    if pyodbc is None:
        raise QgsProcessingException(
            "pyodbc is required to read MDB files but could not be imported. "
            "Install pyodbc (and the Microsoft Access Database Engine / ODBC driver) for your QGIS Python environment."
        )

    try:
        drivers = [d.strip() for d in pyodbc.drivers()]
    except Exception as e:
        # Some environments can throw querying drivers; still try connect later.
        if feedback is not None:
            feedback.reportError(f"Unable to query ODBC drivers via pyodbc: {e}")
        drivers = []

    if drivers and ACCESS_ODBC_DRIVER_NAME not in drivers:
        msg = (
            "Microsoft Access ODBC driver not found. "
            f"Expected '{ACCESS_ODBC_DRIVER_NAME}'. "
            "Install the Microsoft Access Database Engine (matching your QGIS bitness) "
            "or configure an ODBC driver that can read .mdb/.accdb files."
        )
        if feedback is not None:
            feedback.reportError(f"Available ODBC drivers: {drivers}")
        raise QgsProcessingException(msg)


def _test_mdb_connection(mdb_file, feedback=None, timeout_seconds=5):
    """Attempt a short ODBC connect. This catches missing drivers, bitness mismatches, and corrupt DB early."""
    _require_access_odbc_driver(feedback)
    normalized = _normalize_mdb_path(mdb_file)
    if not os.path.isfile(normalized):
        raise QgsProcessingException(f"MDB file not found: {normalized}")
    conn_str = _access_connection_string(normalized)
    try:
        conn = pyodbc.connect(conn_str, timeout=timeout_seconds)
        try:
            cur = conn.cursor()
            # Cheap sanity query (doesn't read table data)
            _ = [t.table_name for t in cur.tables()]
        finally:
            conn.close()
    except Exception as e:
        if pyodbc is not None and isinstance(e, pyodbc.Error):
            sqlstate = e.args[0] if getattr(e, 'args', None) else ''
            raise QgsProcessingException(f"ODBC connection failed: {sqlstate} - {e}")
        raise QgsProcessingException(f"ODBC connection failed: {e}")


def create_wkt(geom_type, vertices):
    """Creates a WKT string with 3D support if vertices include z values."""
    if not vertices:
        return None

    # Determine dimensionality (assume each vertex is a 3-tuple)
    dim = 3 if len(vertices[0]) == 3 else 2

    if geom_type == "Point":
        if dim == 3:
            return f"POINT Z ({vertices[0][0]} {vertices[0][1]} {vertices[0][2]})"
        else:
            return f"POINT ({vertices[0][0]} {vertices[0][1]})"
    elif geom_type == "LineString":
        if dim == 3:
            coords = ", ".join(f"{x} {y} {z}" for (x, y, z) in vertices)
            return f"LINESTRING Z ({coords})"
        else:
            coords = ", ".join(f"{x} {y}" for (x, y, _) in vertices)
            return f"LINESTRING ({coords})"
    elif geom_type == "Polygon":
        # Ensure the ring is closed.
        if not is_closed(vertices) and len(vertices) >= 3:
            vertices.append(vertices[0])
        if dim == 3:
            coords = ", ".join(f"{x} {y} {z}" for (x, y, z) in vertices)
            return f"POLYGON Z (({coords}))"
        else:
            coords = ", ".join(f"{x} {y}" for (x, y, _) in vertices)
            return f"POLYGON (({coords}))"
    elif geom_type == "MultiPoint":
        # Create a MULTIPOINT WKT. Each point is enclosed in parentheses.
        if dim == 3:
            coords = ", ".join(f"({x} {y} {z})" for (x, y, z) in vertices)
            return f"MULTIPOINT Z ({coords})"
        else:
            coords = ", ".join(f"({x} {y})" for (x, y, _) in vertices)
            return f"MULTIPOINT ({coords})"
    else:
        return None


def get_feature_tables(mdb_file, feedback):
    """Retrieves feature tables and their geometry fields, handling variations in GFeatures."""
    try:
        _require_access_odbc_driver(feedback)
    except QgsProcessingException as e:
        feedback.reportError(str(e))
        return {}
    feature_tables = {}
    conn_str = _access_connection_string(mdb_file)
    try:
        with pyodbc.connect(conn_str) as conn:
            cursor = conn.cursor()
            available_tables = [table_info.table_name for table_info in cursor.tables()]
            feedback.pushInfo(f"Available tables in MDB: {available_tables}")

            gfeatures_table = None
            for table_info in cursor.tables():
                if table_info.table_name.upper() == 'GFEATURES':
                    gfeatures_table = table_info.table_name
                    break

            if not gfeatures_table:
                feedback.reportError("GFeatures table not found in MDB.")
                return {}

            col_names = _get_column_names(cursor, gfeatures_table)
            feedback.pushInfo(f"Columns in {gfeatures_table}: {col_names}")

            # Prefer a named feature column; do not assume the first column is the feature name.
            feature_name_col = None
            for col in col_names:
                if col and col.upper() in {"FEATURENAME", "FEATURECLASSNAME", "NAME"}:
                    feature_name_col = col
                    break
            if not feature_name_col:
                feature_name_col = col_names[0]
            geom_field_col = None
            geom_type_col = None
            for col in col_names:
                if col.upper() == "PRIMARYGEOMETRYFIELDNAME":
                    geom_field_col = col
                elif col.upper() == "GEOMETRYTYPE":
                    geom_type_col = col

            if not all([geom_field_col, geom_type_col]):
                feedback.reportError("Required columns (PRIMARYGEOMETRYFIELDNAME, GEOMETRYTYPE) not found in GFeatures table.")
                return {}

            # Identifiers are validated and bracket-quoted by
            # _quote_access_identifier(); values use ? placeholders. Access ODBC
            # cannot parameterise identifiers, so they are safely interpolated.
            sql = (
                "SELECT "  # nosec B608
                + ", ".join(
                    _quote_access_identifier(col)
                    for col in (feature_name_col, geom_field_col, geom_type_col)
                )
                + " FROM "
                + _quote_access_identifier(gfeatures_table)
            )
            feedback.pushInfo(f"Executing SQL: {sql}")
            cursor.execute(sql)

            for row in cursor.fetchall():
                table_name, geom_field, geometry_type = row
                feature_tables[table_name] = (geom_field, geometry_type)

    except Exception as e:
        if pyodbc is not None and isinstance(e, pyodbc.Error):
            sqlstate = e.args[0] if getattr(e, 'args', None) else ''
            feedback.reportError(f"ODBC error: {sqlstate} - {e}")
            return {}
        feedback.reportError(f"Error getting feature tables: {e}")
        return {}

    return feature_tables


def get_attribute_fields(mdb_file, table_name, feedback):
    """Gets attribute fields and types, handling reserved words and case."""
    try:
        _require_access_odbc_driver(feedback)
    except QgsProcessingException as e:
        feedback.reportError(str(e))
        return {}
    attribute_fields = {}
    conn_str = _access_connection_string(mdb_file)
    try:
        with pyodbc.connect(conn_str) as conn:
            cursor = conn.cursor()
            available_tables = [table_info.table_name for table_info in cursor.tables()]
            feedback.pushInfo(f"Available tables in MDB: {available_tables}")

            fieldlookup_table = None
            attributeprop_table = None
            for table_info in cursor.tables():
                if table_info.table_name.upper() == 'FIELDLOOKUP':
                    fieldlookup_table = table_info.table_name
                elif table_info.table_name.upper() == 'ATTRIBUTEPROPERTIES':
                    attributeprop_table = table_info.table_name

            if not fieldlookup_table or not attributeprop_table:
                feedback.reportError("FieldLookup or AttributeProperties table not found.")
                return {}

            fl_col_names = _get_column_names(cursor, fieldlookup_table)
            feedback.pushInfo(f"Columns in {fieldlookup_table}: {fl_col_names}")
            ap_col_names = _get_column_names(cursor, attributeprop_table)
            feedback.pushInfo(f"Columns in {attributeprop_table}: {ap_col_names}")

            fieldname_col = None
            featurename_col = None
            indid_col_fl = None
            indid_col_ap = None
            fieldtype_col = None

            for col in fl_col_names:
                if col.upper() == "FIELDNAME":
                    fieldname_col = col
                elif col.upper() == "FEATURENAME":
                    featurename_col = col
                elif col.upper() == "INDEXID":
                    indid_col_fl = col
            for col in ap_col_names:
                if col.upper() == "FIELDTYPE":
                    fieldtype_col = col
                elif col.upper() == "INDEXID":
                    indid_col_ap = col

            if not all([fieldname_col, featurename_col, indid_col_fl, fieldtype_col, indid_col_ap]):
                feedback.reportError("Required columns not found in metadata tables. Check column names in FieldLookup and AttributeProperties.")
                return {}

            sql = (
                "SELECT fl."  # nosec B608
                + _quote_access_identifier(fieldname_col)
                + ", ap."
                + _quote_access_identifier(fieldtype_col)
                + " FROM "
                + _quote_access_identifier(fieldlookup_table)
                + " AS fl INNER JOIN "
                + _quote_access_identifier(attributeprop_table)
                + " AS ap ON fl."
                + _quote_access_identifier(indid_col_fl)
                + " = ap."
                + _quote_access_identifier(indid_col_ap)
                + " WHERE fl."
                + _quote_access_identifier(featurename_col)
                + " = ?"
            )
            feedback.pushInfo(f"Executing SQL: {sql}")
            cursor.execute(sql, table_name)

            for row in cursor.fetchall():
                field_name, field_type = row
                field_type_str = get_field_type_string(field_type)
                attribute_fields[field_name] = field_type_str

    except Exception as e:
        if pyodbc is not None and isinstance(e, pyodbc.Error):
            sqlstate = e.args[0] if getattr(e, 'args', None) else ''
            feedback.reportError(f"ODBC error: {sqlstate} - {e}")
            return {}
        feedback.reportError(f"Error getting attribute fields for {table_name}: {e}")
        return {}

    return attribute_fields


def get_field_type_string(field_type_code):
    """Converts a numeric field type code to a string (PLACEHOLDER)."""
    if field_type_code == 4:
        return "INTEGER"
    elif field_type_code == 10:
        return "TEXT"
    elif field_type_code == 7:
        return "DOUBLE"
    else:
        return "UNKNOWN"


def import_table_as_memory_layer(mdb_file, table_name, geom_field_name, geometry_type_code, import_crs, feedback):
    """Imports a single feature table as a memory layer."""
    try:
        _require_access_odbc_driver(feedback)
    except QgsProcessingException as e:
        return None, str(e)

    conn_str = _access_connection_string(mdb_file)
    try:
        with pyodbc.connect(conn_str) as conn:
            cursor = conn.cursor()
            if not geom_field_name and geometry_type_code == GRAPHIC_TYPE_CODE:
                # Text classes leave PrimaryGeometryFieldName empty in
                # GFeatures; find the BLOB column from the table schema.
                candidates = find_candidate_geometry_fields(
                    _get_column_names(cursor, table_name))
                if not candidates:
                    return None, f"No geometry BLOB column found in text table {table_name}"
                geom_field_name = candidates[0]
            sql = (
                "SELECT * FROM "  # nosec B608
                + _quote_access_identifier(table_name)
                + " WHERE "
                + _quote_access_identifier(geom_field_name)
                + " IS NOT NULL"
            )
            feedback.pushInfo(f"Executing SQL: {sql}")
            cursor.execute(sql)
            col_names = [desc[0] for desc in cursor.description]

            if geom_field_name not in col_names:
                return None, f"Geometry field '{geom_field_name}' not found in table {table_name}"

            geom_index = col_names.index(geom_field_name)

            first_row = cursor.fetchone()
            if not first_row:
                return None, f"No records with non-null geometry in table {table_name}"

            # Determine layer geometry type.
            if geometry_type_code == 1:
                layer_type = "LineString"
            elif geometry_type_code == 2:
                layer_type = "Polygon"
            elif geometry_type_code in (3, GRAPHIC_TYPE_CODE):
                layer_type = "Point"
            elif geometry_type_code == 10:
                # For code 10, inspect the first feature.
                test_blob = first_row[geom_index]
                test_vertices = parse_blob(test_blob)
                if test_vertices is None:
                    feedback.reportError(f"Failed to parse geometry for first row in table {table_name}")
                    return None, "Failed to parse geometry for geometry type code 10"
                if len(test_vertices) == 1:
                    layer_type = "Point"
                elif is_closed(test_vertices) and len(test_vertices) >= 4:
                    layer_type = "Polygon"
                else:
                    layer_type = "MultiPoint"
            else:
                feedback.reportError(f"Unsupported geometry type code: {geometry_type_code} in table {table_name}")
                return None, f"Unsupported geometry type code: {geometry_type_code}"

            # Create the memory layer.
            mem_layer = QgsVectorLayer(f"{layer_type}?crs={import_crs.authid()}", table_name, "memory")
            dp = mem_layer.dataProvider()

            attribute_fields = get_attribute_fields(mdb_file, table_name, feedback)
            attribute_field_names = [n for n in attribute_fields.keys() if n != geom_field_name]

            fields = []
            for field_name in attribute_field_names:
                field_type = attribute_fields.get(field_name)
                if field_type == "INTEGER":
                    fields.append(QgsField(field_name, FIELD_TYPE_INT))
                elif field_type == "DOUBLE":
                    fields.append(QgsField(field_name, FIELD_TYPE_DOUBLE))
                else:
                    fields.append(QgsField(field_name, FIELD_TYPE_STRING))
            # Add extra fields.
            if geometry_type_code == GRAPHIC_TYPE_CODE:
                fields.append(QgsField("label_text", FIELD_TYPE_STRING))
            fields.append(QgsField("depth", FIELD_TYPE_DOUBLE))
            fields.append(QgsField("source", FIELD_TYPE_STRING))
            dp.addAttributes(fields)
            mem_layer.updateFields()

            # Iterate rows in a streaming fashion to reduce memory pressure and driver stress.
            max_features = getattr(feedback, "_subsea_mdb_max_features", 0) or 0
            batch_size = getattr(feedback, "_subsea_mdb_batch_size", 1000) or 1000
            features_batch = []
            source_name = os.path.basename(mdb_file)

            processed = 0
            written = 0
            skipped_parse = 0
            skipped_invalid = 0

            def row_iter():
                yield first_row
                for r in cursor:
                    yield r

            for row in row_iter():
                if feedback.isCanceled():
                    break

                processed += 1
                if max_features and processed > max_features:
                    break

                blob = row[geom_index]
                label_text = None
                if geometry_type_code == GRAPHIC_TYPE_CODE:
                    decoded = decode_geometry_blob(blob)
                    label_text = decoded.text if decoded is not None else None
                vertices = parse_blob(blob)
                if not vertices:
                    skipped_parse += 1
                    continue

                # Skip any geometries containing NaN/Inf coordinates.
                if any((not math.isfinite(x) or not math.isfinite(y) or not math.isfinite(z)) for (x, y, z) in vertices):
                    skipped_invalid += 1
                    continue

                if layer_type == "Polygon" and not is_closed(vertices):
                    vertices.append(vertices[0])

                wkt = create_wkt(layer_type, vertices)
                if not wkt:
                    skipped_invalid += 1
                    continue

                geom = QgsGeometry.fromWkt(wkt)
                if geom is None or geom.isEmpty():
                    skipped_invalid += 1
                    continue

                feat = QgsFeature(mem_layer.fields())
                feat.setGeometry(geom)

                depths = [v[2] for v in vertices]
                avg_depth = sum(depths) / len(depths) if depths else None

                row_dict = dict(zip(col_names, row))
                attr_values = []
                for field_name in attribute_field_names:
                    value = row_dict.get(field_name)
                    field_def = mem_layer.fields().field(field_name)
                    try:
                        if field_def.type() == FIELD_TYPE_INT:
                            attr_values.append(int(value) if value is not None else None)
                        elif field_def.type() == FIELD_TYPE_DOUBLE:
                            attr_values.append(float(value) if value is not None else None)
                        else:
                            attr_values.append(str(value) if value is not None else "")
                    except (ValueError, TypeError):
                        attr_values.append(None)

                if geometry_type_code == GRAPHIC_TYPE_CODE:
                    attr_values.append(label_text if label_text is not None else "")
                attr_values.append(avg_depth)
                attr_values.append(source_name)
                feat.setAttributes(attr_values)

                features_batch.append(feat)
                if len(features_batch) >= batch_size:
                    dp.addFeatures(features_batch)
                    written += len(features_batch)
                    features_batch = []

            if features_batch:
                dp.addFeatures(features_batch)
                written += len(features_batch)

            feedback.pushInfo(
                f"{table_name}: processed={processed}, written={written}, "
                f"skipped_parse={skipped_parse}, skipped_invalid={skipped_invalid}"
            )
            mem_layer.updateExtents()
            return mem_layer, None

    except Exception as e:
        if pyodbc is not None and isinstance(e, pyodbc.Error):
            sqlstate = e.args[0] if getattr(e, 'args', None) else ''
            feedback.reportError(f"ODBC error: {sqlstate} - {e}")
            return None, str(e)
        feedback.reportError(
            f"Error processing table {table_name}: {e}\n" + traceback.format_exc()
        )
        return None, str(e)


def is_closed(vertices, tol=1e-6):
    """Checks if the first and last vertices are nearly equal."""
    if len(vertices) < 2:
        return False
    x0, y0, _ = vertices[0]
    xn, yn, _ = vertices[-1]
    return abs(x0 - xn) <= tol and abs(y0 - yn) <= tol


class _MdbFieldValueConverter(QgsVectorFileWriter.FieldValueConverter):
    def __init__(self, renamed_fields):
        super().__init__()
        self._renamed_fields = renamed_fields

    def fieldDefinition(self, field):
        output_field = QgsField(field)
        output_field.setName(self._renamed_fields.get(field.name(), field.name()))
        return output_field

    def convert(self, field_index, value):
        return value


def resolve_gpkg_field_names(source_fields):
    """Return ``(renamed, reserved)`` for GeoPackage-safe field names.

    GeoPackage column names are case-insensitive, so a source ``Depth`` column
    and the derived ``depth`` attribute collide and the whole table fails to be
    created. First occurrences keep their name — source attributes are listed
    before derived ones — and later collisions are suffixed. ``fid`` is always
    renamed because it is the GeoPackage primary key.
    """
    reserved = set()
    deferred = []
    for field in source_fields:
        name = field.name()
        key = name.casefold()
        if key == "fid" or key in reserved:
            deferred.append(name)
            continue
        reserved.add(key)

    renamed = {}
    for name in deferred:
        base = "source_fid" if name.casefold() == "fid" else name
        candidate = base
        suffix = 2
        while candidate.casefold() in reserved:
            candidate = f"{base}_{suffix}"
            suffix += 1
        reserved.add(candidate.casefold())
        renamed[name] = candidate
    return renamed, reserved


def _write_to_temporary_gpkg(source_layer, layer_name, source_crs, context, feedback):
    """Stream a worker layer to an indexed, session-managed GeoPackage.

    ``source_crs`` is *assigned* to the incoming CRS-less GeoJSON layer. No
    coordinate transform is performed; MDB coordinates are written unchanged.
    """
    if source_crs and source_crs.isValid():
        source_layer.setCrs(source_crs)

    gpkg_path = processing_generate_temp_filename(
        _safe_temp_stem(layer_name) + ".gpkg",
        context,
    )
    storage_layer_name = _safe_temp_stem(layer_name)
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG"
    options.layerName = storage_layer_name

    renamed_fields, reserved_names = resolve_gpkg_field_names(list(source_layer.fields()))
    if renamed_fields:
        feedback.pushInfo(
            "  Renamed for GeoPackage (column names are case-insensitive): "
            + ", ".join(f"{old} -> {new}" for old, new in sorted(renamed_fields.items()))
        )

    fid_name = "__subsea_fid"
    suffix = 2
    while fid_name.casefold() in reserved_names:
        fid_name = f"__subsea_fid_{suffix}"
        suffix += 1
    field_converter = _MdbFieldValueConverter(renamed_fields)
    options.fieldValueConverter = field_converter
    options.layerOptions = ["SPATIAL_INDEX=YES", f"FID={fid_name}"]
    if hasattr(options, "feedback"):
        options.feedback = feedback

    result = QgsVectorFileWriter.writeAsVectorFormatV3(
        source_layer,
        gpkg_path,
        context.transformContext(),
        options,
    )
    writer_error = result[0] if isinstance(result, tuple) else result
    error_message = result[1] if isinstance(result, tuple) and len(result) > 1 else ""
    error_scope = getattr(QgsVectorFileWriter, "WriterError", QgsVectorFileWriter)
    if writer_error != getattr(error_scope, "NoError"):
        feedback.reportError(f"Could not create disk-backed layer {layer_name}: {error_message}")
        return None

    layer = QgsVectorLayer(
        f"{gpkg_path}|layername={storage_layer_name}",
        layer_name,
        "ogr",
    )
    if not layer.isValid():
        feedback.reportError(f"Could not open disk-backed layer {layer_name}")
        return None
    return layer


class ImportMdbAlgorithm(QgsProcessingAlgorithm):
    INPUT_MDB = 'INPUT_MDB'
    # The stored key stays 'TARGET_CRS' so existing models and scripts keep
    # working. It has only ever assigned the CRS of the MDB coordinates.
    SOURCE_CRS = 'TARGET_CRS'
    TARGET_CRS = SOURCE_CRS
    OUTPUT_LAYERS = 'OUTPUT_LAYERS'

    #: Geometry types loaded without SUBSEA_MDB_LOAD_ALL_GEOMS=1.
    DEFAULT_GEOMETRY_TYPES = frozenset({'LineString', 'Polygon', 'Point'})

    @classmethod
    def _should_load_geometry_type(cls, geometry_type_name, load_all_geoms):
        return bool(load_all_geoms) or str(geometry_type_name) in cls.DEFAULT_GEOMETRY_TYPES

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterMultipleLayers(
            self.INPUT_MDB,
            self.tr('Input MDB File(s)'),
            QgsProcessing.TypeFile,
        ))
        self.addParameter(QgsProcessingParameterCrs(
            self.SOURCE_CRS,
            self.tr('Source CRS / CRS of coordinates in MDB'),
            optional=False,
        ))
        self.addOutput(QgsProcessingOutputMultipleLayers(self.OUTPUT_LAYERS, self.tr('Imported Layers')))

    def _run_worker(self, args, feedback, timeout=600):
        worker_path = os.path.join(os.path.dirname(__file__), 'mdb_odbc_worker.py')

        # In QGIS on Windows, sys.executable is often qgis-bin.exe (NOT a Python interpreter).
        # Prefer the bundled python3.exe / python.exe next to qgis-bin.exe.
        exe_dir = os.path.dirname(sys.executable)
        candidates = [
            os.environ.get('QGIS_PYTHON_EXECUTABLE', ''),
            os.path.join(exe_dir, 'python3.exe'),
            os.path.join(exe_dir, 'python.exe'),
            os.path.join(exe_dir, 'python3'),
            os.path.join(exe_dir, 'python'),
            os.path.join(exe_dir, 'python-qgis.bat'),
            shutil.which('python3') or '',
            sys.executable,
        ]
        python_exe = ''
        for c in candidates:
            if c and os.path.exists(c) and os.path.basename(c).lower().startswith('python'):
                python_exe = c
                break
        if not python_exe:
            # Fallback: last resort (may still be qgis-bin.exe)
            python_exe = sys.executable

        cmd = [python_exe, '-u', worker_path] + args
        feedback.pushInfo('Running MDB worker: ' + ' '.join(cmd))
        if feedback.isCanceled():
            raise QgsProcessingException('MDB import canceled.')
        try:
            process = subprocess.Popen(  # nosec B603,B607 - trusted worker path, no shell, args passed as a list.
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
            deadline = None if not timeout else time.monotonic() + timeout
            # The pipes must be drained WHILE the worker runs: a worker whose
            # stdout exceeds the OS pipe buffer (a few KB) blocks on its final
            # write and never exits, deadlocking a poll()-only loop.
            # communicate(timeout=...) starts background reader threads on the
            # first call and resumes them on every retry, so it both drains and
            # stays cancellable.
            while True:
                if feedback.isCanceled():
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    try:
                        process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                    raise QgsProcessingException('MDB import canceled.')
                if deadline is not None and time.monotonic() >= deadline:
                    process.kill()
                    try:
                        process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                    raise QgsProcessingException(f'MDB worker timed out after {timeout} seconds.')
                try:
                    stdout, stderr = process.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    continue
        except Exception as e:
            if isinstance(e, QgsProcessingException):
                raise
            raise QgsProcessingException(f'Failed to run MDB worker: {e}')

        if process.returncode != 0:
            # Worker writes JSON to stderr on error.
            err = (stderr or '').strip()
            try:
                err_obj = json.loads(err) if err else None
            except Exception:
                err_obj = None

            if err_obj and isinstance(err_obj, dict):
                msg = err_obj.get('error') or 'MDB worker failed'
                tb = err_obj.get('traceback')
                if tb:
                    feedback.reportError(tb)
                raise QgsProcessingException(msg)

            msg = (stderr or stdout or '').strip()
            if not msg:
                msg = (
                    f"MDB worker failed with exit code {process.returncode}. "
                    "This often indicates a native ODBC/Access driver crash or bitness mismatch."
                )
            raise QgsProcessingException(msg)

        out = (stdout or '').strip()
        if not out:
            return None
        return json.loads(out)

    @staticmethod
    def _register_output_layer(context, layer, layer_name, group_name):
        layer.setName(layer_name)
        context.temporaryLayerStore().addMapLayer(layer)
        details = QgsProcessingContext.LayerDetails(layer_name, context.project())
        details.forceName = True
        if hasattr(details, 'groupName'):
            details.groupName = group_name
        context.addLayerToLoadOnCompletion(layer.id(), details)

    def processAlgorithm(self, parameters, context, feedback):
        # The bundled pure-Python reader works on any platform; only fall back
        # to requiring Windows/ODBC when it is missing (broken install).
        have_pure_reader = os.path.isdir(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'lib', 'access_parser'))
        if os.name != 'nt' and not have_pure_reader:
            raise QgsProcessingException(
                "MDB import needs the plugin's bundled MDB reader (lib/access_parser is "
                "missing — try reinstalling the plugin) or Windows with the Microsoft "
                "Access Database Engine ODBC driver."
            )

        mdb_files = self.parameterAsFileList(parameters, self.INPUT_MDB, context)
        source_crs = self.parameterAsCrs(parameters, self.SOURCE_CRS, context)
        if not source_crs or not source_crs.isValid():
            raise QgsProcessingException(
                "No valid CRS provided. Set the Source CRS of the coordinates in the MDB.")

        normalized_files = []
        seen_paths = set()
        for mdb_file in mdb_files:
            normalized = os.path.abspath(os.path.normpath(os.fspath(mdb_file)))
            normalized_key = os.path.normcase(normalized)
            if normalized_key in seen_paths:
                continue
            seen_paths.add(normalized_key)
            if not os.path.isfile(normalized):
                raise QgsProcessingException(f"MDB file not found: {normalized}")
            if os.path.splitext(normalized)[1].lower() not in {'.mdb', '.accdb'}:
                raise QgsProcessingException(f"Input must be a .mdb or .accdb file: {normalized}")
            normalized_files.append(normalized)

        if not normalized_files:
            raise QgsProcessingException("Select at least one MDB or ACCDB file.")

        total_input_bytes = sum(os.path.getsize(path) for path in normalized_files)
        feedback.pushInfo(
            f"Selected {len(normalized_files)} database(s), "
            f"{total_input_bytes / (1024 * 1024):.1f} MB total. "
            "Outputs are stored as disk-backed temporary GeoPackages to limit RAM use."
        )

        isolate = os.environ.get('SUBSEA_MDB_NO_SUBPROCESS', '0') not in {'1', 'true', 'True'}
        keep_temp = os.environ.get('SUBSEA_MDB_KEEP_TEMP', '0') in {'1', 'true', 'True'}
        load_all_geoms = os.environ.get('SUBSEA_MDB_LOAD_ALL_GEOMS', '0') in {'1', 'true', 'True'}
        schema_discovery = os.environ.get('SUBSEA_MDB_SCHEMA_DISCOVERY', '0') in {'1', 'true', 'True'}
        max_features_env = os.environ.get('SUBSEA_MDB_MAX_FEATURES', '0')
        try:
            max_features = int(max_features_env)
        except Exception:
            max_features = 0

        output_layers = {}
        failures = []
        for file_index, mdb_file in enumerate(normalized_files, start=1):
            if feedback.isCanceled():
                raise QgsProcessingException("MDB import canceled.")
            file_label = os.path.basename(mdb_file)
            feedback.setProgress(int((file_index - 1) * 100 / len(normalized_files)))
            feedback.setProgressText(
                f"Importing database {file_index} of {len(normalized_files)}: {file_label}"
            )
            feedback.pushInfo(f"Importing MDB {file_index} of {len(normalized_files)}: {file_label}")
            try:
                file_outputs = self._process_mdb_file(
                    mdb_file,
                    source_crs,
                    context,
                    feedback,
                    isolate=isolate,
                    keep_temp=keep_temp,
                    load_all_geoms=load_all_geoms,
                    max_features=max_features,
                    schema_discovery=schema_discovery,
                )
            except Exception as exc:  # noqa: BLE001 - one bad file must not kill the batch
                if feedback.isCanceled():
                    raise QgsProcessingException("MDB import canceled.")
                failures.append(f"{file_label}: {exc}")
                feedback.reportError(failures[-1])
                continue
            output_layers.update(file_outputs)
            feedback.setProgress(int(file_index * 100 / len(normalized_files)))

        if not output_layers:
            detail = "; ".join(failures)
            message = "No valid layers were imported from the selected MDB files."
            raise QgsProcessingException(f"{message} {detail}" if detail else message)

        if failures:
            feedback.reportError(
                f"Imported {len(normalized_files) - len(failures)} of {len(normalized_files)} MDB files."
            )

        feedback.setProgress(100)
        return {self.OUTPUT_LAYERS: output_layers}

    def _process_mdb_file(
        self,
        mdb_file,
        source_crs,
        context,
        feedback,
        isolate,
        keep_temp,
        load_all_geoms,
        max_features,
        schema_discovery=False,
    ):
        file_name = os.path.basename(mdb_file)
        file_ref = os.path.splitext(file_name)[0]
        output_namespace = os.path.normcase(os.path.abspath(mdb_file))

        temp_root = processing_temp_folder(context)
        free_bytes = shutil.disk_usage(temp_root).free
        required_bytes = max(512 * 1024 * 1024, os.path.getsize(mdb_file) * 4)
        if free_bytes < required_bytes:
            raise QgsProcessingException(
                f"Insufficient temporary disk space for {file_name}: "
                f"{free_bytes / (1024 ** 3):.1f} GB free, approximately "
                f"{required_bytes / (1024 ** 3):.1f} GB required. "
                "Free space or set a different Processing temporary folder."
            )

        temp_dir = ''
        if isolate:
            # In isolate mode we intentionally avoid touching ODBC in-process.
            if keep_temp:
                temp_dir = tempfile.mkdtemp(prefix='subsea_mdb_')
                feedback.pushInfo(f'Keeping worker files in: {temp_dir}')
            else:
                temp_marker = processing_generate_temp_filename('mdb_worker', context)
                temp_dir = os.path.dirname(temp_marker)
                feedback.pushInfo(f'Using managed temp dir: {temp_dir}')

            listing = self._run_worker(
                [
                    '--mode', 'list',
                    '--mdb', mdb_file,
                    '--schema-discovery', '1' if schema_discovery else '0',
                ],
                feedback,
            )
            discovered, non_spatial = self._read_listing(listing)
            self._report_non_spatial_tables(non_spatial, feedback)
            if not discovered:
                raise QgsProcessingException('No feature tables found in the MDB (worker list returned empty).')

            feature_tables = {
                name: (meta.get('geom_field_name'), meta.get('geometry_type_code'))
                for name, meta in discovered.items()
            }
            for name, meta in discovered.items():
                if meta.get('discovery') == 'schema':
                    feedback.pushInfo(
                        f"  {name}: not registered in GFeatures; discovered from the table schema"
                    )
        else:
            # Expert-only fallback for debugging environments where subprocess execution is blocked.
            feedback.reportError(
                'SUBSEA_MDB_NO_SUBPROCESS is enabled. Running ODBC reads in-process; this may crash QGIS.'
            )
            _require_access_odbc_driver(feedback)
            _test_mdb_connection(mdb_file, feedback=feedback)
            feature_tables = get_feature_tables(mdb_file, feedback)

        if not feature_tables:
            raise QgsProcessingException("No feature tables found in the MDB.")

        if source_crs and source_crs.isValid():
            feedback.pushInfo(
                f"Assigning source CRS {source_crs.authid()} to the imported coordinates "
                "(no reprojection is performed)."
            )
        else:
            raise QgsProcessingException(
                "No valid CRS provided. Set the Source CRS of the coordinates in the MDB.")

        output_layers = {}
        table_count = len(feature_tables)
        for table_index, (table_name, (geom_field_name, geometry_type_code)) in enumerate(
            feature_tables.items(),
            start=1,
        ):
            if feedback.isCanceled():
                raise QgsProcessingException("MDB import canceled.")
            feedback.setProgressText(
                f"{file_name}: table {table_index} of {table_count} - {table_name}"
            )
            feedback.pushInfo(f"Processing table: {table_name}")

            if isolate:
                out_base = os.path.join(temp_dir, _safe_temp_stem(table_name))
                # Always split in the worker.
                # Rationale: GeoMedia MDB metadata can mislabel geometry types; splitting is the most
                # reliable way to prevent LineString features being imported as Points.
                info = self._run_worker(
                    [
                        '--mode', 'export',
                        '--mdb', mdb_file,
                        '--table', table_name,
                        '--geom-field', str(geom_field_name or ''),
                        '--geom-type', str(int(geometry_type_code)) if geometry_type_code is not None else '-1',
                        '--out', out_base,
                        '--max-features', str(int(max_features or 0)),
                        '--split', '1',
                    ],
                    feedback,
                )
                if not isinstance(info, dict):
                    feedback.reportError(
                        f'Skipping table {table_name}: the worker returned no result'
                    )
                    continue

                outputs = info.get('outputs') or {}
                self._report_table_result(table_name, info, feedback)
                if not outputs:
                    continue

                for geom_type_name, path in outputs.items():
                    if not self._should_load_geometry_type(geom_type_name, load_all_geoms):
                        feedback.pushInfo(
                            f"  Skipping {geom_type_name} layer for '{table_name}' "
                            "(set SUBSEA_MDB_LOAD_ALL_GEOMS=1 to include)")
                        continue
                    if not path or not os.path.exists(path):
                        feedback.reportError(
                            f"  {table_name}: the worker reported a {geom_type_name} layer "
                            "but the file is missing")
                        continue
                    table_label = table_name
                    if len(outputs) > 1:
                        table_label = f"{table_name} ({geom_type_name})"
                    layer_name = f"{file_ref} - {table_label}"
                    src_layer = QgsVectorLayer(path, layer_name, 'ogr')
                    if not src_layer.isValid():
                        feedback.reportError(f'Skipping {layer_name}: output layer invalid')
                        continue
                    layer = _write_to_temporary_gpkg(
                        src_layer,
                        layer_name,
                        source_crs,
                        context,
                        feedback,
                    )
                    if layer is None:
                        continue

                    self._register_output_layer(context, layer, layer_name, file_name)
                    output_layers[f"{output_namespace}::{table_name}::{geom_type_name}"] = layer.id()
                continue
            else:
                mem_layer, error = import_table_as_memory_layer(
                    mdb_file,
                    table_name,
                    geom_field_name,
                    geometry_type_code,
                    source_crs,
                    feedback,
                )
                if error:
                    feedback.reportError(f"Skipping table {table_name}: {error}")
                    continue

                # IMPORTANT: Do NOT add layers directly to QgsProject from a processing algorithm.
                # Algorithms may run in a background thread and direct project mutations can crash QGIS.
                layer_name = f"{file_ref} - {table_name}"
                self._register_output_layer(context, mem_layer, layer_name, file_name)
                output_layers[f"{output_namespace}::{table_name}"] = mem_layer.id()

        # Cleanup runs only after every worker file has been opened, copied to a
        # GeoPackage and validated above. Failures here never fail the import.
        if isolate and temp_dir and not keep_temp:
            self._cleanup_worker_temp(temp_dir, feedback)

        if not output_layers:
            raise QgsProcessingException("No valid layers were imported from the MDB.")

        return output_layers

    @staticmethod
    def _cleanup_worker_temp(temp_dir, feedback):
        try:
            leftovers = os.listdir(temp_dir)
        except Exception as exc:  # noqa: BLE001 - cleanup is best effort
            feedback.pushDebugInfo(f'Could not list worker temp dir for cleanup: {exc}')
            return
        failed = 0
        for name in leftovers:
            try:
                os.remove(os.path.join(temp_dir, name))
            except Exception:  # noqa: BLE001
                failed += 1
        try:
            os.rmdir(temp_dir)
        except Exception as exc:  # noqa: BLE001
            feedback.pushDebugInfo(f'Worker temp dir left in place: {exc}')
        if failed:
            feedback.pushDebugInfo(f'{failed} worker temp file(s) could not be removed.')

    @staticmethod
    def _read_listing(listing):
        """Return ``(tables, non_spatial)`` from a worker discovery response.

        Accepts both the structured envelope and the older flat mapping.
        """
        if not isinstance(listing, dict):
            return {}, []
        if 'tables' in listing and isinstance(listing.get('tables'), dict):
            return listing['tables'], listing.get('non_spatial') or []
        return listing, []

    @staticmethod
    def _report_non_spatial_tables(non_spatial, feedback):
        for entry in non_spatial or []:
            if not isinstance(entry, dict):
                continue
            name = entry.get('table', '?')
            rows = entry.get('row_count')
            row_text = f"{rows} rows" if isinstance(rows, int) else "row count unavailable"
            feedback.pushInfo(
                f"  {name}: {row_text}; {entry.get('reason', 'no geometry')}; "
                "treated as a non-spatial table"
            )

    @staticmethod
    def _summarise_table_result(info):
        outputs = info.get('outputs') or {}
        parts = [
            f"rows={info.get('row_count', 0)}",
            f"non-null BLOBs={info.get('non_null_geometry_count', 0)}",
            f"BLOB decoded={info.get('blob_decoded_count', 0)}",
        ]
        if info.get('secondary_blob_decoded_count'):
            parts.append(f"secondary BLOB decoded={info['secondary_blob_decoded_count']}")
        parts.append(f"XY fallback={info.get('xy_fallback_count', 0)}")
        if info.get('invalid_geometry_count'):
            parts.append(f"unusable rows={info['invalid_geometry_count']}")
        fields_used = info.get('geometry_fields_used') or []
        if len(fields_used) > 1:
            parts.append("geometry fields=" + ", ".join(str(name) for name in fields_used))
        parts.append(
            "outputs=" + (", ".join(sorted(outputs)) if outputs else "none"))
        summary = f"{info.get('table', '?')}: " + ", ".join(parts)
        message = info.get('message')
        return f"{summary}; {message}" if message else summary

    @classmethod
    def _report_table_result(cls, table_name, info, feedback):
        """Log a per-table summary, escalating populated-but-unusable tables."""
        status = info.get('status')
        summary = cls._summarise_table_result(info)
        if status == 'success':
            feedback.pushInfo(f"  {summary}")
        elif status == 'empty':
            feedback.pushInfo(f"  {summary}")
        else:
            # Rows exist but nothing could be turned into geometry: that is a
            # real problem and must not look like an empty table.
            feedback.reportError(f"  {summary}")

    def name(self):
        return 'import_mdb'

    def displayName(self):
        return self.tr('Import MDB')

    def group(self):
        return self.tr('MDB Tools')

    def groupId(self):
        return 'mdb_tools'

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return ImportMdbAlgorithm()

    def shortHelpString(self):
        return self.tr("""<h3>Import MDB (Experimental)</h3>
<p><b><font color="red">Warning:</font> This tool is experimental and may not work with all GeoMedia-formatted MDB files. Use with caution.</b></p>
<p>This tool imports feature tables from one or more Microsoft Access Database (.mdb or .accdb) files, typically created by Intergraph GeoMedia, into QGIS as new temporary layers. It is not limited to bathymetry &ndash; any GeoMedia feature class (contours, seabed classifications, survey points, infrastructure polygons, etc.) can be loaded.</p>

<h4>How it Works</h4>
<p>The tool connects to the MDB file and looks for a <code>GFeatures</code> table to identify the feature classes within the database. For each feature class found, it reads the geometry from a binary (BLOB) field and creates a corresponding QGIS layer. Setting <code>SUBSEA_MDB_SCHEMA_DISCOVERY=1</code> additionally inspects every physical table so that populated tables missing from <code>GFeatures</code> can be offered when they carry strong spatial evidence (a GeoMedia geometry field or a recognised coordinate pair); metadata, lookup and companion <code>*_Name</code>/<code>*_Text</code> tables are reported but never loaded as geometry layers. That extra pass costs a table-definition parse per table, so it is off by default.</p>
<p>GeoMedia point, oriented point, polyline, polygon, boundary (polygons with holes), collection and graphic-text BLOBs are all decoded. Text feature classes (for example <code>Description</code>, <code>Sediment_Classification</code> or <code>*_Name</code> annotation tables) import as point layers carrying the label string in a <code>label_text</code> field, ready for labelling in QGIS. If a row's BLOB cannot be decoded and the table has an explicit coordinate pair (Easting/Northing, X/Y, Longitude/Latitude or Lon/Lat, matched case-insensitively), a point is built from those columns instead. Fallback geometry is never silent: every feature records how its geometry was obtained and the log reports BLOB-decoded and fallback counts separately.</p>
<p>By default, the tool imports <b>LineString</b> layers (e.g. bathymetric contour lines, cable routes), <b>Polygon</b> layers (e.g. seabed feature classifications, sediment type areas, restricted areas), and <b>Point</b> layers (e.g. survey points, fixes, assets). Each geometry type is loaded as a separate layer so they never conflict. MultiPoint and other multi-part geometries can be included by setting the <code>SUBSEA_MDB_LOAD_ALL_GEOMS=1</code> environment variable.</p>
<p>It automatically adds three fields to each new layer:
<ul>
  <li><b>depth:</b> The average Z-value of the feature's vertices, if available (useful for bathymetric data; will be empty/zero for non-3D features).</li>
    <li><b>source:</b> The filename of the source MDB file for per-feature traceability.</li>
    <li><b>geometry_source:</b> <code>blob</code> when the geometry came from the GeoMedia BLOB, or <code>xy_fallback</code> when it was built from a coordinate pair.</li>
</ul>
Text features additionally carry a <b>label_text</b> field holding the decoded label string.

</p>

<h4>Prerequisites</h4>
<p><b>None for typical files:</b> the plugin bundles a pure-Python MDB reader, so the tool works out of the box on Windows, macOS, and Linux &mdash; no Microsoft Access Database Engine, ODBC driver, or pyodbc installation is needed.</p>
<p>If the bundled reader cannot handle a particular file (for example a password-protected or unusual Jet variant), the tool automatically falls back to ODBC, which requires Windows with the <b>Microsoft Access Database Engine</b> driver and <code>pyodbc</code> installed in the QGIS Python environment.</p>
<p><b>Stability note:</b> Each MDB is read sequentially in a separate, cancellable subprocess. Results are converted to indexed, disk-backed temporary GeoPackages instead of being copied into RAM. Progress shows the current database and table. Large batches can still take time and require temporary disk space; the tool checks for practical free-space headroom before each file.</p>

<h4>Input Parameters</h4>
<ul>
    <li><b>Input MDB File(s):</b> Add one or more GeoMedia MDB/ACCDB files. The picker supports selecting several files at once.</li>
  <li><b>Source CRS / CRS of coordinates in MDB:</b> You <b>must</b> manually select the Coordinate Reference System (CRS) that the coordinates in the MDB are stored in. The tool cannot detect it. This CRS is <i>assigned</i> to the imported layers &mdash; no reprojection is performed &mdash; so providing the wrong CRS will result in misplaced data.</li>
</ul>

<h4>Outputs</h4>
<ul>
    <li><b>Imported Layers:</b> The tool creates an indexed temporary GeoPackage layer for each feature table and geometry type successfully imported. Layer names are prefixed with the source file reference, and on QGIS 3.32 or newer each file's layers are placed in a group named after that file. For example, a table containing both line and polygon features will produce separate layers.</li>
</ul>

<h4>Known Limitations & Troubleshooting</h4>
<ul>
  <li><b>BLOB Format:</b> GeoMedia point, polyline, polygon, boundary, collection and graphic-text BLOBs are supported. Other vendor-specific variants (for example arc primitives) are still reported as <code>parse_failed</code> rather than imported. Text placement (rotation, alignment, font) is not preserved &mdash; only the anchor point and the string.</li>
  <li><b>Metadata Tables:</b> It relies on specific system tables like <code>GFeatures</code>, <code>FieldLookup</code>, and <code>AttributeProperties</code>. If these are missing or have an unexpected structure, only schema-based discovery is available.</li>
    <li><b>Large batches:</b> Temporary GeoPackages remain available for the QGIS session. If space is limited, change the temporary folder under Processing settings to a drive with more free space. Canceling Processing terminates the active MDB worker.</li>
    <li><b>Errors:</b> Every attempted table reports row counts, BLOB-decoded counts and fallback counts in the Log Messages Panel. A populated table that yields no geometry is reported as an error, not silently skipped. Other tables and files continue importing where possible.</li>
  <li><b>Advanced options (env vars):</b>
    <ul>
      <li><code>SUBSEA_MDB_KEEP_TEMP=1</code> &ndash; keeps intermediate GeoJSONs for debugging</li>
      <li><code>SUBSEA_MDB_MAX_FEATURES=N</code> &ndash; limits rows per table</li>
      <li><code>SUBSEA_MDB_LOAD_ALL_GEOMS=1</code> &ndash; also loads MultiPoint and other multi-part layers (default loads LineString, Polygon, and Point)</li>
      <li><code>SUBSEA_MDB_SCHEMA_DISCOVERY=1</code> &ndash; also inspects physical tables missing from <code>GFeatures</code> (slower; <code>SUBSEA_MDB_SCHEMA_BUDGET=N</code> caps the inspection at N seconds, default 30)</li>
      <li><code>SUBSEA_MDB_NO_SUBPROCESS=1</code> &ndash; forces in-process ODBC (not recommended; may crash QGIS)</li>
    </ul>
  </li>
</ul>
""")

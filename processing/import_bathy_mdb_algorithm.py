# import_bathy_mdb_algorithm.py
# -*- coding: utf-8 -*-
"""
ImportBathyMdbAlgorithm: Imports GeoMedia MDB feature tables into QGIS.
Relies on user-provided CRS. Adds 'depth' and 'source' attributes.
Handles Point, LineString, Polygon, and MultiPoint geometries (2D and 3D).
By default loads LineString (contour lines) and Polygon (seabed features) layers.
"""

import os
import struct
import math
import traceback
import json
import tempfile
import subprocess
import sys
try:
    import pyodbc
except Exception:  # pragma: no cover
    pyodbc = None
from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterFile,
    QgsProcessingParameterCrs,
    QgsProcessingOutputMultipleLayers,
    QgsVectorLayer,
    QgsField,
    QgsFeature,
    QgsGeometry,
    QgsProcessingException,
    QgsCoordinateReferenceSystem,
    QgsProcessingContext,
    QgsWkbTypes,
)


ACCESS_ODBC_DRIVER_NAME = "Microsoft Access Driver (*.mdb, *.accdb)"


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
    conn_str = rf"Driver={{{ACCESS_ODBC_DRIVER_NAME}}};DBQ={mdb_file};"
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


def parse_blob(blob):
    try:
        if blob is None:
            return None

        # pyodbc can return memoryview/bytearray for BLOB columns
        if isinstance(blob, memoryview):
            blob = blob.tobytes()

        if len(blob) < 20:
            return None

        # 16-byte "magic" prefix
        magic_16 = blob[:16]
        # The standard tail is the last 15 bytes we know from old "c2ff..." signature
        standard_tail = bytes.fromhex("ffd20fbc8ccf11abde08003601b769")

        # Check if bytes 1..16 match
        if magic_16[1:] != standard_tail:
            # Optionally log or allow more variations
            return None

        # Now read the next 4 bytes as the point count
        num_points = struct.unpack("<i", blob[16:20])[0]
        if num_points < 0 or num_points > 100000:
            return None

        # The total length we expect for the geometry is 20 + (24 * num_points)
        expected_length = 20 + (24 * num_points)
        if len(blob) < expected_length:
            return None

        vertices = []
        offset = 20
        for _ in range(num_points):
            coords = struct.unpack("<ddd", blob[offset : offset + 24])
            vertices.append(coords)
            offset += 24

        return vertices

    except struct.error:
        return None
    except Exception:
        return None



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
    conn_str = rf"Driver={{{ACCESS_ODBC_DRIVER_NAME}}};DBQ={mdb_file};"
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

            cursor.execute(f"SELECT * FROM [{gfeatures_table}] WHERE 1=0")
            col_names = [desc[0] for desc in cursor.description]
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

            sql = f"""
                SELECT [{feature_name_col}], [{geom_field_col}], [{geom_type_col}]
                FROM [{gfeatures_table}]
                WHERE [{geom_type_col}] <> 33
            """
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
    conn_str = rf"Driver={{{ACCESS_ODBC_DRIVER_NAME}}};DBQ={mdb_file};"
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

            cursor.execute(f"SELECT * FROM [{fieldlookup_table}] WHERE 1=0")
            fl_col_names = [desc[0] for desc in cursor.description]
            feedback.pushInfo(f"Columns in {fieldlookup_table}: {fl_col_names}")
            cursor.execute(f"SELECT * FROM [{attributeprop_table}] WHERE 1=0")
            ap_col_names = [desc[0] for desc in cursor.description]
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

            sql = f"""
                SELECT [fl].[{fieldname_col}], [ap].[{fieldtype_col}]
                FROM [{fieldlookup_table}] fl
                INNER JOIN [{attributeprop_table}] ap ON [fl].[{indid_col_fl}] = [ap].[{indid_col_ap}]
                WHERE [fl].[{featurename_col}] = ?
            """
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

    conn_str = rf"Driver={{{ACCESS_ODBC_DRIVER_NAME}}};DBQ={mdb_file};"
    try:
        with pyodbc.connect(conn_str) as conn:
            cursor = conn.cursor()
            sql = f"SELECT * FROM [{table_name}] WHERE [{geom_field_name}] IS NOT NULL"
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
            elif geometry_type_code == 3:
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
                    fields.append(QgsField(field_name, QVariant.Int))
                elif field_type == "DOUBLE":
                    fields.append(QgsField(field_name, QVariant.Double))
                else:
                    fields.append(QgsField(field_name, QVariant.String))
            # Add extra fields.
            fields.append(QgsField("depth", QVariant.Double))
            fields.append(QgsField("source", QVariant.String))
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
                        if field_def.type() == QVariant.Int:
                            attr_values.append(int(value) if value is not None else None)
                        elif field_def.type() == QVariant.Double:
                            attr_values.append(float(value) if value is not None else None)
                        else:
                            attr_values.append(str(value) if value is not None else "")
                    except (ValueError, TypeError):
                        attr_values.append(None)

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


def _memory_uri_for_layer(source_layer, import_crs):
    """Build a memory provider URI matching the source layer geometry as closely as practical."""
    wkb_name = QgsWkbTypes.displayString(source_layer.wkbType())
    if not wkb_name or wkb_name == "Unknown":
        geom_type = source_layer.geometryType()
        if geom_type == QgsWkbTypes.PointGeometry:
            wkb_name = "Point"
        elif geom_type == QgsWkbTypes.LineGeometry:
            wkb_name = "LineString"
        elif geom_type == QgsWkbTypes.PolygonGeometry:
            wkb_name = "Polygon"
        else:
            wkb_name = "None"

    if wkb_name == "None":
        return "None"

    return f"{wkb_name}?crs={import_crs.authid()}"


def _clone_to_memory_layer(source_layer, layer_name, import_crs, feedback):
    """Copy an OGR/temp layer into a true in-memory scratch layer."""
    mem_uri = _memory_uri_for_layer(source_layer, import_crs)
    mem_layer = QgsVectorLayer(mem_uri, layer_name, "memory")
    if not mem_layer.isValid():
        feedback.reportError(f"Could not create memory layer for {layer_name}")
        return None

    dp = mem_layer.dataProvider()
    dp.addAttributes(list(source_layer.fields()))
    mem_layer.updateFields()

    features = [f for f in source_layer.getFeatures()]
    if features:
        dp.addFeatures(features)
    mem_layer.updateExtents()

    if import_crs and import_crs.isValid():
        mem_layer.setCrs(import_crs)

    return mem_layer


class ImportBathyMdbAlgorithm(QgsProcessingAlgorithm):
    INPUT_MDB = 'INPUT_MDB'
    TARGET_CRS = 'TARGET_CRS'
    OUTPUT_LAYERS = 'OUTPUT_LAYERS'

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFile(self.INPUT_MDB, self.tr('Input MDB File'), extension='mdb'))
        self.addParameter(QgsProcessingParameterCrs(self.TARGET_CRS, self.tr('Coordinate System'), optional=False))
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
            os.path.join(exe_dir, 'python-qgis.bat'),
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
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
        except Exception as e:
            raise QgsProcessingException(f'Failed to run MDB worker: {e}')

        if completed.returncode != 0:
            # Worker writes JSON to stderr on error.
            err = (completed.stderr or '').strip()
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

            msg = (completed.stderr or completed.stdout or '').strip()
            if not msg:
                msg = (
                    f"MDB worker failed with exit code {completed.returncode}. "
                    "This often indicates a native ODBC/Access driver crash or bitness mismatch."
                )
            raise QgsProcessingException(msg)

        out = (completed.stdout or '').strip()
        if not out:
            return None
        return json.loads(out)

    def processAlgorithm(self, parameters, context, feedback):
        if os.name != 'nt':
            raise QgsProcessingException(
                "MDB import requires a Windows ODBC driver (Microsoft Access Database Engine)."
            )

        mdb_file = self.parameterAsFile(parameters, self.INPUT_MDB, context)
        target_crs = self.parameterAsCrs(parameters, self.TARGET_CRS, context)
        # NOTE: MDB import runs in a subprocess by default to avoid silent QGIS crashes
        # caused by native ODBC driver issues. Advanced/debug options are available via
        # environment variables (keeps the Processing UI simple).
        isolate = os.environ.get('SUBSEA_MDB_NO_SUBPROCESS', '0') not in {'1', 'true', 'True'}
        keep_temp = os.environ.get('SUBSEA_MDB_KEEP_TEMP', '0') in {'1', 'true', 'True'}
        load_all_geoms = os.environ.get('SUBSEA_MDB_LOAD_ALL_GEOMS', '0') in {'1', 'true', 'True'}
        max_features_env = os.environ.get('SUBSEA_MDB_MAX_FEATURES', '0')
        try:
            max_features = int(max_features_env)
        except Exception:
            max_features = 0

        if not mdb_file or not os.path.exists(mdb_file):
            raise QgsProcessingException("Invalid MDB file selected.")

        ext = os.path.splitext(mdb_file)[1].lower()
        if ext not in {'.mdb', '.accdb'}:
            raise QgsProcessingException("Input must be a .mdb or .accdb file.")

        temp_dir = ''
        if isolate:
            # In isolate mode we intentionally avoid touching ODBC in-process.
            temp_dir = tempfile.mkdtemp(prefix='subsea_mdb_')
            feedback.pushInfo(f'Using temp dir: {temp_dir}')

            tables = self._run_worker(['--mode', 'list', '--mdb', mdb_file], feedback)
            if not tables:
                raise QgsProcessingException('No feature tables found in the MDB (worker list returned empty).')

            feature_tables = {
                name: (meta.get('geom_field_name'), meta.get('geometry_type_code'))
                for name, meta in tables.items()
            }
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

        output_layers = {}
        for table_name, (geom_field_name, geometry_type_code) in feature_tables.items():
            feedback.pushInfo(f"Processing table: {table_name}")
            if target_crs and target_crs.isValid():
                import_crs = target_crs
                feedback.pushInfo(f"Using Target CRS: {target_crs.authid()}")
            else:
                raise QgsProcessingException("No valid CRS provided. Set a Target CRS.")

            if isolate:
                out_base = os.path.join(temp_dir, f'{table_name}')
                # Always split in the worker.
                # Rationale: GeoMedia/Makai MDB metadata can mislabel geometry types; splitting is the most
                # reliable way to prevent LineString features being imported as Points.
                split_mixed = True
                info = self._run_worker(
                    [
                        '--mode', 'export',
                        '--mdb', mdb_file,
                        '--table', table_name,
                        '--geom-field', geom_field_name,
                        '--geom-type', str(int(geometry_type_code)),
                        '--out', out_base,
                        '--max-features', str(int(max_features or 0)),
                        '--split', '1' if split_mixed else '0',
                    ],
                    feedback,
                )
                if not info:
                    feedback.reportError(f'Skipping table {table_name}: worker produced no output')
                    continue

                outputs = info.get('outputs') if isinstance(info, dict) else None
                if outputs and isinstance(outputs, dict):
                    # Split mode can create multiple layers.
                    # By default load LineString (contour lines), Polygon (seabed features / sediment areas),
                    # and Point layers. MultiPoint is less common and still requires SUBSEA_MDB_LOAD_ALL_GEOMS=1.
                    default_geom_types = {'LineString', 'Polygon', 'Point'}
                    found_types = list(outputs.keys())
                    feedback.pushInfo(f"  Geometry types found in '{table_name}': {found_types}")
                    for geom_type_name, path in outputs.items():
                        if (not load_all_geoms) and str(geom_type_name) not in default_geom_types:
                            feedback.pushInfo(f"  Skipping {geom_type_name} layer for '{table_name}' (set SUBSEA_MDB_LOAD_ALL_GEOMS=1 to include)")
                            continue
                        if not path or not os.path.exists(path):
                            continue
                        layer_name = table_name
                        src_layer = QgsVectorLayer(path, layer_name, 'ogr')
                        if not src_layer.isValid():
                            feedback.reportError(f'Skipping {layer_name}: output layer invalid')
                            continue
                        layer = _clone_to_memory_layer(src_layer, layer_name, import_crs, feedback)
                        if layer is None:
                            continue

                        context.temporaryLayerStore().addMapLayer(layer)
                        details = QgsProcessingContext.LayerDetails(layer_name, context.project())
                        context.addLayerToLoadOnCompletion(layer.id(), details)
                        output_layers[f"{table_name}::{geom_type_name}"] = layer.id()
                else:
                    # Non-split mode: expect a single GeoJSON at out_base + '.geojson'
                    out_geojson = out_base + '.geojson'
                    if not os.path.exists(out_geojson):
                        feedback.reportError(f'Skipping table {table_name}: worker produced no output file')
                        continue

                    src_layer = QgsVectorLayer(out_geojson, table_name, 'ogr')
                    if not src_layer.isValid():
                        feedback.reportError(f'Skipping table {table_name}: output layer invalid')
                        continue
                    layer = _clone_to_memory_layer(src_layer, table_name, import_crs, feedback)
                    if layer is None:
                        continue

                    context.temporaryLayerStore().addMapLayer(layer)
                    details = QgsProcessingContext.LayerDetails(table_name, context.project())
                    context.addLayerToLoadOnCompletion(layer.id(), details)
                    output_layers[table_name] = layer.id()
                continue
            else:
                mem_layer, error = import_table_as_memory_layer(
                    mdb_file,
                    table_name,
                    geom_field_name,
                    geometry_type_code,
                    import_crs,
                    feedback,
                )
                if error:
                    feedback.reportError(f"Skipping table {table_name}: {error}")
                    continue

                # IMPORTANT: Do NOT add layers directly to QgsProject from a processing algorithm.
                # Algorithms may run in a background thread and direct project mutations can crash QGIS.
                context.temporaryLayerStore().addMapLayer(mem_layer)
                details = QgsProcessingContext.LayerDetails(table_name, context.project())
                context.addLayerToLoadOnCompletion(mem_layer.id(), details)
                output_layers[table_name] = mem_layer.id()

        if isolate and temp_dir and not keep_temp:
            try:
                # Best-effort cleanup. If files are locked, leave them.
                for fn in os.listdir(temp_dir):
                    try:
                        os.remove(os.path.join(temp_dir, fn))
                    except Exception:
                        pass
                try:
                    os.rmdir(temp_dir)
                except Exception:
                    pass
            except Exception:
                pass

        if not output_layers:
            raise QgsProcessingException("No valid layers were imported from the MDB.")

        return {self.OUTPUT_LAYERS: output_layers}

    def name(self):
        return 'import_bathy_mdb'

    def displayName(self):
        return self.tr('Import Bathy MDB')

    def group(self):
        return self.tr('MDB Tools')

    def groupId(self):
        return 'mdb_tools'

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return ImportBathyMdbAlgorithm()

    def shortHelpString(self):
        return self.tr("""<h3>Import Bathy MDB (Experimental)</h3>
<p><b><font color="red">Warning:</font> This tool is experimental and may not work with all GeoMedia-formatted MDB files. Use with caution.</b></p>
<p>This tool attempts to import feature tables from a Microsoft Access Database (.mdb) file, typically created by Intergraph GeoMedia, into QGIS as new memory layers.</p>

<h4>How it Works</h4>
<p>The tool connects to the MDB file and looks for a <code>GFeatures</code> table to identify the feature classes within the database. For each feature class found, it reads the geometry from a binary (BLOB) field and creates a corresponding QGIS layer.</p>
<p>By default, the tool imports <b>LineString</b> layers (e.g. bathymetric contour lines), <b>Polygon</b> layers (e.g. seabed feature classifications, sediment type areas), and <b>Point</b> layers. Each geometry type is loaded as a separate layer so they never conflict. MultiPoint geometries can be included by setting the <code>SUBSEA_MDB_LOAD_ALL_GEOMS=1</code> environment variable.</p>
<p>It automatically adds two fields to each new layer:
<ul>
  <li><b>depth:</b> The average Z-value of the feature's vertices, if available.</li>
  <li><b>source:</b> The filename of the source MDB file for traceability.</li>
</ul>
</p>

<h4>Prerequisites</h4>
<p>This tool requires the <b>Microsoft Access Database Engine</b> (or a compatible ODBC driver) to be installed on your system. Without it, QGIS cannot connect to the MDB file. This generally means the tool will only function on a Windows operating system.</p>
<p><b>Stability note:</b> To avoid silent QGIS crashes caused by native ODBC drivers, this tool reads the MDB in a separate subprocess and then loads the exported layers into QGIS.</p>

<h4>Input Parameters</h4>
<ul>
  <li><b>Input MDB File:</b> The GeoMedia MDB file you want to import.</li>
  <li><b>Coordinate System:</b> You <b>must</b> manually select the Coordinate Reference System (CRS) of the data in the MDB file. The tool cannot automatically detect it. Providing the wrong CRS will result in misplaced data.</li>
</ul>

<h4>Outputs</h4>
<ul>
  <li><b>Imported Layers:</b> The tool will create a new layer for each feature table and geometry type successfully imported from the MDB. For example, a table containing both line and polygon features will produce separate layers. Layers are added directly to your QGIS project.</li>
</ul>

<h4>Known Limitations & Troubleshooting</h4>
<ul>
  <li><b>BLOB Format:</b> The tool is designed to parse a specific binary format for geometry. If your MDB uses a different format, the import will fail.</li>
  <li><b>Metadata Tables:</b> It relies on specific system tables like <code>GFeatures</code>, <code>FieldLookup</code>, and <code>AttributeProperties</code>. If these are missing or have an unexpected structure, the tool will not work.</li>
  <li><b>Errors:</b> If a table fails to import, a message will be shown in the Log Messages Panel. Check the log for details about ODBC errors or parsing failures.</li>
  <li><b>Advanced options (env vars):</b>
    <ul>
      <li><code>SUBSEA_MDB_KEEP_TEMP=1</code> &ndash; keeps intermediate GeoJSONs for debugging</li>
      <li><code>SUBSEA_MDB_MAX_FEATURES=N</code> &ndash; limits rows per table</li>
      <li><code>SUBSEA_MDB_LOAD_ALL_GEOMS=1</code> &ndash; also loads MultiPoint layers (default loads LineString, Polygon, and Point)</li>
      <li><code>SUBSEA_MDB_NO_SUBPROCESS=1</code> &ndash; forces in-process ODBC (not recommended; may crash QGIS)</li>
    </ul>
  </li>
</ul>
""")

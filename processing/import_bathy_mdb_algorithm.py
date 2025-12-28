# import_bathy_mdb_algorithm.py
# -*- coding: utf-8 -*-
"""
ImportBathyMdbAlgorithm: Imports GeoMedia MDB feature tables into QGIS.
Relies on user-provided CRS. Adds 'depth' and 'source' attributes.
Handles Point, LineString, Polygon, and MultiPoint geometries (2D and 3D).
"""

import os
import struct
try:
    import pyodbc
except Exception:  # pragma: no cover
    pyodbc = None
from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.core import (QgsProcessing,
                       QgsProcessingAlgorithm,
                       QgsProcessingParameterFile,
                       QgsProcessingParameterCrs,
                       QgsProcessingOutputMultipleLayers,
                       QgsVectorLayer,
                       QgsProject,
                       QgsField,
                       QgsFeature,
                       QgsGeometry,
                       QgsProcessingException,
                       QgsCoordinateReferenceSystem)


def parse_blob(blob):
    try:
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

    except struct.error as e:
        print(f"Error unpacking blob: {e}")
        return None
    except Exception as e:
        print(f"Unexpected error parsing blob: {e}")
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
    if pyodbc is None:
        feedback.reportError(
            "pyodbc is required to read MDB files but could not be imported. "
            "Install pyodbc (and the Microsoft Access Database Engine / ODBC driver) for your QGIS Python environment."
        )
        return {}
    feature_tables = {}
    conn_str = r"Driver={Microsoft Access Driver (*.mdb, *.accdb)};DBQ=" + mdb_file + ";"
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

            # Assume the first column is the feature name.
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
    if pyodbc is None:
        feedback.reportError(
            "pyodbc is required to read MDB files but could not be imported. "
            "Install pyodbc (and the Microsoft Access Database Engine / ODBC driver) for your QGIS Python environment."
        )
        return {}
    attribute_fields = {}
    conn_str = r"Driver={Microsoft Access Driver (*.mdb, *.accdb)};DBQ=" + mdb_file + ";"
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
    if pyodbc is None:
        return None, (
            "pyodbc is required to read MDB files but could not be imported. "
            "Install pyodbc (and the Microsoft Access Database Engine / ODBC driver) for your QGIS Python environment."
        )
    conn_str = r"Driver={Microsoft Access Driver (*.mdb, *.accdb)};DBQ=" + mdb_file + ";"
    try:
        with pyodbc.connect(conn_str) as conn:
            cursor = conn.cursor()
            sql = f"SELECT * FROM [{table_name}] WHERE [{geom_field_name}] IS NOT NULL"
            feedback.pushInfo(f"Executing SQL: {sql}")
            cursor.execute(sql)
            rows = cursor.fetchall()
            col_names = [desc[0] for desc in cursor.description]

            if not rows:
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
                test_blob = rows[0][col_names.index(geom_field_name)]
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
            fields = []
            for field_name, field_type in attribute_fields.items():
                if field_name == geom_field_name:
                    continue
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

            features = []
            for row in rows:
                row_dict = dict(zip(col_names, row))
                blob = row_dict[geom_field_name]
                vertices = parse_blob(blob)
                if vertices is None:
                    continue

                # For polygons, ensure closure.
                if layer_type == "Polygon" and not is_closed(vertices):
                    vertices.append(vertices[0])

                wkt = create_wkt(layer_type, vertices)
                if not wkt:
                    feedback.reportError(f"Failed to create WKT for table {table_name}")
                    continue

                feat = QgsFeature()
                feat.setGeometry(QgsGeometry.fromWkt(wkt))

                depths = [v[2] for v in vertices]
                avg_depth = sum(depths) / len(depths) if depths else None

                attr_values = []
                # Use the attribute_fields order (skip the geometry field).
                for field_name, qgis_field in zip(attribute_fields.keys(), mem_layer.fields()):
                    if field_name == geom_field_name:
                        continue
                    try:
                        value = row_dict[field_name]
                        if qgis_field.type() == QVariant.Int:
                            attr_values.append(int(value) if value is not None else None)
                        elif qgis_field.type() == QVariant.Double:
                            attr_values.append(float(value) if value is not None else None)
                        else:
                            attr_values.append(str(value) if value is not None else "")
                    except (ValueError, TypeError) as e:
                        feedback.reportError(f"Error converting attribute {field_name}: {e}")
                        attr_values.append(None)
                attr_values.append(avg_depth)
                attr_values.append(os.path.basename(mdb_file))
                feat.setAttributes(attr_values)
                features.append(feat)

            dp.addFeatures(features)
            mem_layer.updateExtents()
            return mem_layer, None

    except Exception as e:
        if pyodbc is not None and isinstance(e, pyodbc.Error):
            sqlstate = e.args[0] if getattr(e, 'args', None) else ''
            feedback.reportError(f"ODBC error: {sqlstate} - {e}")
            return None, str(e)
        feedback.reportError(f"Error processing table {table_name}: {e}")
        return None, str(e)


def is_closed(vertices, tol=1e-6):
    """Checks if the first and last vertices are nearly equal."""
    if len(vertices) < 2:
        return False
    x0, y0, _ = vertices[0]
    xn, yn, _ = vertices[-1]
    return abs(x0 - xn) <= tol and abs(y0 - yn) <= tol


class ImportBathyMdbAlgorithm(QgsProcessingAlgorithm):
    INPUT_MDB = 'INPUT_MDB'
    TARGET_CRS = 'TARGET_CRS'
    OUTPUT_LAYERS = 'OUTPUT_LAYERS'

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFile(self.INPUT_MDB, self.tr('Input MDB File'), extension='mdb'))
        self.addParameter(QgsProcessingParameterCrs(self.TARGET_CRS, self.tr('Coordinate System'), optional=False))
        self.addOutput(QgsProcessingOutputMultipleLayers(self.OUTPUT_LAYERS, self.tr('Imported Layers')))

    def processAlgorithm(self, parameters, context, feedback):
        if pyodbc is None:
            raise QgsProcessingException(
                "pyodbc is required to read MDB files but could not be imported. "
                "Install pyodbc (and the Microsoft Access Database Engine / ODBC driver) for your QGIS Python environment."
            )
        mdb_file = self.parameterAsFile(parameters, self.INPUT_MDB, context)
        target_crs = self.parameterAsCrs(parameters, self.TARGET_CRS, context)
        if not mdb_file or not os.path.exists(mdb_file):
            raise QgsProcessingException("Invalid MDB file selected.")

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

            mem_layer, error = import_table_as_memory_layer(mdb_file, table_name, geom_field_name, geometry_type_code, import_crs, feedback)
            if error:
                feedback.reportError(f"Skipping table {table_name}: {error}")
                continue
            QgsProject().instance().addMapLayer(mem_layer)
            output_layers[table_name] = mem_layer.id()

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
<p>The tool connects to the MDB file and looks for a `GFeatures` table to identify the feature classes within the database. For each feature class found, it reads the geometry from a binary (BLOB) field and creates a corresponding QGIS layer.</p>
<p>It automatically adds two fields to each new layer:
<ul>
  <li><b>depth:</b> The average Z-value of the feature's vertices, if available.</li>
  <li><b>source:</b> The filename of the source MDB file for traceability.</li>
</ul>
</p>

<h4>Prerequisites</h4>
<p>This tool requires the <b>Microsoft Access Database Engine</b> (or a compatible ODBC driver) to be installed on your system. Without it, QGIS cannot connect to the MDB file. This generally means the tool will only function on a Windows operating system.</p>

<h4>Input Parameters</h4>
<ul>
  <li><b>Input MDB File:</b> The GeoMedia MDB file you want to import.</li>
  <li><b>Coordinate System:</b> You <b>must</b> manually select the Coordinate Reference System (CRS) of the data in the MDB file. The tool cannot automatically detect it. Providing the wrong CRS will result in misplaced data.</li>
</ul>

<h4>Outputs</h4>
<ul>
  <li><b>Imported Layers:</b> The tool will create a new memory layer for each feature table successfully imported from the MDB. These layers are added directly to your QGIS project.</li>
</ul>

<h4>Known Limitations & Troubleshooting</h4>
<ul>
  <li><b>BLOB Format:</b> The tool is designed to parse a specific binary format for geometry. If your MDB uses a different format, the import will fail.</li>
  <li><b>Metadata Tables:</b> It relies on specific system tables like `GFeatures`, `FieldLookup`, and `AttributeProperties`. If these are missing or have an unexpected structure, the tool will not work.</li>
  <li><b>Errors:</b> If a table fails to import, a message will be shown in the Log Messages Panel. Check the log for details about ODBC errors or parsing failures.</li>
</ul>
""")

# import_cable_lay_algorithm.py
# -*- coding: utf-8 -*-
"""
ImportCableLayAlgorithm
Import cable lay CSV data to QGIS as a point layer
"""

from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterFile,
    QgsProcessingParameterString,
    QgsProcessingParameterFeatureSink,
    QgsFeature,
    QgsFields,
    QgsField,
    QgsPointXY,
    QgsGeometry,
    QgsWkbTypes,
    QgsCoordinateReferenceSystem,
    QgsProcessingLayerPostProcessorInterface
)
import csv
import os

class ImportCableLayAlgorithm(QgsProcessingAlgorithm):
    INPUT = 'INPUT'
    START_DATE = 'START_DATE'
    OUTPUT = 'OUTPUT'
    DOWNSAMPLE = 'DOWNSAMPLE'
    PARSE_TIME = 'PARSE_TIME'

    def tr(self, string):
        return QCoreApplication.translate('ImportCableLayAlgorithm', string)

    def createInstance(self):
        return ImportCableLayAlgorithm()

    def name(self):
        return 'import_cable_lay'

    def displayName(self):
        return self.tr('Import Cable Lay Data (CSV)')

    def group(self):
        return self.tr('Other Tools')

    def groupId(self):
        return 'other_tools'

    def shortHelpString(self):
        return self.tr("""
<h3>Import Cable Lay Data (CSV)</h3>
<p>This tool imports cable lay CSV files as a point layer in QGIS.</p>

<h4>How it Works</h4>
<p>The tool reads cable lay CSV files and converts them into QGIS point features. It automatically:
<ul>
  <li>Detects and skips the units row if present</li>
  <li>Converts ship positions from degrees and decimal minutes format (e.g., "01 19.4445189N") to decimal degrees</li>
  <li>Parses time data using the project start date to create ISO timestamps, if enabled</li>
  <li>Detects field types (numeric vs text)</li>
  <li>Option to downsample the data by loading every Nth record to reduce file size</li>
</ul>
</p>

<h4>Input Parameters</h4>
<ul>
  <li><b>Cable Lay CSV File:</b> The CSV file containing the cable lay data.</li>
  <li><b>Project Start Date:</b> (Optional) Input day count 1's date in YYYY-MM-DD format. Only required if "Parse Time Data" is enabled. This is used to convert the relative time data in the CSV to absolute timestamps.</li>
  <li><b>Parse Time Data:</b> (Optional) Check to enable time parsing and create ISO_Time field. Uncheck if you want to skip time parsing (useful for date line crossings or when you prefer to keep the original day count format).</li>
  <li><b>Downsample Factor:</b> (Optional) Load every Nth record to reduce data density. Set to 1 to load all records, 10 to load every 10th record, etc. Useful for large datasets or overview visualisations.</li>
</ul>

<h4>Required CSV Columns</h4>
<p>The CSV file must contain these columns for the tool to work:</p>
<ul>
  <li><b>Time:</b> Time data in the format <code>day count,HH:MM:SS</code> (e.g., "12,14:23:45")</li>
  <li><b>Ship Latitude:</b> Ship latitude in degrees and decimal minutes format (e.g., "01 19.4445189N")</li>
  <li><b>Ship Longitude:</b> Ship longitude in degrees and decimal minutes format (e.g., "172 59.7102158E")</li>
</ul>

<h4>Other CSV Columns</h4>
<p>All other columns found in the CSV, including navigation, cable, plow, and coordinate data, will also be imported and preserved in the output.</p>

<h4>Output</h4>
<p>The tool creates a point layer with the following features:</p>
<ul>
  <li><b>Geometry:</b> Point features at ship positions in WGS 84 (EPSG:4326)</li>
  <li><b>Original Data:</b> All columns from the input CSV with appropriate data types (numeric fields detected automatically)</li>
  <li><b>Enhanced Fields:</b>
    <ul>
      <li><b>ISO_Time:</b> Converted timestamp in ISO format (only if time parsing is enabled)</li>
      <li><b>Lat_dd, Lon_dd:</b> Ship position in decimal degrees</li>
      <li><b>source_file:</b> Name of the source CSV file for traceability</li>
    </ul>
  </li>
</ul>

<h4>Notes</h4>
<ul>
  <li>The tool automatically detects numeric fields (including those with units like % or measurement suffixes) and imports them as proper numeric types for analysis and styling.</li>
  <li>Ship latitude and longitude coordinates are automatically converted from degrees and decimal minutes to decimal degrees and stored in separate fields for easier use.</li>
  <li>The output layer is automatically named using the input CSV filename.</li>
  <li>Use the downsample feature for large datasets - downsample factor of 60 means 1 point per minute if data is recorded every second.</li>
  <li><b>Date Line Crossings:</b> If your cable crosses the international date line, consider unchecking "Parse Time Data" to avoid date/time complications and keep the original day count format.</li>
  <li>All processing messages and detected field types are shown in the Log Messages Panel for verification.</li>
</ul>
""")

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFile(
                self.INPUT,
                self.tr('Cable Lay CSV File'),
                behavior=QgsProcessingParameterFile.File,
                extension='csv'
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.START_DATE,
                self.tr('Project Start Date (YYYY-MM-DD) - only needed if parsing time'),
                defaultValue='',
                optional=True
            )
        )
        from qgis.core import QgsProcessingParameterNumber, QgsProcessingParameterBoolean
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.PARSE_TIME,
                self.tr('Parse Time Data (uncheck to skip time parsing, e.g., for date line crossings)'),
                defaultValue=True
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.DOWNSAMPLE,
                self.tr('Downsample Factor (load every Nth record, 1 = all)'),
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=1,
                minValue=1
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr('Output Layer'),
                type=QgsProcessing.TypeVectorPoint
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        import datetime
        path = self.parameterAsFile(parameters, self.INPUT, context)
        start_date = self.parameterAsString(parameters, self.START_DATE, context)
        parse_time = self.parameterAsBool(parameters, self.PARSE_TIME, context)
        downsample = self.parameterAsInt(parameters, self.DOWNSAMPLE, context)
        
        # Validate that start date is provided if time parsing is enabled
        if parse_time and not start_date.strip():
            raise Exception('Project Start Date is required when time parsing is enabled.')
        
        records = []
        with open(path, newline='', encoding='utf-8') as csvfile:
            reader = csv.reader(csvfile)
            rows = list(reader)
            if len(rows) < 2:
                raise Exception('CSV file is empty or missing header.')
            header = rows[0]
            
            # Skip the units row - look for a row that starts with '#' or contains units
            data_start_idx = 1
            if len(rows) > 1:
                second_row = rows[1]
                # Check if second row is a units row (starts with # or contains common units)
                if (second_row and len(second_row) > 0 and 
                    (str(second_row[0]).strip().startswith('#') or 
                     any(unit in str(cell).lower() for cell in second_row[:5] 
                         for unit in ['km', 'hr', 'deg', 'm', 'kn', '%', 'dd,hh:mm:ss']))):
                    data_start_idx = 2
                    feedback.pushInfo(f"Detected units row: {second_row[:5]}... - Skipping it.")
            
            data_rows = rows[data_start_idx:]
            # Downsample: only keep every Nth row
            data_rows = data_rows[::downsample] if downsample > 1 else data_rows
            for row in data_rows:
                if len(row) != len(header):
                    continue
                record = dict(zip(header, row))
                records.append(record)

        required_cols = ['Time', 'Ship Latitude', 'Ship Longitude']
        for col in required_cols:
            if not records or col not in records[0]:
                raise Exception(f'Missing required column: {col}')

        # Prepare output fields with robust type inference
        csv_cols = list(records[0].keys())
        extra_fields = ['Lat_dd', 'Lon_dd', 'source_file']
        if parse_time:
            extra_fields.insert(0, 'ISO_Time')  # Add ISO_Time as first extra field if parsing time
        fields = QgsFields()
        col_types = {}
        def infer_type(col):
            # Known text-only columns that should always be strings
            text_columns = ['Record', 'Plow Status', 'Roto Select', 'Ship Latitude', 'Ship Longitude', 
                           'Plow Latitude (USBL)', 'Plow Longitude (USBL)', 'Plow Latitude (Tow Wire)', 
                           'Plow Longitude (Tow Wire)', 'Time']
            if col in text_columns:
                return 'str'
            
            # Sample up to 100 records for performance on large datasets
            sample_records = records[:100] if len(records) > 100 else records
            
            numeric_count = 0
            decimal_count = 0
            total_values = 0
            
            for rec in sample_records:
                val = rec.get(col, '').strip()
                if val == '' or val.lower() in ['null', 'na', 'n/a', 'none', '-']:
                    continue
                
                total_values += 1
                
                # Handle common decimal separators and clean the value
                val_clean = val.replace(',', '.')
                
                # Remove common numeric suffixes/prefixes that indicate numeric data
                val_clean = val_clean.rstrip('%')  # Remove percentage signs
                val_clean = val_clean.replace(' ', '')  # Remove spaces
                
                # Handle negative numbers
                is_negative = val_clean.startswith('-')
                if is_negative:
                    val_clean = val_clean[1:]
                
                # Check for obvious non-numeric content (letters, except 'e' for scientific notation)
                # But ignore common numeric indicators
                has_letters = False
                for c in val_clean:
                    if c.isalpha() and c.lower() != 'e':
                        has_letters = True
                        break
                
                if has_letters:
                    # If we find letters (except 'e'), it's definitely a string
                    return 'str'
                
                # Add back negative sign if it was there
                if is_negative:
                    val_clean = '-' + val_clean
                
                try:
                    fval = float(val_clean)
                    numeric_count += 1
                    
                    # Check if it's a decimal number
                    if ('.' in val_clean and val_clean.count('.') == 1) or \
                       'e' in val_clean.lower() or \
                       not fval.is_integer():
                        decimal_count += 1
                except (ValueError, TypeError):
                    # If we can't convert to float, it's a string
                    return 'str'
            
            # If we have no values to analyze, default to string
            if total_values == 0:
                return 'str'
            
            # If most values are numeric (allow for some parsing errors)
            if numeric_count >= total_values * 0.8:  # 80% threshold
                if decimal_count > 0:
                    return 'float'
                else:
                    return 'int'
            else:
                return 'str'

        for col in csv_cols:
            if col not in ['geometry']:
                t = infer_type(col)
                col_types[col] = t
                if t == 'int':
                    fields.append(QgsField(col, QVariant.LongLong))
                elif t == 'float':
                    fields.append(QgsField(col, QVariant.Double))
                else:
                    fields.append(QgsField(col, QVariant.String))
        
        # Provide feedback on detected field types
        type_summary = []
        for col, t in col_types.items():
            type_summary.append(f"{col}: {t}")
        feedback.pushInfo(f"Detected field types: {', '.join(type_summary)}")
        
        # Debug: Show sample values for a few key columns that should be numeric
        debug_cols = ['Ship KP', 'Ship Speed', 'Ship Gyro', 'Surface Slack', 'Top Tension']
        for debug_col in debug_cols:
            if debug_col in col_types and len(records) > 0:
                # Show first few actual values, not just the first one
                sample_vals = [records[i].get(debug_col, 'N/A') for i in range(min(3, len(records)))]
                feedback.pushInfo(f"Sample values for '{debug_col}': {sample_vals} -> Type: {col_types[debug_col]}")
        
        # Show total records processed
        feedback.pushInfo(f"Total data records processed: {len(records)}")
        
        for extra in extra_fields:
            if extra not in csv_cols:
                if extra in ['Lat_dd', 'Lon_dd']:
                    fields.append(QgsField(extra, QVariant.Double))
                else:
                    fields.append(QgsField(extra, QVariant.String))

        (sink, dest_id) = self.parameterAsSink(
            parameters,
            self.OUTPUT,
            context,
            fields,
            QgsWkbTypes.Point,
            QgsCoordinateReferenceSystem('EPSG:4326')
        )

        for record in records:
            # Parse and filter - only parse time if requested
            if parse_time:
                iso_time = self._parse_time_value(record.get('Time', ''), start_date)
                if iso_time is None:
                    continue
            else:
                iso_time = None
            
            lat_dd = self._parse_dms_to_dd_value(record.get('Ship Latitude', ''))
            lon_dd = self._parse_dms_to_dd_value(record.get('Ship Longitude', ''))
            if lat_dd is None or lon_dd is None:
                continue
            # Build feature
            feat = QgsFeature(fields)
            for i, field in enumerate(fields):
                name = field.name()
                if name == 'ISO_Time' and parse_time and iso_time is not None:
                    val = iso_time.strftime('%Y-%m-%dT%H:%M:%S')
                elif name == 'ISO_Time' and not parse_time:
                    # Skip ISO_Time field if time parsing is disabled
                    continue
                elif name == 'Lat_dd':
                    val = lat_dd
                elif name == 'Lon_dd':
                    val = lon_dd
                elif name == 'source_file':
                    val = os.path.basename(path)
                else:
                    val = record.get(name, '')
                    t = col_types.get(name)
                    
                    # Handle empty or null-like values
                    if val == '' or val is None or str(val).strip().lower() in ['null', 'na', 'n/a', 'none', '-']:
                        val = None
                    elif t == 'int':
                        try:
                            # Handle comma as decimal separator and convert to int
                            val_clean = str(val).strip().replace(',', '.')
                            # Remove common suffixes
                            val_clean = val_clean.rstrip('%')
                            val_clean = val_clean.replace(' ', '')
                            val = int(float(val_clean))
                        except (ValueError, TypeError):
                            val = None
                    elif t == 'float':
                        try:
                            # Handle comma as decimal separator
                            val_clean = str(val).strip().replace(',', '.')
                            # Remove common suffixes
                            val_clean = val_clean.rstrip('%')
                            val_clean = val_clean.replace(' ', '')
                            val = float(val_clean)
                        except (ValueError, TypeError):
                            val = None
                    # For string type, keep as is but strip whitespace
                    elif t == 'str' and val is not None:
                        val = str(val).strip()
                feat.setAttribute(i, val)
            point = QgsPointXY(lon_dd, lat_dd)
            feat.setGeometry(QgsGeometry.fromPointXY(point))
            sink.addFeature(feat)

        # Set output layer name to input file name (without extension)
        filename = os.path.splitext(os.path.basename(path))[0]
        self._renamer = Renamer(filename)
        context.layerToLoadOnCompletionDetails(dest_id).setPostProcessor(self._renamer)

        return {self.OUTPUT: dest_id}


class Renamer(QgsProcessingLayerPostProcessorInterface):
    def __init__(self, layer_name):
        self.name = layer_name
        super().__init__()

    def postProcessLayer(self, layer, context, feedback):
        layer.setName(self.name)

# Move helper methods back to ImportCableLayAlgorithm
    
    
def _parse_dms_to_dd_value(self, val):
    import re
    match = re.match(r"(\d+)\s+([\d\.]+)([NSEW])", str(val).strip())
    if not match:
        return None
    deg, min_val, hemi = match.groups()
    dd = float(deg) + float(min_val) / 60
    if hemi in ['S', 'W']:
        dd *= -1
    return dd

def _parse_time_value(self, t, start_date_str):
    from datetime import datetime, timedelta
    try:
        base_date = datetime.strptime(start_date_str, "%Y-%m-%d")
    except Exception:
        return None
    try:
        days, hms = str(t).split(',')
        h, m, s = map(int, hms.split(':'))
        return base_date.replace(hour=h, minute=m, second=s) + timedelta(days=int(days) - 1)
    except Exception:
        return None

def _parse_dms_to_dd(self, series):
    import re
    def parse_one(val):
        match = re.match(r"(\d+)\s+([\d\.]+)([NSEW])", str(val).strip())
        if not match:
            return None
        deg, min_val, hemi = match.groups()
        dd = float(deg) + float(min_val) / 60
        if hemi in ['S', 'W']:
            dd *= -1
        return dd
    return series.astype(str).apply(parse_one)

def _parse_time(self, series, start_date_str):
    from datetime import datetime, timedelta
    try:
        base_date = datetime.strptime(start_date_str, "%Y-%m-%d")
    except Exception:
        return [None] * len(series)
    def parse_one(t):
        try:
            days, hms = str(t).split(',')
            h, m, s = map(int, hms.split(':'))
            return base_date.replace(hour=h, minute=m, second=s) + timedelta(days=int(days) - 1)
        except Exception:
            return None
    return [parse_one(t) for t in series]

# Attach these methods to ImportCableLayAlgorithm
ImportCableLayAlgorithm._parse_dms_to_dd_value = _parse_dms_to_dd_value
ImportCableLayAlgorithm._parse_time_value = _parse_time_value
ImportCableLayAlgorithm._parse_dms_to_dd = _parse_dms_to_dd
ImportCableLayAlgorithm._parse_time = _parse_time

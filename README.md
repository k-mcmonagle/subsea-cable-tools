# Subsea Cable Tools

**Subsea Cable Tools** is a QGIS Plugin created to support working with subsea telecom and power cable data in QGIS.


---

## 🚀 Features

### 🔧 Processing Algorithms
Accessible via the QGIS Processing Toolbox under **Subsea Cable Tools**:
- **Import Excel RPL** – Load route position lists (RPLs) as LineStrings from Excel. Now supports an optional 'Chart No' field and saves user-defined column mappings between sessions.
- **Import Bathy from MDB** – Extract bathymetric data from `.mdb` survey databases.
- **Highlight KP Ranges** – Visualise KP ranges on cable routes from a CSV file. Output now includes only `start_kp`, `end_kp`, and (if provided) `custom_label`, plus a new `length_km` field.
- **Highlight KP Range** – Visualise a single KP range on a cable route. Output fields streamlined for clarity.
- **Find Nearest KP** – Get the closest KP on a cable route to a layer of point(s) data.
- **Place KP Points** – Place points along a cable route at a specified interval. Now optionally samples depth values from a raster layer (e.g., MBES), includes CRS check, and warns for points outside raster extent.
- **Place KP Points from CSV** – Place points along a cable route from a CSV file.
- **Place Single KP Point** – Place a single point along a cable route at a specified KP.
- **Merge MBES Rasters** – Merge multiple MBES raster layers into a single raster, preserving depth (Z) values and ensuring NoData areas are transparent.
- **Create Raster from XYZ** – Convert large XYZ (Easting, Northing, Depth) files to raster using a robust VRT-based method, with support for direct rasterization and IDW interpolation.

### 🗺️ Map & Dockable Tools
- **KP Mouse Tool** – An interactive tool that provides the closest KP and DCC of the mouse pointer dynamically. Features a persistent tooltip, improved usability, and continuous KP calculation.
- **KP Data Plotter** – New dockable tool for plotting KP-based data from table layers against a reference line, with interactive crosshair, map marker, support for multiple data fields, y-axis reversal, and tooltips.
- **Straight Line Diagram (SLD)** – Dockable SLD plot for an RPL line + events layer with a synced map marker.
- **SLD KP Ranges (NEW)** – Use a table layer of KP ranges as the single source of truth and visualise the same ranges on the SLD and on the map at the same time.
- **Live Data Tool** – Dockable tool for receiving and displaying real-time data (map point + cards/plots/tables) from incoming text strings.

#### Live Data Tool quick start

1. Open **Live Data**.
2. In **Live Data Control → Connection**, set Host/Port and choose a **Message Format**.
3. Set **Latitude Field** / **Longitude Field** to match the parsed field names.
4. Click **Connect** (TCP) or use **Mock/Test** to replay a table layer as live data.

#### Message Formats

The Live Data Tool is **string-first**: it receives raw lines (typically from TCP) and parses each line into fields.

Supported formats:

- `csv_header`: First non-empty line is a CSV header row, subsequent lines are records
- `csv_fixed`: Records are CSV with a fixed, user-defined column list
- `kv`: Key/value pairs (e.g. `Lat_dd=...,Lon_dd=...,Heading=...`)
- `json`: One JSON object per line
- `regex`: Regex with named capture groups (advanced)

#### Mock/Test (no external server required)

Use **Live Data Control → Mock/Test** to replay an existing table layer already loaded in your QGIS project. This runs through the same parsing pipeline as TCP, and is intended to replace the old “CSV over TCP” test server script for most workflows.

#### SLD KP Ranges quick start
1. Open the **Straight Line Diagram** dock.
2. Select an **RPL Line Layer**.
3. (Optional) Select an **RPL Points (Events) Layer** to show event ticks.
4. Create or choose a **KP Ranges (Table) Layer**:
	- Click **New ranges table** to create an editable in-memory ranges table, or
	- Select an existing editable table layer (GeoPackage/SQLite recommended; CSV tables are typically not editable).
5. Ensure your table has (or map fields to) **Start**, **End**, and **Ref line**.
	- Ref line is used for traceability; ranges are filtered to the currently selected RPL line.
6. Click **Ranges** to add/edit/delete ranges.
7. Click **Draw SLD**.
8. Toggle **Show ranges on map** to generate a derived line layer: **SLD KP Ranges (derived)**.
9. Use **Save map ranges...** to export the derived layer (e.g. to GeoPackage).

---

## ✨ Version 1.3.0

**Released: 2025-09-06**

This version introduces major new features, improvements, and bug fixes:

### Added
- **Import Cable Lay:** New processing algorithm to import cable lay CSV files (with time column in day count,HH:MM:SS format and DMS coordinates) as a point layer.
- **Import Ship Outline:** New processing algorithm to import a ship outline from a DXF file as a polyline or polygon, with user-defined scale, rotation, and CRP offset. All features are merged into a single geometry by default, and the default geometry type is now Polyline.
- **Place Ship Outlines at Points:** New processing algorithm to place a ship outline geometry at each point in a selected point layer, rotated to a heading field and supporting additional CRP and rotation offsets.
- **Catenary Calculator tool:** Added a basic subsea cable catenary calculator tool.
- **Depth Profile tool:** New dockable profile tool to plot depth or slope from an MBES raster or contour layer along either a selected route line layer or a user‑drawn temporary line.
- **Transit Measure Tool:** New interactive map tool for measuring cumulative geodesic distances along user-drawn paths, with transit duration calculations based on configurable speed and distance units. Supports saving measurements as vector layers with detailed attributes.
- **New icons:** Added new icons for the KP Plot tool and KP mouse tool.

### Fixed
- **Plugin Installation Reliability:** Bundled Python libraries (e.g., openpyxl) are now always available to the plugin by automatically adding the plugin's `lib/` directory to the Python path. This prevents `ModuleNotFoundError` for openpyxl and similar issues, ensuring seamless installation for all users via the QGIS Plugin Repository.
- No user action required; users do not need to manually install Python packages for the plugin to work.
- **Import Excel RPL Robustness:** Added robust error handling for Excel file loading. If an Excel file is corrupted, unsupported, or cannot be read, the tool now raises a clear error message and prevents QGIS from crashing. This helps users diagnose file issues and improves plugin stability.
- **Import Excel RPL Smart Data Detection:** Fixed UnboundLocalError and improved end-of-data detection. The tool now automatically detects when RPL data ends (e.g., when encountering user workings or other tables below the RPL) and gracefully stops processing instead of crashing. Includes intelligent handling of conversion errors and invalid coordinate data, with clear feedback about what was successfully imported.
- **Import Excel RPL Output Layer Naming:** Output layer names now correctly reflect the source Excel file name, ensuring that the generated point and line layers are named after the input file for better traceability and usability.
- **Memory Management:** Enhanced resource cleanup to prevent memory leaks and improve plugin reliability. Improved cleanup of matplotlib figures, map tool rubber bands, vertex markers, and event connections when tools are deactivated or the plugin is unloaded. Added proper destructors and exception handling during cleanup operations.
- **Exit Crash Fix (KP Mouse Tool & KP Plotter):** Resolved a rare access violation (Windows fatal exception: access violation in QGraphicsItemPrivate::removeExtraItemCache during QGIS shutdown) that occurred after using the KP Mouse Map Tool or KP Plotter. The cleanup logic was revised to avoid manual `scene.removeItem()` calls and potential double-deletion of `QgsRubberBand` / `QgsVertexMarker` objects. Now uses a safe hide + `deleteLater()` strategy with `sip.isdeleted` guarding and lighter destructors.

---
It's a new tool and not extensively tested yet, so apologies if you encounter issues or find the documentation lacking. Please report issues or suggestions via the [GitHub issue tracker](https://github.com/k-mcmonagle/subsea-cable-tools/issues). I'll continue to test and improve the tools.

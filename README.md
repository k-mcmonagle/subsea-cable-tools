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

### 📦 Dependencies (end users)

- This plugin vendors key Python libraries under `lib/` (e.g. `openpyxl`, `pyqtgraph`) so users do **not** need to install pip packages for typical workflows.
- **MDB Import:** requires Windows + a working Microsoft Access ODBC driver (Microsoft Access Database Engine) and `pyodbc` available to the QGIS Python environment.

### 🗺️ Map & Dockable Tools
- **KP Mouse Tool** – An interactive tool that provides the closest KP and DCC of the mouse pointer dynamically. Supports **Ellipsoidal** (project ellipsoid; fallback WGS84) and **Cartesian** (planar, in project CRS units) distance modes; the configuration dialog shows both total route lengths (to **0.001 km**, ~1 m) when possible.
- **KP Data Plotter** – Dockable tool for plotting KP-based data from table layers against a reference line, with interactive crosshair, map marker, support for multiple data fields, y-axis reversal, and tooltips.
- **Depth Profile** – Dockable profile tool to plot depth or slope from MBES raster(s) or contour layer(s) along either a selected route line layer or a user-drawn temporary line. Supports invert KP / slope axis options and adaptive raster sampling.
- **Catenary Calculator** and **Catenary Calculator V2 (Experimental)** – Dialog tools for subsea cable catenary calculations; V2 supports multi-segment cable assemblies and bodies.
- **Transit Measure Tool** – Interactive map tool for measuring cumulative geodesic distances along user-drawn paths, with transit duration calculations and an optional Quick Buffer.

#### Distance / KP measurement notes

- Most tools use QGIS' built-in `QgsDistanceArea` with the **project ellipsoid** (fallback **WGS84**) for **ellipsoidal/geodesic** distances.
- **Cartesian/planar** measurements are only meaningful in a **projected** CRS. If the project CRS is geographic (degrees), cartesian values are disabled/marked as not available.
- Where a layer CRS differs from the project CRS, tools may transform geometries to the project CRS before measuring (the KP Mouse Tool does this). Some existing tools currently assume the project CRS and selected layer CRS match.

---

## ✨ Version 1.5.0

**Released: 2026-04-26**

This is the first published release since 1.3.0 and consolidates a large body of internal 1.4.x development.

### Highlights
- Many new processing algorithms, including **Extract KP Ranges (Rule Based)**, **Identify Features Intersecting RPL**, **RPL Route Comparison**, **Translate KP Between RPLs**, **Identify RPL Crossing Points**, **Identify RPL Area Listing**, **Merge / Group KP Range Tables**, **KP Range Depth + Slope Summary**, **Identify Hazards in Lay Corridor**, **Export Chartlets**, **Plot Line Segments from Table**, **Extract A/C Points** and **Extract Lines Intersecting Polygons**.
- **Catenary Calculator V2 (Experimental)** – multi-segment cable assemblies and bodies.
- **KP Mouse Tool** now supports ellipsoidal vs cartesian distance modes (remembered between sessions), right-click depth sampling, configurable copy-to-clipboard, and a **Go to KP…** action.
- **Depth Profile** improvements: invert KP / slope axes, multiple raster support with resolution-based preference, refresh control, and adaptive sampling.
- **Import Bathy from MDB** rework: subprocess ODBC reads (no more silent crashes), better GeoMedia metadata parsing, scratch-layer outputs, and Polygon/Point layers loaded alongside LineString.
- **Transit Measure Tool** Quick Buffer.
- Updated minimum QGIS version to **3.22**, lazy-imported plotting deps, and a more robust processing-provider registration so a single bad algorithm can't hide the rest.

### Withdrawn from this release
A few in-development tools that appeared in unreleased internal builds are **not included** in 1.5.0 and are being held back for further work. They may return in a future version.

See [CHANGELOG.md](CHANGELOG.md) for the full list of changes.

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

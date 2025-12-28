# Changelog

All notable changes to the Subsea Cable Tools QGIS plugin will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.4.0] - 2025-10-10
### Added
- **Live Data Tool:** New dockable tool for receiving and plotting real-time ship position data from a TCP server. Basic tool for now but would like to add live trend graphs and possibly use QGIS as client screens onboard.
- **Catenary Calculator v2 (Experimental):** Added an updated/alternative catenary calculator dialog (v2). Experimental for now and still in testing.
- **Plot Line Segments from Table:** New processing algorithm under "Other Tools" to create line segments from a table layer with start and end latitude/longitude columns. Optionally creates a point layer for the endpoints. All original attributes are preserved with a source_table field added.
- **Extract A/C Points from RPL:** New processing algorithm to extract Alter Course points from an RPL line (including multi-feature routes), outputting KP, turn angle, and optional threshold/bin fields for easy symbology.
- **RPL Route Comparison:** New processing algorithm to compare design vs as-laid RPL routes, calculating position offsets including radial, along-track, and cross-track distances.
- **Translate KP Between RPLs (Points):** New processing algorithm to translate KP values from one RPL reference to another, creating corresponding points on the target RPL.
- **Identify RPL Crossing Points:** New processing algorithm to find crossing points between an RPL line layer and one or more asset line layers. Outputs KP, lat/lon, relative crossing angle, and crossed asset attributes. Also supports optional buffer polygons around the crossed asset near each crossing.
- **Identify RPL Area Listing.** New processing algorithm that takes input of an RPL line layer, and one or more polygon layers (e.g. seabed features, or hazard areas), and outputs a line layer traced over the RPL line with breaks at the edges of the polygon layer features. The polygon feature attributes are picked up by the new line layer and start and end KP are also included.
- **Merge KP Range Tables:** New Processing tool under "KP Ranges" to combine two KP-range tables with mismatched intervals.
  - Supports canonical segmentation (non-overlapping KP intervals), summarise (Table B values into Table A ranges with min/max/avg or aggregated single value), and a simple lookup mode (copy one field with overlap resolution rules).
  - Includes overlap handling options (first/most-specific/min/max/mean/weighted-mean/error), optional full-coverage checks, and remembers last-used parameters between runs.
- **Group Adjacent KP Ranges by Field:** New processing tool under "KP Ranges" that merges consecutive KP intervals sharing the same attribute value, keeping one feature per run and optionally nulling conflicting other fields.
- **KP Range Depth + Slope Summary:** New Processing tool under "KP Ranges" to sample bathymetry along KP-range line features and append summary fields.
  - Supports Raster(s) (prefers highest resolution where rasters overlap) or 1–2 Contour layers (minor/major) with selectable depth fields.
  - Outputs depth min/max/avg, along-route slope min/max/avg, and cross-track (side slope) min/max/avg.
  - Includes optional adaptive raster sampling (step derived from raster resolution along the route) and optional directional extremes (up/down, port/stbd).
- **Identify Hazards in Lay Corridor:** Processing tool that compares point, line, and polygon layers against a lay corridor polygon and RPL reference, exporting point/line/area proximity listings with KP/DCC, lat/lon, and JSON-encoded source attributes for every encroaching feature.
- **Export Chartlets Based on KP Range List:** Processing tool that walks through each KP range/segment (table or geometry) and creates a per-section map PNG, either using segment geometries directly or extracting the KP span from an RPL line, with configurable extra buffers, exported layers, and layout elements.

### Changed
- **KP Mouse Map Tool:** Improved to calculate KP based on geodetic measurements by default, with option for Cartesian calculations when the layer uses a projected CRS. Also added option to sample depth value at the mouse position from a raster or contour layer using right click. Also added option to configure the copy to clipboard function. Also added a "Go to KP..." option in the dropdown
- **Depth Profile tool:** Improved raster/contour inputs and performance for long routes.
  - Contours: second contour layer is now optional (works with 1 or 2 contour layers).
  - Rasters: supports selecting multiple raster layers; overlapping rasters prefer higher resolution first and missing coverage remains null with warnings.
  - Added Refresh control and live sample/probe estimate readout.
  - Added optional adaptive raster sampling (step derived from raster resolution along the route).
- **Transit Measure Tool:** Added a Quick Buffer tool to apply a buffer to the route.
- **Processing Toolbox Grouping:** Made some changes to the grouping to try to make it more intuitive.

### Fixed
- Added option to invert slope angle/percentage calculation in Depth Profile tool, with default not inverted.
- Fixed some cleanup issues with the transit measure tool.
- Fixed an issue with the Nearest KP tool not working well with RPLs that have multisegment splits.

## [1.3.0] - 2025-09-06

### Added
- **Import Cable Lay:** New processing algorithm to import cable lay CSV files (with time column in day count,HH:MM:SS format and DMS coordinates) as a point layer.
- **Import Ship Outline:** New processing algorithm to import a ship outline from a DXF file as a polyline or polygon, with user-defined scale, rotation, and CRP offset. All features are merged into a single geometry by default, and the default geometry type is now Polyline.
- **Place Ship Outlines at Points:** New processing algorithm to place a ship outline geometry at each point in a selected point layer, rotated to a heading field and supporting additional CRP and rotation offsets.
- **Catenary Calculator tool:** Added a basic subsea cable catenary calculator tool.
- **Depth Profile tool:** New dockable profile tool to plot depth or slope from an MBES raster or contour layer along either a selected route line layer or a user‑drawn temporary line.
- **Transit Measure Tool:** New interactive map tool for measuring cumulative geodesic distances along user-drawn paths, with transit duration calculations based on configurable speed and distance units. Supports saving measurements as vector layers with detailed attributes.
- **New icons** Added some new icons for the KP Plot tool and KP mouse tool.

### Fixed
- **Plugin Installation Reliability:** Bundled Python libraries (e.g., openpyxl) are now always available to the plugin by automatically adding the plugin's `lib/` directory to the Python path. This prevents `ModuleNotFoundError` for openpyxl and similar issues, ensuring seamless installation for all users via the QGIS Plugin Repository.
- No user action required; users do not need to manually install Python packages for the plugin to work.
- **Import Excel RPL Robustness:** Added robust error handling for Excel file loading. If an Excel file is corrupted, unsupported, or cannot be read, the tool now raises a clear error message and prevents QGIS from crashing. This helps users diagnose file issues and improves plugin stability.
- **Import Excel RPL Smart Data Detection:** Fixed UnboundLocalError and improved end-of-data detection. The tool now automatically detects when RPL data ends (e.g., when encountering user workings or other tables below the RPL) and gracefully stops processing instead of crashing. Includes intelligent handling of conversion errors and invalid coordinate data, with clear feedback about what was successfully imported.
- **Import Excel RPL Output Layer Naming:** Output layer names now correctly reflect the source Excel file name, ensuring that the generated point and line layers are named after the input file for better traceability and usability.
- **Memory Management:** Enhanced resource cleanup to prevent memory leaks and improve plugin reliability. Improved cleanup of matplotlib figures, map tool rubber bands, vertex markers, and event connections when tools are deactivated or the plugin is unloaded. Added proper destructors and exception handling during cleanup operations.
- **Exit Crash Fix (KP Mouse Tool & KP Plotter):** Resolved a rare access violation (Windows fatal exception: access violation in QGraphicsItemPrivate::removeExtraItemCache during QGIS shutdown) that occurred after using the KP Mouse Map Tool or KP Plotter. The cleanup logic was revised to avoid manual `scene.removeItem()` calls and potential double-deletion of `QgsRubberBand` / `QgsVertexMarker` objects. Now uses a safe hide + `deleteLater()` strategy with `sip.isdeleted` guarding and lighter destructors.

## [1.2.0] - 2025-07-13

### Added
- **KP Data Plotter:** New dockable tool for plotting KP-based data from table layers against a reference line, with interactive crosshair, map marker, and support for multiple data fields, y-axis reversal, and tooltips.
- **Merge MBES Rasters:** New tool to merge multiple MBES raster layers into a single raster, preserving depth (Z) values and ensuring NoData areas are transparent. Useful for mosaicking adjacent or overlapping MBES tiles.
- **Create Raster from XYZ:** New tool to convert XYZ (Easting, Northing, Depth) files to raster using a robust VRT-based method. Supports direct rasterization and IDW interpolation, with auto grid size detection and CRS selection.
- **KP Range Highlighter:** Added `length_km` field to output, representing the segment length in km.
- **Import Excel RPL:** Added an optional 'Chart No' field to the import tool and output layers.
### Changed
- **Import Excel RPL:** User-defined column mappings are now saved between sessions, improving usability when importing multiple files with the same format.
- **KP Range Highlighter:** Output layer now only includes `start_kp`, `end_kp`, and (if provided) `custom_label` fields.
- **KP Range Highlighter:** The `custom_label` field is only included if a value is provided by the user.
- **Place KP Points Along Route:** This tool has been enhanced to optionally sample depth values from a raster layer (e.g., MBES), creating a KPZ output. It now includes a CRS check and provides warnings for points outside the raster's extent.
### Fixed
- **Import Excel RPL:** Fixed a bug causing a `KeyError` when the optional 'Chart No' field was not provided.
- **KP Range Highlighter:** No longer attempts to copy source layer attributes, preventing type conversion errors.

## [1.1.0] - 2025-07-05

### Changed
- **Enhanced KP Mouse Tool Tooltip.**
  - The tooltip is now persistent, remaining visible when the mouse is stationary.
  - It only appears when the QGIS window is active, preventing it from showing over other applications.
  - Terminology has been updated to `KP`, `rKP`, and `DCC`.
  - The display order has been adjusted to show `KP`/`rKP` first and `DCC` last for better readability.
- **Overhauled KP Mouse Map Tool for improved usability and robustness.**
  - Replaced the simple toggle with a dedicated toolbar button featuring a dropdown menu for quick access to configuration.
  - The tool now works seamlessly with both dissolved (single-part) and exploded (multi-part) line layers, ensuring accurate KP calculation across the entire route.
  - KP (chainage) calculation is now continuous and precise, correctly handling complex line geometries with multiple features and segments.
- **Improved KP Range Highlighter from CSV tool workflow.** The tool now requires the user to load data as a table layer, enabling KP field selection via dropdown menus for a more intuitive experience.
- **Simplified CSV tool output.** The output now automatically includes all columns from the source table (except KP fields), plus `source_table` and `source_line` fields for better traceability.
- **Improved robustness of KP Range tools.** Both highlighter tools now correctly handle multi-segment (non-dissolved) line layers internally.
- Replaced the single nearest point lookup with a more robust segment-based approach
- Modified distance calculation method to ensure accurate measurements

### Added
- **Added a new configuration dialog for the KP Mouse Map Tool.**
  - Users can now select any line layer in their project as the reference for KP measurements.
  - Added ability to choose the preferred unit for distance measurement (metres, km, miles, nautical miles).
  - Added a new option to display Reverse KP, calculated from the end of the line.
  - The dialog displays key metrics for the selected reference layer, including total length and vertex count (AC Count).
- **Implemented persistent settings for the KP Mouse Map Tool.**
  - The selected reference layer, measurement unit, and Reverse KP display setting are now saved and automatically reloaded between QGIS sessions.
- **Added three new tools for placing KP points:**
  - **Place KP Points Along Route:** Places points at regular, user-defined intervals (e.g., 1 km, 50 km, custom).
  - **Place KP Points from CSV:** Places points based on KP values from a CSV file or other table layer, preserving all original attributes.
  - **Place Single KP Point:** Places a single point at a user-specified KP, with attributes for lat/lon.
- Added detailed, user-friendly help panels with step-by-step instructions to all KP tools (Highlighters and Point Placers).

### Fixed
- Resolved multiple QGIS API compatibility issues (`closestSegment`, `closestPoint`, `nearestPoint`) in the KP Mouse Map Tool to ensure it functions reliably across different QGIS versions.
- Fixed a bug in the KP Range Highlighter from CSV tool caused by an incorrect method call (`.name()` instead of `.sourceName()`).
- Fixed an earlier bug in the CSV tool related to line length calculation.
- Improved the `nearest_kp_algorithm.py` to accurately calculate the shortest distance from points to the reference line
  - Implemented a segment-by-segment approach to find the true nearest point
  - Fixed handling of multipart line geometries
  - Enhanced KP value calculation based on the exact position along the line
  - Resolved issues where the drawn lines weren't showing the true shortest distance

## [1.0.0] - Initial Release

### Added
- Initial release of the Subsea Cable Tools plugin
- Nearest KP algorithm for finding the closest point on reference lines
- KP range CSV algorithm
- KP range highlighter algorithm
- Import bathymetry MDB algorithm
- Import Excel RPL algorithm
- KP Mouse MapTool for interactive KP measurement
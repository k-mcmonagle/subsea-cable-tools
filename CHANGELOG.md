# Changelog
## [1.3.0] - 2025-09-06
### Added


All notable changes to the Subsea Cable Tools QGIS plugin will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
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

---
## ✨ Version 1.2.0

**Released: 2025-07-16**

This version includes several major new features, improvements, and bug fixes:

### Added
- **KP Data Plotter:** New dockable tool for plotting KP-based data from table layers against a reference line, with interactive crosshair, map marker, and support for multiple data fields, y-axis reversal, and tooltips.
- **Merge MBES Rasters:** New tool to merge multiple MBES raster layers into a single raster, preserving depth (Z) values and ensuring NoData areas are transparent.
- **Create Raster from XYZ:** New tool to convert XYZ (Easting, Northing, Depth) files to raster using a robust VRT-based method. Supports direct rasterization and IDW interpolation, with auto grid size detection and CRS selection.
- **KP Range Highlighter:** Added `length_km` field to output, representing the segment length in km.
- **Import Excel RPL:** Added an optional 'Chart No' field to the import tool and output layers.

### Changed
- **Import Excel RPL:** User-defined column mappings are now saved between sessions, improving usability when importing multiple files with the same format.
- **KP Range Highlighter:** Output layer now only includes `start_kp`, `end_kp`, and (if provided) `custom_label` fields.
- **KP Range Highlighter:** The `custom_label` field is only included if a value is provided by the user.
- **Place KP Points Along Route:** Enhanced to optionally sample depth values from a raster layer (e.g., MBES), creating a KPZ output. Now includes a CRS check and provides warnings for points outside the raster's extent.

### Fixed
- **Import Excel RPL:** Fixed a bug causing a `KeyError` when the optional 'Chart No' field was not provided.
- **KP Range Highlighter:** No longer attempts to copy source layer attributes, preventing type conversion errors.

---
It's a new tool and not extensively tested yet, so apologies if you encounter issues or find the documentation lacking. Please report issues or suggestions via the [GitHub issue tracker](https://github.com/k-mcmonagle/subsea-cable-tools/issues). I'll continue to test and improve the tools.

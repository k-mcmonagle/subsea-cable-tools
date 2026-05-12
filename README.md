# Subsea Cable Tools

**Subsea Cable Tools** is a QGIS plugin for working with subsea telecom and power cable data: route position lists (RPLs), KP-based queries, bathymetry, lay corridors, and related survey/engineering tasks.

Targets QGIS 3.22 or newer and declares compatibility through QGIS 4.x (`qgisMaximumVersion=4.99`). See [docs/qgis_compatibility_test_plan.md](docs/qgis_compatibility_test_plan.md) for the validation checklist before publishing a release.

---

## Features

### Processing algorithms

Available in the Processing Toolbox under **Subsea Cable Tools**, grouped into:

- **Route handling** â€“ Import Excel RPL, Import Cable Lay, Import / Place Ship Outlines, Plot Line Segments from Table, Translate KP Between RPLs, RPL Route Comparison.
- **KP & ranges** â€“ Place KP Points (along route, from CSV, single), Find Nearest KP, KP Range CSV / Highlighter / Merge / Group, Extract KP Ranges (Rule Based), Extract A/C Points, KP Range Depth + Slope Summary.
- **RPL listings & crossings** â€“ Identify RPL Crossing Points, Identify RPL Area Listing, Identify Features Intersecting RPL, Dynamic Buffer (Lay Corridor), Extract Lines Intersecting Polygons, Export KP Section Chartlets.
- **Bathymetry** â€“ Import MDB (`import_mdb`, formerly Import Bathy MDB), Add Depth to Point Layer, Create Raster from XYZ, Merge MBES Rasters, Calculate Seabed Length.

### Map & dockable tools

- **KP Mouse Tool** â€“ live KP/DCC under the cursor; ellipsoidal or cartesian distance modes; geodesic range ring; "Go to KPâ€¦".
- **KP Data Plotter** â€“ dockable plot of KP-based table data against a route, with crosshair, marker and per-field axis assignment.
- **Depth Profile** â€“ dockable profile from MBES raster(s) or contours along a route or temporary line; depth/slope plots with adaptive sampling.
- **Catenary Calculator** (legacy) and **Catenary Calculator V2** (multi-segment).
- **Transit Measure Tool** â€“ cumulative geodesic distance along a drawn path with transit-time output and an optional Quick Buffer.

### Distance & CRS methodology

- **Ellipsoidal by default.** All distance/KP measurements go through one shared `QgsDistanceArea` helper that uses the project ellipsoid, with a WGS84 fallback when the project ellipsoid is unset.
- **Cartesian opt-in.** KP-emitting algorithms expose a Distance mode (Ellipsoidal / Cartesian); Cartesian is rejected on geographic CRSes.
- **Layer-CRS measurement.** Tools measure in the layer's own CRS to avoid silent unit confusion. Mismatched inputs (e.g. KP Plotter line vs project, Find Nearest KP points vs paths, Place KP Points sample raster) are auto-reprojected with a feedback note. The exception is "Translate KP Between RPLs", which still requires both layers to share a CRS.
- **Geodesic interpolation along a route.** On geographic CRSes, points placed at a given KP follow the great circle on each segment instead of being linearly interpolated in lon/lat.

### Dependencies

The plugin vendors `openpyxl`, `pyqtgraph` and `et_xmlfile` under `lib/`, added to `sys.path` only when missing from the host QGIS Python. End users do not need to install pip packages for typical workflows; plugin plotting tools use the vendored `pyqtgraph` backend.

The MDB import additionally requires Windows + the Microsoft Access Database Engine ODBC driver and `pyodbc` available to the QGIS Python.

---

See [CHANGELOG.md](CHANGELOG.md) for the full version history.

Issues and suggestions are welcome on the [GitHub issue tracker](https://github.com/k-mcmonagle/subsea-cable-tools/issues).


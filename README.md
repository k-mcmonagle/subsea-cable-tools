# Subsea Cable Tools

Subsea Cable Tools is a personal, experimental QGIS plugin for subsea telecom and power cable data. It started as a few useful Processing algorithms packaged together; development has accelerated as AI coding tools have improved. It targets QGIS 3.22+ and QGIS 4.x and is not associated with any company.

**Check all outputs against source data and independent methods before using them for engineering or operational decisions.** Features are still being developed and may contain errors.

## Processing algorithms

Find these in the QGIS Processing Toolbox under **Subsea Cable Tools**.

### RPL tools

- **Import RPL to Workbench (auto-detect)** — detect and import an Excel or CSV route position list.
- **Import Excel RPL (legacy, deprecated)** — import an RPL with manual column mapping.
- **Add RPL Layers to Workbench** — register existing RPL point and line layers.
- **Compare Design vs As-Laid Routes** — calculate differences between two routes.
- **Translate KP Between RPLs (Points)** — transfer point KPs between route references.
- **Extract A/C Points from RPL** — extract alter-course positions.
- **Identify RPL Crossing Points** — locate route crossings.
- **Identify RPL Area Listing** — list areas crossed by a route.
- **Identify Features Intersecting RPL** — list nearby or intersecting features.
- **Calculate Seabed Length** — estimate sampled route length over bathymetry.

### KP points and ranges

- **Place KP(Z) Points Along Route** — create regularly spaced route points, optionally with depth.
- **Place KP Points from CSV** — place points at KPs listed in a table.
- **Place Single KP Point** — locate one KP on a route.
- **Nearest KP** — find each point's closest route KP.
- **Add Depth to Point Layer** — sample bathymetry at existing points.
- **KP Range Highlighter** — map specified route sections.
- **KP Range Highlighter from CSV** — map sections listed in a CSV.
- **Merge KP Range Tables** — combine range tables.
- **Group Adjacent KP Ranges by Field** — join consecutive ranges with matching values.
- **Extract KP Ranges (Rule Based)** — create ranges matching selected rules.
- **KP Range Depth + Slope Summary** — summarise depth and slope by range.

### Cable lay data

- **Create Cable Lay GeoPackage** — create standard layers for imported lay data.
- **Import Cable Lay Data (CSV)** — import cable lay records.
- **Import Event Log** — import operation events.
- **Import Slack Log** — import slack records.
- **Import Body Log** — import body-position records.
- **Import 3D Model Solutions** — import model solution records.
- **Import As-Laid** — import as-laid records.
- **Import Plough Data** — import plough records.
- **Recompute ISO Time (fix start date)** — correct times after a start-date change.
- **Run Cable Lay QC** — flag gaps, duplicates and other data issues.

### Bathymetry and other utilities

- **Import MDB** — import GeoMedia Access feature classes.
- **Import Path File (.pthmdb)** — import route and profile data from a path database.
- **Create Raster from XYZ** — grid XYZ bathymetry files.
- **Merge MBES Rasters** — combine bathymetry grids.
- **Dynamic Buffer (Lay Corridor)** — create a route corridor with changing width.
- **Export KP section chartlets** — export map images for route sections.
- **Extract Lines Intersecting Polygons** — select lines crossing areas.
- **Import Ship Outline (DXF)** — import a vessel outline.
- **Place Ship Outlines at Points** — position vessel outlines at map points.
- **Place Outline Along Route (KP)** — position an outline at a route KP.
- **Plot Line Segments from Table** — draw segments from tabular coordinates.

## Map and dock tools

These are in **Plugins ▸ Subsea Cable Tools**. The main toolbar has **KP Mouse Tool**, **KP Plot**, **Depth Profile**, **Catenary Calculator V2** and **Transit Measure**; tools under active development are in its **Experimental** dropdown.

- **KP Mouse Tool** — inspect live route KP and nearby depth on the map.
- **KP Plot** — plot KP-indexed table values along a route.
- **Depth Profile** — plot bathymetry and slope along a line.
- **Catenary Calculator V2** — explore a two-dimensional static cable model.
- **Transit Measure** — measure a drawn path and estimate transit time.
- **Cable Route Workbench** — manage cable systems, RPL revisions and assemblies.
- **Planner** — build map-linked vessel and resource schedules.
- **Burial Planner** — develop burial plans from routes and survey data.
- **Cable Lay Data Explorer** — inspect and analyse imported lay records.
- **Cable Lay Simulator (3D)** — explore cable-lay and deployment scenarios.
- **BU Lowering Tool (3D)** — explore branching-unit lowering scenarios.
- **KP settings** — choose geodesic or grid-based KP measurement.
- **Save Layers to GeoPackage** — save selected project layers into one GeoPackage.

The [KP and bathymetry methods](docs/KP_AND_BATHYMETRY.md) explain distance, depth and slope conventions. The [changelog](CHANGELOG.md) records releases. Contributions and [pull requests](https://github.com/k-mcmonagle/subsea-cable-tools/pulls) are welcome. Please report bugs and suggestions in the [issue tracker](https://github.com/k-mcmonagle/subsea-cable-tools/issues).

Ground Model numeric profiles: see the [import and assignment workflow](docs/GROUND_NUMERIC_PROFILES.md) for CPT and other investigation data.

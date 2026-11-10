# Subsea Cable Tools

Subsea Cable Tools is a personal, experimental QGIS plugin for subsea telecom and power cable data. It started as a few useful Processing algorithms packaged together; development has accelerated as AI coding tools have improved. It targets QGIS 3.22+ and QGIS 4.x and is not associated with any company.

**Check all outputs against source data and independent methods before using them for engineering or operational decisions.** Features are still being developed and may contain errors.

## GeoPackage locations

New project files suggest a subfolder beside the saved QGIS project: `burial_plans/`, `workbench/`, `planner/`, `span_analysis/`, `cable_lay/` or `route_comments/`. Choose another location in the file dialog if needed. Additional plans or scenarios use the already open GeoPackage. Existing files stay in their current locations; the plugin does not move or copy them. The cable type library keeps its shared, user-selected location.

To reorganise an existing project, close QGIS, move the GeoPackages, then reopen the project. Keeping each filename and using the suggested subfolder allows automatic recovery of recognised files. For another folder or a renamed file, open the relevant tool and use **Plan file → Open existing plans** (Burial Planner), **File → Open existing Workbench**, or the tool's **Open** action. Matching broken layers are repaired in place, keeping their IDs and styles. If QGIS shows its unavailable-layer dialog for another folder, keep the unavailable layers so the tool can relink them. Save the QGIS project after relinking. A missing saved file is not replaced with a new empty database, and ambiguous matches require you to choose the file.

Move a Route Comments register's `<filename>_attachments` folder alongside its GeoPackage so its images remain available. For an unsaved QGIS project, file suggestions use a writable QGIS profile folder instead.

## Processing algorithms

Find these in the QGIS Processing Toolbox under **Subsea Cable Tools**.

### RPL tools

- **Import RPL to Workbench (auto-detect)** — detect and import an Excel or CSV route position list.
- **Import Excel RPL (legacy, deprecated)** — import an RPL with manual column mapping.
- **Add RPL Layers to Workbench** — register existing RPL point and line layers.
- **Compare Design vs As-Laid Routes** — pair design and as-laid events (tolerating renamed or mistyped events) and report along-track, cross-course and radial offsets; the Workbench's *Compare RPLs ▸ Events* tab does the same interactively ([method](docs/RPL_EVENT_COMPARISON.md)).
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
- **Densify Route Along KP Path** — a display copy of a route that draws where its KPs lie in any map CRS.
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

### Geotechnical

- **Import AGS CPT Data** — read cone penetration tests from AGS 3 / AGS 4 files, optionally joined to a position and summary table, into a locations layer and readings and strata tables with derived parameters ([method](docs/GEOTECHNICAL_CPT.md)).

### Bathymetry and other utilities

- **Import MDB** — import GeoMedia Access feature classes, with the CRS detected from the file.
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

- **KP Mouse Tool** — inspect live route KP and nearby depth on the map; right-click to add a route comment.
- **KP Plot** — plot KP-indexed table values along a route.
- **Depth Profile** — plot bathymetry and slope along a line.
- **Catenary Calculator V2** — explore a two-dimensional static cable model; its *Steady Lay (beta)* tab gives the cable angle, layback and tensions while laying at a given ship speed, with current drag.
- **Transit Measure** — measure a drawn path and estimate transit time.
- **Cable Route Workbench** — manage cable systems, RPL revisions and assemblies.
- **Planner** — build map-linked vessel and resource schedules.
- **Burial Planner** — develop burial plans from routes and survey data.
- **Cable Lay Data Explorer** — inspect and analyse imported lay records; its *Lay Assessment* tab lists KP ranges of possible suspensions, loop risk and tension-limit exceedances, with a seabed profile of where the cable rests ([method](docs/LAY_ASSESSMENT.md)).
- **Span & Slack Analysis (beta)** — rest a cable on the seabed along a route, vary the bottom tension or slack, and see spans, loop risk, residual tension and friction effects, with a bending-stiffness detail model and a tension sweep ([method](docs/SPAN_ANALYSIS.md)).
- **Route Comments** — capture, search and export spatial comments on routes (route points, KP ranges, map locations) in a shared GeoPackage register, with replies, images and custom fields ([guide](docs/ROUTE_COMMENTS.md)).
- **Cable Type Library** — keep your cable and rope types (weights, NPTS / NOTS / NTTS / CBL, MBR) in a GeoPackage of your own.
- **KP settings** — choose geodesic or grid-based KP measurement.
- **Save Layers to GeoPackage** — save selected project layers into one GeoPackage.

The [KP and bathymetry methods](docs/KP_AND_BATHYMETRY.md) explain distance, depth and slope conventions. The [changelog](CHANGELOG.md) records releases. Contributions and [pull requests](https://github.com/k-mcmonagle/subsea-cable-tools/pulls) are welcome. Please report bugs and suggestions in the [issue tracker](https://github.com/k-mcmonagle/subsea-cable-tools/issues).

Ground Model numeric datasets: see the [dataset workflow](docs/GROUND_NUMERIC_PROFILES.md) for investigation data in tables; CPTs delivered as AGS files are imported directly (see [CPTs from AGS files](docs/GEOTECHNICAL_CPT.md)).

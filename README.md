# Subsea Cable Tools

Subsea Cable Tools is a personal, experimental QGIS plugin for subsea telecom and power cable data. It started as a few useful Processing algorithms packaged together; development has accelerated as AI coding tools have improved. It targets QGIS 3.22+ and QGIS 4.x and is not associated with any company.

**Check all outputs against source data and independent methods before using them for engineering or operational decisions.** Features are still being developed and may contain errors.

## GeoPackage locations

New project files suggest a subfolder beside the saved QGIS project: `burial_plans/`, `workbench/`, `planner/`, `span_analysis/`, `cable_lay/` or `route_comments/`. Choose another location in the file dialog if needed. Additional plans or scenarios use the already open GeoPackage. Existing files stay in their current locations; the plugin does not move or copy them. The cable type library keeps its shared, user-selected location.

To reorganise an existing project, close QGIS, move the GeoPackages, then reopen the project. Keeping each filename and using the suggested subfolder allows automatic recovery of recognised files. For another folder or a renamed file, open the relevant tool and use **Plan file → Open existing plans** (Burial Planner), **File → Open existing Workbench**, or the tool's **Open** action. Matching broken layers are repaired in place, keeping their IDs and styles. If QGIS shows its unavailable-layer dialog for another folder, keep the unavailable layers so the tool can relink them. Save the QGIS project after relinking. A missing saved file is not replaced with a new empty database, and ambiguous matches require you to choose the file.

Ground Model **Sources…** lists original measurement files and live placement layers. Locate a moved workbook, AGS file or layer and save its link while retaining imported CPT readings and numeric measurements. The numeric dataset's **Check…** window also opens Sources. Missing originals and investigations without KP ranges are reported separately.

Move a Route Comments register's `<filename>_attachments` folder alongside its GeoPackage so its images remain available. For an unsaved QGIS project, file suggestions use a writable QGIS profile folder instead.

## Processing algorithms

Find these in the QGIS Processing Toolbox under **Subsea Cable Tools**.

### RPL tools

- **Import RPL to Workbench (auto-detect)** — detect and import an Excel or CSV route position list.
- **Import Excel RPL (legacy, deprecated)** — import an RPL with manual column mapping.
- **Add RPL Layers to Workbench** — register existing RPL point and line layers.
- **Compare Design vs As-Laid Routes** — pair design and as-laid events (tolerating renamed or mistyped events) and report along-track, cross-course and radial offsets; the Workbench's *Compare RPLs ▸ Events* tab does the same interactively ([method](docs/RPL_EVENT_COMPARISON.md)).
- **Translate KP Between RPLs (Points)** — transfer point KPs between route references.
- **Translate KPs and Ranges Between RPLs** — translate KP fields, tables, pasted lists or point/line geometry; retain source attributes and review flags, with optional points/ranges on the new route ([guide](docs/KP_TRANSLATION.md)).
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
- **Attach descriptions to seabed polygons (experimental)** — assemble annotation fragments into label points and follow leaders to selected soil polygons; under **Other Tools**.
- **Dynamic Buffer (Lay Corridor)** — create a route corridor with changing width.
- **Export KP section chartlets** — export map images for route sections.
- **Extract Lines Intersecting Polygons** — select lines crossing areas.
- **Import Ship Outline (DXF)** — import a vessel outline.
- **Place Ship Outlines at Points** — position vessel outlines at map points.
- **Place Outline Along Route (KP)** — position an outline at a route KP.
- **Plot Line Segments from Table** — draw segments from tabular coordinates.

## Seabed description assembly (experimental)

After importing an MDB, open **Other Tools → Attach descriptions to seabed polygons (experimental)**.
Select the description point layer and run to create assembled labels. The text field is detected
automatically for normal MDB imports; select it explicitly if several possible fields are present.
Add the relevant leader lines and soil polygon layers if you also want polygon descriptions.
Inspect the results at the drawing scale. Use GeoPackage or temporary outputs to preserve full
descriptions. Rotation, alignment, spacing and distance tolerances are under **Advanced parameters**.

The tool creates copies and leaves the imported layers unchanged:

- **Assembled description points** contain one point per proposed text block. `assembled_text`
  is labelled automatically. Green points have consistent layout supported by a leader anchor;
  blue points preserve single original text; amber points use layout alone and should be checked.
  Red points need review: their proposal is in `candidate_text`, with accepted text left empty.
- **Soil polygons with description candidates** retain original attributes and add
  `soil_description`, `description_status`, candidate text and source references. Only a unique
  accepted description is filled automatically. Different descriptions, overlapping polygons,
  boundary targets and invalid geometry remain review cases. Existing field names are preserved;
  newly generated fields receive a suffix if needed.
- **Leader association checks** show which text block and polygon each leader could connect,
  with the reason for accepting or rejecting it. Several leaders may share a description block.

Certainty describes geometric evidence, not confirmation of the soil interpretation. The tool
never assigns a polygon merely because it is nearest. Ambiguous text grouping, insufficient
spacing evidence and invalid geometry are explicit; no source text is silently discarded.
Geographic inputs (such as EPSG:4326) and layers with different CRSs are supported without manual
reprojection. Long routes can cross UTM zones, the date line and polar regions; there is no overall
route-length or latitude restriction. Text spacing and leader distances use the WGS84 ellipsoid.
Polygon checks use local coordinates centred on each target, with adaptive subdivision of
transformed edges (2 mm numerical tolerance; boundary checks include a 4 mm review margin).
These are calculation tolerances, not a statement of survey accuracy. Geographic polygon edges
crossing the date line follow the short longitude interval. Outputs keep the text layer's CRS;
old measurement CRS preferences are ignored. Valid source CRS definitions and datum transformations
are still required. Invalid, ambiguous or unmeasurable geometries remain review cases, and feature
count/complexity safeguards still apply. This optional tool does not change general-purpose MDB importing.

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
## Damaged or conflicting Burial Planner files

If SQLite reports **database disk image is malformed**, the affected write failed.
Exclusion results can remain visible in the current session without being saved;
the planner marks them **NOT SAVED** and stops generation after that failure.
Other edits may have reached the file, so keep every candidate copy.

For OneDrive / SharePoint conflicts, close QGIS on all editing machines and pause
sync before copying or recovering files. Preserve each GeoPackage together with
any matching `-wal`, `-shm` and `-journal` files; do not delete those sidecars or
replace a recent backup-named file with an older original based on its name.

Use **Burial Planner → Plan file… → Check / recover GeoPackages…** to select
copies and compare SQLite integrity, saved plan names and modification times,
change-log times, and stored row counts. File timestamps alone cannot establish
which copy has the intended work. Damaged files can produce incomplete summaries.
The check is available even when a plan file cannot be opened.

**Create checked copy…** makes a new SQLite snapshot including committed WAL
data. **Try index repair in a new copy…** also rebuilds indexes in that copy and
keeps the output only if its SQLite integrity check passes. This can fix some
index damage; it cannot salvage unreadable table pages, merge conflicting edits,
or recover changes that never saved. Review the recovered plans before adopting
the new file using **Open existing plans…**. A passing SQLite check does not
validate every geometry, external source link, or plan decision.

Starting a new file is a reasonable alternative when rebuilding is easier.
Keep working GeoPackages in an unsynced local folder with one editor at a time.
Hand off checked snapshots after closing QGIS; cloud file sync does not coordinate
concurrent database edits. Burial Planner, Workbench and Planner migrations now
require checked SQLite snapshots before changing an older schema, preserve
existing backups, and stop if a backup fails.

# Subsea Cable Tools

**Subsea Cable Tools** is a QGIS plugin for working with subsea telecom and power cable data: route position lists (RPLs), KP-based queries, bathymetry, lay corridors, and related survey/engineering tasks.

Targets QGIS 3.22 or newer and declares compatibility through QGIS 4.x (`qgisMaximumVersion=4.99`).

> **A note from the author (v1.6.1).** This release is a large jump from the previously published version — it introduces a substantial number of new tools, Processing algorithms and features in a single step. It is being published as a **work in progress**, so please treat it as such. As always, **do not rely on the output uncritically — sanity-check and double-check results** against your own data and established methods before using them for any operational or engineering decision.
>
> — Kieran McMonagle

---

## Features

### Processing algorithms

Available in the Processing Toolbox under **Subsea Cable Tools**, grouped into:

- **Route handling** – guided Import RPL wizard (Workbench) and Import RPL to Workbench (auto-detect) Processing tool, Import Excel RPL (legacy), Import Cable Lay, Import / Place Ship Outlines, Plot Line Segments from Table, Translate KP Between RPLs, RPL Route Comparison.
- **KP & ranges** – Place KP Points (along route, from CSV, single), Find Nearest KP, KP Range CSV / Highlighter / Merge / Group, Extract KP Ranges (Rule Based), Extract A/C Points, KP Range Depth + Slope Summary.
- **RPL listings & crossings** – Identify RPL Crossing Points, Identify RPL Area Listing, Identify Features Intersecting RPL, Dynamic Buffer (Lay Corridor), Extract Lines Intersecting Polygons, Export KP Section Chartlets.
- **Bathymetry** – Import MDB (`import_mdb`, formerly Import Bathy MDB), Add Depth to Point Layer, Create Raster from XYZ, Merge MBES Rasters, Calculate Seabed Length.

### Map & dockable tools

- **KP Mouse Tool** – live KP/DCC under the cursor; ellipsoidal or cartesian distance modes; geodesic range ring; "Go to KP…".
- **KP Data Plotter** – dockable plot of KP-based table data against a route, with crosshair, marker and per-field axis assignment.
- **Depth Profile** – dockable profile from MBES raster(s) or contours along a route or temporary line; depth/slope plots with adaptive sampling.
- **Catenary Calculator V2** (multi-segment) — 2D static model with multi-span drape, chute wrap geometry and buoyancy analysis; see [catenary/MODEL_NOTES.md](catenary/MODEL_NOTES.md) for the precise assumptions and validity envelope. (The legacy V1 calculator was removed in 1.7.0.)
- **Cable Lay Simulator (3D)** *(beta, new in 1.7.0)* — the next-generation catenary tool: interactive software-rendered 3D view plus profile/plan views over real bathymetry (including grids sampled from a project raster), with hydrodynamic drag throughout. Three modes: **Static hang** (V2 physics in 3D with current loading), **Steady lay** (ship speed + pay-out solved in the vessel frame, validated against Zajac 1957 closed forms which are shown live as quick answers), and **Operation simulation** (quasi-static stepping with a timeline scrubber: branching-unit deployment, final-bight lay-down, transient straight lay). CSV / 3D DXF export and results-to-map layers. Assumptions and validation status: [catenary/v3/V3_MODEL_NOTES.md](catenary/v3/V3_MODEL_NOTES.md).
- **Transit Measure Tool** – cumulative geodesic distance along a drawn path with transit-time output and an optional Quick Buffer.
- **Cable Route Workbench** – system-first management of cable segments, physical cable make-up and RPL revisions. The main tree is **System → Cable segment**: each segment contains an ordered cable make-up (one or more assembly placements with explicit joints) alongside its RPL revisions; the reusable assembly catalogue is available from **Manage assemblies…** instead of occupying a separate root branch. System and segment pages use a simple **Table | Schematic** layout. A system lists segment-level revision, assembly, section, length, cable-type and status figures. A segment lists its cable make-up, latest-RPL Positions and event-to-event RPL Sections, and revision history. Its schematic deliberately shows only the physical assembly/joint chain—assembly name and cable type above each line, used length below—without every geographic RPL event. Schematics are pan/zoom canvases, wrap long chains by default, provide a readable **Home** view plus an explicit **Fit all**, and build lazily. Detailed RPL editors retain configurable position/section/leg columns and revision-stable Start/End topology labels, with cached navigation. In this model, a cable segment is the stable route identity normally represented by one RPL; an RPL section runs between event positions; an assembly section is a homogeneous length within an assembly. No additional runtime dependency is required.

- **Planner** *(beta, new in 1.8.0)* — build multi-resource planning scenarios, draw or link spatial tasks, turn route legs and selected waypoints into timed operations, and import whole/partial RPLs with cable-type speed mappings plus Lay/PLGR/Plough/ROV/Recover section assignments. Route and point sketches can snap to existing points/vertices and line segments, or accept an exact WGS84 latitude/longitude or KP on an existing route task. MS Project-style indentation turns a normal row into a calculated, collapsible group summary whenever following tasks are indented beneath it; a contiguous selection can also be wrapped in a new group directly. Vessels/resources are project-level and shared by every scenario (deleting a scenario never deletes them), each with its own colour, default speed, and start offset; resource lanes run concurrently while cross-resource predecessors coordinate SIMOPS. Scenarios can schedule forward from a start or backward from a required finish. Optional advanced controls add per-scenario absolute resource availability, FS/SS/FF/SF links, lag, date constraints, milestones, explicit positions on linked routes, critical-path float, baselines, and SIMOPS/resource warnings without changing the simple defaults. New tasks inherit the preceding task's resource and predecessor; after a line operation they reference its actual finishing endpoint as a point location. Task-table columns size to their contents by default and can be resized, drag-reordered, and shown/hidden from a header right-click, with the layout remembered per user. Double-click a row number (or right-click → Zoom to task) to zoom the map to a task; playback tints the rows of tasks under way, and a live totals row summarises span, duration, and fuel. The compact table displays measured nautical-mile distance and speed, supports undo/redo, and plans can be animated or copied into MS Project. Fuel planning: give each vessel Transit/DP/Anchor/Port burn rates (per 24 h, t or m³), a start fuel, and an optional cost per unit; pick a fuel mode per task and bunker on port calls, then track burned fuel and remaining-on-board per task plus a per-resource fuel report with run-dry warnings. Waiting/standby consumption is represented by an explicit task rather than implicit gap burn. Fuel ROB can optionally be shown in moving playback labels. Baseline/actuals tools record progress, actual dates, remaining duration, and timestamped operational-change notes. A per-user standard-tasks library includes operation type and inserts curated templates as ordinary editable tasks, with CSV import/export for sharing across an organisation; RPL operation mappings can also be saved as ordered ProtectionMethod rules.

- **Burial Planner** *(beta)* — a guided **Plan → Inputs → Bathymetry Profile → Exclusions → Plan Builder → Review** workflow that turns an RPL plus survey data into a burial plan. Inputs owns route, scope and source selection; Bathymetry Profile prepares persistent depth/longitudinal/cross/absolute terrain data; Exclusions owns its ordered rules plus coarse spatial-search/sliver controls; Plan Builder owns the minimum viable candidate-section length and event/section refinement. The criteria stack (water depth / slope thresholds with signed direction-aware limits and optional water-depth-banded limits, crossings/proximity with per-feature buffers, seabed-soils polygon classes, KP-range tables, manual ranges — each carrying its criterion class and source reference) is resolved into Exclusion Areas, Constraint Influence Zones, Screening annotations, Insufficient-Information ranges and candidate burial sections with auto-placed start/end events (PLDN/PLUP in plough mode, JET_START/JET_STOP for ROV jet). The plan is always visible in three synchronised views — map layers, a longitudinal profile pane with draggable event markers, and event/section tables with confirm/lock/split/merge and operating-envelope conclusion workflows. Every change lands in an append-only log with exact rollback; events, sections and the input register export to CSV (KP to 3 decimal places with lat/lon and a metadata header), client burial proposals can be imported and diffed against each generation, and rule sets travel as JSON or copy losslessly from Workbench assessments. Analysis runs in the background over the scoped KP window with per-rule caching and 0.1 m boundary refinement. **Beta — sanity-check the output against your own data and established methods before using it for any operational or engineering decision.** No criteria values are shipped; all limits are user-entered with a source-reference field.

### Distance & CRS methodology

- **Ellipsoidal by default.** All distance/KP measurements go through one shared `QgsDistanceArea` helper that uses the project ellipsoid, with a WGS84 fallback when the project ellipsoid is unset.
- **Cartesian opt-in.** KP-emitting algorithms expose a Distance mode (Ellipsoidal / Cartesian); Cartesian is rejected on geographic CRSes.
- **Layer-CRS measurement.** Tools measure in the layer's own CRS to avoid silent unit confusion. Mismatched inputs (e.g. KP Plotter line vs project, Find Nearest KP points vs paths, Place KP Points sample raster) are auto-reprojected with a feedback note. The exception is "Translate KP Between RPLs", which still requires both layers to share a CRS.
- **Geodesic interpolation along a route.** On geographic CRSes, points placed at a given KP follow the great circle on each segment instead of being linearly interpolated in lon/lat.
- **Planner playback follows the planned feature.** Planner route lengths and speed/duration calculations remain ellipsoidal on the project ellipsoid, segment by segment. Playback points and completed/remaining bands are clipped from the stored route in the current map CRS, so they do not replace a sparse sketched edge with a visually different great-circle arc. CRS changes are handled by transforming the route before display while retaining ellipsoidal measurement.

### Slope methodology

- **Longitudinal by default.** Every slope reported by the Depth Profile tool, KP Mouse live profile, KP Range Depth + Slope Summary, the Workbench assessment and the Burial Planner is measured **along the profile line** (route KP or range line): `atan2(Δdepth, Δchainage)` in degrees (the Depth Profile tool also offers percent). It is *not* a raster gradient — steepness across or oblique to the line is not captured by these values.
- **Cross-profile on request.** The Depth Profile tool and KP Range Depth + Slope Summary can additionally compute **side slope**: an ordinary-least-squares fit of depth vs signed cross-track distance over a configurable port/starboard search transect, perpendicular to the local route tangent. The Burial Planner samples a two-point cross slope at ± its cross offset: Auto uses the bounded profile step for local terrain; enter the plough's half track width to evaluate the span under the vehicle.
- **Sign convention (plugin-wide).** Positive along-route slope = **shoaling with increasing KP/chainage** (up-slope, the terrestrial elevation convention); positive side slope = **deeper to starboard** (the vehicle leans to starboard). The seabed datum is auto-detected (positive-down depths vs negative elevations) so signs do not depend on how the raster/contours store depth. The Depth Profile tool's *Invert Slope Sign* option flips its report to +ve = deepening if preferred. The Burial Planner maps travel direction onto signed limits by swapping the down/up-slope limits when installing against KP (B→A), rather than flipping the series.
- **Sampling.** Rasters are sampled per station with nearest-cell provider sampling (band 1, best-resolution raster first where several overlap). Contour bathymetry is evaluated where contours actually **cross** the line, with linear interpolation between crossings (major/minor layers merged, duplicate crossings collapsed). Slope windows: per sampling interval in the profile tools; Workbench Assessment uses a central difference over ± its analysis step. The Burial Planner derives local longitudinal slope from its denser persisted profile (a ± profile-step central difference), so a coarse rule-search step does not flatten short, steep terrain. A Burial slope rule can instead set a **slope evaluation length** to average across the burial vehicle's bearing length. Acquisition and 0.1 m boundary refinement use the same selected scale.

### Dependencies

The plugin vendors `openpyxl`, `pyqtgraph`, `et_xmlfile`, `access_parser`, `construct` and `tabulate` under `lib/`, added to `sys.path` only when missing from the host QGIS Python. End users do not need to install pip packages for typical workflows; plugin plotting tools use the vendored `pyqtgraph` backend.

The MDB import works out of the box on any platform via the vendored pure-Python `access_parser` reader. If a particular file cannot be read that way, it falls back to ODBC, which requires Windows + the Microsoft Access Database Engine ODBC driver and `pyodbc` available to the QGIS Python.

---

### Testing

Run the full check suite (calculation cores, distance/KP utilities, an
end-to-end seabed-length check, provider registration) from a QGIS Python:

```
"C:\...\OSGeo4W\bin\python-qgis.bat" tests\run_qgis_smoke_tests.py
```

The runner boots a headless `QgsApplication` when needed and works under both
QGIS 3.22+ (Qt5) and QGIS 4.x (Qt6). The pure calculation suites
(`tests/test_catenary_solver.py`, `tests/test_simple_catenary.py`) also run on
any plain Python (NumPy required for the V2 solver).

---

See [CHANGELOG.md](CHANGELOG.md) for the full version history.

Issues and suggestions are welcome on the [GitHub issue tracker](https://github.com/k-mcmonagle/subsea-cable-tools/issues).

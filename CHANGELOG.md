# Changelog

<!--
Changelog guidelines (for contributors and coding agents; hidden when rendered):
- Add new entries under "## [Unreleased]", in the EXISTING subsection. Each version uses
  Added / Changed / Deprecated / Removed / Fixed / Security at most once, in that order.
  If the subsection you need is missing, create it in its place. Never append a second "### Added".
- One short, user-facing sentence per bullet (two at most), led by the tool name in bold:
  "- **Depth Profile:** ...". Say what changed for the user, not how it was done.
- No file paths, function/class names, test names, benchmarks or before/after essays.
  Engineering detail goes in the commit message; design decisions go in DECISIONS.md.
- Extend an existing bullet for the same feature instead of adding a near-duplicate.
- Always call out what users must know: changed results or sign conventions, data/format
  migrations, renamed tools or parameters, security fixes, and "beta - check outputs" caveats.
- On release: rename [Unreleased] to "[x.y.z] - YYYY-MM-DD", start a fresh [Unreleased],
  and update the compare links at the bottom of this file.
-->

All notable changes to the Subsea Cable Tools QGIS plugin are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The earlier engineering-level history (up to 1.9.0, plus unreleased work to September 2026) is archived in [docs/history/CHANGELOG-detailed-to-1.9.md](docs/history/CHANGELOG-detailed-to-1.9.md).

## [Unreleased]

### Fixed

- **Burial Planner — Exclusions:** a KP range table criterion now reports rows it cannot read (wrong start/end fields, empty or non-numeric KPs) and ranges outside the scope, instead of silently firing nowhere.

- **Burial Planner profile:** show the route extent before scope is applied and use three-decimal KP tick labels on bathymetry and slope plots, avoiding scientific notation on an empty profile.

- **KP displays:** show KPs to three decimal places (1 m), keeping precise Full route bounds internally when applying scope.

- **Burial Planner:** set ordinary project line layers, including temporary routes, without Workbench RPL fields; use their measured extents for Full route scope and retain short-route precision.

- **Burial Planner on Qt6:** load the SVG report widget from its Qt6 module, including OSGeo4W builds without the QGIS wrapper, while retaining Qt5 compatibility.

### Added

- **Ground Model:** numeric depth profiles from mapped investigation tables, separate polygon/KP assignments, stable colour controls, missing-data flags and source inspection alongside soil classes.

- **Burial Planner — Risk Profile:** new *KP-range table* check registers each row of a start/end KP table, with or without geometry (e.g. a desktop-study hazard list), as a hazard over its KP range, with Low/Medium/High risk from its text or numeric attributes or a default.

- **Burial Planner — KP-range tables:** Exclusions criteria and Risk Profile checks record the RPL their KPs are quoted on and the KP unit (km or m). KPs on another RPL are translated to the plan's route by seabed position, and stretches the translation cannot trust are listed in the analysis and scan messages; existing criteria are read on the plan's RPL and ask you to confirm it.

- **Burial Planner:** Acquired Data, Assessment and Reporting tabs are in development and shown disabled; Planning and all its subtabs are unchanged.

- **Icons:** distinct toolbar and menu icons for the Cable Route Workbench, Planner, Burial Planner, Cable Lay Data Explorer and KP settings.
- **KP settings:** new *Subsea Cable Tools ▸ KP settings…* switches KP measurement plugin-wide between Geodesic (WGS84, the default) and Cartesian grid distances (a chosen CRS, else the project CRS when projected, else the route's UTM zone), so Cartesian KP now works for routes in any CRS.
- **Burial Planner — KPs tied to the RPL:**
  - Plans on an RPL that starts at a non-zero KP now use that start KP, and the Inputs tab warns when the RPL's printed KPs differ from the measured KPs (it recognises RPLs chained on a grid).
  - *Set route* can move a plan to another RPL or revision either keeping seabed positions (every stored KP is re-referenced in one undoable edit, with a preview of the largest change) or keeping KP numbers, and the planner says when a newer revision of the plan's RPL exists.
  - Imports ask which RPL their KPs refer to and translate KPs from another RPL by seabed position.
- **Burial Planner — import a plan:** *Plan Builder ▸ Import plan* brings in an existing plan from a KP-range table (CSV, TSV or Excel in any layout) or from any RPL in the Workbench, read from its PLDN/PLUP, PLB, MFE, burial and skip events in many phrasings or from its protection-method column. Every row can be reviewed and corrected first, and the import can replace the plan or overlay only a KP window, as one undoable edit.
- **Burial Planner — multi-tool plans:** one plan can mix ploughs (PLDN/PLUP), trenchers/ROVs and the new Mass Flow Excavator type (Start/End PLB) and the new Inspection type, each section labelled by its own tool, with a continuous change of tool shown as one tool-transition boundary (*Set tool for KP range…*).
- **Burial Planner — Ground Model and BAS tabs:** soil units along the route by depth below seabed, and the Burial Assessment Study's conclusions as an editable spreadsheet-style KP-range register; both import from CSV/XLSX, can be drawn on the map beside the route and appear in the HTML report.
- **Burial Planner — KP re-referencing:** ground models, BAS registers and other KP tables delivered against another RPL revision are mapped onto the plan's route by route geometry, matched KP pairs or a constant shift, keeping the delivered KPs and flagging assumed stretches.
- **Burial Planner — target burial depth by KP range:** a plan default plus KP-range overrides (typed, picked on the map or read from the RPL), followed by the Ground Model plot, Plan Builder, exports and report.
- **Burial Planner — inputs:** *Manage inputs…* registers many layers at once from a searchable, geometry-filtered picker, and the register shows whether each input still resolves and which criteria use it, with *Edit / relink…*.
- **Burial Planner — profile:** two-point measurements, a *True scale (1:1)* view, an *Overlays* menu (including full-height Risk Profile hazard bands and an Insufficient Information filter by criterion), a readout that names the exclusions under the cursor, and a *Reading the slope panel* guide.
- **Burial Planner — attribute conditions:** risk checks and seabed-soil exclusions pick the attribute from the layer's fields and accept values, clear numeric bins and QGIS expressions; rules saved by earlier versions load and evaluate unchanged.
- **Burial Planner:** criterion bars show the KP and ranges under the cursor (right-click lists them nearest first, no-data drawn grey), and BAS number columns can show a fixed number of decimal places without changing the stored value.
- **KP Mouse quick profile:** Space freezes and resumes the profile; repeatable two-point measurements with labelled, draggable triangles (length, X, Y, endpoint angle) and independent units; raw-cell and source views, seabed/slope shading, PNG/CSV export, a true-scale (1:1) default, route-KP axis labels and a cursor synchronised between plot and map.
- **Depth Profile:** the same on-plot measurements, vertical exaggeration, *Save PNG…* of every plot, a summary line, *Selected only*, and an *X-axis KP* option that labels a drawn line with the nearest KP on a chosen route.
- **KP axes:** profile KP axes tick at round intervals chosen from the zoom level and label to 3 decimal places; with *Route KP labels*, the KP Mouse profile places each tick where the nearest route KP takes that value.
- **Bathymetry source conventions:** per-layer depth/elevation, units, datum label and sampling settings, shared by Depth Profile, the KP Mouse Tool and the Burial Planner.
- **Save Layers to GeoPackage:** in the *Subsea Cable Tools* menu and the Layers panel right-click menu, saves the selected vector layers (including those in selected groups) into one GeoPackage and repoints the project layers at it, keeping names, styles, groups, joins and expression fields.
- **Import MDB:** a new *Save to GeoPackages in folder* option writes one GeoPackage per database instead of temporary layers, and text features gain `label_rotation` and `label_alignment` fields, with rich-text labels converted to plain text (original kept in `label_rtf`).
- **Cable Route Workbench — Compare revisions:** any two RPL revisions of a segment side by side, with statistics, mapped position and leg differences (an inserted alter course reads as one addition, not a renumbering) and CSV export.
- **Cable Route Workbench — schematic topology:** build the cable system on the system schematic (connect endpoints, add BUs, BMHs and joints, *Suggest connections…* from matching RPL end events), expand segments into their RPL sections, and see per-cable-type length totals.
- **Cable Route Workbench:** *File… ▸ Cable type colours…* sets a personal cable-type palette that follows you between projects, and RPLs can be exported to KML.
- **Cable Lay Data Explorer — Project tab:** create, open, duplicate, delete and compact the project's cable-lay GeoPackage, with an inventory of its standard layers.
- **Cable Lay Data Explorer — Manage tab:** per-source-file control of imported data: fix a wrong start date, mark rows active, standby or excluded, remove rows, fill time gaps from a second source without deleting anything, review the import/edit history, import files into any layer and enable the QGIS Temporal Controller.
- **Cable Lay Data Explorer — navigation:** two-way selection sync with the map, record stepping (Ctrl+Left/Right), pinned plot tooltips with extra fields, a status-bar readout, go-to from plots and QC findings, and an *Active rows only* view.
- **Recompute ISO Time (fix start date):** a new Cable Lay Data Import tool corrects a wrong project start date in place, without re-importing, and can remove the duplicates this reveals.
- **Cable lay GeoPackages:** every import and management edit is recorded in `import_log` / `edit_log` tables inside the GeoPackage.
- **KP Range Highlighter from CSV:** rows whose start and end KP are equal (or closer than a new threshold) are written to a second, point output instead of being dropped.

### Changed

- **KP definition (all tools):** KP is now always geodesic on WGS84 (no longer the project ellipsoid), positions are placed along each segment as drawn so KP → point → KP round-trips exactly (previously it could differ by tens of metres on long geographic legs), and multi-feature routes are joined in `SeqNo`/layer order without re-ordering.
- **KP Mouse Tool:** its *Cartesian* checkbox is now the plugin-wide KP setting (an existing tick carries over), which Depth Profile, KP Plotter, the Planner, the RPL comparison/crossing/area-listing/chartlet/outline tools and the default *Distance mode* of every KP processing tool follow.
- **Burial Planner — compatibility:** existing plans on RPLs that start at a non-zero KP are renumbered once when opened (logged and undoable); plans keep their own KP mode, so changing the global setting never renumbers a plan; existing trencher plans now read Start/End PLB instead of TRENCH_START/TRENCH_END, with stored data untouched.
- **Burial Planner — bathymetry:** set in a *Configure bathymetry…* dialog like other inputs, with every configured layer listed in the register; stored profiles now record their sources and resolution, so profiles sampled by earlier versions must be rebuilt.
- **Burial Planner — saved analysis:** the latest Exclusions recompute and Risk Profile scans are saved with the plan and marked *out of date* per criterion or check when their settings change, and a Recompute no longer changes how Plan Builder edits derive sections.
- **Burial Planner:** a simpler route picker; *New plan* no longer asks for a method; profile currency is judged on content, with the reason shown and a *Re-check* button; smaller stored profiles, faster refreshes and wider columns; the sections CSV gains `target_burial_m`.
- **Slope and profile sampling:** rasters are sampled by no-data-aware bilinear interpolation at native cells, contours keep exact crossings (conflicting depths become gaps), and slope baselines never span missing coverage or a change of source. Automatic raster slopes widen their baseline over repeated terraces and say so, and cross tilt is reported separately from a new maximum local cross slope (also available as a burial criterion).
- **Seabed length:** covered seabed length excludes unknown stretches and source seams, and covered plan length is reported separately.
- **MBES tools:** Merge MBES Rasters writes a `.sources.json` companion so engineering profiles can read the original native grids, and explains resolution and file lifetime in its log; Create Raster from XYZ warns about several soundings per cell and rejects non-finite values.
- **Cable Route Workbench:** map layers are grouped and named like the tree (*System · Segment · Revision · Lines/Points*), and *File… ▸ Organise map layers* re-files existing projects.
- **Cable Lay Data Explorer:** importing into an existing layer appends instead of rewriting the table; gap analysis and the Manage tab are much faster on large datasets; Manage edits run in the background with a cancellable progress dialog; the Manage tab comes first and *Table layer* is now *Active layer*.
- **KP Plotter:** the reverse option now reads *Table KPs are reverse KPs*.
- **Depth Profile:** right-click ▸ *Centre map on this KP* replaces panning the map on every right-click.
- **Depth Profile:** profiles and side slopes are computed in the background with a progress bar and *Cancel*, so QGIS stays responsive on long routes (results are unchanged); raster files are no longer locked after a profile, and skipped samples or contours are reported instead of silently left blank.
- **Calculate Seabed Length:** much faster on long routes; contours with Z values or in another CRS are used, and the gap between parts of a multi-part route no longer counts as seabed.
- **Processing tools:** long-running tools can be cancelled and report progress, tools warn when features or contours are skipped instead of silently leaving them out, and every tool's *Help* opens the documentation.
- **KP Mouse Tool:** much smoother on long routes; it now reads exactly the same KP as every other tool (previously it could differ by centimetres to metres inside long legs) and chains RPL legs in SeqNo order like the other tools.
- **KP Plotter:** hovering the plot no longer re-renders the whole map on every mouse move.
- **Raster sampling:** rasters are read in cached tiles with identical values, and raster files are no longer locked after a background analysis.
- **Bundled libraries** no longer override copies already installed with QGIS (for example openpyxl on QGIS 3.40), for this plugin or any other.

### Deprecated

- **Import Excel RPL (legacy):** hidden from the toolbox in favour of *Import RPL*; existing models that use it keep working unchanged.

### Removed

- **Depth Profile:** the *Interpolate Between Contours* option, which had no effect (exact crossings are always used).

### Fixed

- **Burial Planner:** large imports and batch edits no longer freeze QGIS, the input register lists every configured bathymetry layer rather than only the first, and an error with empty layer groups is fixed.
- **Burial Planner — KPs:** *Targets from RPL* and the RPL-depth fallback measure each position instead of using printed KPs, scope beyond the route is refused, moving the project folder no longer marks plans stale, and KP-range CSV imports work on plans laid against KP.
- **Burial Planner — re-referencing:** a constant shift no longer flags every value as extrapolated, shifts and matched pairs record their provenance, splitting a ground unit splits its delivered KPs, and a typed KP edit can no longer be undone by a later re-reference.
- **Identify RPL Crossing Points / Identify RPL Area Listing:** on looping routes, KP comes from the nearest route segment instead of the earliest one within 0.5 m.
- **Depth Profile:** no longer crashes on every raster profile (reported on QGIS 3.22) or when showing messages on QGIS 4; with Reverse KP the marker is at the right end of the route; the map marker uses the route's CRS and is removed on close.
- **Depth Profile — route and output:** a single route keeps its digitised direction, several features join end to end (with a warning for gaps), the CSV `Seabed_Length` adds up to the plotted length at bends, failure messages give the real reason, and contour profiles and the map marker are much faster.
- **Cable Route Workbench:** a style you saved is no longer overwritten when layers reload, the schematic toolbar no longer drifts while panning, crowded event labels are thinned, and the system schematic, tree and table agree on the latest revision.
- **Cable Lay Data Explorer:** Manage edits no longer silently skip rows hidden by a map filter (a layer in edit mode is refused), plot hover no longer crashes on QGIS 4, duplicate checks treat `1` and `1.0` as equal, and the append message no longer miscounts.
- **Import MDB:** UTF-16 text labels are no longer cut to their first letter, stray NUL characters are removed, geometry columns with unfamiliar names are recognised, and multi-part layers left out by default are reported with their feature count.
- **Burial Planner — plan files:** opening or checking an ordinary GeoPackage no longer changes or locks it, plan files on network shares no longer use a journal mode that is unsafe there, and creating or renaming a plan always records its change-log entry.
- **Burial Planner — deleting a plan** now also removes its ground-model and BAS rows.
- **Burial Planner — window:** closing and reopening the panel keeps it in step with the current project, and results from an analysis stopped by closing the panel are no longer applied.
- **Identify RPL Crossing Points / Identify RPL Area Listing:** no longer crash when an intersection mixes points and lines.
- **Add Depth to Point Layer / Dynamic Buffer Lay Corridor:** depths are now found from contour layers in latitude/longitude (previously every lookup silently returned no depth).
- **Compare Design vs As-Laid Routes:** the cross-track sign now follows the documented convention (+ starboard, − port); earlier versions reported it reversed, so **re-run comparisons made with an earlier version** if you rely on the sign (magnitudes are unchanged).
- **Cable Lay Simulator:** table resize grips work on QGIS 4; after editing inputs during a solve, *Solve* restarts on the latest inputs, and results from outdated inputs are always marked stale.
- **Catenary Calculator V2:** non-numeric assembly or seabed-profile cells are listed under *Warnings* instead of silently becoming zero, and the minimum bend radius check no longer passes by default where a radius cannot be computed.
- **Plugin reload:** unloading or reloading the plugin stops running solves and tasks, removes every map marker, rubber band, menu entry and toolbar button, and releases the plugin's map tools.
- **Tools that fail to open** now always say why, with details in the *Subsea Cable Tools* tab of the Log Messages panel.
- **Import Event Log:** each event now records the file it came from, so a second event log with overlapping times is no longer dropped as duplicates, and Cable Lay Data Explorer can manage event-log rows by source file.

## [1.9.0] - 2026-08-29

### Added

- **Burial Planner (beta):** a new guided tool (Experimental toolbar menu) that turns an RPL plus survey data into a burial plan: which sections are buried, where burial starts and ends, and why the rest is skipped. **Beta — sanity-check outputs against your own methods before operational use.** No criteria values, turn radii or layback values are shipped; all limits are user-entered with a source reference.
  - **Exclusions:** an ordered stack of criteria (water depth; longitudinal, cross or absolute slope, optionally banded by water depth or measured over the vehicle footprint; crossings and proximity; seabed-soil polygons with optional route corridors; KP-range tables; manual ranges; data coverage) resolved into Exclusion Areas, influence zones, screening flags, Insufficient Information and candidate sections, with boundaries refined to 0.1 m and extensions in metres or multiples of water depth.
  - **Bathymetry profile:** depths along the route and at a cross offset are sampled once and stored with the plan, shown on a synced profile with longitudinal/cross/absolute slope panels, a plan-outcome strip, sea level and map hover, and flagged stale (with *Resample*) when inputs change.
  - **Burial Tools and risks:** a project registry of ploughs and trenchers with operating configurations and optional DXF footprints (drawn live on the map), per-section tool and skip-handling assignment, and Risk Profile checks that record hazards, such as course changes above a set angle, at user-defined risk levels.
  - **Installation Paths:** a planning-grade burial-tool path through every RPL course change (tangent fillets or pass-through), with a water-depth-banded turning radius, plough layback and barge track, a vessel registry with turn check and outline, manual path adjustments, a KP-vs-DCC deviation plot and per-turn diagnostics; all burial analysis and KPs stay on the RPL.
  - **Plan Builder and review:** event and section tables with confirm, lock, split, merge, delete and *Merge → Plough Section / Plough Skip*; Insufficient Information resolved as skip or burial (singly or in bulk); reasons recorded in notes and an undoable change log; CSV import/export, rule-set JSON, rules copied from Workbench assessments, optional rKP and Lat/Lon columns, and a self-contained HTML report.
  - **Plans and layers:** plans live in a per-project GeoPackage (backed up before each schema upgrade); analysis runs in the background with cancellation; each plan's layers sit in their own subgroup and are repaired when a project opens; the tool opens as a floating window.
- **Cable Route Workbench — guided RPL import:** *Import RPL (Excel/CSV)…* takes a workbook or CSV straight to a registered revision, detecting worksheet, layout, coordinate encoding, units and column mapping (with reasons and reusable profiles), validating the result and previewing it on the map; registration is rollback-safe and keeps an import audit. *Import RPL to Workbench (auto-detect)* does the same in Processing.
- **Cable Route Workbench — more import routes:** *New RPL from route line or points (KML...)...* registers a plain line or ordered points (with optional event labels) as a revision, and *Import path file (.pthmdb)...* registers a path database, optionally extracting its assemblies.
- **Cable Route Workbench — systems and make-up:** a System → Cable segment tree with an ordered physical cable make-up per segment, *Manage assemblies…*, and Table | Schematic overviews with readable pan/zoom schematics.
- **Cable Route Workbench:** RPL points classify as assembly components, geographic references or both, using rules editable in *Edit event classification rules…* (replacing the body/geographic/installation buckets; unmatched events now default to geographic), and RPL layers get standard cable-type symbology (saved as the GeoPackage default) and are restored when a project opens.
- **Import Path File (.pthmdb):** a new MDB Tools algorithm loads a path database's route, positions, segments, assembly points, corridor, side slopes and bathymetry profile, with CRS and KP units detected from the file and no Access/ODBC install needed.
- **Place Outline Along Route (KP):** a new Other Tools algorithm places a ship or tool outline at a list or series of KPs along a route, rotated to the local heading.
- **KP Mouse Tool:** several raster and contour depth layers can be used at once, and **D** toggles a live depth and slope profile along the range line.
- **BU Lowering Tool (3D) (beta):** a focused dialog for lowering a branching unit over two pre-laid legs, with a quick analytic model, a full-solver *Verify* and its own saved settings.
- **Cable Lay Simulator (3D):** a BU integration editor describes the whole Y (trunk and both legs) measured from the branching unit, laid ends can be picked on the map, and cable is drawn running over the sheave/chute arc.
- **Planner — fuel, cable and reports:** per-vessel fuel profiles with bunkers, remaining on board and cost; cable loaded/laid/recovered and cable onboard; and a Reports window (fuel, breakdowns, progress S-curve, plan vs actual, milestones and key dates, cable onboard) with saved custom reports and CSV/Excel/PNG/SVG export.
- **Planner — scheduling:** optional FS/SS/FF/SF links with lag, date constraints, milestones, critical path, backward scheduling from a required finish, baselines and actuals with an auditable progress history, and per-task speed profiles that playback follows.
- **Planner — tasks:** import from MS Project or CSV, duplicate tasks and groups, a standard-tasks library and user-defined operation types (shareable as CSV/JSON), default operations and ProtectionMethod rules for RPL imports, and *Add from RPL…* with an *Import RPL file* shortcut.
- **Planner — table and playback:** durations in days or hours, per-task distance units, manual distances for non-route tasks, one location shared by several tasks, zoom to task, a totals row, highlighted rows during playback, and task-paced or custom playback speeds.
- **Import MDB:** GeoMedia text (annotation) classes import as point layers with a `label_text` field; several files import in one run into per-file layer groups; and geometry is recovered from secondary geometry columns or explicit coordinate columns (tagged in `geometry_source`) when the primary geometry is missing.
- **Create Raster from XYZ:** true multi-file import, a per-cell *Bin Average* method and optional gap filling.
- **Depth Profile:** an optional slope evaluation window (e.g. a plough bearing length), also in KP Range Depth + Slope Summary.

### Changed

- **Slope sign convention (all tools):** along-route slope is positive when shoaling with increasing KP and side slope positive when deeper to starboard, with depth-versus-elevation data detected automatically. Depth Profile's *Invert Slope Sign* now means positive = deepening and starts unchecked, and KP Range Depth + Slope Summary's directional fields are renamed to positive magnitudes (`slope_down_max_deg`, `slope_up_max_deg`, `side_stbd_max_deg`, `side_port_max_deg`); Workbench assessment slope limits are unaffected.
- **Slope calculations:** all tools share one slope calculation over a distance-based window that never spans missing data or a change of raster and no longer reports a fabricated 0° first station; contour profiles report no data beyond the first and last crossings.
- **Import MDB:** works out of the box on any platform with a bundled Access reader (no Access driver or pyodbc), keeping ODBC as an automatic fallback; large batches use disk-backed temporary layers; the CRS parameter is now *Source CRS / CRS of coordinates in MDB* and is assigned, not transformed.
- **Merge MBES Rasters:** always keeps the finest input resolution (previously the first input's), respects each file's NoData, refuses mixed CRSs, offers nearest or bilinear resampling and warns that mosaics are for display and profiling.
- **Create Raster from XYZ:** coordinates are treated as cell centres (removing a half-cell shift), spacing is detected per axis, headers and semicolon/tab delimiters are accepted, and implausible grids or CRSs are refused with a clear message.
- **Cable Route Workbench:** *Register RPL revision* is now *Add RPL from layers* (Processing: *Add RPL Layers to Workbench*; algorithm id `register_rpl` unchanged); revisions sort numerically and you choose the revision rather than the tool guessing the latest; the dock floats as a real window; existing workbench GeoPackages upgrade in place.
- **Planner:** resources are project-level and shared by all scenarios; task-table columns can be resized, reordered and hidden; existing planner GeoPackages are upgraded in place after a backup.
- **Experimental toolbar menu:** Cable Route Workbench, Planner, Burial Planner, Cable Lay Data Explorer, Cable Lay Simulator and BU Lowering Tool share one *Experimental* toolbar dropdown; their *Subsea Cable Tools* menu entries remain.
- **Long routes:** KP lookups are indexed, so full-resolution profiles and analysis stay fast on routes of 1,000 km and more.

### Removed

- **Cable Route Workbench:** the separate Assembly Library tree root and the bar-style Straight Line Diagram, replaced by *Manage assemblies…* and the node-and-line schematics.

### Fixed

- **Cable Route Workbench:** closing or switching projects with the dock open could delete registered RPLs, with their fits and assessments, from the workbench GeoPackage; duplicate layers are no longer loaded because of path differences; the RPL Manager survives its layers being deleted; *Add RPL from layers* refuses to re-register existing workbench layers.
- **RPL import:** split degrees/minutes/hemisphere coordinates are detected reliably despite stray footer values or hemisphere-first columns.
- **Processing tools:** KP tools and 19 other algorithms no longer abort on a zero-length or null geometry, or silently drop it and shift every later KP.
- **Import MDB:** a source `Depth` column no longer discards every feature of a table; point, polygon-with-holes and multi-part GeoMedia geometries are decoded; every table reports what happened; and a hang on large table listings is fixed.
- **Burial Planner — safety:** a no-data gap can no longer be excluded away by a threshold breach interpolated across it, absolute-slope rules treat stations without cross samples as Insufficient Information, and Auto slope no longer averages short steep faces over the coarse rule step.
- **Burial Planner — reliability:** freezes on long contour routes, with many sections or on zero-length sections are fixed, as is a crash on right-clicking a table header; a background result can no longer land on another plan; save failures are reported; a duplicated plan is marked stale when its inputs change.
- **Depth Profile:** with several rasters selected, each is drawn as its own line instead of one blended line (*Plot each raster*, [#6](https://github.com/k-mcmonagle/subsea-cable-tools/issues/6)); the CSV's latitude/longitude columns no longer contain projected coordinates, and re-running *Generate* no longer reuses the previous run's coordinates.
- **Import Ship Outline (DXF):** no longer crashes when an output CRS other than EPSG:3857 is chosen.
- **KP Mouse Tool:** no longer floods the log on every mouse move after an edit session, and the live slope reads the true gradient on coarse grids.
- **Cable Lay Simulator / BU Lowering Tool quick model:** bed tension no longer decays to 0 kN, tension and geometry stay continuous through BU touchdown, non-converged frames are flagged, and the trunk no longer kinks or detaches from the BU.
- **Catenary Calculator V2:** touchdown on undulating seabed profiles is verified and falls back to a robust solve.
- **Planner:** the GeoPackage is no longer created in `C:\WINDOWS\system32` (a writable profile folder is used, and another location can be chosen); dragged tasks no longer disappear and the list auto-scrolls during a drag; a newly linked route is no longer pinned at its end; the Progress S-curve no longer crashes; the transport bar no longer jitters; GeoPackage errors no longer break the table.
- **QGIS 4 compatibility:** remaining Qt6 issues in the Cable Lay Simulator, Explorer, Workbench and KP Mouse Tool are fixed, and a layer-filter deprecation warning on QGIS 3.34+ is gone.

## [1.8.0] - 2026-07-15

Workbench and Planner schemas are versioned; the plugin backs up a GeoPackage before upgrading it.

### Added

- **Cable Route Workbench (new):** RPLs, cable assemblies and cable systems become project entities stored in a per-project workbench GeoPackage.
  - **RPL Manager:** browse registered RPLs with live position and segment tables, configure depth sources, and drag positions on the map with distances, bearings and slack recomputed live and one-step undo; slack is preserved as a percentage (planning) or as cable length (as-laid).
  - **Assembly Manager:** a library of cable and rigging assemblies, imported from catenary JSON or extracted from an RPL after reviewing its event classification, shown on a Straight Line Diagram and exportable to the catenary tools.
  - **Assembly fit:** anchor an assembly at a KP and walk it along the route's slack to get body landing positions and section spans as map layers.
  - **System topology:** connect RPLs through BU, joint and BMH nodes to form cable systems.
  - **Route Suitability / Burial Assessment:** an ordered rule stack (depth and slope thresholds, proximity, polygon attributes, KP-range tables, manual ranges) that classifies the route per installation method as allowed, risk 1–3 or excluded, with per-rule coverage bars, a styled result layer and CSV export; results go stale when the RPL is edited.
- **Register RPL into Workbench:** a new RPL Tools algorithm adds an imported RPL point/line pair to the workbench.
- **Planner (beta, new):** a dockable spatial planner for scenarios of ordered, map-linked tasks on concurrent resource lanes, stored in a per-project planner GeoPackage.
  - Tasks link to project features or to points and routes sketched on the map (with snapping, WGS84 or KP entry); distance and speed give a live duration, and predecessor/lag links and resource offsets drive the schedule. Links to external features rely on stable feature IDs and show an amber repair state if they break.
  - MS Project-style indented groups, multi-select move and delete, drag reordering and full undo/redo.
  - Multi-resource SIMOPS with cross-resource links, RPL import (whole or partial, grouped by cable type, protection method or vessel, with lay speeds), merging of adjacent route tasks, animated playback with labelled resource markers, and copy to the MS Project Entry table.

### Fixed

- **Planner:** playback markers and progress bands follow the drawn route instead of a great-circle arc between sparse vertices, while distances stay ellipsoidal.

## [1.7.0] - 2026-07-06

### Added

- **Cable Lay Simulator (3D) (beta):** the next-generation catenary tool, with an interactive software-rendered 3D view (works over RDP), synchronised profile and plan views, and CSV, 3D DXF and map-layer exports. Results are planning-grade estimates: validated against closed forms, the V2 solver and physical invariants, not against commercial software or field data; read the V3 model notes (linked from the README) before operational use.
  - **Static hang:** V2 physics in 3D with multi-segment assemblies, point bodies, seabed friction and contact on real bathymetry (including a grid sampled from a QGIS raster) and current drag, for a single span, a held branching unit or a held final bight.
  - **Steady lay:** the stationary lay configuration with drag and depth-varying current, shown beside closed-form quick answers.
  - **Operation simulation:** time-stepped branching-unit deployment, final-bight lay-down and straight lay, with a timeline scrubber.
- **Cable Lay Data Explorer:** a standalone window over one or more cable-lay layers, with a data table, configurable multi-series plots (dual axes, slope series, statistics, event and QC overlays, synced crosshair, UTC time axis, pop-out panels) and tabs for QC (time gaps, distance gaps, decimal precision, duplicates), Inspection (thresholds, off-line distance) and Processing (as-laid listings and an as-laid RPL by best fit).
- **Run Cable Lay QC:** a new *Cable Lay QC & Analysis* algorithm runs the same checks and writes a `qc_findings` layer.

### Changed

- **Create Cable Lay GeoPackage:** also creates `qc_findings` and `qc_config`, and adds the layers to the project in a group named after the GeoPackage, ordered like the import tools.

### Removed

- **Catenary Calculator (Legacy V1):** removed (marked Legacy in 1.5.1); use Catenary Calculator V2 or the Cable Lay Simulator.

### Fixed

- **Cable Lay Data Explorer:** no longer hangs on large datasets; layers load and QC runs in the background with a cancellable progress dialog; sorted tables keep selections mapped to the right records.

## [1.6.3] - 2026-07-01

### Fixed

- **Catenary Calculator V2:** on QGIS 3 the input panel can be widened again instead of clipping labels and buttons, and the Results panel can be resized against the plot (layout only, no calculation changes).

## [1.6.2] - 2026-07-01

### Security

- **Import MDB:** security-scan suppressions on the Access SQL are now placed where the QGIS plugin repository's Bandit scan recognises them; the queries were already safe (identifiers are validated and quoted, values are parameterised). The vendored plotting library's in-process SVG parsing is annotated the same way. No functional change.

## [1.6.1] - 2026-06-23

### Fixed

- **Import MDB:** Windows paths are normalised before connecting, fixing *Not a valid file name* errors with some Access ODBC drivers, and multi-vertex features are imported as lines instead of being forced to points by unreliable metadata.

## [1.6.0] - 2026-06-22

### Added

- **Cable Lay Data Import (new Processing group):** *Create Cable Lay GeoPackage* sets up the empty standard layers, and seven importers load Cable Lay Data (CSV), Event Log, Slack Log, Body Log, 3D Model Solutions, As-Laid and Plough Data into them. Each importer takes several files at once, appends to its layer in the chosen GeoPackage and removes duplicates, so re-importing a file is safe. No new dependencies.

## [1.5.1] - 2026-06-19

The one intentional behaviour change is Catenary Calculator V2's *Bottom Tension* (see Changed); KP and measurement workflows are otherwise unchanged when defaults are kept.

### Added

- **Catenary Calculator V2 — seabed:** Flat, Sloped and Profile seabed modes (profiles typed, pasted or loaded from CSV); in Profile mode the cable is automatically draped over the seabed, with free spans and on-bed sections reported.
- **Catenary Calculator V2 — assemblies:** per-segment seabed friction, bending stiffness with a minimum-bend-radius check, and buoyant segments (negative weight), with surface-piercing detection and an estimate of excess buoyancy.
- **Catenary Calculator V2 — diagnostics:** convergence and seabed-penetration reporting with a red warning banner, a warning when segments fall back to the default weight, and a hover readout of clearance, contact state and bend radius (replacing the *Query at s* input).
- **Distance mode:** KP-emitting algorithms offer Ellipsoidal (default) or Cartesian distances.
- **QGIS 4:** the plugin declares compatibility with QGIS 4 / Qt6 (`qgisMaximumVersion=4.99`).
- **Documentation:** catenary model notes stating what the V1 and V2 models do and do not represent, and a README section on distance and CRS methodology.

### Changed

- **Catenary Calculator V2:** *Bottom Tension* is now the actual cable tension at touchdown rather than its horizontal component (identical on a flat seabed); input sections are collapsible and reordered, and several labels are renamed (e.g. *Counter Reference*).
- **Distance measurement:** every tool measures through one shared helper that falls back to WGS84 when the project ellipsoid is unset.
- **CRS handling:** KP Plotter, Nearest KP and Place KP Points (depth raster) reproject mismatched inputs with a warning instead of refusing, and Translate KP Between RPLs explains its same-CRS requirement.
- **Plotting:** the catenary calculators, KP Plotter and Depth Profile use the bundled pyqtgraph instead of matplotlib, and bundled libraries are only used when QGIS lacks them.
- **Naming:** *Import Bathy MDB* is renamed *Import MDB* (algorithm id `import_mdb`), and reference-line inputs across the KP and RPL tools are labelled *Reference Line Layer* (parameter ids unchanged, so saved models keep working).

### Deprecated

- **Catenary Calculator (V1):** labelled *(Legacy)* and scheduled for removal; use Catenary Calculator V2.

### Fixed

- **Distance measurement:** an unset project ellipsoid (*None / Planimetric*) no longer silently makes "ellipsoidal" distances planar, which reported degrees as kilometres on geographic CRSs; RPL Route Comparison and other tools get the same fallback.
- **KP placement:** points placed at a KP on geographic CRSs follow the great circle, and the KP Mouse Tool range ring is a true geodesic circle.
- **Catenary Calculator V2:** touchdown now converges on undulating profiles, a stale *Water Depth* no longer distorts Profile mode, failed solves no longer corrupt the Bottom Tension input, an incomplete profile is reported instead of silently using a flat seabed, and a spurious refinement warning on slopes is gone.
- **Catenary Calculator (V1):** *Catenary Length*, *Layback* and *Top Tension* modes no longer fail for long cables, long laybacks or unachievable inputs.
- **Calculate Seabed Length:** interval mode no longer crashes, interval segments follow the route instead of straight chords, and zero coverage falls back to plan length as the warning states.
- **Import Excel RPL:** invalid DMS coordinates and mistyped column letters are reported instead of being imported or skipped silently.
- **KP Mouse Tool:** fixed QGIS 4 startup, dialog and tooltip issues, and placed points and range rings appear immediately.
- **Place KP Points from CSV:** fixed a crash introduced during the distance-helper change.
- **Other fixes:** the Dynamic Buffer (Lay Corridor) tool joins the Other Tools group, KP Plotter only offers table layers as data tables, and dock-widget cleanup is safer.

## [1.5.0] - 2026-04-26

First published release since 1.3.0, consolidating internal 1.4.x development.

### Added

- **Extract KP Ranges (Rule Based):** KP-range listings (and optional segments) from an RPL categorised by an attribute field.
- **Identify Features Intersecting RPL** (formerly *Identify RPL Lay Corridor Proximity Listing*): intersects point, line and polygon layers with an RPL, optionally trimmed to a lay corridor.
- **Identify Hazards in Lay Corridor:** proximity listings of layers against a lay corridor and RPL, with KP/DCC and lat/lon.
- **Identify RPL Crossing Points:** crossings between an RPL and asset lines, with KP, lat/lon, crossing angle and optional buffers.
- **Identify RPL Area Listing:** the RPL split at polygon edges, carrying the polygon attributes and start/end KP.
- **RPL Route Comparison** (now *Compare Design vs As-Laid Routes*): radial, along-track and cross-track offsets between design and as-laid routes.
- **Translate KP Between RPLs (Points):** translates KP values from one RPL to another.
- **Extract A/C Points from RPL:** alter-course points with KP and turn angle.
- **Merge KP Range Tables** and **Group Adjacent KP Ranges by Field:** combine KP-range tables with mismatched intervals, and merge consecutive ranges sharing a value.
- **KP Range Depth + Slope Summary:** depth and slope statistics for KP-range features from rasters or contours.
- **Export Chartlets Based on KP Range List** (now *Export KP section chartlets*): a map PNG per KP section.
- **Plot Line Segments from Table** and **Extract Lines Intersecting Polygons:** new Other Tools.
- **Catenary Calculator V2 (experimental):** multi-segment cable assemblies and bodies.

### Changed

- **KP Mouse Tool:** geodesic KP by default with an optional Cartesian mode (remembered), route length for both, right-click depth sampling, a configurable copy format and *Go to KP…*.
- **Depth Profile:** *Invert KP Axis* and *Invert Slope Axis* options (*Invert Slope* renamed *Invert Slope Sign*), optional second contour layer, multiple rasters (finest wins), a Refresh control and adaptive raster sampling.
- **Transit Measure Tool:** a Quick Buffer option.
- **Import MDB:** runs in a separate process so a driver fault cannot crash QGIS, handles mixed-geometry GeoMedia tables, loads polygons and points by default and outputs temporary layers.
- **General:** clearer Processing Toolbox groups, a minimum of QGIS 3.22, and optional plotting dependencies loaded only when needed.

### Removed

- Several in-development tools from internal 1.4.x builds are withdrawn pending further work.

### Fixed

- **Processing provider:** one failing tool no longer hides the whole toolbox.
- **Create Raster from XYZ:** GDAL availability checks and an IDW fix.
- **Import MDB:** a missing `pyodbc` gives a clear message instead of breaking the provider.
- **Plotting:** pyqtgraph is bundled, so it no longer has to be installed separately.
- **Transit Measure / Nearest KP:** cleanup fixes, and correct results on multi-segment RPLs.

## [1.3.0] - 2025-09-06

### Added

- **Import Cable Lay:** imports cable lay CSV files (day-count time, DMS coordinates) as a point layer.
- **Import Ship Outline** and **Place Ship Outlines at Points:** import a ship outline from DXF (scale, rotation, CRP offset) and place it at points, rotated to a heading field.
- **Catenary Calculator:** a basic subsea cable catenary calculator.
- **Depth Profile:** a dockable depth or slope profile from an MBES raster or contours along a route or a drawn line.
- **Transit Measure Tool:** cumulative geodesic distance along a drawn path with transit time, savable as a layer.
- New icons for the KP Plot and KP Mouse tools.

### Fixed

- **Installation:** bundled libraries such as openpyxl are always found, so no manual package installation is needed.
- **Import Excel RPL:** unreadable files give a clear error instead of crashing QGIS, the end of the RPL table is detected, and output layers are named after the source file.
- **Stability:** better cleanup of tools and plots, and a crash on QGIS exit after using the KP Mouse Tool or KP Plotter is fixed.

## [1.2.0] - 2025-07-13

### Added

- **KP Data Plotter:** a dockable plot of KP-based table data against a reference line, with crosshair, map marker and multiple fields.
- **Merge MBES Rasters** and **Create Raster from XYZ:** mosaic MBES rasters, and grid XYZ files by direct rasterization or IDW with automatic grid size.
- **KP Range Highlighter:** a `length_km` output field.
- **Import Excel RPL:** an optional *Chart No* field.

### Changed

- **Import Excel RPL:** column mappings are remembered between sessions.
- **KP Range Highlighter:** outputs only `start_kp`, `end_kp` and, when given, `custom_label`.
- **Place KP Points Along Route:** can sample depth from a raster (KPZ output), with a CRS check and out-of-extent warnings.

### Fixed

- **Import Excel RPL:** a `KeyError` when *Chart No* was not provided.
- **KP Range Highlighter:** type-conversion errors from copied source attributes.

## [1.1.0] - 2025-07-05

### Added

- **KP Mouse Tool:** a configuration dialog (any line layer as reference, distance units, Reverse KP, route metrics) with settings remembered between sessions.
- **Place KP Points Along Route**, **Place KP Points from CSV** and **Place Single KP Point:** new KP point tools.
- Step-by-step help panels for all KP tools.

### Changed

- **KP Mouse Tool:** a persistent tooltip (KP, rKP, DCC) shown only when QGIS is active, a toolbar button with a configuration menu, and continuous KP across multi-feature and multi-part routes.
- **KP Range Highlighter from CSV:** takes a table layer with KP fields picked from drop-downs, and keeps all source columns plus `source_table` / `source_line`.
- **KP Range tools** and **Nearest KP:** handle multi-segment lines, with nearest distances found segment by segment.

### Fixed

- **KP Mouse Tool:** compatibility with different QGIS versions.
- **KP Range Highlighter from CSV:** a crash and a line-length calculation error.
- **Nearest KP:** true shortest distances and KPs on multipart lines.

## [1.0.0] - 2025-04-05

### Added

- Initial release: Nearest KP, KP Range CSV, KP Range Highlighter, Import Bathy MDB and Import Excel RPL algorithms, and the KP Mouse map tool.

[Unreleased]: https://github.com/k-mcmonagle/subsea-cable-tools/compare/v1.9.0...HEAD
[1.9.0]: https://github.com/k-mcmonagle/subsea-cable-tools/compare/v1.8.0...v1.9.0
[1.8.0]: https://github.com/k-mcmonagle/subsea-cable-tools/compare/v1.7.0...v1.8.0
[1.7.0]: https://github.com/k-mcmonagle/subsea-cable-tools/compare/v1.6.3...v1.7.0
[1.6.3]: https://github.com/k-mcmonagle/subsea-cable-tools/compare/v1.6.2...v1.6.3
[1.6.2]: https://github.com/k-mcmonagle/subsea-cable-tools/compare/v1.6.1...v1.6.2
[1.6.1]: https://github.com/k-mcmonagle/subsea-cable-tools/compare/v1.6.0...v1.6.1
[1.6.0]: https://github.com/k-mcmonagle/subsea-cable-tools/compare/v1.5.1...v1.6.0
[1.5.1]: https://github.com/k-mcmonagle/subsea-cable-tools/compare/v1.5.0...v1.5.1
[1.5.0]: https://github.com/k-mcmonagle/subsea-cable-tools/compare/v1.3.0...v1.5.0
[1.3.0]: https://github.com/k-mcmonagle/subsea-cable-tools/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/k-mcmonagle/subsea-cable-tools/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/k-mcmonagle/subsea-cable-tools/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/k-mcmonagle/subsea-cable-tools/releases/tag/v1.0.0

# Changelog

All notable changes to the Subsea Cable Tools QGIS plugin will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.6.3] - 2026-07-01

UI fix for the **Subsea Cable Catenary Calculator V2** dialog layout — **no calculation changes**.

### Fixed
- **Catenary Calculator V2 (QGIS 3 / Qt5) window layout:** removed the hard maximum width on the left input panel so the main splitter can be dragged fully to the right. On QGIS 3 the panel was capped at 540 px, which clipped the assembly button row and the longer field labels with no way to reveal them (QGIS 4 / Qt6 was unaffected). The inner panel now keeps a minimum width and shows a horizontal scrollbar instead of silently truncating content, and the dialog opens slightly larger with a wider default left share.
- **Resizable Results panel:** the Results text box and the plot now sit in a vertical splitter, so the Results panel can be enlarged or shrunk (it was previously pinned near its minimum height). Both panes keep a minimum height so neither collapses.

## [1.6.2] - 2026-07-01

Security-scan compliance patch only — **no functional change** from 1.6.1.

### Security
- Corrected the placement of Bandit `# nosec` suppressions on the dynamically-built Microsoft Access SQL in `processing/import_mdb_algorithm.py` and `processing/mdb_odbc_worker.py` so the QGIS plugin repository's automated Bandit scan recognises them. The queries were already safe: table/column identifiers are validated and bracket-quoted by `_quote_access_identifier()` (which rejects brackets and control characters), and row values are bound with `?` parameter placeholders. Access ODBC cannot parameterise identifiers, so they must be interpolated. The suppressions previously sat on the closing line of each multi-line statement, but Bandit attributes the finding to the first line, so they are now placed there.
- Annotated the internal XML parsing in the vendored `lib/pyqtgraph/exporters/SVGExporter.py` (`# nosec B318` / `B408`): it parses only a literal constant and SVG bytes generated in-process by `QSvgGenerator`, never untrusted external input.

## [1.6.1] - 2026-06-23

Non-breaking maintenance patch for **Import MDB** focused on Access ODBC connection reliability and geometry classification for bathy-style tables.

### Fixed
- **Import MDB (ODBC path handling):** normalises Windows MDB paths before connect and uses a driver-compatible `DBQ` connection string format, fixing `HY000 / -1044 Not a valid file name` failures seen on some Microsoft Access ODBC driver builds.
- **Import MDB (geometry split inference):** in worker split mode, geometry classification now prioritises actual vertex shape so multi-vertex features are emitted as `LineString` (unless explicit polygon metadata applies) instead of being forced to `Point` by unreliable source metadata. This restores expected line outputs for bathy-style GeoMedia tables while preserving true point and polygon imports.

### Added
- **Regression tests:** added focused tests for MDB worker geometry inference across point/line/polygon/ambiguous metadata cases.

## [1.6.0] - 2026-06-22

Adds a lightweight **Cable Lay Data Import** toolset for turning cable lay source files into GeoPackage layers, with no new third-party dependencies.

### Added
- **New "Cable Lay Data Import" Processing group** for building a single GeoPackage of cable lay data, with no new third-party dependencies. It contains a setup tool and seven importers:
  - **Create Cable Lay GeoPackage** — writes a GeoPackage pre-populated with all the empty canonical layers (correct geometry type and CRS): `cable_lay`, `event_logs`, `slack_logs` (lines), `body_logs`, `model_solutions`, `as_laid`, `plough_data` (all WGS84 / EPSG:4326). Layer names are prefixed with the GeoPackage file name (e.g. `ProjectX_cable_lay`) so they group and identify cleanly in the layer tree. Safe to re-run: existing layers are left untouched and only missing ones are added.
  - **Import Cable Lay Data (CSV)**, **Import Event Log**, **Import Slack Log** (LineString segments), **Import Body Log**, **Import 3D Model Solutions** (touchdown or ship position selectable), **Import As-Laid** and **Import Plough Data**. The existing cable-lay CSV importer was refactored onto the same base so all seven behave identically (it keeps its downsample and parse-time-toggle options).
- **GeoPackage-centric, incremental workflow.** Every importer accepts **multiple files at once** (multi-select) and writes to its **fixed canonical layer**. The destination is either an **existing layer chosen from a dropdown** — typically one created by *Create Cable Lay GeoPackage* — or a **GeoPackage file** to create/append to (created if missing). When an already-loaded layer is targeted it is refreshed in place rather than duplicated. Layer names are prefixed with the GeoPackage file name (e.g. `ProjectX_slack_logs`) for clean grouping/identification in the layer tree. Re-running grows that same layer and removes duplicates on a per-type key (e.g. slack logs on file + KP1 + KP2; positions on timestamp + source file), so data that arrives daily or weekly builds up cleanly and re-importing a file is safe. Using canonical (prefixed) layer names avoids file-named scratch layers; a combined layer's attribute table can be exported to CSV with QGIS's built-in *Export → Save Features As*.
- **Shared, dependency-free helpers** (`processing/cable_lay_parsers.py`, `processing/cable_lay_import_base.py`): degrees/decimal-minutes → decimal degrees (tolerant of `17 09.7399N` and `17 09.7399 N`), day-count time (`day,HH:MM:SS`) + project start date → ISO timestamp, numeric/text column-type inference, the merge/deduplicate machinery, and GeoPackage read/write that preserves other layers. Implemented with the Python standard library and PyQGIS only — no pandas/geopandas/shapely.
- **Test coverage:** new `tests/test_cable_lay_importers.py` exercises the coordinate/time helpers, type inference and deduplication, and runs the setup tool plus the cable-lay, slack, body and plough importers end-to-end into a GeoPackage (including a multi-file merge and a re-run that de-duplicates). Registered in `tests/run_qgis_smoke_tests.py`.

## [1.5.1] - 2026-06-19

Maintenance and consistency release focused on distance/KP measurement and CRS handling, plus substantial enhancements to Catenary Calculator V2 (sloped / profiled seabed support, convergence and seabed-penetration reporting, and distributed buoyancy). Existing KP/measurement workflows are unchanged when defaults are kept; the one intentional behaviour change is that Catenary V2's *Bottom Tension* input is now the actual tension at the touchdown (`T_TDP`) rather than the horizontal force component (`H`) — identical on a flat seabed.

### Added
- **Catenary Calculator V2 — automatic multi-span seabed drape (standard model on profiled seabeds).** A second, independent static solver (`catenary/drape_solver.py`: lumped-node dynamic relaxation with kinetic damping, tension-only axial springs driven to the inextensible limit by rest-length correction, penalty seabed contact, Coulomb stick-slip friction, optional discrete bending stiffness) resolves the **full drape of the cable over the seabed profile**, including resting on high spots the single-span model can only flag as penetration, free spans bridging depressions, an anchored or free bottom end, and seabed friction. In **Profile seabed mode the drape runs automatically as part of every solve** and the drape-resolved geometry is what is plotted, hovered and exported (clamped onto the bed so the displayed cable never penetrates the seabed); the raw single-span curve through a high spot is no longer shown. The drape's boundary-condition inputs live in **Geometry** (Seabed end condition, On-bed length beyond TDP) and are only shown in Profile mode. Each free span and the hang from the ship (extent, length, max clearance, min bend radius, max tension) plus top/end tensions and contact status are reported in a table in the **Results** pane. Flat/Sloped modes keep the exact tangential single-span solution (a drape adds nothing there) and report the equivalent single-hang row. Validated against closed-form catenary results (top tension within 1 %, touchdown within 2.5 %, frictionless flat bed transfers H to the anchor within 2 %) in `tests/test_drape_solver.py`, and end-to-end through the dialog in `tests/test_catenary_v2_dialog.py`. Friction equilibria are lay-history dependent and reported as indicative. See `catenary/MODEL_NOTES.md`.
- **Catenary Calculator V2 — per-segment seabed friction in the Assembly table.** New "Friction μ" column on segment rows (blank = default 0.3; typical 0.2–0.4 sand/clay, 0.4–0.6 rock) mapped into the drape solver as a per-node friction array, so e.g. an armoured shore-end and a lightweight main lay can carry different friction coefficients in one model. Round-trips through the assembly JSON (`friction_mu`).
- **Catenary Calculator V2 — per-segment bending stiffness (EI) and minimum-bend-radius (MBR) check.** New Solve Mode defaults plus Assembly segment columns for *EI* (kN·m²) and *MBR* (m); blank segment values inherit the Solve Mode defaults, and MBR `0` disables the check for that segment. EI is modelled in the drape solver as energy-consistent discrete three-node moments (`M = EI·κ`, lumped-mass curvature `κ = 2θ/(L1+L2)`), now using a compliance-weighted joint EI where adjacent assembly segments differ. The MBR check maps every modelled bend back to the local assembly segment, checks chute contact segment-by-segment, and uses adjacent-segment EI/MBR for point-load body kink estimates (`R ≈ √(EI·T)/P`), raising a red integrity banner when a local limit is violated.
- **Catenary Calculator V2 — interactive query on the plotted line.** The crosshair hover readout now reports seabed clearance, contact state (on seabed / suspended, when draped) and local bend radius at the nearest cable point, in addition to tension, KP, counter and segment — replacing the old "Query at s from chute" spin-box (the hover covers the on-bed section of the drape too).
- **Catenary Calculator V2 — buoyant assembly segments.** A negative segment weight is now honoured as distributed buoyancy (bows the cable upward), matching negative point loads and legacy distributed components. Previously any non-positive assembly-segment weight was silently replaced by the fallback cable weight, so buoyant segments had no effect. Blank/zero still means "use fallback weight" (back-compatible, warned); a new advisory flags net-buoyant segments and non-physical negative *in-air* weights.
- **Catenary Calculator V2 — surface-piercing detection and redundant-buoyancy estimate.** When a buoyant bight rises above sea level anywhere other than the final run up to the chute, the solver flags it, reports the affected arc length, the max height and an estimated **redundant (excess) buoyancy in kN**, and the plot clamps the displayed cable (and any bodies) to the surface over the region with a distinct orange marker — physically that section would float at the surface, not arc through the air. Tensions/geometry in flagged regions are explicitly marked non-physical.
- **Catenary Calculator V2 — convergence reporting and seabed-penetration detection.** The solver now judges convergence directly from the boundary-height and input residuals (tolerances scale with the integration step) and records a `converged` flag plus per-residual OK flags, instead of silently plotting a best-effort (possibly disconnected) curve. The dialog surfaces non-convergence with a red banner in the results pane and a colour-coded row in the solver-diagnostics view. Free-span **seabed clearance** is now computed for every solve; on a profiled seabed where the self-consistent single-span cable would pass through a high spot between the touchdown and the chute, the penetration is detected and clearly flagged (banner + red markers on the plot showing where the cable lies below the bed) while the geometry is kept self-consistent (the touchdown stays registered with the chute boundary condition rather than being relocated, which a single-span static model cannot do physically). Minimum clearance and penetration depth are reported in diagnostics. The seabed plot now renders the full defined profile, including bathymetry beyond the cable extent.
- **Catenary Calculator V2 — distributed buoyancy.** Effective cable weight is no longer clamped to a small positive floor, so distributed-buoyancy components (net-negative weight) now correctly bow the cable upward.
- **Catenary Calculator V2 — sloped / profiled seabed support.** The solver previously assumed a flat horizontal seabed at the chute-depth elevation; it now accepts a seabed *profile* and applies the correct touchdown-point (TDP) boundary condition `V(0) = H · tan(α_TDP)`, so the cable departs the bed tangent to the local seabed slope. Three modes are exposed in the V2 dialog:
  - **Flat** (default, bit-for-bit identical to previous behaviour).
  - **Sloped** — single global planar slope, depth taken at the chute as the reference. Positive slope = bed deepens away from chute.
  - **Profile** — arbitrary (distance-from-chute, depth) polyline; depth is linearly interpolated and slope is taken from an adaptive centred-difference of the polyline. Rows can be entered by hand, pasted from the clipboard, or loaded from a CSV/TSV file.
  Tension at TDP is reported as `T_TDP = H / cos(α_TDP)` and the cable-on-seabed visualisation, hover tooltips, and DXF export all follow the seabed polyline rather than a single horizontal line. The solve uses an Aitken-Δ² accelerated fixed-point on the TDP horizontal position (typically ≤3 iterations) on top of the existing H-bisection; the flat case retains the single-solve fast path. A sliding-stability warning is emitted when `|tan α| > 0.2` (advisory) or `> 0.4` (severe). Per-solve diagnostics now include TDP iteration count, TDP world position, TDP depth, and TDP slope. Legacy callers passing only `water_depth_m` continue to work unchanged.
- **QGIS 4 compatibility layer:** added `qgis_compat.py` to centralise Qt/QGIS enum, QAction, dialog execution, processing parameter, layer type, geometry type and field type aliases for QGIS 3.22+ and QGIS 4 / Qt6.
- **Catenary model documentation:** added `catenary/MODEL_NOTES.md` documenting exactly what the V1 and V2 catenary models do and do not represent (2D static single-span only; no dynamics, current drag, bending stiffness, elasticity, 3D routing or multi-span contact), the governing equations, sign/unit conventions, and the recommended path for anything beyond that scope.
- **Catenary Calculator V2 — fallback-weight warning for assembly segments.** An assembly segment with blank/zero weight inside the free span silently inherits the global fallback cable weight; the solver diagnostics now name the affected segments so a user who intended a neutrally-buoyant section is not misled.
- **Calculation cores separated from UI:** the legacy V1 closed-form calculator now lives in `catenary/simple_catenary.py` (pure Python, no Qt imports, re-exported from the dialog module for compatibility), matching the existing V2 split (`catenary_solver.py` vs dialog).
- **Test coverage:** new pure-Python suite `tests/test_simple_catenary.py` (closed-form identities, mode round-trips, regression cases for the V1 bracketing failures); new `tests/test_seabed_length.py` end-to-end check of Calculate Seabed Length against an analytic planar-slope bathymetry (elongation ratio matches `sqrt(1+m²)` to 5e-4) including the previously-crashing KP-interval mode; three new V2 solver regression tests (sloped-seabed refinement deltas, `cfg` restore on failed solve, fallback-weight warning). `tests/run_qgis_smoke_tests.py` now initialises a (GUI-enabled, never shown) `QgsApplication` and runs all suites (69 checks total, including drape-solver physics checks and headless V2-dialog functional tests) under both QGIS 3.40 LTR / Qt5 and QGIS 4.0 / Qt6.

### Changed
- **Catenary Calculator V2 — `Bottom Tension` input now means actual cable tension at the TDP (`T_TDP`), not the horizontal force component (`H`).** Previously the user-entered value was fed straight to `H`, so on a sloped seabed the reported "Bottom Tension" (`= H / cos α_TDP`) did not match the input. The solver now back-calculates `H = T_TDP · cos α_TDP` per outer iteration; on a flat seabed `cos α = 1` and behaviour is bit-for-bit unchanged.
- **Catenary Calculator V2 dialog — all input sections are now collapsible** and reordered as: **Geometry** (water depth, chute top height, chute radius, seabed mode/slope/profile) → **Assembly** → **Solve Mode** → **Display** → **Counter Reference** → **Route KP Reference**. The previously separate "Seabed" header is folded into Geometry. "Cable Assembly" is shortened to "Assembly"; "Cable Count" is renamed to "Counter Reference" (reflecting that it is a generic cable-counter reading rather than a count of items). Results pane labels "Tension at Contact" / "Tangent Angle at Contact" are now "Tension at Chute Contact" / "Tangent Angle at Chute Contact" for clarity.
- **Compatibility validation tooling:** added `tests/check_qgis_compat.py` and `tests/run_qgis_smoke_tests.py` so static checks and QGIS Python smoke checks are repeatable before publishing.
- **Distance mode option** on KP-emitting processing algorithms (Place KP Points Along Route, Place KP Points from CSV, Place Single KP Point, Find Nearest KP, KP Range CSV, KP Range Highlighter, Extract KP Ranges Rule Based, Extract A/C Points): choose **Ellipsoidal (geodesic, recommended)** — the existing default — or **Cartesian (planar, projected CRS only)**. Cartesian mode is rejected (with a clear error) when the input CRS is geographic.
- **Shared `make_distance_area` helper** in `kp_range_utils.py` so every tool builds its `QgsDistanceArea` the same way and falls back to WGS84 when the project ellipsoid is unset.
- **Centralised distance/KP measurement:** every `QgsDistanceArea` in the plugin (KP Mouse Tool, Transit Measure Tool, KP Data Plotter, Depth Profile, RPL Route Comparison, all KP-emitting processing algorithms) now flows through `make_distance_area`. Removes several duplicated builder blocks and guarantees the WGS84 fallback is applied.
- **Processing provider** (`subsea_cable_processing_provider.py`): algorithms are grouped by toolbox group and sorted alphabetically within each group, for easier maintenance and PR diffs.
- **Vendored libraries:** `lib/` is only added to `sys.path` when at least one of `openpyxl` / `pyqtgraph` / `et_xmlfile` is missing from the host environment, avoiding global path pollution when QGIS already ships a compatible version.
- **Plotting backend:** Catenary Calculator (legacy and V2), KP Data Plotter and Depth Profile now use the vendored `pyqtgraph` package instead of `matplotlib`, so these GUI tools open in QGIS environments that do not ship matplotlib.
- **KP Data Plotter:** measurements are now performed in the line-layer CRS; the on-map marker is reprojected to the project CRS at display time. Reference and route layers no longer need to share a CRS for correct KPs (matching CRSes still recommended for best display performance).
- **Find Nearest KP** (`processing/nearest_kp_algorithm.py`): mismatched Points/Paths CRSes are now reprojected automatically with a feedback warning instead of being rejected outright. Distances are computed in the Paths-layer CRS.
- **Place KP Points Along Route** (`processing/place_kp_points_algorithm.py`): when a depth raster is provided in a different CRS, sample points are now reprojected to the raster CRS instead of the algorithm refusing to run.
- **Translate KP Between RPLs** (`processing/translate_kp_from_rpl_to_rpl_algorithm.py`): same-CRS requirement is retained (semantically, both layers must share the same reference frame), but the error message now names both layers' CRSes and recommends reprojecting one of them.
- **Catenary Calculator (v1):** menu label now appended with " (Legacy)"; planned for removal in 1.6 in favour of Catenary Calculator V2.
- **Reference-line input labels standardised** to "Reference Line Layer" across the KP-emitting and RPL processing algorithms (parameter IDs unchanged, so saved Processing models and scripts keep working).
- **Import Bathy MDB → Import MDB:** renamed the processing algorithm id to `import_mdb`, refreshed the user-facing label/help text, and aligned terminology with the tool's real scope (any GeoMedia MDB feature class, not bathymetry only).
- **QGIS 4.0 / Qt6 compatibility:** plotting-heavy GUI tools now use vendored `pyqtgraph` instead of `matplotlib`; `import sip` switched to `from qgis.PyQt import sip` in the dock widgets and KP Mouse Tool; `resources.py` now uses the `qgis.PyQt` shim; deprecated `QgsProcessingParameterString.FlagAdvanced` replaced with `Qgis.ProcessingParameterFlag.Advanced` (with fallback). Plugin metadata now declares `qgisMaximumVersion=4.99`. Bare Qt enum references (`Qt.LeftDockWidgetArea`, `Qt.UserRole`, `Qt.Checked`, `Qt.ItemIsEditable`, `Qt.CrossCursor`, `Qt.Key_Escape`, `Qt.LeftButton` / `Qt.RightButton`, `Qt.DashLine`, `Qt.SolidLine`, `Qt.RichText`, `Qt.WindowModal`, `Qt.blue` / `Qt.yellow` / `Qt.magenta` / `Qt.red`, etc.) across the dock widgets, map tools and catenary v2 dialog have been replaced with their scoped equivalents (`Qt.DockWidgetArea.LeftDockWidgetArea`, `Qt.ItemDataRole.UserRole`, `Qt.CheckState.Checked`, `Qt.ItemFlag.ItemIsEditable`, `Qt.CursorShape.CrossCursor`, `Qt.Key.Key_Escape`, `Qt.MouseButton.LeftButton`, `Qt.PenStyle.DashLine`, `Qt.TextFormat.RichText`, `Qt.WindowModality.WindowModal`, `Qt.GlobalColor.blue`, …), which work under both PyQt5 >= 5.11 and PyQt6. Remaining direct QGIS message, layer, geometry, processing parameter and field type enum usage now routes through `qgis_compat.py`.
- **Documentation:** README updated with a "Distance & CRS methodology" section.

### Fixed
- **Catenary Calculator V2 — touchdown position could silently fail to converge on undulating seabed profiles.** The TDP fixed-point iteration (Picard + Aitken Δ²) is non-contractive on wavy bathymetry (|g′| > 1 when local slopes are steep); on failure it fell back to its *initial guess* with no warning. That guess came from `S_guess = water_depth + chute_height + 1` using the raw Water Depth spinbox, so e.g. a stale 1000 m spinbox over a 150 m-deep profile produced "TDP at 1011.00 m from chute" with a 253 m layback — an inconsistent world frame in which the automatic drape sampled the flat profile tail instead of the waves under the cable and the displayed cable cut straight through the seabed humps. Fixes: (1) the TDP seed and `water_depth_m` now use the seabed depth at the chute, not the spinbox; (2) Aitken extrapolations are clamped into the observed landscape; (3) on non-convergence the solver runs a **robust bracketed bisection** on `f(x) = layback(x) − x` (coarsened integration step for the scan, full resolution for the lock-in), selecting the *smallest* self-consistent touchdown — the first tangential touchdown coming from the chute; (4) every solve now records a **TDP self-consistency residual** (`layback − TDP x`) in diagnostics, and an inconsistent result is flagged non-converged with a red banner instead of rendering silently; (5) the auto-drape refuses to run in an inconsistent frame (reported in Results). Regression-tested at solver level (`test_undulating_profile_tdp_self_consistency_with_bad_seed`) and end-to-end through the dialog with the exact field configuration (`test_wavy_profile_with_stale_water_depth_stays_consistent`).
- **Catenary Calculator V2 — stale Water Depth spinbox no longer distorts Profile-mode rendering and checks.** Plot depth bounds now follow the sampled seabed (previously a 1000 m spinbox over a 150 m profile stretched the axis to 1000 m on a fresh view); TDP/plot label placement and DXF text sizing use the touchdown depth; the Catenary Length minimum-feasible-length check uses the shallowest profile depth instead of the spinbox (which could spuriously reject feasible lengths, or under-check); the DXF on-bed assembly extension follows the seabed profile instead of a flat line at the spinbox depth.
- **Ellipsoid `"NONE"` sentinel bypassed the WGS84 fallback (silent planar units).** `QgsProject.ellipsoid()` returns the literal string `"NONE"` (not an empty string) when the project ellipsoid is unset or set to "None / Planimetric". `make_distance_area` only treated the *empty* string as unset, so `setEllipsoid("NONE")` succeeded and every "ellipsoidal" measurement silently degraded to planar — **degrees reported as if they were kilometres** on geographic CRSes. The helper now treats `"NONE"`/empty as unset and falls back to WGS84 (as documented), and verifies the `setEllipsoid` return value. Planar measurement remains available via the explicit Cartesian mode.
- **Catenary Calculator V2 — spurious refinement deltas on sloped/profiled seabeds.** The half-step "refinement check" diagnostic re-integrated from a default TDP world-x of 0, sampling the seabed depth at the chute instead of at the converged touchdown. On a 10° slope a fully converged solve reported a bogus ~116 m position delta, wrongly suggesting severe discretisation error. The refined integration now seeds the converged TDP position.
- **Catenary Calculator V2 — failed solves no longer corrupt the Bottom Tension input.** In Bottom Tension mode on a sloped seabed the solver internally rescales the user's `T_TDP` to `H = T·cos α` inside `cfg`; if the solve raised (infeasible configuration), the scaled value was left behind and silently changed the next solve's input. The restore now runs in a `finally` block.
- **Catenary Calculator V2 — "Profile" seabed mode with an incomplete table no longer silently computes on a flat seabed.** Fewer than two valid profile rows now raises a clear error instead of falling back to Flat while the UI claims a profile is in use.
- **Catenary Calculator (Legacy V1) — "Catenary Length" mode failed for lengths > ~2.41× water depth.** The bisection upper bracket (`q·S`) sits below the true root whenever `S > (1+√2)·h`, so e.g. a 300 m cable in 100 m water raised "Function does not change sign over interval". Replaced with the exact closed form `H = q·(S² − h²)/(2h)` (from the identity `s² = h² + 2ah`).
- **Catenary Calculator (Legacy V1) — "Layback" mode failed for layback > ~14× water depth** (fixed upper bracket `q·h·100`), a realistic shallow-water case (e.g. 350 m layback in 20 m water). The bracket now starts from the flat-catenary estimate `a ≈ x²/(2h)` and expands until it spans the root.
- **Catenary Calculator (Legacy V1) — "Top Tension" mode** now uses the exact identity `H = T_top − q·h` and rejects physically unachievable inputs (`T_top ≤ q·h`) with a clear message instead of a cryptic bracketing error.
- **Calculate Seabed Length — "Output at Regular KP Intervals" mode crashed.** A duplicated fields/sink construction block created the output sink a second time *without* the `kp_start`/`kp_end`/segment fields, so writing interval rows failed. The duplicate block is removed.
- **Calculate Seabed Length — interval segments were straight chords, not the route.** `_extract_segment` joined the interval's endpoints with a straight line, under-reporting segment plan length and sampling bathymetry off-route on curved routes. It now uses the shared `extract_line_segment` linear-referencing primitive so segments follow the actual route.
- **Calculate Seabed Length — zero-coverage fallback now matches its warning.** With no bathymetry coverage the warning claimed "seabed length equals plan length" but the output was `0.0`; the output now falls back to the plan (2D) length as stated.
- **Import Excel RPL — DMS coordinate validation.** Latitude/longitude parsing now rejects minutes ≥ 60, latitude hemisphere letters other than N/S, longitude hemisphere letters other than E/W, and out-of-range results (>90°/>180°), routing those rows through the existing invalid-row warning instead of importing silent garbage (e.g. a transposed lat/lon column mapping was previously accepted).
- **QGIS smoke-test harness never initialised `QgsApplication`,** so the ellipsoid/CRS registry was empty and 9 of 20 distance/KP smoke checks failed identically on QGIS 3.40 and 4.0 regardless of plugin correctness (and, conversely, could not have caught the `"NONE"` regression above). The runner now boots a headless `QgsApplication` when run standalone.
- **RPL Route Comparison:** distance calculator now goes through `make_distance_area`. Previously it called `setEllipsoid(project.ellipsoid())` directly with no fallback, so an unset project ellipsoid silently disabled ellipsoidal mode and produced planar/degree distances on geographic CRSes.
- **Calculate Seabed Length:** narrowed two bare `except:` clauses (which would have swallowed `KeyboardInterrupt` / `SystemExit`).
- **Dynamic Buffer (Lay Corridor):** group label corrected from `"Other tools"` to `"Other Tools"` so the algorithm joins the rest of the Other Tools group instead of creating a duplicate.
- **Plugin entry points:** removed an unused `run()` method on the main plugin class along with its dead `SubseaCableToolsDialog` import and `.ui` file (the dialog was never wired to any action and `run()` would have raised `AttributeError` if invoked).
- **KP Mouse Tool (Qt6):** replaced three remaining `.exec_()` dialog/menu calls with the shared compatibility exec helper for PyQt5/PyQt6 compatibility.
- **KP Mouse Tool toolbar button (Qt6):** popup mode now routes through `qgis_compat.py` instead of using `QToolButton.MenuButtonPopup` directly, which fixes plugin startup on QGIS 4 / PyQt6.
- **KP Mouse Tool / cartesian mode:** previously, on QGIS ≥ 3.30 (where `setEllipsoidalMode()` is gone) selecting cartesian mode left the distance area in an undefined state. Building via the shared helper now correctly leaves the ellipsoid unset for planar measurements.
- **KP Mouse Tool — range ring alignment:** the dashed range ring rendered around the click origin now sits exactly on the cursor's reported geodesic range. The ring is built as a true geodesic circle on the spheroid (sampled with `QgsDistanceArea.computeSpheroidProject` and transformed back to the project CRS), instead of an Euclidean / flat-Earth approximation. Previously the ring drifted off the cursor whenever the project CRS had a non-unity scale factor at that location (e.g. Web Mercator at high latitudes) or used non-metre map units. The same fix applies to the saved "Place Range Ring" polygon feature.
- **KP Mouse Tool — distance-area builder:** the tool now builds its ellipsoidal and (optional) cartesian `QgsDistanceArea` instances via the shared `make_distance_area` helper, picking up the WGS84 ellipsoid fallback and skipping the cartesian one cleanly on geographic CRSes.
- **Geodesic interpolation along a route:** `point_at_kp` and `extract_line_segment` (used by Place KP Points, KP Range tools, Plot Line Segments from Table, Identify RPL Crossing Points, etc.) now interpolate along each segment using `QgsDistanceArea.computeSpheroidProject` when the geometry CRS is geographic, instead of linearly interpolating in lon/lat. The old behaviour returned points off the great circle on long east-west segments at high latitude (a 60° east-west segment could miss the geodesic midpoint by tens of kilometres). Projected (metre) CRSes are unaffected and continue to use exact planar interpolation.
- **Empty-ellipsoid silent fallback:** several tools previously called `setEllipsoid(project.ellipsoid())` with no fallback. When the project ellipsoid was unset this silently disabled ellipsoidal mode, returning planar units (in degrees on a geographic CRS — wrong KPs). All call sites now go through `make_distance_area`, which applies a `WGS84` fallback when the project ellipsoid is empty.
- **Place KP Points from CSV:** fixed a `NameError` (`source` undefined) introduced during the helper migration; the algorithm now builds its distance calculator from the input line layer.
- **Depth Profile dock widget:** removed a duplicated initialisation block that re-initialised four `segment_*` lists during `__init__`.
- **Catenary Calculator V2:** removed a duplicate `activateWindow()` call left over from copy-paste.
- **Import Excel RPL:** invalid column-letter inputs (e.g. typos like "AA1" or "ZZZZ") now raise a feedback warning instead of silently being treated as an empty/skipped column. Also replaced the deprecated `QgsProcessingParameterString.FlagAdvanced` reference for QGIS 4.x compatibility.
- **KP Data Plotter:** non-spatial table layers are now the only layers offered in the Data Table combo (a stray `elif` branch was also adding line layers, which was rarely the intent). Removed unused `QgsMapLayerProxyModel` import and a duplicate `QgsWkbTypes` import.
- **Dock widgets stability:** removed the `__del__` destructor on the KP Data Plotter dock widget (cleanup already runs from `closeEvent`). Python destructors during interpreter shutdown were the root cause of the access-violation crash fixed in 1.3.0; this removes the last remaining footgun ahead of the Qt6 migration.
- **KP Mouse Tool (Qt6) — dialog buttons:** `QDialogButtonBox` button constants (`Ok`, `Cancel`, etc.) are now accessed through the compatibility layer; PyQt6 moved these under `StandardButton` scope, causing `AttributeError` when opening the KP Config dialog on QGIS 4.
- **KP Mouse Tool (Qt6) — event position:** `QgsMapMouseEvent.globalPos()` does not exist in QGIS 4 / PyQt6; replaced with a compatibility helper that returns `event.globalPos()` on QGIS 3 or falls back to `QCursor.pos()` on QGIS 4. Fixes tooltip positioning on mouse move.
- **KP Mouse Tool — point placement visibility:** added immediate canvas refresh after placing a point or range ring, so the new feature appears instantly without requiring a manual pan or zoom. Previously the feature was added to the layer but the canvas wasn't redrawn.

## [1.5.0] - 2026-04-26

This is the first published release since 1.3.0 and consolidates all stable improvements from internal 1.4.x development. Several experimental BETA tools that shipped in internal 1.4.x builds have been **temporarily withdrawn** pending further development; their source remains available on the `develop` branch on GitHub.

### Added
- **Extract KP Ranges (Rule Based):** New processing algorithm under "KP Ranges" to generate KP range listings (and optional segment geometries) by categorising an RPL line layer by a chosen attribute field (similar to QGIS categorized symbology).
- **Identify Features Intersecting RPL** (formerly *Identify RPL Lay Corridor Proximity Listing*): Generic tool that intersects point/line/polygon layers against an RPL line, with optional Lay Corridor input for trimming/clipping. Runs with any provided geometry types and only produces the corresponding outputs.
- **Plot Line Segments from Table:** New processing algorithm under "Other Tools" to create line segments from a table layer with start and end latitude/longitude columns. Optionally creates a point layer for the endpoints.
- **Extract A/C Points from RPL:** New processing algorithm to extract Alter Course points from an RPL line (including multi-feature routes), outputting KP, turn angle, and optional threshold/bin fields.
- **RPL Route Comparison:** New processing algorithm to compare design vs as-laid RPL routes, calculating position offsets including radial, along-track, and cross-track distances.
- **Translate KP Between RPLs (Points):** New processing algorithm to translate KP values from one RPL reference to another, creating corresponding points on the target RPL.
- **Identify RPL Crossing Points:** New processing algorithm to find crossing points between an RPL line layer and one or more asset line layers. Outputs KP, lat/lon, relative crossing angle, and crossed asset attributes; supports optional buffer polygons around the crossed asset near each crossing.
- **Identify RPL Area Listing:** New processing algorithm that takes an RPL line layer and one or more polygon layers (e.g. seabed features, hazard areas), and outputs a line layer traced over the RPL with breaks at polygon edges. Polygon attributes are picked up by the new line layer and start/end KP are included.
- **Merge KP Range Tables:** New tool under "KP Ranges" to combine two KP-range tables with mismatched intervals. Supports canonical segmentation (non-overlapping KP intervals), summarise (Table B values into Table A ranges), and a simple lookup mode. Includes overlap handling options and remembers last-used parameters.
- **Group Adjacent KP Ranges by Field:** New tool under "KP Ranges" that merges consecutive KP intervals sharing the same attribute value, keeping one feature per run and optionally nulling conflicting other fields.
- **KP Range Depth + Slope Summary:** New tool under "KP Ranges" to sample bathymetry along KP-range line features and append depth/slope summary fields. Supports raster(s) (prefers highest resolution where rasters overlap) or 1–2 contour layers.
- **Identify Hazards in Lay Corridor:** Processing tool that compares point/line/polygon layers against a lay corridor polygon and RPL reference, exporting proximity listings with KP/DCC, lat/lon, and JSON-encoded source attributes.
- **Export Chartlets Based on KP Range List:** Processing tool that walks through each KP range and creates a per-section map PNG, either using segment geometries directly or extracting the KP span from an RPL line.
- **Extract Lines Intersecting Polygons:** New tool under "Other Tools" to combine intersecting features from multiple line layers into a single output, with optional clip-to-polygon and CRS-safe processing.
- **Catenary Calculator V2 (Experimental):** Updated catenary calculator dialog that can model multi-segment cable assemblies and bodies.

### Changed
- **KP Mouse Tool:** Calculates KP based on geodetic measurements by default with an option for Cartesian calculations when the layer uses a projected CRS. Now remembers the setting between sessions, shows total route length for both methods (to 0.001 km) in the configuration metrics, and is more robust when the reference layer is removed/reloaded. Right-click samples a depth value at the mouse position from a raster or contour layer; the copy-to-clipboard format is now configurable, and a "Go to KP..." option has been added.
- **Depth Profile tool:** Added an **Invert KP Axis** option to flip the KP axis without renumbering plotted values, plus an **Invert Slope Axis** option for slope plots, and renamed **Invert Slope** to **Invert Slope Sign** for clarity. Improved raster/contour inputs and performance for long routes — the second contour layer is now optional, multiple raster layers are supported (overlapping rasters prefer higher resolution; missing coverage stays null with warnings), Refresh control + live sample/probe estimate readout, and optional adaptive raster sampling derived from raster resolution.
- **Transit Measure Tool:** Added a Quick Buffer tool to apply a buffer to the route.
- **Import MDB:** Reworked execution to run ODBC reads in a subprocess to prevent silent QGIS crashes. Improved GeoMedia `GFeatures` metadata parsing, added automatic handling for ambiguous/mixed geometry tables, and fixed closed-line features being misclassified as polygons. Now loads Polygon and Point layers by default alongside LineString layers, and outputs scratch (memory) layers so users can save them via normal QGIS workflows.
- **Processing Toolbox Grouping:** Refined groupings to be more intuitive.
- **QGIS Compatibility Baseline:** Updated declared minimum QGIS version to 3.22.
- **Qt Imports:** Standardised plugin-owned Qt imports to `qgis.PyQt` for improved cross-install compatibility.
- **Optional Plotting Dependencies:** Plotting-heavy dock widgets (e.g. matplotlib-dependent) are now imported lazily so the plugin can still load if optional deps are missing.

### Fixed
- **Processing Provider Robustness:** Provider now registers algorithms defensively so a single tool import failure won't hide the whole toolbox.
- **MBES Raster (XYZ):** Added GDAL algorithm availability checks and fallback logic for IDW interpolation; fixed an indentation error in the IDW branch which could prevent the tool from running.
- **MDB Import Robustness:** Made the MDB import tool resilient to missing `pyodbc` (fails at runtime with a clear message instead of breaking provider load).
- **Plotting Dependency Reliability:** Vendored `pyqtgraph` under `lib/` to avoid requiring users to install it separately.
- **Depth Profile:** Added option to invert slope angle/percentage calculation (default not inverted).
- **Transit Measure:** Fixed cleanup issues.
- **Nearest KP:** Fixed an issue with multi-segment RPL splits.

### Removed
- Several in-development tools that appeared in unreleased internal builds have been withdrawn from this release pending further work. They may return in a future version.

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

# QGIS Compatibility Test Plan

This plan verifies that Subsea Cable Tools remains compatible with QGIS 3.22 and current QGIS 3 builds while preparing for QGIS 4.0 / Qt6.

## Static Checks

Run from the plugin root with the project virtual environment or any Python 3 interpreter:

```powershell
python tests/check_qgis_compat.py
```

Expected result: `QGIS compatibility check passed.`

The checker scans plugin-owned Python files and excludes `lib/`, `.venv/`, the checker itself, and `qgis_compat.py` where compatibility fallbacks intentionally live.

## QGIS Python Smoke Checks

Run this in each target QGIS runtime:

```powershell
python tests/run_qgis_smoke_tests.py
```

Use an OSGeo4W shell or another shell where the selected QGIS install has configured `PYTHONPATH`, `PATH`, and QGIS libraries.

Target matrix:

- QGIS 3.22, the declared minimum supported version.
- Current or previous QGIS 3 LTR/current release used by the team.
- QGIS 4.0 / Qt6 preview or release build.

The smoke runner checks:

- Distance helper round trips.
- KP geometry utility round trips.
- Processing provider registration.
- Main plugin module import.

## Manual GUI Smoke Checks

Run at least in current QGIS 3 and QGIS 4.0. Run a reduced pass in QGIS 3.22 if available.

1. Start QGIS with a clean test profile.
2. Install or enable the plugin.
3. Confirm the Processing Toolbox shows `Subsea Cable Tools`.
4. Open the KP Mouse Tool settings, choose a reference line, save, and activate the tool.
5. Right-click the map and check Copy KP, Copy KP/DCC, Place Point, Place Range Ring, and Go to KP.
6. Open KP Data Plotter and plot a table against a route line.
7. Open Depth Profile and refresh a profile using a route line or temporary line.
8. Open Catenary Calculator and Catenary Calculator V2.
9. Open Transit Measure Tool, add waypoints, and create output layers.
10. Disable and re-enable the plugin, then close QGIS without shutdown errors.

## Packaging Checks

Before publishing:

1. Package from a clean git tree.
2. Install the generated zip into clean QGIS 3 and QGIS 4 profiles.
3. Repeat the static, smoke, and critical GUI checks against the installed package.
4. Run QGIS plugin repository validation or qgis-plugin-ci validation if available.

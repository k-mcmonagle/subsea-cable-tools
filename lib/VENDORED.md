# Vendored third-party packages

Everything under `lib/` is third-party code shipped inside the plugin so that
users install nothing: QGIS users generally cannot `pip install` into the
QGIS Python, and none of these packages is guaranteed to be present there.
The plugin's `__init__.py` puts `lib/` on `sys.path` at load time, and only
when at least one of these packages is missing from the host Python (so a
QGIS that already ships a compatible version keeps its own copy).

`lib/` is authoritative. `requirements.txt` pins the same versions for
dev/CI environments that want them outside QGIS; update both when
re-vendoring.

| Package | Version | Licence | Upstream | Why it is vendored |
|---|---|---|---|---|
| openpyxl | 3.1.5 | MIT | https://foss.heptapod.net/openpyxl/openpyxl | Reads/writes `.xlsx` (Import Excel RPL, planner and burial exports, RPL sheets). Not bundled with every QGIS build (e.g. absent from the QGIS 4.0 OSGeo4W Python). |
| et_xmlfile | 2.0.0 | MIT | https://foss.heptapod.net/openpyxl/et_xmlfile | Runtime dependency of openpyxl >= 3.1. |
| pyqtgraph | 0.13.7 | MIT | https://github.com/pyqtgraph/pyqtgraph | Fast interactive plots (Depth Profile, KP Plotter, catenary / lay simulator, Burial Planner profile). Never shipped with QGIS. Needs NumPy, which QGIS provides. |
| access_parser | 0.0.6 | Apache-2.0 | https://github.com/ClarotyICS/access_parser | Pure-Python reader for Jet/Access `.mdb` files, so Import MDB and path-file (`.pthmdb`) import work without the Microsoft Access ODBC driver (ODBC remains a fallback). |
| construct | 2.10.70 | MIT | https://github.com/construct/construct | Dependency of access_parser. |
| tabulate | 0.10.0 | MIT | https://github.com/astanin/python-tabulate | Dependency of access_parser. Its metadata says Python >= 3.10, but it imports and works on Python 3.9 (checked); re-check when upgrading, since QGIS 3.22-3.28 ship Python 3.9. |

Versions come from the `*.dist-info` folders, which also carry each
package's licence file. The CET colour-map data inside pyqtgraph carries its
own CC-BY / CC0 notices (`lib/pyqtgraph/colors/maps/`).

## Local changes

openpyxl, et_xmlfile, access_parser, construct and tabulate are unmodified
wheel contents (verified against their `RECORD` hashes, ignoring line
endings). **pyqtgraph is trimmed and patched** - do not overwrite it with a
fresh wheel without re-applying these:

* Removed (unused by the plugin; several failed the plugins.qgis.org security
  scan): `examples/`, `opengl/`, `flowchart/`, `console/`, `dockarea/`,
  `multiprocess/`, `configfile.py`, `widgets/RemoteGraphicsView.py`.
* `pyqtgraph/__init__.py` - no longer imports the removed modules.
* `pyqtgraph/Qt/__init__.py` - drops the `subprocess`-based `loadUiType`
  helper for PySide.
* `pyqtgraph/exporters/SVGExporter.py` - hardened SVG export (patch marker
  `__SUBSEA_SVG_EXPORTER_PATCH_VERSION__`), Bandit `nosec` annotations.
* `pyqtgraph/metaarray/MetaArray.py` - `eval` replaced by
  `ast.literal_eval`; loading pickled data refused.

## Not shipped in the release zip

`lib/pyqtgraph/icons/peegee/` (the pyqtgraph application-icon images) is
kept in the repository but excluded from the plugin zip via
`.gitattributes` (`export-ignore`): pyqtgraph only loads it from `mkQApp()`
when it has to create its own `QApplication`, which never happens inside
QGIS (and a missing icon file would only yield an empty icon).

# -*- coding: utf-8 -*-
"""Acquired burial data for the Burial Planner: import, store and assess.

Delivered files (trencher or plough logs, burial surveys) are kept byte for
byte in their own GeoPackage beside the plan file, parsed to compressed
columns, and assessed on 1 m design-KP bins against the plan's target depth.

* :mod:`.roles`   column roles, units and the aliases *Suggest* uses (pure)
* :mod:`.profile` import profiles: which column takes which role (pure)
* :mod:`.reader`  CSV / text / XLSX tables to numpy columns (pure + numpy)
* :mod:`.overlap` overlapping deliveries in a dataset: newer wins, recorded
* :mod:`.bins`    worst-case 1 m bins, latest-wins selection, statistics
* :mod:`.store`   the burial data GeoPackage (sqlite3 + numpy, no QGIS)
* :mod:`.location` where a project's data file lives (QGIS project entries)
* :mod:`.samples` deterministic synthetic deliveries for tests and examples

The measure is depth of lowering (DOL): metres below mean seabed level,
positive down.
"""

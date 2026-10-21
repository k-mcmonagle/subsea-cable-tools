# -*- coding: utf-8 -*-
"""Acquired burial data for the Burial Planner: import, store and assess.

Delivered files (trencher or plough logs, burial surveys) are kept byte for
byte in their own GeoPackage beside the plan file, parsed to compressed
columns, and assessed on 1 m design-KP bins against the plan's target depth.

Engine (no Qt):

* :mod:`.roles`      column roles, units and the aliases *Suggest* uses
* :mod:`.profile`    import profiles: which column takes which role
* :mod:`.reader`     CSV / text / XLSX tables to numpy columns
* :mod:`.overlap`    overlapping deliveries in a dataset: newer wins, recorded
* :mod:`.bins`       worst-case 1 m bins, latest-wins selection, statistics
* :mod:`.store`      the burial data GeoPackage (sqlite3 + numpy)
* :mod:`.importer`   review deliveries, then import them in one transaction
* :mod:`.assessment` datasets against the plan's target on the plan's KPs
* :mod:`.samples`    deterministic synthetic deliveries for tests and examples

QGIS side:

* :mod:`.location`   where a project's data file lives (project entries)
* :mod:`.controller` the data file across project changes, background tasks
* :mod:`.task`       the background task
* :mod:`.kp_check`   delivered KPs against the records' positions
* :mod:`.mapping_widget`, :mod:`.import_wizard`, :mod:`.help` — the UI pieces
  (the tabs are ``burial/tabs/acquired_tab.py`` and ``assessment_tab.py``)

The measure is depth of lowering (DOL): metres below mean seabed level,
positive down.
"""

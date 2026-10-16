# -*- coding: utf-8 -*-
"""Geotechnical data: AGS files and cone penetration tests (CPT).

Pure python (no QGIS, no numpy) so the Burial Planner, the Processing
algorithms and the plain-Python test suite share one reader:

- ``ags``        AGS 3.x / 4.x file syntax: groups, headings, units, rows.
- ``cpt``        CPT tests from an AGS file, units normalised, and the
                 derived parameters (qt, Rf, qnet, Bq, Ic, SBTn, Su, Dr).
- ``positions``  joining a separate position / summary table to the tests
                 by CPT name, coordinate text and stratigraphy columns.
"""

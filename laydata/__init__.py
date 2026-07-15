# -*- coding: utf-8 -*-
"""Cable-lay data QC & analysis engine.

Pure-Python, UI-free package that loads a cable-lay GeoPackage layer into
column arrays (numpy) and runs quality-control checks against it. The engine is
consumed by both the Cable Lay Data Explorer window and the "Run Cable Lay QC"
processing algorithm, and its core has *no* QGIS-UI dependency so it can be
unit-tested from a plain Python interpreter.

numpy is always available inside QGIS (it is a hard dependency of the bundled
pyqtgraph) so no new third-party dependency is introduced.
"""

from .dataset import LayDataset
from .qc_base import Finding, ParamSpec, QcCheck, QcRunner, Severity
from .qc_checks import ALL_CHECKS, checks_by_id, make_check

__all__ = [
    "LayDataset",
    "Finding",
    "ParamSpec",
    "QcCheck",
    "QcRunner",
    "Severity",
    "ALL_CHECKS",
    "checks_by_id",
    "make_check",
]

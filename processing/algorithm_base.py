# -*- coding: utf-8 -*-
"""Shared plumbing for the plugin's Processing algorithms.

* :class:`SubseaCableAlgorithm` — common base class. Gives every algorithm a
  ``helpUrl()`` (the Help button in the algorithm dialog) pointing at the
  plugin documentation.
* :func:`deprecated_flag` — the "Deprecated" algorithm flag on QGIS 3 and 4.
* :func:`gdal_creation_options` — the creation-options parameter of a GDAL
  child algorithm, whichever name the running QGIS gives it.
"""

from __future__ import annotations

from typing import Dict

from qgis.core import Qgis, QgsApplication, QgsProcessingAlgorithm

#: Plugin documentation (the README's Processing algorithms section).
HELP_URL = "https://github.com/k-mcmonagle/subsea-cable-tools#processing-algorithms"


class SubseaCableAlgorithm(QgsProcessingAlgorithm):
    """Base class for every Subsea Cable Tools Processing algorithm."""

    def helpUrl(self):  # noqa: N802 (Qt API name)
        return HELP_URL


def deprecated_flag():
    """``Deprecated`` algorithm flag (Qgis.ProcessingAlgorithmFlag on 3.36+/4)."""
    scope = getattr(Qgis, "ProcessingAlgorithmFlag", None)
    if scope is not None and hasattr(scope, "Deprecated"):
        return scope.Deprecated
    return QgsProcessingAlgorithm.FlagDeprecated


def gdal_creation_options(algorithm_id: str, options: str) -> Dict[str, str]:
    """``{parameter name: options}`` for a GDAL child algorithm's creation options.

    QGIS 3.x calls the parameter ``OPTIONS``; newer releases (QGIS 4) call it
    ``CREATION_OPTIONS`` and keep ``OPTIONS`` only as a deprecated alias. The
    name is read from the algorithm the running QGIS actually registers.
    """
    algorithm = QgsApplication.processingRegistry().algorithmById(algorithm_id)
    if algorithm is not None and algorithm.parameterDefinition("CREATION_OPTIONS") is not None:
        return {"CREATION_OPTIONS": options}
    return {"OPTIONS": options}

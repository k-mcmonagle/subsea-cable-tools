# -*- coding: utf-8 -*-
"""Cable Lay Data Explorer package.

A standalone (non-docked) analysis window plus supporting panels that operate on
an imported cable-lay GeoPackage, sharing the ``laydata`` QC engine with the
Run Cable Lay QC processing algorithm.
"""

from .explorer_window import CableLayExplorerWindow

__all__ = ["CableLayExplorerWindow"]

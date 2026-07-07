# -*- coding: utf-8 -*-
"""WorkbenchSelectionBus — cross-dock selection signals.

A tiny shared QObject that lets the Assembly Manager (SLD) and RPL Manager
(map/tables) point at the same place without knowing about each other:

- kpSelected(rpl_id, kp_km): "look at this KP on this route" (SLD -> map)
- cableDistSelected(assembly_id, cable_m): "look at this cable distance"
  (map/RPL -> SLD)
"""

from __future__ import annotations

from qgis.PyQt.QtCore import QObject, pyqtSignal


class WorkbenchSelectionBus(QObject):
    kpSelected = pyqtSignal(str, float)          # rpl_id, kp_km
    cableDistSelected = pyqtSignal(str, float)   # assembly_id, cable_dist_m


_bus = None


def selection_bus() -> WorkbenchSelectionBus:
    global _bus
    if _bus is None:
        _bus = WorkbenchSelectionBus()
    return _bus

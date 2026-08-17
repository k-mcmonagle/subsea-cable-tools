# -*- coding: utf-8 -*-
"""Fast, geometry-free summaries for Workbench navigation and schematics."""

from __future__ import annotations

import os
from collections import OrderedDict
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from qgis.core import QgsFeatureRequest, QgsProject


@dataclass(frozen=True)
class RplPositionSummary:
    pos: object = None
    event: str = ""
    kp_km: Optional[float] = None
    cable_km: Optional[float] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    depth_m: Optional[float] = None


@dataclass(frozen=True)
class RplSectionSummary:
    start_event: str = ""
    end_event: str = ""
    start_pos: object = None
    end_pos: object = None
    start_kp_km: Optional[float] = None
    end_kp_km: Optional[float] = None
    route_length_km: Optional[float] = None
    cable_length_km: Optional[float] = None
    cable_type: str = ""
    leg_count: int = 0


@dataclass(frozen=True)
class RplSummary:
    position_count: int = 0
    leg_count: int = 0
    section_count: int = 0
    start_pos: object = None
    end_pos: object = None
    start_event: str = ""
    end_event: str = ""
    start_kp_km: Optional[float] = None
    end_kp_km: Optional[float] = None
    route_length_km: Optional[float] = None
    cable_length_km: Optional[float] = None
    cable_type: str = ""
    positions: Tuple[RplPositionSummary, ...] = ()
    sections: Tuple[RplSectionSummary, ...] = ()


class RplSummaryCache:
    """Small process-local LRU; summaries invalidate when registry metadata changes."""

    def __init__(self, capacity: int = 256):
        self.capacity = capacity
        self._items = OrderedDict()

    def get(self, store, rpl: Optional[Dict]) -> RplSummary:
        if not rpl:
            return RplSummary()
        key = (
            os.path.normcase(os.path.abspath(store.gpkg_path)),
            str(rpl.get("rpl_id") or ""),
            str(rpl.get("modified_utc") or ""),
            str(rpl.get("points_layer") or ""),
            str(rpl.get("lines_layer") or ""),
        )
        cached = self._items.pop(key, None)
        if cached is not None:
            self._items[key] = cached
            return cached
        summary = _read_summary(store, rpl)
        self._items[key] = summary
        while len(self._items) > self.capacity:
            self._items.popitem(last=False)
        return summary

    def invalidate(self, rpl_id: str = "") -> None:
        if not rpl_id:
            self._items.clear()
            return
        for key in [key for key in self._items if key[1] == str(rpl_id)]:
            self._items.pop(key, None)


_CACHE = RplSummaryCache()


def rpl_summary(store, rpl: Optional[Dict]) -> RplSummary:
    return _CACHE.get(store, rpl)


def invalidate_rpl_summary(rpl_id: str = "") -> None:
    _CACHE.invalidate(rpl_id)


def _read_summary(store, rpl: Dict) -> RplSummary:
    points = _open_layer(store, rpl.get("points_layer") or "")
    lines = _open_layer(store, rpl.get("lines_layer") or "")
    point_rows = _point_rows(points)
    line_rows = _line_rows(lines)
    if not point_rows:
        return RplSummary(leg_count=len(line_rows))

    first, last = point_rows[0], point_rows[-1]
    intermediate_events = sum(
        1 for row in point_rows[1:-1] if str(row.get("event") or "").strip())
    section_count = intermediate_events + (1 if len(point_rows) >= 2 else 0)

    route_values = [row.get("route_km") for row in line_rows]
    cable_values = [row.get("cable_km") for row in line_rows]
    route_length = _complete_sum(route_values)
    cable_length = _complete_sum(cable_values)
    if route_length is None:
        route_length = _difference(first.get("kp"), last.get("kp"))
    if cable_length is None:
        cable_length = _difference(first.get("cable_kp"), last.get("cable_kp"))
    cable_types = []
    for row in line_rows:
        value = str(row.get("cable_type") or "").strip()
        if value and value not in cable_types:
            cable_types.append(value)
    if len(cable_types) > 3:
        cable_type = " / ".join(cable_types[:3]) + f" / +{len(cable_types) - 3}"
    else:
        cable_type = " / ".join(cable_types)
    boundaries = [0]
    boundaries.extend(index for index, row in enumerate(point_rows[1:-1], 1)
                      if str(row.get("event") or "").strip())
    if len(point_rows) > 1:
        boundaries.append(len(point_rows) - 1)
    sections = []
    for start_index, end_index in zip(boundaries, boundaries[1:]):
        section_legs = line_rows[start_index:end_index]
        section_types = []
        for leg in section_legs:
            value = str(leg.get("cable_type") or "").strip()
            if value and value not in section_types:
                section_types.append(value)
        sections.append(RplSectionSummary(
            start_event=str(point_rows[start_index].get("event") or "").strip(),
            end_event=str(point_rows[end_index].get("event") or "").strip(),
            start_pos=point_rows[start_index].get("pos"),
            end_pos=point_rows[end_index].get("pos"),
            start_kp_km=point_rows[start_index].get("kp"),
            end_kp_km=point_rows[end_index].get("kp"),
            route_length_km=_complete_sum(
                [leg.get("route_km") for leg in section_legs]),
            cable_length_km=_complete_sum(
                [leg.get("cable_km") for leg in section_legs]),
            cable_type=" / ".join(section_types) if section_types else "",
            leg_count=len(section_legs),
        ))
    positions = tuple(RplPositionSummary(
        pos=row.get("pos"), event=str(row.get("event") or "").strip(),
        kp_km=row.get("kp"), cable_km=row.get("cable_kp"),
        latitude=row.get("lat"), longitude=row.get("lon"),
        depth_m=row.get("depth"),
    ) for row in point_rows)
    return RplSummary(
        position_count=len(point_rows),
        leg_count=len(line_rows),
        section_count=section_count,
        start_pos=first.get("pos"), end_pos=last.get("pos"),
        start_event=str(first.get("event") or "").strip(),
        end_event=str(last.get("event") or "").strip(),
        start_kp_km=first.get("kp"), end_kp_km=last.get("kp"),
        route_length_km=route_length, cable_length_km=cable_length,
        cable_type=cable_type, positions=positions, sections=tuple(sections),
    )


def _open_layer(store, layer_name: str):
    if not layer_name:
        return None
    try:
        from .project_layers import find_layer
        existing = find_layer(QgsProject.instance(), store.gpkg_path, layer_name)
        if existing is not None and existing.isValid():
            return existing
    except Exception:
        pass
    return store.open_layer(layer_name)


def _point_rows(layer):
    if layer is None or not layer.isValid():
        return []
    names = (
        "SeqNo", "PosNo", "Event", "DistCumulative", "CableDistCumulative",
        "Latitude", "Longitude", "ApproxDepth",
    )
    fields = layer.fields()
    indexes = [fields.indexOf(name) for name in names]
    request = _attribute_request([index for index in indexes if index >= 0])
    rows = []
    for fallback, feature in enumerate(layer.getFeatures(request)):
        values = [_value(feature, index) for index in indexes]
        rows.append({
            "seq": _number(values[0], fallback), "pos": values[1],
            "event": values[2], "kp": _float(values[3]),
            "cable_kp": _float(values[4]), "lat": _float(values[5]),
            "lon": _float(values[6]), "depth": _float(values[7]),
        })
    rows.sort(key=lambda row: row["seq"])
    return rows


def _line_rows(layer):
    if layer is None or not layer.isValid():
        return []
    names = ("SeqNo", "DistBetweenPos", "CableDistBetweenPos", "CableType")
    fields = layer.fields()
    indexes = [fields.indexOf(name) for name in names]
    request = _attribute_request([index for index in indexes if index >= 0])
    rows = []
    for fallback, feature in enumerate(layer.getFeatures(request)):
        values = [_value(feature, index) for index in indexes]
        rows.append({
            "seq": _number(values[0], fallback), "route_km": _float(values[1]),
            "cable_km": _float(values[2]), "cable_type": values[3],
        })
    rows.sort(key=lambda row: row["seq"])
    return rows


def _attribute_request(indexes):
    request = QgsFeatureRequest()
    if indexes:
        request.setSubsetOfAttributes(indexes)
    no_geometry = getattr(QgsFeatureRequest, "NoGeometry", None)
    if no_geometry is None:
        flag_scope = getattr(QgsFeatureRequest, "Flag", None)
        no_geometry = getattr(flag_scope, "NoGeometry")
    request.setFlags(no_geometry)
    return request


def _value(feature, index):
    if index < 0:
        return None
    value = feature[index]
    if type(value).__name__ == "QVariant":
        return None if not value.isValid() or value.isNull() else value.value()
    return value


def _float(value):
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _number(value, fallback):
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _complete_sum(values) -> Optional[float]:
    return (sum(float(value) for value in values)
            if values and all(value is not None for value in values) else None)


def _difference(start, end) -> Optional[float]:
    return None if start is None or end is None else float(end) - float(start)

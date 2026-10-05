"""Reading a numeric dataset's sources in QGIS: measurements and live placement.

A dataset's ``config`` has three parts (see DECISIONS.md):

- ``measurements``: the file or layer the profiles were imported from, the
  columns chosen by header name, depth unit, decimal comma, missing-value
  codes, point support, and a fingerprint of the source at import so a later
  change can be reported (*Reload* re-reads it with the same choices);
- ``placement``: the layer saying where each investigation applies — a
  KP-range table (start/end KP fields, KP unit and reference RPL, as the
  Exclusions and Risk KP-range tables) or polygons crossed by the route.
  It is read live every time the plot refreshes, never copied;
- ``colours``: see ``numeric_profiles.colour_settings``.
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

from qgis.core import QgsProject, QgsVectorLayer

from . import kp_table, map_layers, numeric_profiles as numeric
from .plan_import import read_grid

PLACEMENT_KP_TABLE = "kp_table"
PLACEMENT_POLYGONS = "polygons"
MAX_ROWS = 1000002


def attribute_text(value) -> str:
    """A layer attribute as table text; NULL becomes blank (missing)."""
    if value is None:
        return ""
    if type(value).__name__ == "QVariant":
        if not value.isValid() or value.isNull():
            return ""
        value = value.value()
    return str(value).strip()


def layer_ref(layer) -> Dict:
    """How a dataset finds a layer again (the registered-input keys)."""
    return {"layer_id_hint": layer.id(), "layer_source": layer.source(),
            "layer_name": layer.name()}


def resolve_layer(ref: Optional[Dict]):
    if not ref:
        return None
    try:
        layer = map_layers.resolve_input_layer(QgsProject.instance(), ref)
    except Exception:
        return None
    return layer if isinstance(layer, QgsVectorLayer) and layer.isValid() else None


def layer_grid(layer) -> List[List[str]]:
    grid = [layer.fields().names()]
    for feature in layer.getFeatures():
        grid.append([attribute_text(v) for v in feature.attributes()])
        if len(grid) >= MAX_ROWS:
            raise ValueError("The layer exceeds one million features; split it.")
    return grid


def read_source_grid(source: Dict) -> Tuple[List[List[str]], List[str]]:
    """``(grid, sheet_names)`` for a measurements source (file or layer)."""
    if source.get("kind") == "layer":
        layer = resolve_layer(source)
        if layer is None:
            raise ValueError(f"The layer '{source.get('layer_name') or '?'}' is not loaded "
                             "and its source cannot be opened.")
        return layer_grid(layer), []
    path = source.get("path") or ""
    if not path or not os.path.exists(path):
        raise ValueError(f"The file {path or '(none)'} no longer exists.")
    grid, sheets = read_grid(path, source.get("sheet") or None, max_rows=MAX_ROWS)
    if len(grid) >= MAX_ROWS:
        raise ValueError("The table exceeds one million rows; split it.")
    return grid, sheets


def source_fingerprint(source: Dict) -> str:
    """Changes when the source file or layer content changes."""
    if source.get("kind") == "layer":
        return map_layers.layer_fingerprint(resolve_layer(source))
    path = source.get("path") or ""
    try:
        stat = os.stat(path)
    except OSError:
        return ""
    return f"{int(stat.st_mtime)}:{stat.st_size}"


def source_label(source: Dict) -> str:
    if source.get("kind") == "layer":
        return f"layer '{source.get('layer_name') or '?'}'"
    label = os.path.basename(source.get("path") or "") or "(no file)"
    return label + (f" [{source['sheet']}]" if source.get("sheet") else "")


def parse_measurements(grid, measurements: Dict, dataset: Dict) -> List[Dict]:
    """Profiles from a grid using the dataset's stored column choices."""
    header_row = int(measurements.get("header_row") or 1)
    if header_row > len(grid):
        raise ValueError("The header row is beyond the end of the table.")
    headers = list(grid[header_row - 1])
    mapping = numeric.column_indices(headers, measurements.get("columns") or {})
    return numeric.import_profiles(
        grid[header_row:], mapping, dataset_id=dataset.get("dataset_id") or "",
        variable=dataset.get("variable") or "", units=dataset.get("units") or "",
        depth_scale=float(measurements.get("depth_scale") or 1.0),
        sample_support=float(measurements.get("point_support_m") or 0.02),
        decimal_comma=bool(measurements.get("decimal_comma")),
        missing=str(measurements.get("missing") or "").split(";"),
        provenance={"source": source_label(measurements.get("source") or {}),
                    "columns": measurements.get("columns") or {}, "header_row": header_row})


def reload_measurements(dataset: Dict) -> Tuple[List[Dict], Dict]:
    """Re-read a dataset's source with its saved choices: ``(profiles, config)``."""
    config = dict(dataset.get("config") or {})
    measurements = dict(config.get("measurements") or {})
    if not measurements.get("columns"):
        raise ValueError("This dataset has no saved column choices; use Edit… to import it again.")
    grid, _sheets = read_source_grid(measurements.get("source") or {})
    profiles = parse_measurements(grid, measurements, dataset)
    measurements["fingerprint"] = source_fingerprint(measurements.get("source") or {})
    config["measurements"] = measurements
    return profiles, config


def source_changed(dataset: Dict) -> Optional[str]:
    """A note when the measurements source changed since import (else None)."""
    measurements = (dataset.get("config") or {}).get("measurements") or {}
    source = measurements.get("source") or {}
    recorded = measurements.get("fingerprint")
    if not source or not recorded:
        return None
    now = source_fingerprint(source)
    if not now:
        return f"the measurements source ({source_label(source)}) cannot be found"
    if now != recorded:
        return f"{source_label(source)} has changed since it was imported — Reload to use the new values"
    return None


def read_placement(model, placement: Dict, cache: Optional[Dict] = None
                   ) -> Tuple[List[Dict], List[str], object]:
    """``(assignments, notes, layer)`` read live from the placement layer.

    KP-range rows are translated from the RPL their KPs are quoted on onto
    the plan's route, as for the Exclusions and Risk KP-range tables.
    Raises ValueError when the layer or its reference RPL is unusable.
    """
    kind = placement.get("kind")
    if not kind:
        return [], ["no KP ranges chosen yet — use Edit… → KP ranges"], None
    layer = resolve_layer(placement.get("layer"))
    if layer is None:
        name = (placement.get("layer") or {}).get("layer_name") or "?"
        raise ValueError(f"the KP range layer '{name}' is not loaded and cannot be opened")
    id_field = placement.get("id_field") or ""
    if id_field not in layer.fields().names():
        raise ValueError(f"the layer '{layer.name()}' has no field '{id_field}'")
    if kind == PLACEMENT_POLYGONS:
        from .numeric_profile_geometry import polygon_assignments
        assignments, notes = polygon_assignments(model.route, layer, id_field)
        return assignments, notes, layer
    from .rpl_reference import table_kp_map
    start_field, end_field = kp_table.fields(placement)
    wanted = [n for n in (id_field, start_field, end_field) if n in layer.fields().names()]
    rows, ids = [], {}
    for feature in layer.getFeatures():
        ref = str(feature.id())
        rows.append((ref, {n: feature[n] for n in wanted}))
        ids[ref] = attribute_text(feature[id_field])
    ranges, notes = kp_table.read_ranges(rows, placement)
    map_range, ref_notes = table_kp_map(model, placement, cache)
    kp_table.translate(ranges, map_range)
    notes = ref_notes + notes + kp_table.flag_notes(ranges)
    assignments, more = numeric.ranges_to_assignments(
        ranges, ids, f"layer {layer.name()} — {kp_table.reference_text(placement)}")
    return assignments, notes + more, layer

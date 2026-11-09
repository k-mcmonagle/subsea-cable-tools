# -*- coding: utf-8 -*-
"""Small, dependency-free DXF section reader and sub-bottom horizon model.

Optional plan parameters, not a schema migration. Source vertices stay in
drawing coordinates; working segments are KP (km), depth below seabed (m).
Disconnected entities are never joined, sorted together or averaged.
"""

from __future__ import annotations

import bisect
import codecs
import hashlib
import json
import math
import os
from collections import Counter
from dataclasses import asdict, dataclass

from . import dxf_annotations, schema

PARAMS_KEY = "subbottom_horizons"
# Memory budgets, not DXF format limits. Preserve all accepted vertices.
MAX_POINTS = 1_000_000
MAX_HORIZONS = 256
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_TEXT_EDIT_POINTS = 50_000
COLOURS = ("#9c27b0", "#e65100", "#00796b", "#1565c0", "#c62828", "#6d4c41")
# DXF $INSUNITS codes supported by the import controls.
DXF_UNITS = {1: 0.0254, 2: 0.3048, 4: 0.001, 5: 0.01, 6: 1.0, 7: 1000.0}


class Cancelled(Exception):
    """Cooperative cancellation; callers must discard unfinished results."""


def checkpoint(progress, index=0, percent=None, message=""):
    if progress is not None and index % 2048 == 0:
        progress(percent, message)


def _points(points, progress=None):
    for i, (x, y) in enumerate(points):
        checkpoint(progress, i)
        yield [_number(x), _number(y)]


def _number(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("Coordinates must be finite numbers.")
    return value


def checked_segment(points, progress=None):
    """Keep the order; reverse a descending line, reject folds/verticals."""
    out = list(_points(points, progress))
    # Consecutive identical vertices are harmless, unlike different Y at one X.
    out = [p for i, p in enumerate(out) if i == 0 or p != out[i - 1]]
    if len(out) < 2:
        raise ValueError("A horizon segment needs at least two different points.")
    if out[-1][0] < out[0][0]:
        out.reverse()
    if any(b[0] <= a[0] for a, b in zip(out, out[1:])):
        raise ValueError("A section must run consistently along X/KP; vertical or folded lines are unsupported.")
    return out


def read_dxf(path, progress=None):
    """Read model-space ASCII LINE / straight 2D polylines, with diagnostics.

    Narrow by design: block transforms, OCS rotations, curves, closed shapes
    and 3D coordinates must not quietly become plausible but incorrect picks.
    """
    original_stat = os.stat(path)
    size = original_stat.st_size
    if size > MAX_FILE_BYTES:
        raise ValueError(f"DXF exceeds the {MAX_FILE_BYTES // (1024 * 1024)} MiB safety budget.")
    digest = hashlib.sha256()
    checkpoint(progress, message="Reading DXF…")
    layers, annotations, skipped = {}, {}, Counter()
    annotation_count = annotation_chars = 0
    encoding = "utf-8"
    units = 0
    section = ""
    polyline = None
    count = 0

    def add(kind, attrs, vertices=None):
        nonlocal count
        values = dict(attrs)
        layer = values.get(8, "0")
        try:
            if int(values.get(67, "0")) == 1:
                return  # paperspace annotations are not section geometry
            if (float(values.get(210, 0)) != 0 or float(values.get(220, 0)) != 0
                    or float(values.get(230, 1)) != 1):
                raise ValueError("rotated object coordinates")
            if any(float(v) != 0 for c, v in attrs if c in (30, 31, 38, 39, 42)):
                raise ValueError("3D, thickness or curved geometry")
            if kind != "LINE" and int(values.get(70, "0")) != 0:
                # Bit 128 is only a linetype-generation flag.
                if int(values[70]) != 128:
                    raise ValueError("closed or non-2D polyline")
            if kind == "LINE":
                points = [(values[10], values[20]), (values[11], values[21])]
            elif kind == "LWPOLYLINE":
                xs = [v for c, v in attrs if c == 10]
                ys = [v for c, v in attrs if c == 20]
                if len(xs) != len(ys) or int(values.get(90, len(xs))) != len(xs):
                    raise ValueError("incomplete polyline")
                points = list(zip(xs, ys))
            else:
                points = []
                for i, vertex in enumerate(vertices or []):
                    checkpoint(progress, i)
                    v = dict(vertex)
                    if (float(v.get(30, 0)) != 0 or float(v.get(42, 0)) != 0
                            or int(v.get(70, 0)) != 0):
                        raise ValueError("3D or curved polyline")
                    points.append((v[10], v[20]))
            points = list(_points(points, progress))
            checked_segment(points, progress)  # keep source vertex order as delivered
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            skipped[f"{layer}: {kind} — {exc}"] += 1
            return
        count += len(points)
        if count > MAX_POINTS:
            raise ValueError(f"DXF exceeds {MAX_POINTS:,} section vertices (the import memory budget).")
        layers.setdefault(layer, []).append({"handle": values.get(5, ""), "points": points})
        if len(layers) > MAX_HORIZONS:
            raise ValueError(f"DXF exceeds {MAX_HORIZONS} section layers; export just the interpreted horizons.")

    def add_annotation(kind, attrs):
        nonlocal annotation_count, annotation_chars
        layer = dict(attrs).get(8, "0")
        try:
            entity = dxf_annotations.read_entity(kind, attrs)
            if entity is None:
                return
            if (annotation_count >= dxf_annotations.MAX_LABELS
                    or annotation_chars + len(entity["raw_text"]) > dxf_annotations.MAX_TOTAL_TEXT
                    or (layer not in annotations and len(annotations) >= dxf_annotations.MAX_GROUPS)):
                raise ValueError("annotation memory budget exceeded; remaining labels omitted")
            annotations.setdefault(layer, []).append(entity)
            annotation_count += 1
            annotation_chars += len(entity["raw_text"])
        except (KeyError, ValueError, TypeError, OverflowError) as exc:
            skipped[f"{layer}: {kind} — {exc}"] += 1

    def records(handle):
        kind, attrs = "", []
        pairs, consumed, coordinates = 0, 0, 0
        retained = {1, 2, 3, 5, 8, 9, 10, 11, 20, 21, 30, 31, 38, 39, 42, 67, 70,
                    71, 72, 73, 75, 90, 210, 220, 230}
        while True:
            # Limit a single corrupt line before allocating unbounded memory.
            line = handle.readline(65537)
            if not line:
                break
            value = handle.readline(65537)
            if len(line) > 65536 or len(value) > 65536:
                raise ValueError("DXF contains an excessively long group-code line.")
            if not value:
                raise ValueError("Incomplete DXF group-code pair.")
            digest.update(line)
            digest.update(value)
            consumed += len(line) + len(value)
            if consumed > MAX_FILE_BYTES:
                raise ValueError("DXF grew beyond the file safety budget while reading.")
            checkpoint(progress, pairs, min(99, 100 * consumed / max(1, size)), "Reading DXF…")
            pairs += 1
            try:
                code = int(line[3:] if line.startswith(b"\xef\xbb\xbf") else line)
            except ValueError as exc:
                raise ValueError("Expected an ASCII DXF group code.") from exc
            value = value.decode(encoding, errors="replace").rstrip("\r\n")
            if code not in (1, 3):
                value = value.strip()
            if code == 0:
                yield kind, attrs
                kind, attrs, coordinates = value, [], 0
            elif code in retained:
                attrs.append((code, value))
                if code == 10:
                    coordinates += 1
                if coordinates > MAX_POINTS or len(attrs) > MAX_POINTS * 4:
                    raise ValueError("A DXF entity exceeds the vertex/attribute memory budget.")
        yield kind, attrs

    with open(path, "rb") as handle:
        if b"Binary DXF" in handle.read(32):
            raise ValueError("Binary DXF is unsupported; export an ASCII DXF section.")
        handle.seek(0)
        for kind, attrs in records(handle):
            if kind == "SECTION":
                section = dict(attrs).get(2, "")
                if section == "HEADER":
                    header = {value: attrs[i + 1][1] for i, (code, value) in enumerate(attrs[:-1]) if code == 9}
                    page = header.get("$DWGCODEPAGE", "")
                    if page.startswith("ANSI_") and header.get("$ACADVER", "") < "AC1021":
                        candidate = "cp" + page[5:]
                        try:
                            codecs.lookup(candidate)
                            encoding = candidate
                        except LookupError:
                            skipped["unsupported DXF text code page"] += 1
                    for i, pair in enumerate(attrs[:-1]):
                        if pair == (9, "$INSUNITS"):
                            try:
                                units = int(attrs[i + 1][1])
                            except ValueError:
                                pass
                continue
            if kind == "ENDSEC":
                section = ""
            if section != "ENTITIES":
                continue
            if kind == "VERTEX" and polyline is not None:
                polyline[1].append(attrs)
                if len(polyline[1]) > MAX_POINTS:
                    raise ValueError("A DXF polyline exceeds the vertex memory budget.")
                continue
            if kind == "SEQEND" and polyline is not None:
                add("POLYLINE", *polyline)
                polyline = None
                continue
            if polyline is not None:
                skipped["POLYLINE missing SEQEND"] += 1
                polyline = None
            if kind == "POLYLINE":
                polyline = (attrs, [])
            elif kind in ("LINE", "LWPOLYLINE"):
                add(kind, attrs)
            elif kind in ("TEXT", "MTEXT"):
                add_annotation(kind, attrs)
            elif kind in ("ARC", "CIRCLE", "SPLINE", "ELLIPSE", "INSERT", "HATCH", "3DFACE",
                          "ATTRIB", "ATTDEF", "MULTILEADER", "DIMENSION"):
                skipped[f"{dict(attrs).get(8, '0')}: unsupported {kind}"] += 1
    final_stat = os.stat(path)
    if (original_stat.st_size, original_stat.st_mtime_ns) != (final_stat.st_size, final_stat.st_mtime_ns):
        raise ValueError("DXF changed while reading. Try again after it has finished saving.")
    if polyline is not None:
        skipped["POLYLINE missing SEQEND"] += 1
    warnings = [f"{n} × {reason}" for reason, n in skipped.items()]
    if not layers and not annotations:
        raise ValueError("No supported 2D section lines or annotations found. " + "; ".join(warnings[:5]))
    checkpoint(progress, percent=100, message=f"Read {count:,} vertices")
    return {"layers": layers, "annotations": annotations, "units": units, "warnings": warnings,
            "vertex_count": count, "sha256": digest.hexdigest(), "size_bytes": size}


@dataclass
class ImportOptions:
    x_unit_m: float = 1.0
    x_scale: float = 1.0
    x_origin: float = 0.0
    kp_origin: float = 0.0
    reverse_x: bool = False
    y_unit_m: float = 1.0
    y_origin: float = 0.0
    depth_offset_m: float = 0.0
    exaggeration: float = 1.0
    positive_up: bool = False
    reference: str = "seabed"  # seabed | datum
    datum: str = ""

    def _factors(self):
        for value in (self.x_unit_m, self.x_scale, self.y_unit_m, self.exaggeration):
            if _number(value) <= 0:
                raise ValueError("Units, scale and exaggeration must be greater than zero.")
        if self.reference not in ("seabed", "datum"):
            raise ValueError("Choose a vertical reference.")
        if self.reference == "datum" and not self.datum.strip():
            raise ValueError("Name the shared vertical datum (for example MSL or LAT).")
        sx = (-1 if self.reverse_x else 1) * self.x_unit_m * self.x_scale / 1000.0
        sy = (-1 if self.positive_up else 1) * self.y_unit_m / self.exaggeration
        return sx, sy

    def transform_point(self, point):
        sx, sy = self._factors()
        x, y = point
        return [_number(self.kp_origin + (_number(x) - self.x_origin) * sx),
                _number(self.depth_offset_m + (_number(y) - self.y_origin) * sy)]

    def transform(self, points, progress=None):
        sx, sy = self._factors()
        out = []
        for i, (x, y) in enumerate(_points(points, progress)):
            checkpoint(progress, i, 100 * i / max(1, len(points)))
            out.append((self.kp_origin + (x - self.x_origin) * sx,
                        self.depth_offset_m + (y - self.y_origin) * sy))
        return checked_segment(out, progress)


def remap_segment(points, kp_map, progress=None, anchor_xs=None):
    """Retain bends at KP-map anchors, including along a two-vertex DXF line."""
    xs = [p[0] for p in points]
    anchors = anchor_xs if anchor_xs is not None else [a[0] for a in kp_map.anchors]
    extra = anchors[bisect.bisect_right(anchors, xs[0]):bisect.bisect_left(anchors, xs[-1])]
    out = []
    knots = sorted(set(xs + extra))
    for i, kp in enumerate(knots):
        checkpoint(progress, i, 100 * i / max(1, len(knots)))
        out.append([kp_map.map_kp(kp)[0], interpolate(points, xs, kp)])
    return checked_segment(out, progress)


def interpolate(points, xs, kp):
    i = min(max(bisect.bisect_right(xs, kp) - 1, 0), len(points) - 2)
    a, b = points[i], points[i + 1]
    return a[1] + (b[1] - a[1]) * (kp - a[0]) / (b[0] - a[0])


def reference_segments(profile, progress=None):
    """Bathymetry split at no-data and source seams; never bridge either."""
    out, current = [], []
    sources = profile.source_ids or [None] * len(profile.kps)
    previous = None
    for i, (kp, depth, source) in enumerate(zip(profile.kps, profile.depths, sources)):
        checkpoint(progress, i)
        if depth is None or not math.isfinite(depth) or (current and source != previous):
            if len(current) >= 2:
                out.append(checked_segment(current))
            current = []
        if depth is not None and math.isfinite(depth):
            current.append([kp, depth])
        previous = source
    if len(current) >= 2:
        out.append(checked_segment(current))
    return out


def subtract_seabed(segments, reference, progress=None):
    """Piecewise-linear difference, clipped to known, unambiguous seabed."""
    refs = sorted((checked_segment(s, progress) for s in reference), key=lambda s: s[0][0])
    if not refs:
        raise ValueError("No usable seabed reference. Select a seabed DXF layer or sample bathymetry first.")
    for a, b in zip(refs, refs[1:]):
        if b[0][0] < a[-1][0] or (b[0][0] == a[-1][0] and abs(b[0][1] - a[-1][1]) > 1e-6):
            raise ValueError("Seabed reference segments overlap or disagree; choose one interpretation.")
    ends = [s[-1][0] for s in refs]
    indexes = [[p[0] for p in s] for s in refs]
    out, count = [], 0
    for segment in segments:
        checkpoint(progress)
        xs = [p[0] for p in segment]
        index = bisect.bisect_right(ends, xs[0])
        while index < len(refs) and refs[index][0][0] < xs[-1]:
            checkpoint(progress, index)
            ref, rx = refs[index], indexes[index]
            lo, hi = max(xs[0], rx[0]), min(xs[-1], rx[-1])
            if lo < hi:
                knots = sorted(set([lo, hi] + xs[bisect.bisect_right(xs, lo):bisect.bisect_left(xs, hi)]
                                   + rx[bisect.bisect_right(rx, lo):bisect.bisect_left(rx, hi)]))
                count += len(knots)
                if count > MAX_POINTS:
                    raise ValueError("Seabed conversion produces too many vertices for the memory budget.")
                converted = []
                for j, kp in enumerate(knots):
                    checkpoint(progress, j)
                    converted.append([kp, interpolate(segment, xs, kp) - interpolate(ref, rx, kp)])
                out.append(converted)
            index += 1
    return out


def build_horizons(drawing, selected, options, kp_map, reference=None, source=None, progress=None):
    """One named horizon per selected layer, retaining every entity as a segment."""
    rows, warnings = [], list(drawing["warnings"])
    point_count = processed = converted_count = 0
    total = sum(len(e["points"]) for layer in selected for e in drawing["layers"][layer])
    anchor_xs = [a[0] for a in kp_map.anchors]
    for i, layer in enumerate(selected):
        entities = drawing["layers"][layer]
        segments = []
        for entity in entities:
            checkpoint(progress, percent=90 * processed / max(1, total), message=f"Converting {layer}…")
            def phase(start, end):
                def report(percent, message):
                    value = None if percent is None else (
                        90 * (processed + len(entity["points"]) * (start + (end - start) * percent / 100))
                        / max(1, total))
                    checkpoint(progress, percent=value, message=message)
                return report
            transformed = options.transform(entity["points"], phase(0, 0.5))
            segments.append(remap_segment(transformed, kp_map, phase(0.5, 1), anchor_xs))
            processed += len(entity["points"])
            converted_count += len(segments[-1])
            if converted_count > MAX_POINTS:
                raise ValueError("Converted horizons exceed the vertex memory budget.")
        length = sum(s[-1][0] - s[0][0] for s in segments)
        if options.reference == "datum":
            segments = subtract_seabed(segments, reference or [], progress)
            covered = sum(s[-1][0] - s[0][0] for s in segments)
            if covered < length - 1e-9:
                warnings.append(f"{layer}: missing seabed coverage left gaps ({length - covered:.3f} km).")
        if not segments:
            warnings.append(f"{layer}: no overlap with the seabed reference; not imported.")
            continue
        point_count += sum(len(s) for s in segments)
        if point_count > MAX_POINTS:
            raise ValueError("Converted horizons exceed the vertex memory budget.")
        if any(y < -1e-6 for s in segments for _x, y in s):
            warnings.append(f"{layer}: points above seabed; check sign, datum and seabed reference.")
        rows.append({"id": schema.new_id(), "name": layer, "colour": COLOURS[i % len(COLOURS)],
                     "visible": True, "segments": segments,
                     # Source geometry is immutable; editors replace working segments.
                     "source_segments": entities,
                     "source": dict(source or {}, options=asdict(options), kp_map=kp_map.to_dict())})
    if not rows:
        raise ValueError("Select at least one horizon with usable coverage.")
    checkpoint(progress, percent=95, message="Checking horizons…")
    validate_rows(rows, progress)
    checkpoint(progress, percent=100, message="Preview ready")
    return rows, warnings


def validate_rows(rows, progress=None):
    if not isinstance(rows, list):
        raise ValueError("Invalid saved horizon list.")
    if len(rows) > MAX_HORIZONS:
        raise ValueError(f"A plan can hold at most {MAX_HORIZONS} horizon overlays.")
    count, ids = 0, set()
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                or not row["id"] or row["id"] in ids):
            raise ValueError("Invalid or duplicate horizon identifier.")
        ids.add(row["id"])
        if (not isinstance(row.get("name"), str) or not row["name"].strip()
                or not isinstance(row.get("segments"), list) or not row["segments"]):
            raise ValueError("Each horizon needs a name and at least one segment.")
        if not isinstance(row.get("source", {}), dict) or not isinstance(row.get("colour", ""), str):
            raise ValueError("Invalid horizon metadata.")
        for segment in row["segments"]:
            if len(segment) < 2:
                raise ValueError("A horizon segment needs at least two different points.")
            previous = None
            for i, point in enumerate(_points(segment, progress)):
                x, _y = point
                if point != segment[i]:
                    raise ValueError("Saved horizon coordinates must be numeric pairs.")
                if previous is not None and x <= previous:
                    raise ValueError("Saved horizon KPs must increase within each segment.")
                previous = x
            count += len(segment)
        for entity in row.get("source_segments", []):
            count += len(entity["points"])
            for _point in _points(entity["points"], progress):
                pass
        json.dumps({k: v for k, v in row.items() if k not in ("segments", "source_segments")}, allow_nan=False)
        checkpoint(progress)
    if count > MAX_POINTS * 2:
        raise ValueError(f"Saved horizons exceed {MAX_POINTS * 2:,} vertices including originals (the plan memory budget).")


def plan_rows(plan):
    """No writes on read. Refuse unknown formats rather than overwrite them."""
    try:
        params = json.loads(plan.get("params_json") or "{}")
        if not isinstance(params, dict):
            raise ValueError("Invalid plan settings.")
        state = params.get(PARAMS_KEY)
        if state is None:
            return []
        if not isinstance(state, dict) or state.get("version") != 1:
            raise ValueError("Unsupported saved horizon format; this version cannot edit it.")
        rows = state["rows"]
        validate_rows(rows)
        return rows
    except (KeyError, TypeError, OverflowError) as exc:
        raise ValueError("Invalid saved horizon data.") from exc


def plot_arrays(segments):
    xs, ys = [], []
    for segment in segments:
        xs.extend(p[0] for p in segment)
        ys.extend(p[1] for p in segment)
        xs.append(float("nan"))
        ys.append(float("nan"))
    return xs, ys


def parse_vertices(text):
    """Editable KP/depth pairs; blank lines preserve interpretation gaps."""
    segments, points = [], []
    for line in text.splitlines() + [""]:
        if not line.strip():
            if points:
                segments.append(checked_segment(points))
                points = []
            continue
        fields = line.replace(",", " ").split()
        if len(fields) != 2:
            raise ValueError("Enter KP (km), depth below seabed (m), one pair per line; blank lines separate segments.")
        points.append(fields)
    if not segments:
        raise ValueError("Enter at least one segment.")
    return segments

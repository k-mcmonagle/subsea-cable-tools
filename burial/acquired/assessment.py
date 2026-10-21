# -*- coding: utf-8 -*-
"""An assessment: chosen datasets against the plan's target, on the plan's KPs.

:func:`build` (background-safe, no Qt) reads each dataset's records after
its overlap rules (:meth:`.store.StoreReader.effective`), moves their KPs
onto the plan's route when the dataset was referenced to another RPL (a
:class:`..kp_rereference.KpMap` built on the main thread from the
Workbench geometry), bins DOL on the 1 m grid (:mod:`.bins`), takes the
governing dataset per bin (latest by default), classifies against the
target runs and works out the statistics, the below-target ranges, the
gaps, per-section figures and the stretches the status and source bars
draw. Every bin keeps the record behind its worst value, so a click can
name the file, line and time (:func:`provenance`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from ..kp_rereference import KpMap
from . import bins as B
from . import roles as R


@dataclass
class DatasetSpec:
    dataset_id: str
    name: str
    kp_map: Optional[Dict] = None        # KpMap.to_dict(); None: KPs are on the plan's route already
    kp_note: str = ""                    # how the KPs were placed (for the notes)
    bridge_m: Optional[float] = None     # None: the dataset's own default


@dataclass
class Request:
    datasets: List[DatasetSpec]          # in order: ties go to the first
    mode: str = B.MODE_LATEST
    overrides: Sequence[Tuple[float, float, int]] = ()
    target_runs: Sequence[Tuple[float, float, Optional[float]]] = ()
    scope_km: Optional[Tuple[float, float]] = None
    step_m: float = 1.0
    tolerance_m: float = 0.0
    join_gap_m: float = 0.0
    channels: Sequence[str] = ()         # extra channels to load for the plots
    sections: Sequence[Dict] = ()        # plan sections: start_kp, end_kp, kind, section_id
    notes: List[str] = field(default_factory=list)   # the caller's notes (e.g. datasets left out)


@dataclass
class Series:
    """One dataset's records on the plan's KPs, in delivery order."""
    spec: DatasetSpec
    kp: np.ndarray
    dol: np.ndarray
    time: np.ndarray
    file_index: np.ndarray
    line: np.ndarray
    files: List[Dict]
    channels: Dict[str, np.ndarray]
    events: List[str]
    binset: B.BinSet
    extrapolated: int = 0
    in_gap: int = 0

    def plot_order(self) -> np.ndarray:
        """Indices in KP order (for drawing a line along the route)."""
        return np.argsort(self.kp, kind="stable")


@dataclass
class Assessment:
    request: Request
    series: List[Series]
    selection: B.Selection
    target: np.ndarray
    status: np.ndarray
    stats: Dict
    ranges: List[Dict]
    gaps: List[Tuple[float, float]]
    status_runs: Tuple[np.ndarray, np.ndarray, np.ndarray]
    source_runs: Tuple[np.ndarray, np.ndarray, np.ndarray]
    sections: List[Dict] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def extent_km(self) -> Optional[Tuple[float, float]]:
        if self.request.scope_km is not None:
            return tuple(sorted(self.request.scope_km))
        if not len(self.selection):
            return None
        return float(self.selection.kp_km[0]), float((self.selection.index[-1] + 1) * self.selection.step_m / 1000.0)

    def bin_at(self, kp_km: float) -> Optional[int]:
        """Position of the selected bin holding ``kp_km``, or None."""
        if not len(self.selection):
            return None
        j = int(math.floor(kp_km * 1000.0 / self.selection.step_m))
        k = int(np.searchsorted(self.selection.index, j))
        if k < len(self.selection) and self.selection.index[k] == j:
            return k
        return None


def _effective(reader, spec: DatasetSpec, channels: Sequence[str]) -> Tuple[Dict, int, int, List[str]]:
    wanted = list(dict.fromkeys([R.DOL, *channels, R.EVENT]))
    data = reader.effective(spec.dataset_id, wanted)
    notes: List[str] = []
    extrapolated = in_gap = 0
    kp = np.asarray(data[R.KP], dtype=float)
    if spec.kp_map is not None:
        kp_map = KpMap.from_dict(spec.kp_map)
        kp, flag_extra, flag_gap = kp_map.map_array(kp)
        extrapolated, in_gap = int(flag_extra.sum()), int(flag_gap.sum())
        if extrapolated:
            notes.append(f"{spec.name}: {extrapolated:,} records lie beyond the matched routes' ends "
                         "(their KPs are extrapolated).")
        if in_gap:
            notes.append(f"{spec.name}: {in_gap:,} records lie where the routes diverge (KPs interpolated).")
    data[R.KP] = kp
    if spec.kp_note:
        notes.insert(0, f"{spec.name}: {spec.kp_note}")
    return data, extrapolated, in_gap, notes


def _runs_from_bins(selection: B.Selection, level: np.ndarray, gaps: Sequence[Tuple[float, float]],
                    gap_level: int, extent: Optional[Tuple[float, float]]
                    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """KP stretches (km) for a bar: one per bin within ``extent`` (cut to it), plus the gaps, in KP order."""
    step = selection.step_m / 1000.0
    lo = np.concatenate([selection.index * step, [g[0] for g in gaps]])
    hi = np.concatenate([(selection.index + 1) * step, [g[1] for g in gaps]])
    lv = np.concatenate([np.asarray(level, dtype=np.int64), np.full(len(gaps), gap_level, dtype=np.int64)])
    if extent is not None:
        a, b = sorted(extent)
        inside = (hi > a) & (lo < b)
        lo, hi, lv = np.maximum(lo[inside], a), np.minimum(hi[inside], b), lv[inside]
    order = np.argsort(lo, kind="stable")
    return lo[order], hi[order], lv[order]


def build(reader, request: Request, cancelled: Optional[Callable[[], bool]] = None,
          progress: Optional[Callable[[float, str], None]] = None) -> Assessment:
    """The assessment for ``request`` from the data file behind ``reader``."""
    if not request.datasets:
        raise ValueError("Choose at least one dataset to assess.")
    series: List[Series] = []
    notes: List[str] = []
    for index, spec in enumerate(request.datasets):
        if cancelled and cancelled():
            raise InterruptedError("Assessment cancelled")
        if progress:
            progress(index / (len(request.datasets) + 1), f"Reading {spec.name}")
        data, extrapolated, in_gap, more = _effective(reader, spec, request.channels)
        notes.extend(more)
        binset = B.bin_samples(data[R.KP], data[R.DOL], data[R.TIME], request.step_m, spec.bridge_m)
        events = data.get(R.EVENT)
        series.append(Series(
            spec=spec, kp=np.asarray(data[R.KP], dtype=float), dol=np.asarray(data[R.DOL], dtype=float),
            time=np.asarray(data[R.TIME], dtype=float), file_index=np.asarray(data["file_index"]),
            line=np.asarray(data["line"]), files=data["files"],
            channels={key: np.asarray(data[key], dtype=float) for key in request.channels if key in data
                      and not isinstance(data[key], list)},
            events=list(events) if isinstance(events, list) else [""] * len(data["line"]),
            binset=binset, extrapolated=extrapolated, in_gap=in_gap))
    if progress:
        progress(len(request.datasets) / (len(request.datasets) + 1), "Assessing")
    selection = B.select([s.binset for s in series], request.mode, request.overrides)
    target = B.target_at(selection.centre_km, request.target_runs)
    status = B.classify(selection, target, request.tolerance_m)
    stats = B.summarise(selection, status, request.scope_km)
    ranges = B.below_ranges(selection, status, target, request.join_gap_m, request.scope_km)
    extent = request.scope_km
    if extent is None and len(selection):
        extent = (float(selection.kp_km[0]), float((selection.index[-1] + 1) * selection.step_m / 1000.0))
    gaps = B.no_data_ranges(selection, extent) if extent is not None else []
    if not any(depth is not None for _a, _b, depth in request.target_runs):
        notes.append("The plan has no target depth: set one on the Plan tab to assess against it.")
    sections = []
    for row in request.sections:
        try:
            lo, hi = sorted((float(row["start_kp"]), float(row["end_kp"])))
        except (KeyError, TypeError, ValueError):
            continue
        part = B.summarise(selection, status, (lo, hi))
        part.update(section_id=row.get("section_id", ""), kind=row.get("kind", ""), start_kp=lo, end_kp=hi,
                    ranges=len(B.below_ranges(selection, status, target, request.join_gap_m, (lo, hi))))
        sections.append(part)
    status_runs = _runs_from_bins(selection, status, gaps, B.STATUS_NO_DATA, request.scope_km)
    source_runs = _runs_from_bins(selection, selection.source, gaps, -1, request.scope_km)
    if progress:
        progress(1.0, "Assessed")
    return Assessment(request, series, selection, target, status, stats, ranges, gaps, status_runs, source_runs,
                      sections, notes)


def utc_text(seconds: Optional[float]) -> str:
    if seconds is None or not math.isfinite(seconds):
        return ""
    return datetime.fromtimestamp(float(seconds), timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def provenance(assessment: Assessment, kp_km: float) -> Optional[Dict]:
    """What governs the bin at ``kp_km``: dataset, file, line, time, values, target and status."""
    k = assessment.bin_at(kp_km)
    if k is None:
        return None
    sel = assessment.selection
    series = assessment.series[int(sel.source[k])]
    i = int(sel.sample[k])
    file_row = series.files[int(series.file_index[i])] if len(series.files) else {}
    target = assessment.target[k]
    return {
        "kp_from": float(sel.index[k] * sel.step_m / 1000.0),
        "kp_to": float((sel.index[k] + 1) * sel.step_m / 1000.0),
        "dol": float(sel.worst[k]),
        "mean": float(sel.mean[k]),
        "target": None if not math.isfinite(target) else float(target),
        "status": int(assessment.status[k]),
        "dataset": series.spec.name,
        "dataset_id": series.spec.dataset_id,
        "file": file_row.get("name", ""),
        "file_id": file_row.get("file_id", ""),
        "line": int(series.line[i]),
        "time": utc_text(series.time[i]),
        "record_kp": float(series.kp[i]),
        "others": int(sel.others[k]),
        "event": series.events[i] if i < len(series.events) else "",
    }


def channel_labels(assessment: Assessment) -> Dict[str, Tuple[str, str]]:
    """``{channel key: (label, unit)}`` as recorded when the files were imported."""
    out: Dict[str, Tuple[str, str]] = {}
    for series in assessment.series:
        for row in series.files:
            for channel in row.get("channels") or []:
                out.setdefault(channel.get("key", ""), (channel.get("label", ""), channel.get("unit", "")))
    return out


def listing_count(assessment: Assessment, step_out_m: float) -> int:
    """How many rows the listing at ``step_out_m`` has."""
    index = assessment.selection.index
    if not index.size:
        return 0
    ratio = int(round(step_out_m / assessment.selection.step_m))
    return int(np.unique(np.floor_divide(index, ratio)).size)


def listing_rows(assessment: Assessment, step_out_m: float, method: str = B.METHOD_WORST,
                 channels: Sequence[str] = (), limit: Optional[int] = None) -> Tuple[List[str], List[List]]:
    """``(header, rows)`` of the listing at ``step_out_m``: KP range, DOL, target, status, and the record.

    ``limit`` formats only the first rows (a preview); the listing itself is the same.
    """
    rows_out = B.listing(assessment.selection, step_out_m, method, assessment.target)
    if limit is not None:
        rows_out = {key: values[:limit] for key, values in rows_out.items()}
    header = ["KP from", "KP to", f"DOL ({'worst' if method == B.METHOD_WORST else 'mean'}, m)", "Target (m)",
              "Status", "Covered (m)", "Dataset", "File", "Line", "Time (UTC)", "Record KP"]
    labels = channel_labels(assessment)
    for key in channels:
        label, unit = labels.get(key, (key, ""))
        header.append(f"{label} ({unit})" if unit else label)
    out: List[List] = []
    for r in range(len(rows_out["kp_from"])):
        series = assessment.series[int(rows_out["source"][r])]
        i = int(rows_out["sample"][r])
        file_row = series.files[int(series.file_index[i])] if len(series.files) else {}
        target = rows_out["target"][r]
        row = [round(float(rows_out["kp_from"][r]), 6), round(float(rows_out["kp_to"][r]), 6),
               round(float(rows_out["value"][r]), 3), "" if not math.isfinite(target) else round(float(target), 3),
               B.STATUS_NAMES[int(rows_out["status"][r])], round(float(rows_out["cover_m"][r]), 2),
               series.spec.name, file_row.get("name", ""), int(series.line[i]), utc_text(series.time[i]),
               round(float(series.kp[i]), 6)]
        for key in channels:
            values = series.channels.get(key)
            row.append("" if values is None or not math.isfinite(values[i]) else round(float(values[i]), 4))
        out.append(row)
    return header, out

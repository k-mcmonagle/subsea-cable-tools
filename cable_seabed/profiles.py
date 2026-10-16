# -*- coding: utf-8 -*-
"""Seabed profiles without a map: a CSV file, or a synthetic seabed for studies."""

from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

from .planning import Profile


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


_KP_NAMES = ("kp", "kpkm", "kilometrepost", "kilometerpost")
_DIST_NAMES = ("chainage", "chainagem", "distance", "distancem", "dist", "distm", "x", "xm", "kpm")
_DEPTH_NAMES = ("depth", "depthm", "waterdepth", "wd", "seabeddepth", "d")
_ELEV_NAMES = ("elevation", "elevationm", "elev", "z", "zm", "seabed", "seabedelevation", "height")


def read_profile_csv(path: str) -> Profile:
    """``(KP km or chainage m, depth or elevation)`` columns from a CSV file.

    The header names the columns: KP (km) or chainage / distance (m), and
    depth (positive down) or elevation / z (positive up; converted to depth).
    Without a header the first column is KP (km) and the second depth (m).
    Rows without both numbers are skipped.
    """
    with open(path, newline="", encoding="utf-8-sig") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t ")
        except csv.Error:
            dialect = csv.excel
        rows = [row for row in csv.reader(handle, dialect) if any(cell.strip() for cell in row)]
    if not rows:
        raise ValueError("The file is empty.")
    header = [_norm(cell) for cell in rows[0]]

    def is_number(text: str) -> bool:
        try:
            float(text)
            return True
        except ValueError:
            return False

    has_header = not all(is_number(cell) for cell in rows[0][:2] if cell.strip())
    notes: List[str] = []
    if has_header:
        body = rows[1:]
        pos_col = next((i for i, h in enumerate(header) if h in _KP_NAMES), None)
        pos_in_m = False
        if pos_col is None:
            pos_col = next((i for i, h in enumerate(header) if h in _DIST_NAMES), None)
            pos_in_m = pos_col is not None
        depth_col = next((i for i, h in enumerate(header) if h in _DEPTH_NAMES), None)
        elev_col = None
        if depth_col is None:
            elev_col = next((i for i, h in enumerate(header) if h in _ELEV_NAMES), None)
        if pos_col is None or (depth_col is None and elev_col is None):
            raise ValueError("Name the columns KP (km) or Chainage (m), and Depth (m) or Elevation (m).")
    else:
        body = rows
        pos_col, depth_col, elev_col, pos_in_m = 0, 1, None, False
        notes.append("No header: column 1 read as KP (km), column 2 as depth (m, positive down).")
    pos: List[float] = []
    depth: List[float] = []
    for row in body:
        col = depth_col if depth_col is not None else elev_col
        if max(pos_col, col) >= len(row):
            continue
        try:
            p = float(row[pos_col])
            d = float(row[col])
        except ValueError:
            continue
        if not (math.isfinite(p) and math.isfinite(d)):
            continue
        pos.append(p)
        depth.append(-d if elev_col is not None else abs(d))
    if len(pos) < 3:
        raise ValueError("Fewer than three rows with a position and a depth.")
    p = np.asarray(pos, dtype=float)
    d = np.asarray(depth, dtype=float)
    order = np.argsort(p, kind="stable")
    p, d = p[order], d[order]
    keep = np.concatenate(([True], np.diff(p) > 0))
    p, d = p[keep], d[keep]
    kp = p / 1000.0 if pos_in_m else p
    x = (kp - kp[0]) * 1000.0
    notes.append(f"Profile read from a CSV file: {len(x):,} stations, KP {kp[0]:.3f} to {kp[-1]:.3f}.")
    return Profile(x, kp, d, notes=notes, source="CSV file")


@dataclass
class SyntheticSeabed:
    """A generated seabed for studies and teaching."""

    length_m: float = 1000.0
    spacing_m: float = 0.5
    start_kp: float = 0.0
    depth_m: float = 100.0
    slope_deg: float = 0.0            # deepening with KP
    wave_height_m: float = 0.0        # sand waves (crest to trough)
    wave_length_m: float = 50.0
    roughness_m: float = 0.0          # random relief (standard deviation)
    roughness_length_m: float = 5.0   # correlation length of the random relief
    seed: int = 1


def synthetic_profile(spec: SyntheticSeabed) -> Profile:
    length = max(float(spec.length_m), 10.0)
    step = min(max(float(spec.spacing_m), 0.01), length / 3.0)
    count = int(math.floor(length / step)) + 1
    x = np.arange(count) * step
    depth = spec.depth_m + x * math.tan(math.radians(spec.slope_deg))
    if spec.wave_height_m > 0 and spec.wave_length_m > 0:
        depth = depth - 0.5 * spec.wave_height_m * np.sin(2.0 * math.pi * x / spec.wave_length_m)
    if spec.roughness_m > 0:
        rng = np.random.default_rng(int(spec.seed))
        noise = rng.normal(0.0, 1.0, count)
        width = max(int(round(spec.roughness_length_m / step)), 1)
        if width > 1:
            kernel = np.ones(width) / width
            noise = np.convolve(noise, kernel, mode="same")
            std = float(np.std(noise))
            noise = noise / std if std > 0 else noise
        depth = depth + spec.roughness_m * noise
    kp = spec.start_kp + x / 1000.0
    notes = [f"Synthetic seabed: {length:.0f} m, {spec.depth_m:.0f} m deep, slope {spec.slope_deg:.1f}°"
             + (f", sand waves {spec.wave_height_m:.1f} m / {spec.wave_length_m:.0f} m" if spec.wave_height_m else "")
             + (f", roughness {spec.roughness_m:.2f} m" if spec.roughness_m else "") + "."]
    return Profile(x, kp, depth, notes=notes, source="Synthetic seabed")


def profile_from_samples(kp_km, depth_m, notes: Optional[List[str]] = None, source: str = "",
                         turns: Optional[List[Tuple[float, float]]] = None) -> Profile:
    """A profile from sampled ``(KP, depth)`` with chainage from the KPs."""
    kp = np.asarray(kp_km, dtype=float)
    depth = np.asarray(depth_m, dtype=float)
    x = (kp - kp[0]) * 1000.0 if len(kp) else kp
    return Profile(x, kp, depth, turns=list(turns or []), notes=list(notes or []), source=source)

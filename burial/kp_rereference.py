# -*- coding: utf-8 -*-
"""KP re-referencing between RPL revisions (pure python).

Project documents — ground models, Burial Assessment Studies, crossing
schedules, client mark-ups — quote KPs against whichever RPL revision was
current when they were written. The working route moves on; a KP quoted
against Rev B is not the same place on Rev D once a re-route, a shortened
landing or a straightened leg has shifted the chainage downstream of it.

This module owns the *mapping*, never the geometry: a ``KpMap`` is a
monotone piecewise-linear function ``kp_source -> kp_target`` defined by
anchor pairs, plus the diagnostics that say where the mapping is trusted
and where it is an assumption. Anchors come from one of three places:

* **Geometry** (``build_from_samples``): the source route is walked at a
  fixed step, every station is projected onto the target route and the
  (source KP, target KP, offset) triples become anchors. Stations whose
  offset exceeds the tolerance lie on a re-routed stretch and are dropped
  — the mapping interpolates linearly across the gap and flags it. The
  QGIS-side sampler lives in ``rereference_qgis.py``; this module only
  sees plain numbers so it stays testable without QGIS.
* **Matched positions** (``KpMap.from_anchors``): the engineer pairs KPs
  that name the same physical place in both revisions (a BMH, a crossing,
  an alter-course), typically from the RPL change note.
* **A constant shift** (``KpMap.shift``): the degenerate case where a
  revision only re-numbered the start.

Beyond the last anchor on either side the mapping extrapolates with unit
slope (the last known offset carried forward) and flags the result
``extrapolated``. Ranges are mapped endpoint by endpoint; a range whose
mapped length differs from its source length by more than the stretch
tolerance is flagged ``stretched`` so an engineer sees which units the
re-route actually deformed. Nothing here is silently "corrected" — every
mapped value carries its flags, and the map itself serialises for the
plan audit trail.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

METHOD_IDENTITY = "identity"
METHOD_SHIFT = "shift"
METHOD_ANCHORS = "anchors"
METHOD_GEOMETRY = "geometry"

METHOD_LABELS: Dict[str, str] = {
    METHOD_IDENTITY: "Same route (no change)",
    METHOD_SHIFT: "Constant KP shift",
    METHOD_ANCHORS: "Matched KP pairs",
    METHOD_GEOMETRY: "Route geometry (projected)",
}

FLAG_EXTRAPOLATED = "extrapolated"
FLAG_GAP = "gap"            # interpolated across a dropped (diverged) stretch
FLAG_STRETCHED = "stretched"
FLAG_REVERSED = "reversed"  # mapped range collapsed or turned around

_EPS_KM = 1e-9


@dataclass
class MapDiagnostics:
    """What the mapping is built on and where it should not be trusted."""

    method: str = METHOD_IDENTITY
    anchor_count: int = 0
    sample_count: int = 0
    source_extent: Tuple[float, float] = (0.0, 0.0)
    target_extent: Tuple[float, float] = (0.0, 0.0)
    # Source-KP stretches with no usable anchors (re-routes / off-tolerance
    # projections / non-monotone snaps). Mapped values inside are
    # interpolated and flagged ``gap``.
    gap_ranges: List[Tuple[float, float]] = field(default_factory=list)
    max_offset_m: float = 0.0
    dropped_offset: int = 0
    dropped_monotone: int = 0
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "method": self.method,
            "anchor_count": self.anchor_count,
            "sample_count": self.sample_count,
            "source_extent": list(self.source_extent),
            "target_extent": list(self.target_extent),
            "gap_ranges": [list(r) for r in self.gap_ranges],
            "max_offset_m": self.max_offset_m,
            "dropped_offset": self.dropped_offset,
            "dropped_monotone": self.dropped_monotone,
            "notes": list(self.notes),
        }

    def summary(self) -> str:
        """One-paragraph plain-language summary for dialogs and logs."""
        label = METHOD_LABELS.get(self.method, self.method)
        if self.method == METHOD_IDENTITY:
            return "KPs are taken as already referenced to the current route."
        parts = [f"{label}: {self.anchor_count} anchor(s)"]
        if self.sample_count:
            parts.append(f"from {self.sample_count} sampled station(s)")
        if self.gap_ranges:
            gaps = ", ".join(f"KP {a:.3f}–{b:.3f}" for a, b in self.gap_ranges[:6])
            more = "" if len(self.gap_ranges) <= 6 else \
                f" (+{len(self.gap_ranges) - 6} more)"
            parts.append(f"; interpolated across {len(self.gap_ranges)} "
                         f"gap(s): {gaps}{more}")
        if self.max_offset_m > 0:
            parts.append(f"; largest accepted offset {self.max_offset_m:.1f} m")
        text = " ".join(parts).replace(" ;", ";")
        if self.notes:
            text += ". " + " ".join(self.notes)
        return text


class KpMap:
    """Monotone piecewise-linear KP mapping with per-value flags."""

    def __init__(self, anchors: Sequence[Tuple[float, float]],
                 method: str = METHOD_ANCHORS,
                 gap_ranges: Optional[Sequence[Tuple[float, float]]] = None,
                 diagnostics: Optional[MapDiagnostics] = None,
                 source_label: str = "", target_label: str = ""):
        cleaned = _clean_anchors(anchors)
        self._src = [a for a, _b in cleaned]
        self._dst = [b for _a, b in cleaned]
        self.method = method
        self.gap_ranges: List[Tuple[float, float]] = [
            (min(a, b), max(a, b)) for a, b in (gap_ranges or [])]
        self.source_label = source_label
        self.target_label = target_label
        self.diagnostics = diagnostics or MapDiagnostics(
            method=method, anchor_count=len(cleaned),
            source_extent=(self._src[0], self._src[-1]) if cleaned else (0.0, 0.0),
            target_extent=(self._dst[0], self._dst[-1]) if cleaned else (0.0, 0.0),
            gap_ranges=list(self.gap_ranges))

    # -- constructors --------------------------------------------------------
    @classmethod
    def identity(cls) -> "KpMap":
        return cls([], method=METHOD_IDENTITY)

    @classmethod
    def shift(cls, delta_km: float) -> "KpMap":
        delta = float(delta_km)
        m = cls([(0.0, delta), (1.0, 1.0 + delta)], method=METHOD_SHIFT)
        m.diagnostics.notes.append(
            f"Every KP moves by {delta:+.3f} km.")
        return m

    @classmethod
    def from_anchors(cls, pairs: Iterable[Tuple[float, float]],
                     source_label: str = "", target_label: str = "") -> "KpMap":
        """Matched positions; non-monotone pairs are dropped with a note."""
        raw = []
        for entry in pairs:
            try:
                raw.append((float(entry[0]), float(entry[1])))
            except (TypeError, ValueError, IndexError):
                continue
        raw.sort()
        kept, dropped = _monotone_filter(raw)
        m = cls(kept, method=METHOD_ANCHORS, source_label=source_label,
                target_label=target_label)
        m.diagnostics.dropped_monotone = dropped
        if dropped:
            m.diagnostics.notes.append(
                f"{dropped} pair(s) ignored because their target KP ran "
                "backwards against the previous pair.")
        return m

    # -- properties ----------------------------------------------------------
    @property
    def anchors(self) -> List[Tuple[float, float]]:
        return list(zip(self._src, self._dst))

    @property
    def is_identity(self) -> bool:
        return self.method == METHOD_IDENTITY or not self._src

    def to_dict(self) -> Dict:
        return {
            "method": self.method,
            "anchors": [[a, b] for a, b in self.anchors],
            "gap_ranges": [list(r) for r in self.gap_ranges],
            "source_label": self.source_label,
            "target_label": self.target_label,
            "diagnostics": self.diagnostics.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict]) -> "KpMap":
        if not isinstance(data, dict):
            return cls.identity()
        method = str(data.get("method") or METHOD_IDENTITY)
        m = cls(data.get("anchors") or [], method=method,
                gap_ranges=data.get("gap_ranges") or [],
                source_label=str(data.get("source_label") or ""),
                target_label=str(data.get("target_label") or ""))
        diag = data.get("diagnostics")
        if isinstance(diag, dict):
            d = m.diagnostics
            d.anchor_count = int(diag.get("anchor_count") or d.anchor_count)
            d.sample_count = int(diag.get("sample_count") or 0)
            d.max_offset_m = float(diag.get("max_offset_m") or 0.0)
            d.dropped_offset = int(diag.get("dropped_offset") or 0)
            d.dropped_monotone = int(diag.get("dropped_monotone") or 0)
            d.notes = [str(n) for n in diag.get("notes") or []]
        return m

    # -- evaluation ----------------------------------------------------------
    def map_kp(self, kp: float) -> Tuple[float, List[str]]:
        """Target KP for a source KP plus flags (``extrapolated``/``gap``)."""
        value = float(kp)
        if self.is_identity:
            return value, []
        src, dst = self._src, self._dst
        flags: List[str] = []
        if len(src) == 1:
            return value + (dst[0] - src[0]), [FLAG_EXTRAPOLATED]
        if value <= src[0]:
            if value < src[0] - _EPS_KM:
                flags.append(FLAG_EXTRAPOLATED)
            return value + (dst[0] - src[0]), flags
        if value >= src[-1]:
            if value > src[-1] + _EPS_KM:
                flags.append(FLAG_EXTRAPOLATED)
            return value + (dst[-1] - src[-1]), flags
        i = bisect.bisect_right(src, value)
        a0, a1 = src[i - 1], src[i]
        b0, b1 = dst[i - 1], dst[i]
        span = a1 - a0
        t = 0.0 if span <= _EPS_KM else (value - a0) / span
        mapped = b0 + t * (b1 - b0)
        if self._in_gap(value):
            flags.append(FLAG_GAP)
        return mapped, flags

    def map_range(self, start_kp: float, end_kp: float,
                  stretch_tol: float = 0.10) -> Tuple[float, float, List[str]]:
        """Map both ends; flag stretched/compressed and collapsed ranges."""
        a, fa = self.map_kp(start_kp)
        b, fb = self.map_kp(end_kp)
        flags = list(dict.fromkeys(fa + fb))
        # A range that straddles a gap is interpolated inside even when
        # both its ends sit on anchored ground.
        lo_s, hi_s = sorted((float(start_kp), float(end_kp)))
        if FLAG_GAP not in flags and any(
                g_lo < hi_s - _EPS_KM and g_hi > lo_s + _EPS_KM
                for g_lo, g_hi in self.gap_ranges):
            flags.append(FLAG_GAP)
        src_len = abs(float(end_kp) - float(start_kp))
        dst_len = abs(b - a)
        if src_len > _EPS_KM:
            if (b - a) * (float(end_kp) - float(start_kp)) < 0 or dst_len <= _EPS_KM:
                flags.append(FLAG_REVERSED)
            elif abs(dst_len - src_len) / src_len > stretch_tol:
                flags.append(FLAG_STRETCHED)
        return a, b, flags

    def inverse(self) -> "KpMap":
        pairs = [(b, a) for a, b in self.anchors]
        m = KpMap(pairs, method=self.method,
                  source_label=self.target_label, target_label=self.source_label)
        return m

    def _in_gap(self, kp: float) -> bool:
        for lo, hi in self.gap_ranges:
            if lo - _EPS_KM <= kp <= hi + _EPS_KM:
                return True
        return False


# -- builders ----------------------------------------------------------------
def build_from_samples(samples: Iterable[Tuple[float, float, float]],
                       offset_tol_m: float = 25.0,
                       thin_tol_m: float = 0.5,
                       source_label: str = "",
                       target_label: str = "") -> KpMap:
    """A geometry-derived map from ``(source_kp, target_kp, offset_m)``.

    ``offset_m`` is the distance between the source station and its
    projection on the target route. Stations further than ``offset_tol_m``
    from the target route are on a re-routed stretch: their projection
    lands on whatever part of the new route happens to be nearest, which
    says nothing about chainage, so they are dropped and the stretch is
    recorded as a gap. Remaining stations must project monotonically —
    a target KP that runs backwards (a loop, a dog-leg, a near-parallel
    new leg) is dropped the same way. The kept anchors are thinned so the
    stored map stays small: an anchor is kept only where the offset
    ``target - source`` deviates from the straight line between its
    neighbours by more than ``thin_tol_m``.
    """
    cleaned: List[Tuple[float, float, float]] = []
    for entry in samples:
        try:
            s, t, off = float(entry[0]), float(entry[1]), float(entry[2])
        except (TypeError, ValueError, IndexError):
            continue
        if not (math.isfinite(s) and math.isfinite(t)):
            continue
        cleaned.append((s, t, off if math.isfinite(off) else float("inf")))
    cleaned.sort(key=lambda e: e[0])
    diag = MapDiagnostics(method=METHOD_GEOMETRY, sample_count=len(cleaned))
    if not cleaned:
        diag.notes.append("No stations could be projected onto the target route.")
        m = KpMap([], method=METHOD_GEOMETRY, diagnostics=diag,
                  source_label=source_label, target_label=target_label)
        return m

    tol = float(offset_tol_m)
    within = [(s, t) for s, t, off in cleaned if off <= tol]
    diag.dropped_offset = len(cleaned) - len(within)
    diag.max_offset_m = max((off for s, t, off in cleaned if off <= tol),
                            default=0.0)
    kept, dropped_mono = _monotone_filter(within)
    diag.dropped_monotone = dropped_mono

    # Gaps: runs of source stations that did not survive, expressed as the
    # source-KP interval between the surviving neighbours.
    gaps: List[Tuple[float, float]] = []
    if kept:
        survivors = set(s for s, _t in kept)
        prev_kept: Optional[float] = None
        pending_from: Optional[float] = None
        for s, _t, _off in cleaned:
            if s in survivors:
                if pending_from is not None:
                    gaps.append((pending_from, s))
                    pending_from = None
                prev_kept = s
            elif pending_from is None:
                # A dropped run starts: it spans from the previous anchor
                # (or the first station when nothing before it survived).
                pending_from = prev_kept if prev_kept is not None else s
        if pending_from is not None:
            gaps.append((pending_from, cleaned[-1][0]))
        gaps = _merge_ranges(gaps)
    thinned = _thin_anchors(kept, thin_tol_m / 1000.0)
    diag.anchor_count = len(thinned)
    diag.gap_ranges = gaps
    if thinned:
        diag.source_extent = (thinned[0][0], thinned[-1][0])
        diag.target_extent = (thinned[0][1], thinned[-1][1])
    if diag.dropped_offset:
        diag.notes.append(
            f"{diag.dropped_offset} station(s) lay more than {tol:.0f} m "
            "from the target route and were not used as anchors.")
    if dropped_mono:
        diag.notes.append(
            f"{dropped_mono} station(s) projected backwards along the "
            "target route and were not used as anchors.")
    if not thinned:
        diag.notes.append(
            "No station lay within tolerance of the target route: the two "
            "routes do not overlap, or the tolerance is too tight.")
    m = KpMap(thinned, method=METHOD_GEOMETRY, gap_ranges=gaps,
              diagnostics=diag, source_label=source_label,
              target_label=target_label)
    return m


def apply_to_ranges(kp_map: KpMap, ranges: Iterable[Dict],
                    start_key: str = "start_kp", end_key: str = "end_kp",
                    stretch_tol: float = 0.10) -> Tuple[List[Dict], Dict[str, int]]:
    """Map every ``{start_key, end_key}`` row; returns copies with
    ``rereference_flags`` set (comma-joined) and a flag tally."""
    out: List[Dict] = []
    tally: Dict[str, int] = {}
    for row in ranges:
        copy = dict(row)
        try:
            a = float(row.get(start_key))
            b = float(row.get(end_key))
        except (TypeError, ValueError):
            copy["rereference_flags"] = "unmapped"
            tally["unmapped"] = tally.get("unmapped", 0) + 1
            out.append(copy)
            continue
        na, nb, flags = kp_map.map_range(a, b, stretch_tol=stretch_tol)
        lo, hi = (na, nb) if na <= nb else (nb, na)
        copy[start_key] = round(lo, 6)
        copy[end_key] = round(hi, 6)
        copy["rereference_flags"] = ",".join(flags)
        for flag in flags:
            tally[flag] = tally.get(flag, 0) + 1
        out.append(copy)
    return out, tally


def rereference_rows(rows: Iterable[Dict], kp_map: KpMap,
                     source_label: str = "", use_source_kps: bool = True,
                     stretch_tol: float = 0.10,
                     start_key: str = "start_kp", end_key: str = "end_kp"
                     ) -> Tuple[List[Dict], Dict[str, int]]:
    """Map KP-range rows onto the current route, keeping provenance.

    The KPs fed to the map are the delivered source KPs (``src_start_kp``
    / ``src_end_kp``) when present and ``use_source_kps`` is set — a
    second re-reference after a further RPL change then starts from the
    original numbers instead of compounding — otherwise the current KPs,
    which are recorded as the source KPs (with ``src_rpl`` =
    ``source_label``) so provenance is never lost by a mapping.
    """
    prepared: List[Dict] = []
    for row in rows or []:
        copy = dict(row)
        src_a = _float_or_none(copy.get("src_start_kp"))
        src_b = _float_or_none(copy.get("src_end_kp"))
        cur_a = _float_or_none(copy.get(start_key))
        cur_b = _float_or_none(copy.get(end_key))
        if use_source_kps and src_a is not None and src_b is not None:
            copy["_map_start"], copy["_map_end"] = src_a, src_b
        else:
            copy["_map_start"], copy["_map_end"] = cur_a, cur_b
            if cur_a is not None and cur_b is not None:
                copy["src_start_kp"], copy["src_end_kp"] = cur_a, cur_b
                if source_label and not copy.get("src_rpl"):
                    copy["src_rpl"] = source_label
        prepared.append(copy)
    mapped, tally = apply_to_ranges(kp_map, prepared, "_map_start", "_map_end",
                                    stretch_tol=stretch_tol)
    out: List[Dict] = []
    for copy in mapped:
        copy[start_key] = copy.pop("_map_start", copy.get(start_key))
        copy[end_key] = copy.pop("_map_end", copy.get(end_key))
        if source_label and not copy.get("src_rpl"):
            copy["src_rpl"] = source_label
        out.append(copy)
    return out, tally


def _float_or_none(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def parse_anchor_text(text: str) -> List[Tuple[float, float]]:
    """``source_kp, target_kp`` per line (comma/semicolon/tab/space)."""
    pairs: List[Tuple[float, float]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        for sep in (",", ";", "\t"):
            line = line.replace(sep, " ")
        parts = [p for p in line.split() if p]
        if len(parts) < 2:
            continue
        try:
            pairs.append((float(parts[0]), float(parts[1])))
        except ValueError:
            continue
    return pairs


# -- helpers -----------------------------------------------------------------
def _clean_anchors(anchors) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    for entry in anchors or []:
        try:
            a, b = float(entry[0]), float(entry[1])
        except (TypeError, ValueError, IndexError):
            continue
        if math.isfinite(a) and math.isfinite(b):
            out.append((a, b))
    out.sort()
    # Collapse duplicate source KPs (keep the first) so bisect stays sane.
    dedup: List[Tuple[float, float]] = []
    for a, b in out:
        if dedup and abs(dedup[-1][0] - a) <= _EPS_KM:
            continue
        dedup.append((a, b))
    return dedup


_DP_LIMIT = 400


def _monotone_filter(pairs: Sequence[Tuple[float, float]]):
    """Keep the longest non-decreasing (in target KP) subsequence.

    A greedy walk would let one wild early snap discard every later good
    station; the longest-increasing-subsequence keeps the consistent
    majority and drops the outliers instead. Small sets (hand-entered
    anchors) use an O(n²) variant that breaks length ties by offset
    smoothness, so of two equally long candidates the one that keeps
    the offset ``target − source`` steady wins and the wild pair is the
    one dropped; dense geometry samples use the O(n log n) form, where
    ties are immaterial.
    """
    n = len(pairs)
    if n == 0:
        return [], 0
    if n <= _DP_LIMIT:
        return _monotone_filter_dp(pairs)
    tails: List[float] = []
    tails_idx: List[int] = []
    prev = [-1] * n
    for i, (_a, b) in enumerate(pairs):
        pos = bisect.bisect_right(tails, b)
        if pos == len(tails):
            tails.append(b)
            tails_idx.append(i)
        else:
            tails[pos] = b
            tails_idx[pos] = i
        prev[i] = tails_idx[pos - 1] if pos > 0 else -1
    kept_idx = []
    cursor = tails_idx[-1]
    while cursor != -1:
        kept_idx.append(cursor)
        cursor = prev[cursor]
    kept_idx.reverse()
    kept = [pairs[i] for i in kept_idx]
    return kept, n - len(kept)


def _monotone_filter_dp(pairs: Sequence[Tuple[float, float]]):
    n = len(pairs)
    offsets = [b - a for a, b in pairs]
    best_len = [1] * n
    rough = [0.0] * n
    prev = [-1] * n
    for i in range(n):
        for j in range(i):
            if pairs[j][1] > pairs[i][1] + _EPS_KM:
                continue
            length = best_len[j] + 1
            cost = rough[j] + abs(offsets[i] - offsets[j])
            if length > best_len[i] or (length == best_len[i] and cost < rough[i]):
                best_len[i] = length
                rough[i] = cost
                prev[i] = j
    end = max(range(n), key=lambda i: (best_len[i], -rough[i]))
    kept_idx = []
    cursor = end
    while cursor != -1:
        kept_idx.append(cursor)
        cursor = prev[cursor]
    kept_idx.reverse()
    kept = [pairs[i] for i in kept_idx]
    return kept, n - len(kept)


def _thin_anchors(pairs: Sequence[Tuple[float, float]],
                  tol_km: float) -> List[Tuple[float, float]]:
    """Douglas–Peucker on the offset curve ``(src, dst - src)``."""
    if len(pairs) <= 2 or tol_km <= 0:
        return list(pairs)
    xs = [a for a, _b in pairs]
    ys = [b - a for a, b in pairs]
    keep = [False] * len(pairs)
    keep[0] = keep[-1] = True
    stack = [(0, len(pairs) - 1)]
    while stack:
        lo, hi = stack.pop()
        if hi - lo < 2:
            continue
        x0, y0, x1, y1 = xs[lo], ys[lo], xs[hi], ys[hi]
        span = x1 - x0
        best, best_i = -1.0, -1
        for i in range(lo + 1, hi):
            t = 0.0 if span <= _EPS_KM else (xs[i] - x0) / span
            dev = abs(ys[i] - (y0 + t * (y1 - y0)))
            if dev > best:
                best, best_i = dev, i
        if best > tol_km and best_i > 0:
            keep[best_i] = True
            stack.append((lo, best_i))
            stack.append((best_i, hi))
    return [p for p, k in zip(pairs, keep) if k]


def _merge_ranges(ranges: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    for lo, hi in sorted((min(a, b), max(a, b)) for a, b in ranges):
        if out and lo <= out[-1][1] + _EPS_KM:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out

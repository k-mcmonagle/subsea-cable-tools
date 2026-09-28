"""KP axis ticks: round KP intervals labelled to 3 dp (no QGIS / Qt imports).

Two cases share one step ladder:

* **Linear** — the plot X *is* KP (km), e.g. a profile along the route.
  Ticks sit on multiples of a round step (…0.1, 0.25, 0.5, 1, 2, 5…).
* **Mapped** — the plot X is distance along some other line (a range line
  or a hand-drawn profile) and each position has a *nearest route KP*.
  Ticks sit where that KP crosses a round value, so the axis reads
  12.000, 12.250, 12.500 … at their true positions instead of labelling
  evenly spaced distances with awkward KPs (12.344, 13.344 …).

Nearest-KP functions can jump (at a route bend's medial axis the nearest
segment switches), so intervals whose KP changes faster than the line
advances are treated as discontinuities and never receive a tick.
"""
import bisect
import math

# Round KP intervals in km. Sub-kilometre steps favour the quarter ladder
# surveyors use (0.025/0.25); 3 dp labels make 0.001 km the finest step.
KP_STEPS_KM = (0.001, 0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5,
               1.0, 2.0, 5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0)
KP_DECIMALS = 3
# Minimum screen gap between labelled ticks, in pixels ("12.345" + margin).
MIN_LABEL_PX = 70.0
# KP may change at most this many times faster than distance along the
# line before an interval counts as a jump (nearest segment switched).
MAX_KP_RATE = 3.0


def format_kp(kp_km):
    """KP in km to the plugin-wide 3 dp ('' for missing values)."""
    if kp_km is None or not math.isfinite(kp_km):
        return ''
    text = '%.*f' % (KP_DECIMALS, kp_km)
    return '0.000' if text == '-0.000' else text


def nice_kp_step(span_km, max_ticks):
    """Smallest round step giving at most ``max_ticks`` intervals over span."""
    span = abs(float(span_km))
    max_ticks = max(1.0, float(max_ticks))
    for step in KP_STEPS_KM:
        if span / step <= max_ticks:
            return step
    return KP_STEPS_KM[-1]


def max_ticks_for(size_px, min_px=MIN_LABEL_PX):
    return max(1.0, float(size_px) / float(min_px)) if size_px else 5.0


def kp_multiples(lo_km, hi_km, step_km):
    """Multiples of ``step_km`` within [lo, hi], rounded (no 0.30000000004)."""
    lo, hi = sorted((float(lo_km), float(hi_km)))
    first = math.ceil(lo / step_km - 1e-9)
    last = math.floor(hi / step_km + 1e-9)
    if last - first > 100000:
        return []
    return [round(n * step_km, 6) for n in range(int(first), int(last) + 1)]


def linear_kp_ticks(lo, hi, size_px, km_per_unit=1.0, offset_km=0.0,
                    min_px=MIN_LABEL_PX):
    """(step in plot units, [positions]) for an axis whose X maps linearly to KP.

    ``kp = offset_km + x * km_per_unit``; a negative ``km_per_unit`` covers
    reversed numbering.
    """
    lo, hi = sorted((float(lo), float(hi)))
    if not (math.isfinite(lo) and math.isfinite(hi)) or hi <= lo or not km_per_unit:
        return None, []
    kp_a = offset_km + lo * km_per_unit
    kp_b = offset_km + hi * km_per_unit
    step = nice_kp_step(kp_b - kp_a, max_ticks_for(size_px, min_px))
    positions = [(kp - offset_km) / km_per_unit for kp in kp_multiples(kp_a, kp_b, step)]
    positions.sort()
    return step / abs(km_per_unit), positions


class KPCrossings:
    """Positions where a sampled nearest-KP function crosses round KPs.

    ``xs`` are strictly increasing positions along the line in metres and
    ``kps`` the nearest route KP (km) at each, ``None`` where the point does
    not map to the route. ``refine(x_m) -> kp_km`` (optional) re-evaluates
    the true KP so interpolated crossings can be polished with a few secant
    steps; results are cached per step.
    """

    def __init__(self, xs_m, kps_km, refine=None, max_rate=MAX_KP_RATE,
                 tolerance_km=0.00002):
        pairs = [(float(x), None if k is None else float(k))
                 for x, k in zip(xs_m, kps_km)]
        self.xs = [x for x, _k in pairs]
        self.kps = [k for _x, k in pairs]
        self._refine = refine
        self._max_rate = float(max_rate)
        self._tolerance = float(tolerance_km)
        self._cache = {}

    def __bool__(self):
        return any(k is not None for k in self.kps)

    def _continuous(self, i):
        k0, k1 = self.kps[i], self.kps[i + 1]
        if k0 is None or k1 is None:
            return False
        dx = self.xs[i + 1] - self.xs[i]
        # Allow 1 m of slack so near-coincident samples never read as jumps.
        return abs(k1 - k0) * 1000.0 <= self._max_rate * dx + 1.0

    def kp_at(self, x_m):
        """Linearly interpolated nearest KP at ``x_m`` (None across a jump)."""
        xs = self.xs
        if not xs or x_m < xs[0] or x_m > xs[-1]:
            return None
        i = bisect.bisect_right(xs, x_m) - 1
        if i >= len(xs) - 1:
            return self.kps[-1]
        k0, k1 = self.kps[i], self.kps[i + 1]
        if k0 is None or k1 is None:
            return k0 if x_m == xs[i] else None
        if not self._continuous(i):
            return k0 if (x_m - xs[i]) <= (xs[i + 1] - x_m) else k1
        t = (x_m - xs[i]) / (xs[i + 1] - xs[i]) if xs[i + 1] > xs[i] else 0.0
        return k0 + t * (k1 - k0)

    def kp_range(self, x_lo=None, x_hi=None):
        values = [k for x, k in zip(self.xs, self.kps) if k is not None
                  and (x_lo is None or x >= x_lo) and (x_hi is None or x <= x_hi)]
        for edge in (x_lo, x_hi):
            if edge is not None:
                kp = self.kp_at(edge)
                if kp is not None:
                    values.append(kp)
        return (min(values), max(values)) if values else None

    def _polish(self, target, xa, ka, xb, kb):
        """Secant steps inside [xa, xb] towards KP == target."""
        x = xa + (target - ka) / (kb - ka) * (xb - xa)
        if self._refine is None:
            return x
        for _ in range(4):
            try:
                kx = self._refine(x)
            except Exception:
                return x
            if kx is None:
                return x
            if abs(kx - target) <= self._tolerance:
                return x
            if (kx - target) * (ka - target) > 0:
                xa, ka = x, kx
            else:
                xb, kb = x, kx
            if kb == ka:
                return x
            x = xa + (target - ka) / (kb - ka) * (xb - xa)
        return x

    def crossings(self, step_km):
        """[(x_m, kp_km)] for every multiple of ``step_km`` the function crosses."""
        key = round(step_km, 6)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        found = []
        xs, kps = self.xs, self.kps
        for i in range(len(xs) - 1):
            if not self._continuous(i):
                continue
            k0, k1 = kps[i], kps[i + 1]
            lo, hi = sorted((k0, k1))
            for target in kp_multiples(lo, hi, step_km):
                if k0 == k1:
                    x = xs[i]
                elif target == k1 and i + 1 < len(xs) - 1:
                    continue  # counted as the next interval's start
                else:
                    x = self._polish(target, xs[i], k0, xs[i + 1], k1)
                if found and abs(found[-1][0] - x) < 1e-6 and found[-1][1] == target:
                    continue
                found.append((x, target))
        self._cache[key] = found
        return found

    def ticks(self, x_lo_m, x_hi_m, px_per_m, min_px=MIN_LABEL_PX):
        """Round-KP ticks visible in [x_lo_m, x_hi_m]: (step_km, [(x_m, kp)]).

        The step comes from the visible KP span; ticks closer than
        ``min_px`` on screen are thinned (cross-route lines revisit KPs).
        Returns ``(None, [])`` when the view maps to no route KP.
        """
        x_lo_m, x_hi_m = sorted((float(x_lo_m), float(x_hi_m)))
        kp_span = self.kp_range(x_lo_m, x_hi_m)
        if kp_span is None:
            return None, []
        # Size the step from the KP change the visible stretch actually
        # carries, never finer than the screen can label.
        size_px = max(1.0, (x_hi_m - x_lo_m) * px_per_m)
        step = nice_kp_step(kp_span[1] - kp_span[0], max_ticks_for(size_px, min_px))
        min_gap_m = min_px / px_per_m if px_per_m > 0 else 0.0
        kept = []
        for x, kp in self.crossings(step):
            if x < x_lo_m - 1e-9 or x > x_hi_m + 1e-9:
                continue
            if kept and x - kept[-1][0] < min_gap_m:
                continue
            kept.append((x, kp))
        return step, kept

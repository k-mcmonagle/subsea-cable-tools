# -*- coding: utf-8 -*-
"""Pure catenary solver used by the QGIS V2 dialog and tests."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import List, Optional, Tuple

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None


@dataclass
class AssemblyItem:
    """Ordered from chute top down along the cable."""

    kind: str
    name: str
    length_m: float
    q_water_npm: float
    q_air_npm: float
    point_load_kN: float
    color_hex: str = ""


@dataclass
class Component:
    """
    A component that modifies the cable system over a section, or at a point.

    Interpretation:
        - position_m: distance along the free-span cable measured from the TDP upward (m).
            TDP = 0m, chute contact = free_span_length.
        - length_m:
            * if > 0: apply distributed delta weights over [s, s+length]
            * if = 0: point event, usually point_load_kN
        - point_load_kN: positive values add downward vertical load, negative values add buoyancy.
    """

    name: str
    position_m: float
    length_m: float
    delta_q_water_npm: float
    delta_q_air_npm: float
    point_load_kN: float
    reference: str = "tdp"

    @property
    def is_point(self) -> bool:
        return abs(self.length_m) < 1e-9 and abs(self.point_load_kN) > 1e-12


@dataclass
class SolverDiagnostics:
    input_mode: str = ""
    ds_requested_m: float = 0.0
    ds_effective_m: float = 0.0
    integration_steps: int = 0
    free_span_length_m: float = 0.0
    chute_contact_length_m: float = 0.0
    chute_contact_iterations: int = 0
    chute_contact_residual_m: float = 0.0
    boundary_residual_m: float = 0.0
    input_residual_label: str = "Mode residual"
    input_residual: float = 0.0
    input_residual_units: str = ""
    refinement_position_delta_m: Optional[float] = None
    refinement_angle_delta_deg: Optional[float] = None
    refinement_top_tension_delta_kN: Optional[float] = None
    tdp_iterations: int = 0
    tdp_x_world_m: float = 0.0
    tdp_depth_m: float = 0.0
    tdp_slope_deg: float = 0.0
    warnings: List[str] = field(default_factory=list)


def parse_components(text: str) -> List[Component]:
    """
    Parse multiline component definitions.

    Format per line, comma or tab separated:
        name, s_from_tdp_m, length_m, delta_q_water(N/m), delta_q_air(N/m), point_load_kN
    """
    comps: List[Component] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.replace("\t", ",").split(",")]
        while len(parts) < 6:
            parts.append("0")
        name = parts[0] if parts[0] else "Component"
        try:
            s = float(parts[1])
            length = float(parts[2])
            delta_q_water = float(parts[3])
            delta_q_air = float(parts[4])
            point_load = float(parts[5])
        except Exception:
            continue

        comps.append(Component(
            name=name,
            position_m=s,
            length_m=length,
            delta_q_water_npm=delta_q_water,
            delta_q_air_npm=delta_q_air,
            point_load_kN=point_load,
            reference="tdp",
        ))
    comps.sort(key=lambda c: c.position_m)
    return comps


# Backward-compatible private name used by the original dialog module.
_parse_components = parse_components


# ---------------------------------------------------------------------------
# Seabed profile abstraction
# ---------------------------------------------------------------------------
#
# Frame convention for all SeabedProfile implementations:
#   x_world   = horizontal distance from the chute (top-of-chute is at x_world=0)
#               measured positive in the direction the cable lays out (toward
#               and beyond the TDP).
#   depth_at(x_world)   -> water depth (positive, metres) at that x_world.
#   slope_at(x_world)   -> seabed local tangent angle in radians, measured
#                          from horizontal, positive when moving *toward* the
#                          chute the bed rises (i.e. depth decreases toward
#                          chute, which is the typical "bed deepens away from
#                          chute" case). With this sign, V(0)/H = tan(alpha)
#                          and a typical cable-lay (deeper away from chute)
#                          gives V(0) > 0 so the cable departs the TDP angled
#                          upward toward the chute.
#
# Mathematically: tan(slope_at(x_world)) = d(depth)/d(x_world) at x_world,
# because going toward the chute is the -x_world direction and the bed
# elevation y_bed = -depth, so dy_bed/d(toward_chute) = d(depth)/d(x_world).


@dataclass
class FlatSeabed:
    """Constant-depth seabed (preserves legacy behaviour exactly)."""

    depth_m: float

    def depth_at(self, x_world: float) -> float:
        return float(self.depth_m)

    def slope_at(self, x_world: float) -> float:
        return 0.0

    def to_dict(self) -> dict:
        return {"mode": "flat", "depth_m": float(self.depth_m)}


@dataclass
class PlanarSlopeSeabed:
    """Constant-slope seabed.

    Parameters
    ----------
    depth_at_chute_m : float
        Water depth at x_world = 0 (directly below the chute), in metres.
    slope_deg : float
        Slope angle in degrees, positive = bed deepens away from chute
        (i.e. cable departs the TDP angled upward toward the chute).
    """

    depth_at_chute_m: float
    slope_deg: float

    def depth_at(self, x_world: float) -> float:
        return float(self.depth_at_chute_m) + math.tan(math.radians(self.slope_deg)) * float(x_world)

    def slope_at(self, x_world: float) -> float:
        return math.radians(float(self.slope_deg))

    def to_dict(self) -> dict:
        return {
            "mode": "sloped",
            "depth_at_chute_m": float(self.depth_at_chute_m),
            "slope_deg": float(self.slope_deg),
        }


@dataclass
class PolylineSeabed:
    """Piecewise-linear seabed profile defined by (x_world, depth) samples.

    Samples must be sorted by x_world strictly increasing. Outside the sampled
    range, depth and slope are held constant at the nearest endpoint value
    (and slope is zero outside).
    """

    x_world_m: List[float]
    depth_m: List[float]
    slope_smoothing_m: float = 5.0

    def __post_init__(self):
        if len(self.x_world_m) != len(self.depth_m):
            raise ValueError("PolylineSeabed: x_world_m and depth_m must have the same length.")
        if len(self.x_world_m) < 2:
            raise ValueError("PolylineSeabed: at least two samples are required.")
        xs = list(self.x_world_m)
        ds = list(self.depth_m)
        for i in range(1, len(xs)):
            if xs[i] <= xs[i - 1]:
                raise ValueError("PolylineSeabed: x_world_m must be strictly increasing.")
        self.x_world_m = [float(v) for v in xs]
        self.depth_m = [float(v) for v in ds]

    def depth_at(self, x_world: float) -> float:
        xs = self.x_world_m
        ds = self.depth_m
        x = float(x_world)
        if x <= xs[0]:
            return ds[0]
        if x >= xs[-1]:
            return ds[-1]
        # Linear interpolation.
        lo = 0
        hi = len(xs) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if xs[mid] <= x:
                lo = mid
            else:
                hi = mid
        t = (x - xs[lo]) / (xs[hi] - xs[lo])
        return ds[lo] * (1.0 - t) + ds[hi] * t

    def slope_at(self, x_world: float) -> float:
        # Centred-difference over ±max(slope_smoothing_m, 2*local segment).
        x = float(x_world)
        xs = self.x_world_m
        if x <= xs[0] or x >= xs[-1]:
            return 0.0
        # Local segment width for adaptive smoothing.
        lo = 0
        hi = len(xs) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if xs[mid] <= x:
                lo = mid
            else:
                hi = mid
        local_width = xs[hi] - xs[lo]
        h = max(float(self.slope_smoothing_m), 2.0 * local_width)
        x_left = max(xs[0], x - h)
        x_right = min(xs[-1], x + h)
        if x_right <= x_left:
            return 0.0
        d_depth = self.depth_at(x_right) - self.depth_at(x_left)
        d_x = x_right - x_left
        return math.atan2(d_depth, d_x)

    def to_dict(self) -> dict:
        return {
            "mode": "polyline",
            "x_world_m": list(self.x_world_m),
            "depth_m": list(self.depth_m),
            "slope_smoothing_m": float(self.slope_smoothing_m),
        }


def _resolve_seabed(cfg: dict):
    """Return the SeabedProfile from cfg, falling back to FlatSeabed(water_depth_m)."""
    seabed = cfg.get("seabed")
    if seabed is not None and hasattr(seabed, "depth_at") and hasattr(seabed, "slope_at"):
        return seabed
    return FlatSeabed(float(cfg["water_depth_m"]))


class CatenarySystemCalculator:
    """
    Numerical integration of a suspended cable with:
    - constant horizontal tension component H (N)
    - vertical component V changes with distributed weight and point loads
    - medium chosen by current y sign (y<0 => water)
    - optional assembly segments, legacy components, and point loads
    """

    def __init__(self, config: dict):
        if np is None:
            raise ImportError("NumPy is required for the catenary solver.")

        self.cfg = config
        self.seabed = _resolve_seabed(config)

        self.H_N: Optional[float] = None
        self.S_total: Optional[float] = None
        self.layback: Optional[float] = None
        self.exit_angle_deg_from_h: Optional[float] = None
        self.top_tension_kN: Optional[float] = None
        self.bottom_tension_kN: Optional[float] = None
        self.min_radius_m: Optional[float] = None

        self.x: Optional[np.ndarray] = None
        self.y: Optional[np.ndarray] = None
        self.s: Optional[np.ndarray] = None
        self.vertical_force_N: Optional[np.ndarray] = None
        self.tension_kN: Optional[np.ndarray] = None
        self.x_world: Optional[np.ndarray] = None

        # Seabed/TDP state. _tdp_x_world is the world-frame horizontal distance
        # from the chute to the TDP (positive). For FlatSeabed this is just an
        # internal bookkeeping value and does not affect the physics.
        self._tdp_x_world: float = 0.0
        self.tdp_x_world: Optional[float] = None
        self.tdp_depth_m: Optional[float] = None
        self.tdp_slope_deg: Optional[float] = None

        self.s_sea_surface: Optional[float] = None
        self.chute_contact_len_m: float = float(self.cfg.get("chute_contact_len_m", 0.0))
        self.free_span_len_m: Optional[float] = None
        self.diagnostics = SolverDiagnostics()
        self._last_chute_contact_iterations: int = 0
        self._last_chute_contact_residual_m: float = 0.0

    @staticmethod
    def _unit_to_npm(value: float, unit: str) -> float:
        if unit == "N/m":
            return value
        if unit == "kg/m":
            return value * 9.80665
        if unit == "lbf/ft":
            return value * 4.448221615 / 0.3048
        raise ValueError("Unknown unit")

    def _q_effective(
        self,
        y: float,
        s_from_tdp: float,
        S_free: float,
        L_chute_contact: float,
        assembly: List[AssemblyItem],
        comps: List[Component],
    ) -> float:
        if assembly:
            d_from_top = L_chute_contact + (S_free - s_from_tdp)
            q_seg = None
            d_cursor = 0.0
            for it in assembly:
                if it.kind != "segment":
                    continue
                d0 = d_cursor
                d1 = d_cursor + max(0.0, it.length_m)
                if d0 <= d_from_top <= d1:
                    q_seg = it.q_water_npm if y < 0 else it.q_air_npm
                    break
                d_cursor = d1

            if q_seg is None or q_seg <= 0:
                q_seg = self.cfg["q_water_npm"] if y < 0 else self.cfg["q_air_npm"]
            return max(float(q_seg), 1e-9)

        base_q = self.cfg["q_water_npm"] if y < 0 else self.cfg["q_air_npm"]
        q = base_q
        for c in comps:
            if c.length_m <= 0:
                continue
            s0 = c.position_m
            s1 = c.position_m + c.length_m
            if s0 <= s_from_tdp <= s1:
                q += c.delta_q_water_npm if y < 0 else c.delta_q_air_npm
        return max(q, 1e-9)

    def _apply_point_loads(
        self,
        s_prev: float,
        s_new: float,
        V: float,
        S_free: float,
        L_chute_contact: float,
        assembly: List[AssemblyItem],
        comps: List[Component],
    ) -> float:
        if assembly:
            d_cursor = 0.0
            for it in assembly:
                if it.kind == "segment":
                    d_cursor += max(0.0, it.length_m)
                    continue
                if it.kind != "body":
                    continue

                d_body = d_cursor
                if d_body < L_chute_contact:
                    continue
                if d_body > (L_chute_contact + S_free):
                    continue

                s_body = S_free - (d_body - L_chute_contact)
                if s_prev < s_body <= s_new:
                    V += it.point_load_kN * 1000.0
            return V

        for c in comps:
            if not c.is_point:
                continue
            sp = c.position_m
            if s_prev < sp <= s_new:
                V += c.point_load_kN * 1000.0
        return V

    def integrate(self, H_N: float, S_free_m: float, ds: float) -> Tuple[float, float, float, float, float, np.ndarray, np.ndarray, np.ndarray]:
        D = float(self.seabed.depth_at(self._tdp_x_world))
        alpha_tdp = float(self.seabed.slope_at(self._tdp_x_world))
        V_init = H_N * math.tan(alpha_tdp)
        comps = self.cfg.get("components", [])
        assembly = self.cfg.get("assembly", [])
        R = float(self.cfg.get("chute_radius_m", 0.0))

        def integrate_once(L_chute_contact: float):
            s = 0.0
            x = 0.0
            y = -D
            V = V_init

            s_list = [0.0]
            x_list = [x]
            y_list = [y]
            vertical_force_list = [V]
            tension_list = [math.sqrt(H_N * H_N + V * V) / 1000.0]
            sea_cross_s = None

            n_steps = max(1, int(math.ceil(S_free_m / ds)))
            max_steps = int(self.cfg.get("max_integration_steps", 25000))
            if n_steps > max_steps:
                raise ValueError(
                    f"Integration would require {n_steps} steps (S_free={S_free_m:.2f} m, ds={ds:.3f} m), "
                    f"which is too slow for interactive use. Increase ds or use a smaller length/height. "
                    f"(Max allowed steps: {max_steps})"
                )
            ds_eff = S_free_m / n_steps

            split_events: List[Tuple[float, float]] = []

            if assembly:
                d_cursor = 0.0
                for it in assembly:
                    if it.kind == "segment":
                        d_cursor += max(0.0, it.length_m)
                        continue
                    if it.kind != "body":
                        continue
                    if abs(it.point_load_kN) < 1e-12:
                        continue

                    d_body = d_cursor
                    if d_body < L_chute_contact:
                        continue
                    if d_body > (L_chute_contact + S_free_m):
                        continue

                    s_body = S_free_m - (d_body - L_chute_contact)
                    if 0.0 < s_body < S_free_m:
                        split_events.append((float(s_body), float(it.point_load_kN) * 1000.0))
            else:
                for c in comps:
                    if not c.is_point:
                        continue
                    sp = float(c.position_m)
                    if 0.0 < sp < S_free_m:
                        split_events.append((sp, float(c.point_load_kN) * 1000.0))

            if assembly:
                d_cursor = 0.0
                for it in assembly:
                    if it.kind != "segment":
                        continue
                    d_cursor += max(0.0, it.length_m)
                    d_b = d_cursor
                    if d_b <= L_chute_contact:
                        continue
                    if d_b >= (L_chute_contact + S_free_m):
                        continue
                    s_b = S_free_m - (d_b - L_chute_contact)
                    if 0.0 < s_b < S_free_m:
                        split_events.append((float(s_b), 0.0))

            if not assembly and comps:
                for c in comps:
                    if c.length_m <= 0:
                        continue
                    s0 = float(c.position_m)
                    s1 = float(c.position_m + c.length_m)
                    if 0.0 < s0 < S_free_m:
                        split_events.append((s0, 0.0))
                    if 0.0 < s1 < S_free_m:
                        split_events.append((s1, 0.0))

            if split_events:
                split_events.sort(key=lambda t: t[0])
                merged: List[Tuple[float, float]] = []
                tol_s = max(1e-9, 0.25 * ds_eff)
                cur_s, cur_load = split_events[0]
                for se, load in split_events[1:]:
                    if abs(se - cur_s) <= tol_s:
                        cur_load += load
                    else:
                        merged.append((cur_s, cur_load))
                        cur_s, cur_load = se, load
                merged.append((cur_s, cur_load))
                split_events = merged

            ev_idx = 0

            for _ in range(n_steps):
                def do_substep(ds_local: float, y_for_medium: float, s_local_end: float):
                    nonlocal V, x, y
                    if ds_local <= 0:
                        return

                    s_for_weight = s_local_end - 0.5 * ds_local
                    q = self._q_effective(y_for_medium, s_for_weight, S_free_m, L_chute_contact, assembly, comps)

                    V_mid = V + 0.5 * q * ds_local
                    T_mid = math.sqrt(H_N * H_N + V_mid * V_mid)
                    if T_mid <= 0:
                        raise ValueError("Non-physical tension encountered during integration.")
                    x += (H_N / T_mid) * ds_local
                    y += (V_mid / T_mid) * ds_local
                    V = V + q * ds_local

                def integrate_with_sea_split(s_target: float):
                    nonlocal s, x, y, V, sea_cross_s
                    ds_local = s_target - s
                    if ds_local <= 0:
                        s = s_target
                        return

                    y_before = y
                    V_before = V
                    x_before = x

                    do_substep(ds_local, y_before, s_target)
                    y_after = y

                    if sea_cross_s is None and ((y_before < 0 <= y_after) or (y_before > 0 >= y_after)):
                        y = y_before
                        V = V_before
                        x = x_before

                        if abs(y_after - y_before) < 1e-12:
                            frac = 0.5
                        else:
                            frac = (0.0 - y_before) / (y_after - y_before)
                            frac = float(max(0.0, min(1.0, frac)))

                        ds1 = ds_local * frac
                        ds2 = ds_local - ds1
                        s_mid = s + ds1

                        if ds1 > 0:
                            do_substep(ds1, y_before, s_mid)
                        sea_cross_s = s_mid

                        y_eps = 1e-9 if y_before < 0 else -1e-9
                        if ds2 > 0:
                            do_substep(ds2, y_eps, s_target)

                    s = s_target

                s_full = s + ds_eff

                while ev_idx < len(split_events) and split_events[ev_idx][0] <= s + 1e-12:
                    ev_idx += 1

                while ev_idx < len(split_events) and (s < split_events[ev_idx][0] <= s_full):
                    s_event, load_N = split_events[ev_idx]
                    integrate_with_sea_split(float(s_event))
                    if abs(load_N) > 1e-12:
                        V += float(load_N)
                    ev_idx += 1

                integrate_with_sea_split(s_full)

                s_list.append(s)
                x_list.append(x)
                y_list.append(y)
                vertical_force_list.append(V)
                tension_list.append(math.sqrt(H_N * H_N + V * V) / 1000.0)

            theta = math.atan2(V, H_N)
            top_T = math.sqrt(H_N * H_N + V * V)

            return (
                x,
                y,
                V,
                theta,
                top_T,
                np.array(s_list),
                np.array(x_list),
                np.array(y_list),
                np.array(vertical_force_list),
                np.array(tension_list),
                sea_cross_s,
            )

        L_guess = float(self.cfg.get("chute_contact_len_m", 0.0))
        result = integrate_once(L_guess)
        chute_iterations = 0
        chute_residual_m = 0.0

        if assembly and R > 0:
            for iteration_index in range(6):
                x_end, y_end, V_end, theta_end, top_T, s_arr, x_arr, y_arr, v_arr, t_arr, sea_cross_s = result
                L_new = float(R * max(0.0, min(math.pi / 2.0, theta_end)))
                chute_residual_m = L_new - L_guess
                chute_iterations = iteration_index + 1
                if abs(chute_residual_m) < 1e-3:
                    L_guess = L_new
                    break
                L_guess = 0.6 * L_guess + 0.4 * L_new
                result = integrate_once(L_guess)

        x_end, y_end, V_end, theta_end, top_T, s_arr, x_arr, y_arr, v_arr, t_arr, sea_cross_s = result

        self.s_sea_surface = sea_cross_s
        self.chute_contact_len_m = float(L_guess)
        self.free_span_len_m = float(S_free_m)
        self.vertical_force_N = v_arr
        self.tension_kN = t_arr
        self._last_chute_contact_iterations = chute_iterations
        self._last_chute_contact_residual_m = float(chute_residual_m)

        return x_end, y_end, V_end, theta_end, top_T, s_arr, x_arr, y_arr

    @staticmethod
    def _bracket_root(func, x0: float, step: float, max_expand: int = 60) -> Tuple[float, float]:
        a = max(1e-12, x0 - step)
        b = x0 + step

        last_eval_err: Optional[Exception] = None

        def safe_eval(x: float) -> float:
            nonlocal last_eval_err
            try:
                last_eval_err = None
                return float(func(x))
            except Exception as exc:
                last_eval_err = exc
                return float("nan")

        fa = safe_eval(a)
        fb = safe_eval(b)

        for _ in range(max_expand):
            if fa == 0:
                return a, a
            if fb == 0:
                return b, b
            if (not math.isnan(fa)) and (not math.isnan(fb)) and fa * fb < 0:
                return a, b
            step *= 1.6
            a = max(1e-12, x0 - step)
            b = x0 + step
            fa = safe_eval(a)
            fb = safe_eval(b)

        if last_eval_err is not None:
            raise ValueError(
                "Could not bracket a root because the function failed to evaluate in the search range. "
                f"Last evaluation error: {last_eval_err}"
            )
        raise ValueError(
            "Could not bracket a root for the chosen input values. "
            f"Tried x in roughly [{a:.3g}, {b:.3g}] around x0={x0:.3g}; f(a)={fa:.3g}, f(b)={fb:.3g}."
        )

    @staticmethod
    def _bisect(func, a: float, b: float, tol: float = 1e-6, max_iter: int = 120) -> float:
        fa = func(a)
        fb = func(b)
        if abs(fa) < tol:
            return a
        if abs(fb) < tol:
            return b
        if fa * fb > 0:
            raise ValueError("Root not bracketed.")

        lo, hi = a, b
        flo = fa
        for _ in range(max_iter):
            mid = 0.5 * (lo + hi)
            fm = func(mid)
            if abs(fm) < tol or abs(hi - lo) < tol:
                return mid
            if flo * fm < 0:
                hi = mid
            else:
                lo, flo = mid, fm
        return 0.5 * (lo + hi)

    def solve(self):
        """Outer driver. Wraps ``_solve_once`` in a TDP-position fixed-point
        iteration so that a sloped or profiled seabed converges to a TDP
        location consistent with the cable layback. For a flat seabed the
        iteration converges in a single pass (depth and slope are independent
        of x_world). For sloped beds, Aitken's Δ²-acceleration is applied to
        the linearly-convergent Picard sequence to reach an engineering
        tolerance in 2–4 iterations.
        """
        max_tdp_iters = int(self.cfg.get("max_tdp_iters", 8))
        # Engineering tolerance on TDP horizontal position: this drives the
        # depth-at-TDP lookup, so 10 cm is much tighter than is needed for any
        # downstream output (depth changes by tan(α)·tol; bottom tension
        # changes by H/cos(α) which is independent of D for a planar slope).
        tdp_tol_m = max(0.10, float(self.cfg.get("ds_m", 1.0)))

        # If caller hasn't seeded a TDP x-guess, fall back to S_guess as a rough
        # proxy for the typical layback magnitude. For flat seabed this value
        # doesn't influence the result.
        if self._tdp_x_world == 0.0:
            self._tdp_x_world = float(self.cfg.get("S_guess_m", 0.0))

        is_flat = isinstance(self.seabed, FlatSeabed)

        # "Bottom Tension" input semantics: the user types the actual cable
        # tension at the touchdown point, T_TDP. The horizontal force component
        # used internally is H = T_TDP · cos(α_TDP). For a flat seabed
        # cos α = 1 so H == T_TDP exactly (back-compatible). For a sloped or
        # profiled seabed α depends on the TDP world-x, so we rescale H from
        # the user's T_TDP input inside each Picard step using the local slope
        # at the current TDP guess.
        mode = self.cfg.get("input_mode", "")
        t_bottom_input_N: Optional[float] = None
        if mode == "Bottom Tension":
            t_bottom_input_N = float(self.cfg.get("H_input_N", 0.0))

        def _apply_bottom_tension_scaling(x_world: float) -> None:
            if t_bottom_input_N is None:
                return
            alpha = float(self.seabed.slope_at(float(x_world)))
            self.cfg["H_input_N"] = float(t_bottom_input_N) * math.cos(alpha)

        def picard_step(x_in: float) -> float:
            self._tdp_x_world = x_in
            _apply_bottom_tension_scaling(x_in)
            self._solve_once()
            return float(self.layback if self.layback is not None else 0.0)

        tdp_iters_used = 1
        if is_flat:
            # Depth and slope are x-independent: one solve is exact.
            _apply_bottom_tension_scaling(self._tdp_x_world)
            self._solve_once()
        else:
            x0 = self._tdp_x_world
            x_converged = x0
            for tdp_iter in range(max_tdp_iters):
                tdp_iters_used = tdp_iter + 1
                x1 = picard_step(x0)
                if abs(x1 - x0) <= tdp_tol_m:
                    x_converged = x1
                    break

                x2 = picard_step(x1)
                denom = x2 - 2.0 * x1 + x0
                if abs(denom) < 1e-12:
                    x_converged = x2
                    if abs(x2 - x1) <= tdp_tol_m:
                        break
                    x0 = x2
                    continue

                x_aitken = x0 - (x1 - x0) ** 2 / denom
                if abs(x_aitken - x2) <= tdp_tol_m:
                    x_converged = x_aitken
                    break
                x0 = x_aitken
            # Lock in converged x with a final re-solve so stored state matches.
            if abs(self._tdp_x_world - x_converged) > 1e-9:
                self._tdp_x_world = x_converged
                _apply_bottom_tension_scaling(x_converged)
                self._solve_once()

        # Restore the user's T_TDP input in cfg (we scaled it internally to H).
        if t_bottom_input_N is not None:
            self.cfg["H_input_N"] = float(t_bottom_input_N)

        # Final TDP-related outputs.
        alpha_final = float(self.seabed.slope_at(self._tdp_x_world))
        self.tdp_x_world = float(self._tdp_x_world)
        self.tdp_depth_m = float(self.seabed.depth_at(self._tdp_x_world))
        self.tdp_slope_deg = math.degrees(alpha_final)
        # Bottom tension at TDP is T = H/cos(alpha). For flat bed (alpha=0)
        # this collapses to H/1000 → backwards-compatible.
        if self.H_N is not None:
            self.bottom_tension_kN = float(self.H_N) / math.cos(alpha_final) / 1000.0
        # World-frame x array for plotting/exports: chute is at x_world=0,
        # TDP at x_world=tdp_x_world; cable internal x increases toward chute.
        if self.x is not None:
            self.x_world = self._tdp_x_world - self.x

        # Record TDP fixed-point and sliding-stability info in diagnostics.
        if self.diagnostics is not None:
            self.diagnostics.tdp_iterations = int(tdp_iters_used)
            self.diagnostics.tdp_x_world_m = float(self._tdp_x_world)
            self.diagnostics.tdp_depth_m = float(self.tdp_depth_m)
            self.diagnostics.tdp_slope_deg = float(self.tdp_slope_deg)
            tan_alpha = abs(math.tan(alpha_final))
            if tan_alpha > 0.4:
                self.diagnostics.warnings.append(
                    f"Seabed slope at TDP is {self.tdp_slope_deg:.1f}° "
                    f"(|tan α|={tan_alpha:.2f}); friction coefficient must exceed this for "
                    f"the cable to rest stably — sliding is likely on most soils."
                )
            elif tan_alpha > 0.2:
                self.diagnostics.warnings.append(
                    f"Seabed slope at TDP is {self.tdp_slope_deg:.1f}° "
                    f"(|tan α|={tan_alpha:.2f}); check that seabed friction is sufficient to "
                    f"prevent cable sliding."
                )

    def _solve_once(self):
        D = float(self.seabed.depth_at(self._tdp_x_world))
        c_top = self.cfg["chute_exit_height_m"]
        ds = self.cfg["ds_m"]
        mode = self.cfg["input_mode"]
        R = float(self.cfg.get("chute_radius_m", 0.0))
        residual_label = "Mode residual"
        residual_value = 0.0
        residual_units = ""

        def chute_contact_from_theta(theta_rad: float) -> Tuple[float, float]:
            if R <= 0:
                return 0.0, c_top
            theta_rad = float(max(0.0, min(math.pi / 2.0, theta_rad)))
            contact_length = R * theta_rad
            y_departure = c_top - R + R * math.cos(theta_rad)
            return contact_length, y_departure

        def solve_S_free_for_H(H):
            y_dep_min = c_top - R if R > 0 else c_top
            S_min = max(1e-3, D + max(y_dep_min, 0.0) + 1e-6)
            S_guess = max(self.cfg.get("S_guess_m", S_min * 1.5), S_min * 1.2)

            def fS(S_free: float) -> float:
                _, y_end, _, theta_end, _, _, _, _ = self.integrate(H, S_free, ds)
                Lc, y_dep = chute_contact_from_theta(theta_end)
                self.cfg["chute_contact_len_m"] = Lc
                return y_end - y_dep

            try:
                a, b = self._bracket_root(fS, x0=S_guess, step=max(5.0, 0.2 * S_guess))
                return self._bisect(fS, a, b, tol=1e-4)
            except Exception as exc:
                raise ValueError(
                    "Failed to find a free-span length S_free that reaches the chute departure height. "
                    f"This usually means the configuration is infeasible or the solver could not bracket a solution. "
                    f"Try increasing ds (coarser integration), reducing extreme point loads, or checking weights/units. "
                    f"(H={H/1000.0:.3f} kN, initial S_guess={S_guess:.3f} m)\n\nDetails: {exc}"
                )

        if mode == "Catenary Length":
            S_total_in = self.cfg["S_input_m"]

            def fH_total_len(H):
                S_free = solve_S_free_for_H(H)
                _, _, _, theta_end, _, _, _, _ = self.integrate(H, S_free, ds)
                Lc, _ = chute_contact_from_theta(theta_end)
                return (S_free + Lc) - S_total_in

            H0 = max(1.0, self.cfg.get("H_guess_N", 50_000.0))
            try:
                a, b = self._bracket_root(fH_total_len, x0=H0, step=max(5_000.0, 0.2 * H0))
                H = self._bisect(fH_total_len, a, b, tol=1e-3)
            except Exception as exc:
                raise ValueError(
                    "Failed to solve for Bottom Tension (H) that matches the requested total cable length. "
                    "This can happen if the required length is incompatible with the geometry/weights or if bracketing fails. "
                    f"(Requested S={S_total_in:.3f} m, initial H_guess={H0/1000.0:.3f} kN)\n\nDetails: {exc}"
                )
            S_free = solve_S_free_for_H(H)
            x_end, y_end, V_end, theta_end, T_top, s_arr, x_arr, y_arr = self.integrate(H, S_free, ds)
            Lc, _ = chute_contact_from_theta(theta_end)
            S = S_free + Lc
            residual_label = "Total cable length residual"
            residual_value = S - S_total_in
            residual_units = "m"

        elif mode == "Bottom Tension":
            H = self.cfg["H_input_N"]
            if H <= 0:
                raise ValueError("Bottom tension must be > 0.")
            S_free = solve_S_free_for_H(H)
            x_end, y_end, V_end, theta_end, T_top, s_arr, x_arr, y_arr = self.integrate(H, S_free, ds)
            Lc, _ = chute_contact_from_theta(theta_end)
            S = S_free + Lc
            residual_label = "Bottom tension residual"
            residual_value = (H - self.cfg["H_input_N"]) / 1000.0
            residual_units = "kN"

        elif mode == "Layback":
            layback_top_target = self.cfg["layback_input_m"]
            if layback_top_target <= 0:
                raise ValueError("Layback must be > 0.")

            def fH_layback_top(H):
                S_free = solve_S_free_for_H(H)
                x_end, _, _, theta_end, _, _, _, _ = self.integrate(H, S_free, ds)
                layback_top = x_end + R * math.sin(theta_end) if R > 0 else x_end
                return layback_top - layback_top_target

            H0 = max(1.0, self.cfg.get("H_guess_N", 50_000.0))
            try:
                a, b = self._bracket_root(fH_layback_top, x0=H0, step=max(5_000.0, 0.25 * H0))
                H = self._bisect(fH_layback_top, a, b, tol=1e-3)
            except Exception as exc:
                raise ValueError(
                    "Failed to solve for Bottom Tension (H) that matches the requested layback. "
                    "This can happen if the layback target is not achievable for the given geometry/weights, "
                    "or if bracketing fails due to non-monotonic behavior (strong buoyancy/point loads). "
                    f"(Requested layback={layback_top_target:.3f} m, initial H_guess={H0/1000.0:.3f} kN)\n\nDetails: {exc}"
                )
            S_free = solve_S_free_for_H(H)
            x_end, y_end, V_end, theta_end, T_top, s_arr, x_arr, y_arr = self.integrate(H, S_free, ds)
            Lc, _ = chute_contact_from_theta(theta_end)
            S = S_free + Lc
            layback_top = x_end + R * math.sin(theta_end) if R > 0 else x_end
            residual_label = "Layback residual"
            residual_value = layback_top - layback_top_target
            residual_units = "m"

        elif mode in ("Tangent Angle", "Exit Angle"):
            theta_target_rad = math.radians(self.cfg["exit_angle_from_h_deg"])
            if not (0 < theta_target_rad < math.radians(89.9)):
                raise ValueError("Tangent angle must be between 0 and 90 degrees (exclusive).")

            Lc_target, y_dep_target = chute_contact_from_theta(theta_target_rad)

            def fH_exit_angle(H):
                def fS(S_free):
                    _, y_end, _, _, _, _, _, _ = self.integrate(H, S_free, ds)
                    return y_end - y_dep_target

                S_min = max(1e-3, D + max(y_dep_target, 0.0) + 1e-6)
                S_guess = max(self.cfg.get("S_guess_m", S_min * 1.5), S_min * 1.2)
                aS, bS = self._bracket_root(fS, x0=S_guess, step=max(5.0, 0.2 * S_guess))
                S_free = self._bisect(fS, aS, bS, tol=1e-4)
                _, _, _, theta_end, _, _, _, _ = self.integrate(H, S_free, ds)
                return theta_end - theta_target_rad

            H0 = max(1.0, self.cfg.get("H_guess_N", 50_000.0))
            try:
                a, b = self._bracket_root(fH_exit_angle, x0=H0, step=max(5_000.0, 0.25 * H0))
                H = self._bisect(fH_exit_angle, a, b, tol=1e-6)
            except Exception as exc:
                raise ValueError(
                    "Failed to solve for H that matches the requested tangent angle. "
                    "This usually means the target angle is not achievable for the given geometry/weights, "
                    "or that strong point loads/buoyancy make the relationship non-monotonic. "
                    f"(Requested tangent angle={math.degrees(theta_target_rad):.3f}° from horizontal, initial H_guess={H0/1000.0:.3f} kN)\n\nDetails: {exc}"
                )

            def fS_final(S_free):
                _, y_end, _, _, _, _, _, _ = self.integrate(H, S_free, ds)
                return y_end - y_dep_target

            S_min = max(1e-3, D + max(y_dep_target, 0.0) + 1e-6)
            S_guess = max(self.cfg.get("S_guess_m", S_min * 1.5), S_min * 1.2)
            aS, bS = self._bracket_root(fS_final, x0=S_guess, step=max(5.0, 0.2 * S_guess))
            S_free = self._bisect(fS_final, aS, bS, tol=1e-4)
            self.cfg["chute_contact_len_m"] = Lc_target
            x_end, y_end, V_end, theta_end, T_top, s_arr, x_arr, y_arr = self.integrate(H, S_free, ds)
            S = S_free + Lc_target
            residual_label = "Tangent angle residual"
            residual_value = math.degrees(theta_end - theta_target_rad)
            residual_units = "deg"

        elif mode in ("Contact Tension", "Top Tension"):
            T_top_target_N = self.cfg["Ttop_input_N"]
            if T_top_target_N <= 0:
                raise ValueError("Contact tension must be > 0.")

            def fH_top_tension(H):
                S_free = solve_S_free_for_H(H)
                _, _, _, _, T_top, _, _, _ = self.integrate(H, S_free, ds)
                return T_top - T_top_target_N

            H0 = max(1.0, min(T_top_target_N * 0.9, self.cfg.get("H_guess_N", 50_000.0)))
            try:
                a, b = self._bracket_root(fH_top_tension, x0=H0, step=max(5_000.0, 0.25 * H0))
                H = self._bisect(fH_top_tension, a, b, tol=5.0)
            except Exception as exc:
                raise ValueError(
                    "Failed to solve for H that matches the requested contact tension. "
                    "This can happen if the requested contact tension is outside the achievable range for the given geometry/weights. "
                    f"(Requested contact tension={T_top_target_N/1000.0:.3f} kN, initial H_guess={H0/1000.0:.3f} kN)\n\nDetails: {exc}"
                )
            S_free = solve_S_free_for_H(H)
            x_end, y_end, V_end, theta_end, T_top, s_arr, x_arr, y_arr = self.integrate(H, S_free, ds)
            Lc, _ = chute_contact_from_theta(theta_end)
            S = S_free + Lc
            residual_label = "Contact tension residual"
            residual_value = (T_top - T_top_target_N) / 1000.0
            residual_units = "kN"

        else:
            raise ValueError("Invalid solve mode.")

        if "s_arr" not in locals():
            x_end, y_end, V_end, theta_end, T_top, s_arr, x_arr, y_arr = self.integrate(H, S, ds)

        self.H_N = H
        self.S_total = S
        self.layback = x_end + R * math.sin(theta_end) if R > 0 else x_end
        self.exit_angle_deg_from_h = math.degrees(theta_end)
        self.top_tension_kN = T_top / 1000.0
        self.bottom_tension_kN = H / 1000.0

        self.s = s_arr
        self.x = x_arr
        self.y = y_arr

        self.min_radius_m = self._compute_min_radius()

        if mode in ("Tangent Angle", "Exit Angle") and "y_dep_target" in locals():
            boundary_y_departure = float(y_dep_target)
        else:
            _, boundary_y_departure = chute_contact_from_theta(theta_end)

        self.diagnostics = self._build_diagnostics(
            input_mode=mode,
            ds_requested_m=ds,
            free_span_length_m=S_free,
            boundary_residual_m=float(y_end - boundary_y_departure),
            input_residual_label=residual_label,
            input_residual=float(residual_value),
            input_residual_units=residual_units,
            H_N=H,
            x_end=x_end,
            y_end=y_end,
            theta_end=theta_end,
            top_tension_N=T_top,
        )

    def _build_diagnostics(
        self,
        input_mode: str,
        ds_requested_m: float,
        free_span_length_m: float,
        boundary_residual_m: float,
        input_residual_label: str,
        input_residual: float,
        input_residual_units: str,
        H_N: float,
        x_end: float,
        y_end: float,
        theta_end: float,
        top_tension_N: float,
    ) -> SolverDiagnostics:
        integration_steps = max(0, len(self.s) - 1) if self.s is not None else 0
        ds_effective_m = free_span_length_m / integration_steps if integration_steps else float("nan")
        warnings = self._diagnostic_warnings(free_span_length_m)

        (
            refinement_position_delta_m,
            refinement_angle_delta_deg,
            refinement_top_tension_delta_kN,
            refinement_warning,
        ) = self._estimate_refinement_delta(
            H_N=H_N,
            free_span_length_m=free_span_length_m,
            ds_requested_m=ds_requested_m,
            x_end=x_end,
            y_end=y_end,
            theta_end=theta_end,
            top_tension_N=top_tension_N,
        )
        if refinement_warning:
            warnings.append(refinement_warning)

        return SolverDiagnostics(
            input_mode=input_mode,
            ds_requested_m=float(ds_requested_m),
            ds_effective_m=float(ds_effective_m),
            integration_steps=integration_steps,
            free_span_length_m=float(free_span_length_m),
            chute_contact_length_m=float(self.chute_contact_len_m),
            chute_contact_iterations=int(self._last_chute_contact_iterations),
            chute_contact_residual_m=float(self._last_chute_contact_residual_m),
            boundary_residual_m=float(boundary_residual_m),
            input_residual_label=input_residual_label,
            input_residual=float(input_residual),
            input_residual_units=input_residual_units,
            refinement_position_delta_m=refinement_position_delta_m,
            refinement_angle_delta_deg=refinement_angle_delta_deg,
            refinement_top_tension_delta_kN=refinement_top_tension_delta_kN,
            warnings=warnings,
        )

    def _estimate_refinement_delta(
        self,
        H_N: float,
        free_span_length_m: float,
        ds_requested_m: float,
        x_end: float,
        y_end: float,
        theta_end: float,
        top_tension_N: float,
    ) -> Tuple[Optional[float], Optional[float], Optional[float], str]:
        if ds_requested_m <= 0 or free_span_length_m <= 0:
            return None, None, None, "Refinement check did not run because ds or free-span length is invalid."

        refined_cfg = dict(self.cfg)
        refined_cfg["chute_contact_len_m"] = float(self.chute_contact_len_m)
        refined_calc = CatenarySystemCalculator(refined_cfg)
        try:
            refined_x, refined_y, _, refined_theta, refined_top_tension, _, _, _ = refined_calc.integrate(
                H_N,
                free_span_length_m,
                ds_requested_m * 0.5,
            )
        except Exception as exc:
            return None, None, None, f"Refinement check at half step did not run: {exc}"

        return (
            float(math.hypot(refined_x - x_end, refined_y - y_end)),
            float(abs(math.degrees(refined_theta - theta_end))),
            float(abs(refined_top_tension - top_tension_N) / 1000.0),
            "",
        )

    def _diagnostic_warnings(self, free_span_length_m: float) -> List[str]:
        warnings: List[str] = []
        assembly = self.cfg.get("assembly", [])
        comps = self.cfg.get("components", [])

        if not assembly:
            warnings.append("No assembly segments are defined; the solver is using internal fallback cable weights.")

        point_load_in_span = False
        if assembly:
            distance_from_top = 0.0
            for item in assembly:
                if item.kind == "segment":
                    distance_from_top += max(0.0, item.length_m)
                    continue
                if item.kind != "body" or abs(item.point_load_kN) < 1e-12:
                    continue
                if self.chute_contact_len_m <= distance_from_top <= self.chute_contact_len_m + free_span_length_m:
                    point_load_in_span = True
                    break
        else:
            point_load_in_span = any(
                component.is_point and 0.0 <= component.position_m <= free_span_length_m
                for component in comps
            )

        if point_load_in_span:
            warnings.append(
                "Point loads create tension-angle discontinuities; minimum-radius output is not engineering meaningful at those kinks."
            )

        return warnings

    def _compute_min_radius(self) -> float:
        if self.x is None or self.y is None:
            return float("inf")

        x = self.x
        y = self.y

        dx = np.gradient(x)
        dy = np.gradient(y)
        ddx = np.gradient(dx)
        ddy = np.gradient(dy)

        denom = np.power(dx * dx + dy * dy, 1.5)
        denom = np.where(denom < 1e-12, 1e-12, denom)

        kappa = np.abs(dx * ddy - dy * ddx) / denom
        kappa_clip = np.clip(kappa, 0, np.percentile(kappa, 99.5))

        max_kappa = float(np.max(kappa_clip))
        R_cat = float("inf") if max_kappa <= 1e-12 else 1.0 / max_kappa

        R_chute = self.cfg["chute_radius_m"]
        if R_chute > 0:
            return float(min(R_cat, R_chute))
        return float(R_cat)

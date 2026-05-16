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
        D = self.cfg["water_depth_m"]
        comps = self.cfg.get("components", [])
        assembly = self.cfg.get("assembly", [])
        R = float(self.cfg.get("chute_radius_m", 0.0))

        def integrate_once(L_chute_contact: float):
            s = 0.0
            x = 0.0
            y = -D
            V = 0.0

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
        D = self.cfg["water_depth_m"]
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

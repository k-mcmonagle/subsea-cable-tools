# -*- coding: utf-8 -*-
"""Persistence contracts and fingerprints for Installation Paths.

Pure Python: geometry calculation and report/tests can consume path rows
without opening QGIS layers.  Spatial map layers are rebuildable caches over
the WKT retained in ``bp_path_result``.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Dict, List, Optional, Sequence, Tuple

from . import tools as tools_mod

ALGORITHM_VERSION = "1"
MODE_FILLET = "fillet"
MODE_THROUGH = "through_ac"
MODES = (MODE_FILLET, MODE_THROUGH)
MODE_LABELS = {
    MODE_FILLET: "Fillet corners",
    MODE_THROUGH: "Pass through course changes",
}

DEFAULT_CONFIG = {
    "mode": MODE_FILLET,
    # 0 = report deviation but do not apply a hard corridor.
    "max_deviation_m": 0.0,
    "layback_id": "",
    "generate_barge": False,
    # Vessel used for the barge-track turn check and outline display.
    "vessel_id": "",
    # Water-depth-banded minimum turning radius: ordered
    # [{"max_depth_m": ..., "radius_m": ...}, ...].  A course change whose
    # water depth is at most ``max_depth_m`` uses that band's radius; empty
    # means the tool configuration's constant radius applies everywhere.
    "radius_rules": [],
    # Manual path adjustments: [{"kp": ..., "dcc_m": ...}, ...].  Each is a
    # point the tool path must additionally pass through, ``dcc_m`` metres
    # cross-course from the RPL at that KP (positive = port of travel).
    "adjustments": [],
}

# More shaping points than this is no longer "tweaking" — refuse early
# rather than letting the compound solver grind.
MAX_ADJUSTMENTS = 100


def sanitise_adjustments(raw) -> List[Dict[str, float]]:
    """Ordered, validated manual path adjustments; drops unusable entries."""
    out: List[Dict[str, float]] = []
    if not isinstance(raw, (list, tuple)):
        return out
    for value in raw:
        if isinstance(value, dict):
            kp, dcc = value.get("kp"), value.get("dcc_m")
        elif isinstance(value, (list, tuple)) and len(value) >= 2:
            kp, dcc = value[0], value[1]
        else:
            continue
        try:
            kp, dcc = float(kp), float(dcc)
        except (TypeError, ValueError):
            continue
        if math.isfinite(kp) and math.isfinite(dcc) and kp >= 0.0:
            out.append({"kp": round(kp, 5), "dcc_m": round(dcc, 2)})
    out.sort(key=lambda item: item["kp"])
    deduped: List[Dict[str, float]] = []
    for item in out:
        if deduped and abs(item["kp"] - deduped[-1]["kp"]) <= 1e-6:
            deduped[-1] = item   # later entry at the same KP wins
        else:
            deduped.append(item)
    return deduped[:MAX_ADJUSTMENTS]


def sanitise_radius_rules(raw) -> List[Dict[str, float]]:
    """Ordered, validated depth bands; silently drops unusable entries."""
    out: List[Dict[str, float]] = []
    if not isinstance(raw, (list, tuple)):
        return out
    for value in raw:
        if isinstance(value, dict):
            depth, radius = value.get("max_depth_m"), value.get("radius_m")
        elif isinstance(value, (list, tuple)) and len(value) >= 2:
            depth, radius = value[0], value[1]
        else:
            continue
        try:
            depth, radius = float(depth), float(radius)
        except (TypeError, ValueError):
            continue
        if math.isfinite(depth) and math.isfinite(radius) \
                and depth > 0.0 and radius > 0.0:
            out.append({"max_depth_m": depth, "radius_m": radius})
    out.sort(key=lambda item: item["max_depth_m"])
    deduped: List[Dict[str, float]] = []
    for item in out:
        if not deduped or item["max_depth_m"] \
                > deduped[-1]["max_depth_m"] + 1e-9:
            deduped.append(item)
    return deduped


def radius_for_depth(rules: Sequence[Dict[str, float]],
                     depth_m: float) -> Optional[float]:
    """First band covering the depth; None when deeper than every band."""
    depth = abs(float(depth_m))
    for rule in rules:
        if depth <= float(rule["max_depth_m"]) + 1e-9:
            return float(rule["radius_m"])
    return None


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def digest(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def config_from_plan(plan: Optional[Dict]) -> Dict:
    try:
        params = json.loads((plan or {}).get("params_json") or "{}")
    except (ValueError, TypeError):
        params = {}
    raw = params.get("installation_paths") if isinstance(params, dict) else {}
    out = dict(DEFAULT_CONFIG)
    if isinstance(raw, dict):
        out.update(raw)
    if out.get("mode") not in MODES:
        out["mode"] = MODE_FILLET
    try:
        out["max_deviation_m"] = max(
            0.0, float(out.get("max_deviation_m") or 0.0))
    except (TypeError, ValueError):
        out["max_deviation_m"] = 0.0
    out["layback_id"] = str(out.get("layback_id") or "")
    out["generate_barge"] = bool(out.get("generate_barge"))
    out["vessel_id"] = str(out.get("vessel_id") or "")
    out["radius_rules"] = sanitise_radius_rules(out.get("radius_rules"))
    out["adjustments"] = sanitise_adjustments(out.get("adjustments"))
    return out


def layback_points(row: Optional[Dict]) -> List[Tuple[float, float]]:
    if not row:
        return []
    try:
        values = json.loads(row.get("points_json") or "[]")
    except (ValueError, TypeError):
        return []
    out: List[Tuple[float, float]] = []
    if not isinstance(values, list):
        return out
    for value in values:
        if isinstance(value, dict):
            depth, layback = value.get("depth_m"), value.get("layback_m")
        elif isinstance(value, (list, tuple)) and len(value) >= 2:
            depth, layback = value[0], value[1]
        else:
            continue
        try:
            pair = (float(depth), float(layback))
        except (TypeError, ValueError):
            continue
        if all(math.isfinite(item) for item in pair):
            out.append(pair)
    out.sort()
    return out


def layback_profile_payload(row: Optional[Dict]) -> Dict:
    if not row:
        return {}
    return {
        "layback_id": str(row.get("layback_id") or ""),
        "name": str(row.get("name") or ""),
        "points": layback_points(row),
        "outside_mode": str(row.get("outside_mode") or "error"),
        "source_ref": str(row.get("source_ref") or ""),
        "modified_utc": str(row.get("modified_utc") or ""),
    }


def build_fingerprints(plan: Dict, route_fingerprint: str,
                       tool: Optional[Dict], config: Optional[Dict],
                       path_config: Dict,
                       layback_profile: Optional[Dict] = None,
                       depth_fingerprint: str = "") -> Dict[str, str]:
    radius_rules = sanitise_radius_rules(path_config.get("radius_rules"))
    tool_payload = {
        "algorithm": ALGORITHM_VERSION,
        "route": route_fingerprint or "",
        "scope": [plan.get("scope_start_kp"), plan.get("scope_end_kp")],
        "direction": int(plan.get("direction") or 1),
        "mode": path_config.get("mode") or MODE_FILLET,
        "max_deviation_m": float(path_config.get("max_deviation_m") or 0.0),
        "tool_id": str((tool or {}).get("tool_id") or ""),
        "tool_modified": str((tool or {}).get("modified_utc") or ""),
        "config_id": str((config or {}).get("config_id") or ""),
        "min_turn_radius_m": (config or {}).get("min_turn_radius_m"),
        "radius_rules": radius_rules,
        # Depth bands read the bathymetry at every course change, so the
        # geometry goes stale with the depth source; a constant radius
        # never samples depth.
        "radius_depth": depth_fingerprint if radius_rules else "",
    }
    # Only present when non-empty so pre-adjustment results keep their
    # stored fingerprints (adding the key unconditionally would flip every
    # existing result to stale on upgrade).
    adjustments = sanitise_adjustments(path_config.get("adjustments"))
    if adjustments:
        tool_payload["adjustments"] = adjustments
    tool_fp = digest(tool_payload)
    barge_payload = {
        "tool_path": tool_fp,
        "layback": layback_profile_payload(layback_profile),
        # A constant (one-point) profile does not sample bathymetry.
        "depth": depth_fingerprint if len(layback_points(layback_profile)) > 1
        else "",
    }
    return {"tool": tool_fp, "barge": digest(barge_payload)}


def parse_json_field(row: Optional[Dict], name: str, default):
    try:
        value = json.loads((row or {}).get(name) or "")
    except (ValueError, TypeError):
        return default
    return value


def result_state(row: Optional[Dict], current: Dict[str, str]
                 ) -> Dict[str, str]:
    if not row or not row.get("tool_path_wkt"):
        return {"tool": "missing", "barge": "missing"}
    stored = parse_json_field(row, "fingerprints_json", {})
    tool = "current" if stored.get("tool") == current.get("tool") else "stale"
    if not row.get("barge_track_wkt"):
        barge = "missing"
    else:
        barge = ("current" if stored.get("barge") == current.get("barge")
                 and tool == "current" else "stale")
    return {"tool": tool, "barge": barge}


def linestring_wkt(points: Sequence[Tuple[float, float]], places: int = 10
                   ) -> str:
    if len(points) < 2:
        return ""
    fmt = f"{{:.{int(places)}f}}"
    coords = ", ".join(f"{fmt.format(float(x))} {fmt.format(float(y))}"
                       for x, y in points)
    return f"LINESTRING ({coords})"


def parse_linestring_wkt(text: str) -> List[Tuple[float, float]]:
    raw = str(text or "").strip()
    if not raw.upper().startswith("LINESTRING"):
        return []
    start, end = raw.find("("), raw.rfind(")")
    if start < 0 or end <= start:
        return []
    out = []
    for token in raw[start + 1:end].split(","):
        bits = token.strip().split()
        if len(bits) < 2:
            continue
        try:
            out.append((float(bits[0]), float(bits[1])))
        except ValueError:
            return []
    return out


def effective_tool_and_config(plan: Dict, tools: Sequence[Dict]
                              ) -> Tuple[Optional[Dict], Optional[Dict]]:
    return tools_mod.plan_default_config(plan, tools)


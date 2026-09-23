# -*- coding: utf-8 -*-
"""Persisted analysis results and their currency (pure python).

The Exclusions tab's last recompute and the Risk Profile's last scan used to
live only in widget memory: after reopening the project the fire bars fell
back to the last *Generate* (or showed nothing) and the Risk Profile could
not tell "ran, found nothing" from "never run". ``bp_analysis`` now keeps the
latest results per plan together with fingerprints of everything they were
computed from, and this module decides what is still current:

- a **global** fingerprint (scope, direction, method, search step, sliver
  tolerance, route geometry) — any change makes every result out of date;
- a **per-criterion / per-check** fingerprint of the settings that change
  its result (kind, action, class, methods, config, the registered input it
  reads and — for depth/slope criteria — the stored bathymetry profile).
  Names, notes and stack order are deliberately excluded: renaming a
  criterion must not mark its bar stale.

Editing a layer's features on disk is not detected (re-hashing every input
layer on each refresh would be too slow); the tabs say so in their tooltips.
"""

from __future__ import annotations

import hashlib
import json
from typing import Dict, List, Optional, Sequence, Tuple

STATE_CURRENT = "current"
STATE_CHANGED = "changed"      # evaluated, but its settings changed since
STATE_NEW = "new"              # never evaluated in the stored run
STATE_NONE = "none"            # no stored run at all

_GLOBAL_LABELS = {
    "scope": "the scope",
    "direction": "the direction of installation",
    "method": "the burial method",
    "coarse_step_m": "the sample step",
    "sliver_tol_km": "the sliver tolerance",
    "refine_tol_m": "the boundary refinement",
    "route": "the route geometry",
}

# Rule fields that change what a criterion evaluates to.
_RULE_KEYS = ("kind", "action", "risk_level", "criterion_class",
              "methods_json", "config_json", "enabled")
_CHECK_KEYS = ("config_json", "enabled")


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      default=str)


def digest(value) -> str:
    return hashlib.sha1(canonical(value).encode("utf-8")).hexdigest()[:16]


def _json_field(text) -> object:
    """Parse a JSON column for fingerprinting (key order must not matter)."""
    if isinstance(text, (dict, list)):
        return text
    try:
        return json.loads(text or "null")
    except (TypeError, ValueError):
        return str(text or "")


def _input_binding(config: object, inputs_by_id: Dict[str, Dict]) -> Dict:
    """The registered-input rows a config references, by what they point at."""
    ids: List[str] = []

    def walk(value) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "input_id" and item:
                    ids.append(str(item))
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(config)
    binding = {}
    for input_id in sorted(set(ids)):
        row = inputs_by_id.get(input_id) or {}
        binding[input_id] = [str(row.get("layer_source") or ""),
                             str(row.get("config_json") or "")] if row else None
    return binding


def rule_fingerprint(rule: Dict, inputs_by_id: Dict[str, Dict],
                     profile_stamp: str = "") -> str:
    config = _json_field(rule.get("config_json"))
    payload = {key: (_json_field(rule.get(key)) if key.endswith("_json")
                     else rule.get(key)) for key in _RULE_KEYS}
    payload["inputs"] = _input_binding(config, inputs_by_id)
    if (rule.get("kind") or "") == "threshold_profile":
        # Depth/slope criteria evaluate the stored bathymetry profile.
        payload["profile"] = profile_stamp or ""
    return digest(payload)


def check_fingerprint(check: Dict, inputs_by_id: Dict[str, Dict]) -> str:
    config = _json_field(check.get("config_json"))
    payload = {key: (_json_field(check.get(key)) if key.endswith("_json")
                     else check.get(key)) for key in _CHECK_KEYS}
    payload["inputs"] = _input_binding(config, inputs_by_id)
    return digest(payload)


def global_fingerprint(params, route_fp: str = "") -> Dict[str, str]:
    """Named components so a mismatch can say *what* changed."""
    scope = params.scope
    return {
        "scope": f"{scope.start_km:.6f}-{scope.end_km:.6f}",
        "direction": str(1 if int(params.direction or 1) >= 0 else -1),
        "method": str(params.method or ""),
        "coarse_step_m": f"{float(params.coarse_step_m):.3f}",
        "sliver_tol_km": f"{float(params.sliver_tol_km):.6f}",
        "refine_tol_m": f"{float(params.refine_tol_m):.4f}",
        "route": route_fp or "",
    }


def exclusion_fingerprints(params, rules: Sequence[Dict],
                           inputs: Sequence[Dict], route_fp: str = "",
                           profile_stamp: str = "") -> Dict:
    by_id = {str(r.get("input_id") or ""): r for r in inputs or []}
    return {
        "global": global_fingerprint(params, route_fp),
        "rules": {str(r.get("rule_id")): rule_fingerprint(r, by_id,
                                                          profile_stamp)
                  for r in rules or []},
    }


def compare_global(stored: Optional[Dict], current: Dict) -> List[str]:
    """Human reasons why a stored run no longer matches (empty = current).

    A stored component that is empty (e.g. no route fingerprint recorded)
    is not compared — old rows must not report phantom changes.
    """
    if not isinstance(stored, dict):
        return []
    reasons = []
    for key, label in _GLOBAL_LABELS.items():
        old = str(stored.get(key) or "")
        new = str(current.get(key) or "")
        if old and new and old != new:
            reasons.append(label)
    return reasons


def compare_items(stored: Optional[Dict], current: Dict[str, str]
                  ) -> Dict[str, str]:
    """Per-item state: current / changed / new (``STATE_NONE`` when no
    stored run exists at all)."""
    if not isinstance(stored, dict):
        return {key: STATE_NONE for key in current}
    out = {}
    for key, fp in current.items():
        old = stored.get(key)
        if old is None:
            out[key] = STATE_NEW
        elif old == fp:
            out[key] = STATE_CURRENT
        else:
            out[key] = STATE_CHANGED
    return out


# -- (de)serialisation of the bp_analysis row ----------------------------------

def encode_exclusions(context_dict: Dict, nodata: Sequence[Tuple[float, float]],
                      fingerprints: Dict, message: str, warnings: Sequence[str],
                      run_utc: str) -> Dict:
    """Columns for the Exclusions part of a bp_analysis row."""
    payload = dict(context_dict or {})
    payload["nodata"] = [[float(a), float(b)] for a, b in nodata or []]
    payload["warnings"] = [str(w) for w in warnings or []]
    return {
        "run_utc": run_utc,
        "context_json": canonical(payload),
        "fingerprints_json": canonical(fingerprints or {}),
        "message": message or "",
    }


def decode_json(text, default):
    try:
        value = json.loads(text or "")
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def risk_runs(row: Optional[Dict]) -> Dict:
    """``{"runs": {check_id: {...}}, "message": str, "run_utc": str}``."""
    data = decode_json((row or {}).get("risk_json"), {})
    runs = data.get("runs")
    data["runs"] = runs if isinstance(runs, dict) else {}
    return data


def record_risk_run(existing: Dict, check_fps: Dict[str, str],
                    counts: Dict[str, int], warnings: Dict[str, List[str]],
                    message: str, run_utc: str) -> Dict:
    """Merge one scan's per-check results into the stored run records."""
    data = {"runs": dict((existing or {}).get("runs") or {})}
    for check_id, fp in check_fps.items():
        data["runs"][str(check_id)] = {
            "run_utc": run_utc,
            "fp": fp,
            "count": int(counts.get(check_id, 0)),
            "warnings": list(warnings.get(check_id, []))[:5],
        }
    data["message"] = message or ""
    data["run_utc"] = run_utc
    return data


def format_utc(value: str) -> str:
    """``2026-09-23T14:02:11Z`` → ``2026-09-23 14:02 UTC`` (display)."""
    text = str(value or "")
    if len(text) >= 16 and text[10] in "T ":
        return text[:10] + " " + text[11:16] + " UTC"
    return text

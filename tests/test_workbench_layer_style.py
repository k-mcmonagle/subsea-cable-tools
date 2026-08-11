# -*- coding: utf-8 -*-
"""Pure-Python checks for the workbench standard-styling helpers (no QGIS).

Covers the deterministic cable-type colour mapping in
workbench/layer_style.py: canonical armour codes, hashed fallback stability,
and value normalisation. The qgis-facing apply_* functions are exercised by
the QGIS smoke tests.

Run directly: ``python tests/test_workbench_layer_style.py``.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Load the single module by path (plugin folder name has hyphens, and
# layer_style has no package-relative imports).
_spec = importlib.util.spec_from_file_location(
    "sct_layer_style", ROOT / "workbench" / "layer_style.py")
layer_style = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = layer_style
_spec.loader.exec_module(layer_style)


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


def test_normalise_cable_type() -> bool:
    cases = {
        "da": "DA",
        " D.A. ": "DA",
        "sa-h": "SAH",
        "": "",
        None: "",
        "Lw": "LW",
    }
    ok = all(layer_style.normalise_cable_type(k) == v for k, v in cases.items())
    return _result("normalise_cable_type", ok)


def test_known_types_have_canonical_colours() -> bool:
    ok = True
    for token, colour in layer_style.KNOWN_CABLE_TYPE_COLOURS.items():
        ok = ok and layer_style.colour_for_cable_type(token) == colour
    # case / punctuation insensitive
    ok = ok and layer_style.colour_for_cable_type("d.a") == \
        layer_style.KNOWN_CABLE_TYPE_COLOURS["DA"]
    return _result("canonical colours", ok)


def test_unknown_types_stable_and_valid() -> bool:
    a1 = layer_style.colour_for_cable_type("SPECIAL-X")
    a2 = layer_style.colour_for_cable_type("special x")
    b = layer_style.colour_for_cable_type("OTHER-TYPE-42")
    ok = a1 == a2  # normalisation-stable
    ok = ok and a1 in layer_style.FALLBACK_PALETTE
    ok = ok and b in layer_style.FALLBACK_PALETTE
    return _result("fallback colours deterministic", ok, f"{a1} / {b}")


def test_unset_colour() -> bool:
    ok = layer_style.colour_for_cable_type("") == layer_style.UNSET_COLOUR
    ok = ok and layer_style.colour_for_cable_type(None) == layer_style.UNSET_COLOUR
    return _result("unset cable type colour", ok)


def test_hex_format() -> bool:
    pattern = re.compile(r"^#[0-9a-f]{6}$")
    colours = list(layer_style.KNOWN_CABLE_TYPE_COLOURS.values())
    colours += layer_style.FALLBACK_PALETTE
    colours.append(layer_style.UNSET_COLOUR)
    ok = all(pattern.match(c) for c in colours)
    return _result("all colours are lowercase hex", ok)


def run_all():
    return [
        test_normalise_cable_type(),
        test_known_types_have_canonical_colours(),
        test_unknown_types_stable_and_valid(),
        test_unset_colour(),
        test_hex_format(),
    ]


def main() -> int:
    results = run_all()
    failures = results.count(False)
    print(f"{len(results) - failures}/{len(results)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""Burial graph templates (pure Python): sizes, page windows, tokens, ticks, checks.

* A3 / A4 landscape and portrait, a custom size; margins and panels checked
  with plain messages; a stored template normalises to a complete one and
  round-trips through JSON; the fingerprint ignores the name.
* Page windows cover the range with the chosen overlap; one page when the
  range fits.
* Tokens fill in per page; unknown tokens stay as typed.
* Ticks fall on round steps (1, 2, 2.5, 5 x 10^n) and label with the right
  decimals.
"""

from __future__ import annotations

from ..burial.acquired import report_template as T

REQUIRES_QGIS = False


def _result(name: str, ok: bool, detail: str = "") -> bool:
    ok = bool(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def _check(name: str, checks: dict) -> bool:
    failed = [k for k, v in checks.items() if not v]
    return _result(name, not failed, ", ".join(failed))


def test_sizes_and_checks() -> bool:
    a3 = T.default_template()
    portrait = T.normalise({**a3, "page": {**a3["page"], "orientation": "portrait"}})
    custom = T.normalise({**a3, "page": {"size": "Custom", "width_mm": 300, "height_mm": 1000}})
    tiny = T.normalise({**a3, "page": {"size": "Custom", "width_mm": 90, "height_mm": 80}})
    margins = T.normalise({**a3, "page": {**a3["page"], "margin_mm": 60}})
    bars_only = T.normalise({**a3, "panels": [{"kind": "status"}, {"kind": "source"}]})
    bad_axis = T.normalise({**a3, "panels": [{"kind": "dol", "y_min": 2, "y_max": 1}]})
    junk = T.normalise({"panels": [{"kind": "pie"}, "x", {"kind": "channel", "weight": -3}], "overlap_pct": 90})
    return _check("templates: sizes, normalising, checks", {
        "a3": T.page_size_mm(a3) == (420.0, 297.0) and T.problems(a3) == [],
        "portrait": T.page_size_mm(portrait) == (297.0, 420.0),
        "custom": T.page_size_mm(custom) == (1000.0, 300.0),
        "too small": any("too small" in p for p in T.problems(tiny)),
        "margins": any("margins" in p for p in T.problems(margins)),
        "bars only": any("DOL or channel" in p for p in T.problems(bars_only)),
        "axis": any("minimum must be below" in p for p in T.problems(bad_axis)),
        "junk cleaned": [p["kind"] for p in junk["panels"]] == ["channel"] and junk["panels"][0]["weight"] == 0.2
        and junk["overlap_pct"] == 50.0,
        "json": T.from_json(T.to_json(a3)) == T.normalise(a3),
        "fingerprint ignores name": T.fingerprint_text(a3) == T.fingerprint_text({**a3, "name": "Other"}),
        "builtins": [t["name"] for t in T.builtin_templates()] == ["Burial graph A3", "Burial graph A4"],
    })


def test_windows_tokens_ticks() -> bool:
    windows = T.page_windows((0.0, 12.0), 5.0, 5.0)
    exact = T.page_windows((10.0, 15.0), 5.0, 0.0)
    short = T.page_windows((3.0, 3.4), 5.0, 5.0)
    text = T.expand("{plan} KP {kp_start}-{kp_end} p{page}/{pages} {nope}",
                    {"plan": "Main lay", "kp_start": "0.000", "kp_end": "5.000", "page": "1", "pages": "3"})
    ticks = T.nice_ticks(0.0, 5.0, 5)
    odd = T.nice_ticks(3.2, 7.9, 5)
    small = T.nice_ticks(0.42, 0.58, 4)
    return _check("page windows, tokens, ticks", {
        "windows": [(round(a, 3), round(b, 3)) for a, b in windows] == [(0.0, 5.0), (4.75, 9.75), (9.5, 14.5)],
        "exact": exact == [(10.0, 15.0)],
        "short": short == [(3.0, 8.0)],
        "tokens": text == "Main lay KP 0.000-5.000 p1/3 {nope}",
        "ticks": ticks == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0],
        "odd": odd == [4.0, 5.0, 6.0, 7.0],
        "small": small[0] >= 0.42 and small[-1] <= 0.58 and len(small) >= 3,
        "labels": T.tick_label(2.5, 0.5) == "2.5" and T.tick_label(4.0, 1.0) == "4"
        and T.tick_label(0.45, 0.05) == "0.45" and T.tick_label(7.5, 2.5) == "7.5",
        "given step": T.nice_ticks(0.0, 2.0, 5, step=0.5) == [0.0, 0.5, 1.0, 1.5, 2.0],
    })


def run_all() -> list:
    return [test_sizes_and_checks(), test_windows_tokens_ticks()]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)

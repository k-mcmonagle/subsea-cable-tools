# -*- coding: utf-8 -*-
"""Checks for the Burial Planner HTML report (pure python)."""

from __future__ import annotations

import json

from ..burial import report, schema


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


def _plan():
    return {
        "plan_id": "0123456789abcdef", "name": "Report <Plan> & Co",
        "description": "desc", "notes": "assumption notes",
        "method": schema.METHOD_PLOUGH, "rpl_name": "Route A", "rpl_revision": "Rev 2",
        "scope_start_kp": 5.0, "scope_end_kp": 25.0, "direction": 1,
        "status": schema.PLAN_STATUS_DRAFT, "rev_label": "Rev 1",
        "target_burial_m": 1.5,
    }


def _sections():
    return [
        {"section_id": "s1", "kind": schema.SECTION_BURIAL, "start_kp": 5.0,
         "end_kp": 15.0, "length_km": 10.0, "state": "candidate",
         "conclusion": schema.CONCLUSION_NORMAL, "confidence": "high",
         "reason_json": json.dumps({"screening": [
             {"rule": "Steep slope", "start_kp": 6.0, "end_kp": 7.0}]}),
         "notes": "ok"},
        {"section_id": "s2", "kind": schema.SECTION_SKIP, "start_kp": 15.0,
         "end_kp": 20.0, "length_km": 5.0, "state": "candidate",
         "conclusion": "", "confidence": "",
         "reason_json": json.dumps({"dominant_rule": "Cable crossing",
                                    "fired_rules": ["Cable crossing"]}),
         "notes": ""},
        {"section_id": "s3", "kind": schema.SECTION_INSUFFICIENT,
         "start_kp": 20.0, "end_kp": 25.0, "length_km": 5.0,
         "state": "candidate",
         "conclusion": schema.CONCLUSION_INSUFFICIENT,
         "confidence": "insufficient",
         "reason_json": json.dumps({"insufficient_information": True}),
         "notes": ""},
    ]


def _events():
    return [
        {"event_id": "e1", "seq": 0, "event_type": schema.EVENT_BURIAL_START,
         "kp": 5.0, "lat": 50.1234567, "lon": -4.2, "depth_m": 55.2,
         "source": "auto", "status": "confirmed", "locked": 1, "notes": ""},
        {"event_id": "e2", "seq": 1, "event_type": schema.EVENT_BURIAL_END,
         "kp": 15.0, "lat": 50.2, "lon": -4.4, "depth_m": None,
         "source": "manual", "status": "candidate", "locked": 0,
         "notes": "client <request>"},
    ]


def _rules():
    return [
        {"rule_id": "r1", "seq": 0, "name": "Depth > 1500 m", "enabled": 1,
         "kind": "threshold_profile", "action": "exclude",
         "criterion_class": schema.CRITERION_NON_DEVIABLE,
         "source_ref": "Guide v3 §2.1",
         "config_json": json.dumps({"profile": "depth", "op": ">",
                                    "value": 1500.0, "extend_m": 100.0}),
         "notes": ""},
        {"rule_id": "r2", "seq": 1, "name": "Signed slope", "enabled": 1,
         "kind": "threshold_profile", "action": "exclude",
         "criterion_class": schema.CRITERION_PROJECT, "source_ref": "",
         "config_json": json.dumps({"profile": "slope", "slope_signed": True,
                                    "downslope_max_deg": 8.0,
                                    "upslope_max_deg": 10.0,
                                    "slope_window_m": 12.0}),
         "notes": ""},
        {"rule_id": "r3", "seq": 2, "name": "Crossings", "enabled": 0,
         "kind": "proximity", "action": "exclude",
         "criterion_class": schema.CRITERION_SCREENING, "source_ref": "",
         "config_json": json.dumps({"distance_m": 250.0,
                                    "buffer_field": "buffer_m",
                                    "scope_ranges": [
                                        {"start_kp": 5.0, "end_kp": 10.0}]}),
         "notes": ""},
    ]


def _report_html(**overrides):
    kwargs = dict(
        plan=_plan(), sections=_sections(), events=_events(), rules=_rules(),
        inputs=[{"role": schema.INPUT_ROLE_BATHY, "layer_name": "MBES 2 m",
                 "originator": "Survey Co", "revision": "B", "status": "current",
                 "received_utc": "2026-01-01", "quality": "high", "notes": ""}],
        generation={"generation_id": "gen12345678", "run_utc": "2026-02-02T10:00:00Z",
                    "params_json": json.dumps({"coarse_step_m": 50.0}),
                    "inputs_fingerprint_json": json.dumps({"r1": "abc"})},
        change_log=[{"seq": 0, "utc": "2026-02-02T10:00:01Z", "user": "kieran",
                     "action": "generate", "target_id": "gen12345678",
                     "reason": ""}],
        now_utc="2026-02-03T00:00:00Z",
    )
    kwargs.update(overrides)
    return report.build_report_html(**kwargs)


def test_report_content() -> bool:
    html_text = _report_html()
    checks = [
        # header + escaping
        "Report &lt;Plan&gt; &amp; Co" in html_text,
        "client &lt;request&gt;" in html_text,
        "Plough" in html_text,
        "Rev 2" in html_text,
        "KP 5.000" in html_text,
        # summary numbers
        "10.000 km (50%)" in html_text,
        "Within Normal Operating Envelope" in html_text,
        # sections/events/labels
        "Candidate Plough Section" in html_text,
        "Plough Skip" in html_text,
        "PLDN" in html_text and "PLUP" in html_text,
        # rules with source references and condition summaries
        "Guide v3 §2.1" in html_text,
        "depth &gt; 1500.0 m" in html_text,
        "extended 100/100 m (before/after)" in html_text,
        "disabled" in html_text,
        # provenance + log + footer
        "gen12345" in html_text,
        "generate" in html_text,
        "2026-02-03T00:00:00Z" in html_text,
    ]
    ok = all(checks)
    detail = "" if ok else f"failed checks: {[i for i, c in enumerate(checks) if not c]}"
    return _result("report contains header, summary, tables, provenance", ok, detail)


def test_report_profile_image_and_optionals() -> bool:
    png = b"\x89PNG\r\n\x1a\nfakepayload"
    with_img = _report_html(profile_png=png)
    ok = "data:image/png;base64," in with_img
    without = _report_html(profile_png=None, generation=None, change_log=None,
                           inputs=[])
    ok = ok and "data:image/png;base64," not in without
    ok = ok and "Generation provenance" not in without
    ok = ok and "Change log" not in without
    ok = ok and "Input data register" not in without
    ok = ok and "<html>" in without and "</html>" in without
    return _result("report embeds profile PNG; optional blocks drop cleanly", ok)


def test_rule_condition_text() -> bool:
    rules = _rules()
    ok = "down-slope > 8.0°" in report.rule_condition_text(rules[1])
    ok = ok and "slope window 12.0 m" in report.rule_condition_text(rules[1])
    prox = report.rule_condition_text(rules[2])
    ok = ok and "within 250.0 m" in prox and "buffer_m" in prox
    ok = ok and "applies KP 5.000-10.000" in prox
    manual = report.rule_condition_text(
        {"kind": "manual", "config_json": json.dumps(
            {"ranges": [{"start_kp": 1.0, "end_kp": 2.0}]})})
    ok = ok and "1.000-2.000" in manual
    return _result("rule condition summaries per kind", ok)


def run_all() -> list:
    return [
        test_report_content(),
        test_report_profile_image_and_optionals(),
        test_rule_condition_text(),
    ]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)

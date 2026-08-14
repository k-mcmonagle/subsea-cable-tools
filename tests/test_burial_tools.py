# -*- coding: utf-8 -*-
"""Checks for the Burial Tools registry and method vocabulary (pure python).

Tool/config resolution, plan-default inheritance, registry JSON round trip,
trencher vocabulary, method-alias normalisation, schema registration.
"""

from __future__ import annotations

import json

from ..burial import events as ev
from ..burial import io_csv, schema
from ..burial import tools as tools_mod


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


def _tool(tool_id="t1", name="Plough X", tool_type=schema.METHOD_PLOUGH,
          configs=None):
    return {
        "tool_id": tool_id, "name": name, "tool_type": tool_type,
        "source_ref": "Spec 001 Rev A",
        "configs_json": json.dumps(configs if configs is not None else [
            {"config_id": "c1", "label": "Jetting 3 m", "mode": "jetting",
             "track_width_m": 4.5, "min_turn_radius_m": 250.0},
            {"config_id": "c2", "label": "Passive 2 m share",
             "mode": "passive"},
        ]),
        "footprint_wkt": "", "footprint_source": "", "footprint_scale": None,
        "footprint_crp_x": None, "footprint_crp_y": None,
        "footprint_rotation_deg": None, "length_m": None, "width_m": None,
        "notes": "", "created_utc": "", "modified_utc": "",
    }


def test_trencher_vocabulary() -> bool:
    ok = schema.METHOD_TRENCHER in schema.METHODS
    ok = ok and ev.event_label(schema.EVENT_BURIAL_START,
                               schema.METHOD_TRENCHER) == "TRENCH_START"
    ok = ok and ev.event_label(schema.EVENT_BURIAL_END,
                               schema.METHOD_TRENCHER) == "TRENCH_END"
    ok = ok and schema.section_ref_code(
        schema.SECTION_BURIAL, schema.METHOD_TRENCHER) == "TS"
    ok = ok and schema.section_kind_label(
        schema.SECTION_BURIAL, schema.METHOD_TRENCHER) == "Candidate Trench Section"
    ok = ok and "TS = Candidate Trench Section" in \
        schema.section_ref_legend(schema.METHOD_TRENCHER)
    return _result("trencher method vocabulary", ok)


def test_plough_vocabulary_unchanged() -> bool:
    ok = ev.event_label(schema.EVENT_BURIAL_START, schema.METHOD_PLOUGH) == "PLDN"
    ok = ok and schema.section_kind_label(
        schema.SECTION_BURIAL, schema.METHOD_PLOUGH) == "Candidate Plough Section"
    ok = ok and schema.section_ref_legend(schema.METHOD_PLOUGH) == (
        "PS = Candidate Plough Section, SK = Plough Skip, "
        "II = Insufficient Information — numbered in travel order")
    ok = ok and schema.section_kind_label(
        schema.SECTION_SKIP, "") == "Skip"
    return _result("plough/default vocabulary unchanged", ok)


def test_method_alias_normalisation() -> bool:
    ok = schema.normalise_method("jet") == schema.METHOD_ROV_JET
    ok = ok and schema.normalise_method("plough") == schema.METHOD_PLOUGH
    ok = ok and schema.normalise_method("surface") == "surface"
    ok = ok and schema.normalise_methods(["plough", "jet", "jet", ""]) == \
        [schema.METHOD_PLOUGH, schema.METHOD_ROV_JET]
    return _result("method alias normalisation (jet -> rov_jet)", ok)


def test_trencher_event_type_import_aliases() -> bool:
    ok = io_csv.normalise_event_type("TRENCH_START") == schema.EVENT_BURIAL_START
    ok = ok and io_csv.normalise_event_type("trench_end") == schema.EVENT_BURIAL_END
    ok = ok and io_csv.normalise_event_type("TRENCH_STOP") == schema.EVENT_BURIAL_END
    return _result("trencher event-type import aliases", ok)


def test_tool_registration_in_schema() -> bool:
    ok = schema.TABLE_TOOL in schema.REGISTRY_TABLES
    ok = ok and schema.TABLE_KEYS.get(schema.TABLE_TOOL) == "tool_id"
    section_cols = [name for name, _t in schema.SECTION_FIELDS]
    ok = ok and "tool_id" in section_cols and "tool_config_id" in section_cols
    layer_cols = [name for name, _t in schema.SECTIONS_LAYER_FIELDS]
    ok = ok and "tool" in layer_cols
    ok = ok and schema.SCHEMA_VERSION >= 6
    return _result("tool table + section columns registered", ok)


def test_config_parsing_and_display() -> bool:
    tool = _tool()
    configs = tools_mod.parse_configs(tool)
    ok = len(configs) == 2
    ok = ok and tools_mod.config_by_id(tool, "c2").get("mode") == "passive"
    # mode is suppressed when the label already contains it
    ok = ok and tools_mod.config_label(configs[0]) == "Jetting 3 m"
    ok = ok and tools_mod.config_label(
        {"label": "3 m share", "mode": "passive"}) == "3 m share (passive)"
    ok = ok and tools_mod.tool_display([tool], "t1", "c1") == \
        "Plough X — Jetting 3 m"
    ok = ok and tools_mod.tool_display([tool], "missing") == "(unregistered tool)"
    ok = ok and tools_mod.tool_display([tool], "") == ""
    bad = dict(tool, configs_json="not json")
    ok = ok and tools_mod.parse_configs(bad) == []
    return _result("config parsing + display resolution", ok)


def test_section_tool_inheritance() -> bool:
    tool = _tool()
    plan = {"params_json": json.dumps({"tool_id": "t1",
                                       "tool_config_id": "c2"})}
    burial = {"kind": schema.SECTION_BURIAL, "tool_id": "", "tool_config_id": ""}
    explicit = {"kind": schema.SECTION_BURIAL, "tool_id": "t1",
                "tool_config_id": "c1"}
    skip = {"kind": schema.SECTION_SKIP, "tool_id": "t1"}
    ok = tools_mod.section_tool_display(burial, plan, [tool]) == \
        "Plough X — Passive 2 m share"
    ok = ok and tools_mod.section_tool_display(explicit, plan, [tool]) == \
        "Plough X — Jetting 3 m"
    ok = ok and tools_mod.section_tool_display(skip, plan, [tool]) == ""
    ok = ok and tools_mod.section_tool_display(burial, {}, [tool]) == ""
    # A config chosen on a section that inherits the plan's tool overrides
    # the default configuration (the Plan Builder allows exactly that).
    inherited_config = {"kind": schema.SECTION_BURIAL, "tool_id": "",
                        "tool_config_id": "c1"}
    ok = ok and tools_mod.section_tool_display(
        inherited_config, plan, [tool]) == "Plough X — Jetting 3 m"
    return _result("section tool inheritance (blank = plan default)", ok)


def test_registry_json_round_trip() -> bool:
    tools = [_tool(), _tool("t2", "Trencher Y", schema.METHOD_TRENCHER, [])]
    text = tools_mod.registry_json(tools)
    parsed = tools_mod.parse_registry_json(text)
    ok = len(parsed) == 2
    ok = ok and parsed[0]["tool_id"] == "t1"
    ok = ok and parsed[1]["tool_type"] == schema.METHOD_TRENCHER
    ok = ok and tools_mod.parse_configs(parsed[0])[0]["config_id"] == "c1"
    # Alias tool types are normalised on import; ids invented when missing.
    hand_edited = json.dumps({
        "format": tools_mod.TOOL_REGISTRY_FORMAT, "version": 1,
        "tools": [{"name": "Jet Z", "tool_type": "jet"}]})
    parsed2 = tools_mod.parse_registry_json(hand_edited)
    ok = ok and parsed2[0]["tool_type"] == schema.METHOD_ROV_JET
    ok = ok and bool(parsed2[0]["tool_id"])
    failed = False
    try:
        tools_mod.parse_registry_json(json.dumps({"format": "other"}))
    except ValueError:
        failed = True
    ok = ok and failed
    # Hand-edited numeric strings coerce (or drop) instead of crashing the
    # registry table later; empty timestamps are left for the store to stamp.
    messy = json.dumps({
        "format": tools_mod.TOOL_REGISTRY_FORMAT, "version": 1,
        "tools": [{"name": "T", "tool_type": "plough",
                   "length_m": "12,5", "width_m": "not a number",
                   "created_utc": ""}]})
    parsed3 = tools_mod.parse_registry_json(messy)[0]
    ok = ok and parsed3["length_m"] == 12.5
    ok = ok and parsed3["width_m"] is None
    ok = ok and "created_utc" not in parsed3
    return _result("registry JSON round trip + validation", ok)


def test_sections_csv_tool_column() -> bool:
    tool = _tool()
    plan = {"name": "P", "plan_id": "p1", "direction": 1,
            "method": schema.METHOD_PLOUGH,
            "params_json": json.dumps({"tool_id": "t1",
                                       "tool_config_id": "c1"})}
    sections = [
        {"section_id": "s1", "kind": schema.SECTION_BURIAL, "start_kp": 0.0,
         "end_kp": 1.0, "length_km": 1.0, "tool_id": "", "tool_config_id": ""},
        {"section_id": "s2", "kind": schema.SECTION_SKIP, "start_kp": 1.0,
         "end_kp": 2.0, "length_km": 1.0},
    ]
    text = io_csv.sections_csv(plan, sections, tools=[tool])
    ok = "tool" in text  # header present
    ok = ok and "Plough X — Jetting 3 m" in text
    return _result("sections CSV carries the resolved tool", ok)


def run_all() -> list:
    return [
        test_trencher_vocabulary(),
        test_plough_vocabulary_unchanged(),
        test_method_alias_normalisation(),
        test_trencher_event_type_import_aliases(),
        test_tool_registration_in_schema(),
        test_config_parsing_and_display(),
        test_section_tool_inheritance(),
        test_registry_json_round_trip(),
        test_sections_csv_tool_column(),
    ]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)

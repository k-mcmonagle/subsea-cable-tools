# -*- coding: utf-8 -*-
"""Checks for the user-defined Planner operation-type library and JSON sharing."""

from ..planner import operation_types


def _result(name, ok, detail=""):
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""))
    return ok


def test_slugify_and_defaults():
    ok = operation_types.slugify("Post-lay Burial!") == "post_lay_burial"
    ok = ok and operation_types.slugify("   ") == ""
    ok = ok and operation_types.default_operation_types() == []
    ok = ok and len(operation_types.example_operation_types()) > 0
    ok = ok and all(entry["value"] for entry in operation_types.example_operation_types())
    return _result("slugify + blank default + non-empty examples", ok)


def test_normalize_entries():
    raw = [
        {"label": "Cable lay", "value": "lay"},
        {"label": "Trenching"},                       # value derived from label
        {"value": "rov"},                             # label falls back to value
        {"label": "Cable lay", "value": "lay"},        # duplicate value dropped
        {"label": "", "value": ""},                   # empty dropped
        "not a dict",                                 # ignored
    ]
    entries = operation_types.normalize_entries(raw)
    ok = entries == [
        {"value": "lay", "label": "Cable lay"},
        {"value": "trenching", "label": "Trenching"},
        {"value": "rov", "label": "rov"},
    ]
    return _result("normalize derives/dedupes/drops", ok)


def test_as_choices():
    entries = [{"label": "Cable lay", "value": "lay"}]
    choices = operation_types.as_choices(entries)
    ok = choices[0] == operation_types.UNSPECIFIED
    ok = ok and ("lay", "Cable lay") in choices
    # A stored value not in the configured list is appended so data stays visible.
    with_include = operation_types.as_choices(entries, include="legacy_op")
    ok = ok and ("legacy_op", "legacy_op") in with_include
    # An already-present value is not duplicated.
    ok = ok and operation_types.as_choices(entries, include="lay").count(("lay", "Cable lay")) == 1
    return _result("as_choices leads unspecified + include appends", ok)


def test_json_round_trip():
    entries = [{"label": "Cable lay", "value": "lay"}, {"label": "Plough", "value": "plough"}]
    text = operation_types.entries_to_json(entries)
    ok = operation_types.entries_from_json(text) == entries
    ok = ok and operation_types.entries_from_json("") == []
    ok = ok and operation_types.entries_from_json("not json") == []
    return _result("JSON round-trip + tolerant parse", ok)


def test_json_text_import():
    entries, warnings = operation_types.entries_from_json_text(
        '[{"label": "Cable lay", "value": "lay"}, "Plough"]')
    ok = entries == [
        {"value": "lay", "label": "Cable lay"},
        {"value": "plough", "label": "Plough"},
    ] and not warnings
    _empty, empty_warn = operation_types.entries_from_json_text("")
    ok = ok and bool(empty_warn)
    _bad, bad_warn = operation_types.entries_from_json_text("{not json")
    ok = ok and bool(bad_warn)
    return _result("JSON text import (objects + strings + errors)", ok)


def run_all():
    return [
        test_slugify_and_defaults(), test_normalize_entries(), test_as_choices(),
        test_json_round_trip(), test_json_text_import(),
    ]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)

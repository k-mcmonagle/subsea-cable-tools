# -*- coding: utf-8 -*-
"""Checks for the Burial Planner registry schema's table lists (pure python).

``schema.PLAN_CHILD_TABLES`` is the one list ``delete_plan`` and
``duplicate_plan`` walk; it must name every plan-keyed registry table, or a
deleted plan leaves orphaned rows (bp_ground_unit / bp_bas_row once did).
"""

from __future__ import annotations

from ..burial import schema


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


def test_every_plan_keyed_table_is_listed() -> bool:
    plan_keyed = {table for table, specs in schema.REGISTRY_TABLES.items()
                  if table != schema.TABLE_PLAN
                  and any(name == "plan_id" for name, _type in specs)}
    listed = set(schema.PLAN_CHILD_TABLES)
    missing = sorted(plan_keyed - listed)
    extra = sorted(listed - plan_keyed)
    ok = not missing and not extra
    return _result("PLAN_CHILD_TABLES == every plan-keyed registry table", ok,
                   f"missing={missing} extra={extra}")


def test_child_table_modes_and_copy_order() -> bool:
    modes = set(schema.PLAN_CHILD_TABLES.values())
    ok = modes <= {schema.PLAN_TABLE_COPY, schema.PLAN_TABLE_FRESH}
    ok = ok and all(table in schema.TABLE_KEYS
                    for table in schema.PLAN_CHILD_TABLES)
    order = list(schema.PLAN_CHILD_TABLES)
    # Copy re-points references to tables copied earlier.
    for later, earlier in ((schema.TABLE_RULE, schema.TABLE_INPUT),
                           (schema.TABLE_RISK_CHECK, schema.TABLE_INPUT),
                           (schema.TABLE_SECTION, schema.TABLE_EVENT),
                           (schema.TABLE_HAZARD, schema.TABLE_RISK_CHECK)):
        ok = ok and order.index(earlier) < order.index(later)
    # The regression: ground model and BAS rows are copied, so they must
    # also be deleted with their plan.
    ok = ok and schema.PLAN_CHILD_TABLES.get(schema.TABLE_GROUND_UNIT) \
        == schema.PLAN_TABLE_COPY
    ok = ok and schema.PLAN_CHILD_TABLES.get(schema.TABLE_BAS_ROW) \
        == schema.PLAN_TABLE_COPY
    return _result("child-table modes valid; referenced tables copied first",
                   ok)


def run_all() -> list:
    return [
        test_every_plan_keyed_table_is_listed(),
        test_child_table_modes_and_copy_order(),
    ]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)

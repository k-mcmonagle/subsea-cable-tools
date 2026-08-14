# -*- coding: utf-8 -*-
"""Append-only change log + rollback for the Burial Planner.

Pure python. Every mutating action produces exactly one ``bp_change_log``
row. ``before_json`` / ``after_json`` hold the complete affected rows per
registry table::

    {"bp_event": [ {row}, ... ], "bp_section": [ ... ]}

Rollback restores state to just before a selected entry by walking the log
newest-first and inverting each entry: rows present after but not before are
deleted; rows present before are re-upserted. The rollback itself is
appended as a new ``rollback`` entry — history is never deleted.
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Tuple

from . import schema

# Action vocabulary (open; UI shows these verbatim in the log viewer).
ACTION_CREATE_PLAN = "create_plan"
ACTION_EDIT_PLAN = "edit_plan"
ACTION_SET_INPUT = "set_input"
ACTION_DELETE_INPUT = "delete_input"
ACTION_EDIT_RULE = "edit_rule"
ACTION_DELETE_RULE = "delete_rule"
ACTION_GENERATE = "generate"
ACTION_ADD_EVENT = "add_event"
ACTION_MOVE_EVENT = "move_event"
ACTION_EDIT_EVENT = "edit_event"
ACTION_DELETE_EVENT = "delete_event"
ACTION_CONFIRM_EVENT = "confirm_event"
ACTION_LOCK_EVENT = "lock_event"
ACTION_SPLIT_SECTION = "split_section"
ACTION_INSERT_SECTION = "insert_section"
ACTION_MERGE_SECTIONS = "merge_sections"
ACTION_SET_CONCLUSION = "set_conclusion"
ACTION_EDIT_SECTION = "edit_section"
ACTION_IMPORT = "import"
ACTION_ROLLBACK = "rollback"
ACTION_EDIT_RISK_CHECK = "edit_risk_check"
ACTION_DELETE_RISK_CHECK = "delete_risk_check"
ACTION_RISK_SCAN = "risk_scan"
ACTION_ADD_HAZARD = "add_hazard"
ACTION_EDIT_HAZARD = "edit_hazard"
ACTION_DELETE_HAZARD = "delete_hazard"

TableRows = Dict[str, List[Dict]]


def current_user() -> str:
    """OS username, best effort."""
    for key in ("USERNAME", "USER", "LOGNAME"):
        value = os.environ.get(key)
        if value:
            return value
    try:
        import getpass

        return getpass.getuser()
    except Exception:
        return ""


def next_seq(entries: List[Dict]) -> int:
    seqs = [int(e.get("seq") or 0) for e in entries] or [-1]
    return max(seqs) + 1


def make_entry(plan_id: str, seq: int, action: str, target_id: str = "",
               before: Optional[TableRows] = None,
               after: Optional[TableRows] = None,
               reason: str = "", user: str = "",
               now: str = "", change_id: str = "") -> Dict:
    return {
        "change_id": change_id or schema.new_id(),
        "plan_id": plan_id,
        "seq": int(seq),
        "utc": now or schema.utc_now_iso(),
        "user": user or current_user(),
        "action": action,
        "target_id": target_id or "",
        "before_json": json.dumps(before or {}, default=str),
        "after_json": json.dumps(after or {}, default=str),
        "reason": reason or "",
    }


def delta_tables(before: Optional[TableRows], after: Optional[TableRows]
                 ) -> Tuple[TableRows, TableRows]:
    """Reduce full-table before/after snapshots to only the rows that differ.

    Rollback inversion (``invert_entry``) is per-row keyed, so an entry only
    needs the added, removed and modified rows — storing whole tables made
    every edit's log entry grow with plan size. Tables without a registered
    key (e.g. rollback bookkeeping payloads) pass through unchanged, as does
    a table present on only one side.
    """
    before = before or {}
    after = after or {}
    out_before: TableRows = {}
    out_after: TableRows = {}
    for table in set(before) | set(after):
        key = schema.TABLE_KEYS.get(table)
        b_rows = before.get(table) or []
        a_rows = after.get(table) or []
        if not key or not isinstance(b_rows, list) or not isinstance(a_rows, list):
            if table in before:
                out_before[table] = before[table]
            if table in after:
                out_after[table] = after[table]
            continue
        b_by = {str(r.get(key)): r for r in b_rows
                if isinstance(r, dict) and r.get(key)}
        a_by = {str(r.get(key)): r for r in a_rows
                if isinstance(r, dict) and r.get(key)}
        changed_b = [row for k, row in b_by.items() if a_by.get(k) != row]
        changed_a = [row for k, row in a_by.items() if b_by.get(k) != row]
        if changed_b:
            out_before[table] = changed_b
        if changed_a:
            out_after[table] = changed_a
    return out_before, out_after


def _load(payload: str) -> TableRows:
    try:
        data = json.loads(payload or "{}")
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def invert_entry(entry: Dict) -> List[Tuple[str, str, object]]:
    """Operations that undo one entry: [(table, 'delete'|'upsert', payload)].

    'delete' payload is a list of key values (rows added by the entry);
    'upsert' payload is the list of before-rows to restore.
    """
    before = _load(entry.get("before_json") or "")
    after = _load(entry.get("after_json") or "")
    ops: List[Tuple[str, str, object]] = []
    tables = set(before) | set(after)
    for table in sorted(tables):
        key_field = schema.TABLE_KEYS.get(table)
        if not key_field:
            continue
        before_rows = before.get(table) or []
        after_rows = after.get(table) or []
        before_keys = {str(r.get(key_field)) for r in before_rows if r.get(key_field)}
        added = [str(r.get(key_field)) for r in after_rows
                 if r.get(key_field) and str(r.get(key_field)) not in before_keys]
        if added:
            ops.append((table, "delete", added))
        if before_rows:
            ops.append((table, "upsert", before_rows))
    return ops


def rollback_operations(entries: List[Dict], target_change_id: str
                        ) -> Tuple[List[Tuple[str, str, object]], List[Dict]]:
    """Ops restoring state to just before ``target_change_id``.

    ``entries`` is the plan's full log. Returns (ops, undone_entries) with
    entries inverted newest-first down to and including the target. Raises
    ValueError if the target is not in the log.
    """
    ordered = sorted(entries, key=lambda e: int(e.get("seq") or 0))
    try:
        target_index = next(i for i, e in enumerate(ordered)
                            if e.get("change_id") == target_change_id)
    except StopIteration:
        raise ValueError("Change-log entry not found.")
    undone = list(reversed(ordered[target_index:]))
    ops: List[Tuple[str, str, object]] = []
    for entry in undone:
        ops.extend(invert_entry(entry))
    return ops, undone


def latest_effective_entry(entries: List[Dict]) -> Optional[Dict]:
    """Newest change that has not already been covered by a rollback.

    Rollback rows record the IDs they reversed. Keeping this small piece of
    history interpretation here lets the Plan Builder offer a conventional
    one-step Undo without deleting or rewriting the audit trail.
    """
    undone = set()
    for entry in entries:
        if entry.get("action") != ACTION_ROLLBACK:
            continue
        payload = _load(entry.get("after_json") or "")
        undone.update(str(change_id) for change_id in
                      (payload.get("undone_change_ids") or []) if change_id)
    for entry in reversed(sorted(entries, key=lambda row: int(row.get("seq") or 0))):
        if entry.get("action") == ACTION_ROLLBACK:
            continue
        if str(entry.get("change_id") or "") not in undone:
            return entry
    return None

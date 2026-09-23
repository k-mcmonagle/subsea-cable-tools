# -*- coding: utf-8 -*-
"""QGIS-side checks for the BAS register: store round trip, model save
with columns/meta + rollback + duplicate, and the tab/spreadsheet table
(edit, paste growing rows, fill down, undo, sort, insert/remove) plus the
import and columns dialogs under a headless QgsApplication."""

from __future__ import annotations

import os
import tempfile
import time

from qgis.core import QgsProject

from ..burial import bas_model, change_log, schema
from ..burial.plan_model import PlanModel
from ..burial.store import BurialStore

_COUNTER = [0]


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" - {detail}"
    print(msg)
    return ok


def _store() -> BurialStore:
    _COUNTER[0] += 1
    name = f"bp_bas_{os.getpid()}_{int(time.time() * 1000)}_{_COUNTER[0]}.gpkg"
    store = BurialStore(os.path.join(tempfile.gettempdir(), name),
                        QgsProject.instance().transformContext())
    store.migrate()
    return store


def _plan_row(name="Plan B"):
    return {"plan_id": schema.new_id(), "name": name, "description": "",
            "notes": "", "method": "plough", "rpl_id": "",
            "rpl_name": "Route", "rpl_revision": "Rev D",
            "rpl_gpkg_path": "", "rpl_fingerprint": "",
            "scope_start_kp": 0.0, "scope_end_kp": 10.0, "direction": 1,
            "target_burial_m": 1.5, "params_json": "{}"}


_COLS = [{"key": "req_dol_m", "label": "Req DoL (m)", "kind": "number"},
         {"key": "soil", "label": "Soil", "kind": "text"}]


def _rows():
    return [bas_model.decode_row({"row_id": schema.new_id(), "start_kp": 0.0, "end_kp": 4.0,
                                  "values": {"req_dol_m": "1.5", "soil": "Sand"},
                                  "src_start_kp": 0.0, "src_end_kp": 3.9, "src_rpl": "Rev B"}),
            bas_model.decode_row({"row_id": schema.new_id(), "start_kp": 4.0, "end_kp": 7.0,
                                  "values": {"req_dol_m": "2", "soil": "Clay"}})]


def test_store_and_model() -> bool:
    store = _store()
    ok = store._table_exists(schema.TABLE_BAS_ROW)
    plan_id = store.save_plan(_plan_row())
    model = PlanModel(store)
    model.load_plan(plan_id)
    ok = ok and model.bas_rows == [] and model.bas_columns() == []
    ok = ok and model.save_bas(_rows(), columns=_COLS, action=change_log.ACTION_IMPORT_BAS,
                               reason="import", meta_updates={"source_ref": "BAS Rev B"})
    ok = ok and len(model.bas_rows) == 2
    ok = ok and model.bas_rows[0]["values"] == {"req_dol_m": "1.5", "soil": "Sand"}
    ok = ok and model.bas_rows[0]["src_rpl"] == "Rev B" \
        and abs(model.bas_rows[0]["src_end_kp"] - 3.9) < 1e-9
    ok = ok and [c["key"] for c in model.bas_columns()] == ["req_dol_m", "soil"]
    ok = ok and model.bas_meta().get("source_ref") == "BAS Rev B"
    ok = ok and model.plan.get("status") != schema.PLAN_STATUS_STALE
    # Raw store row carries values_json, not a dict
    raw = store.list_bas_rows(plan_id)[0]
    ok = ok and "values_json" in raw and "Sand" in raw["values_json"]
    # Edit then rollback
    trimmed = model.bas_rows[:1]
    ok = ok and model.save_bas(trimmed, reason="drop clay")
    ok = ok and len(model.bas_rows) == 1
    edit = [e for e in store.list_change_log(plan_id)
            if e.get("action") == change_log.ACTION_EDIT_BAS][-1]
    ok = ok and model.rollback_to(edit["change_id"]) and len(model.bas_rows) == 2
    # Columns only
    ok = ok and model.save_bas_columns(_COLS[::-1])
    ok = ok and [c["key"] for c in model.bas_columns()] == ["soil", "req_dol_m"]
    # Duplicate carries rows
    copy_id = store.duplicate_plan(plan_id, "Copy")
    ok = ok and len(store.list_bas_rows(copy_id)) == 2
    return _result("BAS store + model save/rollback/columns/duplicate", ok)


def test_tab_and_spreadsheet() -> bool:
    from qgis.PyQt.QtWidgets import QApplication

    if QApplication.instance() is None:
        return _result("BAS tab", True, "skipped: no QApplication")
    from ..burial.tabs.bas_tab import BasTab
    from ..burial.bas_dialogs import BasColumnsDialog, BasImportDialog

    store = _store()
    plan_id = store.save_plan(_plan_row())
    model = PlanModel(store)
    model.load_plan(plan_id)
    model.save_bas(_rows(), columns=_COLS)

    class _Dock:
        iface = None
        canvas = None
        calls = []

        def workbench_store(self, *_a):
            return None

        def highlight_kp(self, kp):
            self.calls.append(("kp", kp))

        def goto_kp(self, kp):
            self.calls.append(("goto", kp))

        def goto_range(self, a, b):
            self.calls.append(("range", a, b))

        def highlight_range(self, a, b):
            self.calls.append(("hl", a, b))

        def highlight_ranges(self, ranges):
            self.calls.append(("hls", len(ranges)))

    ok_list = []
    dock = _Dock()
    tab = BasTab(model, dock)
    headers = [tab.table.horizontalHeaderItem(c).text() for c in range(tab.table.columnCount())]
    _check(ok_list, 1, headers[:4] == ["Start KP", "End KP", "Req DoL (m)", "Soil"])
    _check(ok_list, 2, tab.table.rowCount() == 2)
    _check(ok_list, 3, "2 row(s), 2 column(s)" in tab.status_label.text())
    _check(ok_list, 4, "3.000 km of the scope has no BAS row" in tab.status_label.text())
    # Cell edit → working copy
    tab.table.item(0, 2).setText("1.8")
    _check(ok_list, 5, tab._dirty and tab._working[0]["values"]["req_dol_m"] == "1.8")
    tab.table.item(1, 1).setText("7.5")
    _check(ok_list, 6, abs(tab._working[1]["end_kp"] - 7.5) < 1e-9)
    tab.table.item(1, 1).setText("junk")   # rejected, keeps 7.5
    _check(ok_list, 7, abs(tab._working[1]["end_kp"] - 7.5) < 1e-9
           and tab.table.item(1, 1).text() == "7.500")
    # A rejected edit leaves no undo entry, so undo reverts the KP edit
    _check(ok_list, 8, tab.table.undo() and tab.table.item(1, 1).text() == "7.000"
           and abs(tab._working[1]["end_kp"] - 7.0) < 1e-9)
    # Paste from "Excel" starting at row 1, col 2: two rows → grows by one
    QApplication.clipboard().setText("2.2\tRock\n2.4\tGravel\n")
    tab.table.setCurrentCell(1, 2)
    tab.table.paste()
    _check(ok_list, 9, tab.table.rowCount() == 3 and len(tab._working) == 3)
    _check(ok_list, 10, tab._working[1]["values"] == {"req_dol_m": "2.2", "soil": "Rock"})
    _check(ok_list, 11, tab._working[2]["values"] == {"req_dol_m": "2.4", "soil": "Gravel"})
    _check(ok_list, 12, tab._working[2]["start_kp"] == 7.0)  # new row starts where the last ended
    # Fill down soil from row 0 over rows 0-2
    tab.table.clearSelection()
    tab.table.setRangeSelected(
        __import__("qgis.PyQt.QtWidgets", fromlist=["QTableWidgetSelectionRange"])
        .QTableWidgetSelectionRange(0, 3, 2, 3), True)
    tab.table.fill_down()
    _check(ok_list, 13, all(r["values"]["soil"] == "Sand" for r in tab._working))
    # Sort by Req DoL descending (two clicks)
    tab._header_clicked(2)
    tab._header_clicked(2)
    _check(ok_list, 14, tab._working[0]["values"]["req_dol_m"] == "2.4")
    tab._sort_by_kp()
    _check(ok_list, 15, tab._working[0]["start_kp"] == 0.0)
    # Insert / duplicate / remove
    tab.table.selectRow(0)
    tab._insert_rows(1, 1)
    _check(ok_list, 16, len(tab._working) == 4 and tab._working[1]["start_kp"] == 4.0)
    tab.table.selectRow(1)
    tab._remove_rows()
    _check(ok_list, 17, len(tab._working) == 3)
    # Selection sync + apply
    tab.table.selectRow(0)
    _check(ok_list, 18, any(c[0] == "hl" for c in dock.calls))
    tab._apply()
    _check(ok_list, 19, not tab._dirty and len(model.bas_rows) == 3)
    _check(ok_list, 20, store.list_bas_rows(plan_id)[2]["values_json"].count("Sand") == 1)
    # Dialogs construct
    import_dialog = BasImportDialog(model, dock)
    _check(ok_list, 21, not import_dialog.ok_button.isEnabled())
    columns_dialog = BasColumnsDialog(model.bas_columns())
    _check(ok_list, 22, columns_dialog.table.rowCount() == 2)
    columns_dialog._add()
    columns_dialog._save()
    _check(ok_list, 23, len(columns_dialog.columns) == 3 and columns_dialog.columns[2]["key"] == "new_column")
    # Decimal places: display rounds, the stored value keeps its precision;
    # re-committing the shown text is not an edit, a new value is stored.
    tab.table.item(0, 2).setText("1.23456")
    tab._apply()
    tab.set_column_decimals("req_dol_m", 2)
    _check(ok_list, 24, bas_model.column_decimals(next(
        c for c in model.bas_columns() if c["key"] == "req_dol_m")) == 2)
    tab.refresh()
    _check(ok_list, 25, tab.table.item(0, 2).text() == "1.23"
           and "1.23456" in tab.table.item(0, 2).toolTip())
    tab.table.item(0, 2).setText("1.23")
    _check(ok_list, 26, not tab._dirty
           and tab._working[0]["values"]["req_dol_m"] == "1.23456")
    tab.table.item(0, 2).setText("1.987")
    _check(ok_list, 27, tab._dirty and tab._working[0]["values"]["req_dol_m"] == "1.987"
           and tab.table.item(0, 2).text() == "1.99")
    tab.set_column_decimals("req_dol_m", None)
    _check(ok_list, 28, tab.table.item(0, 2).text() == "1.987")
    places_dialog = BasColumnsDialog(model.bas_columns())
    _check(ok_list, 29, places_dialog.table.columnCount() == 3)
    for widget in (tab, import_dialog, columns_dialog, places_dialog):
        widget.deleteLater()
    ok = all(v for _n, v in ok_list)
    failed = [n for n, v in ok_list if not v]
    return _result("BAS tab + spreadsheet edit/paste/fill/undo/sort/apply + dialogs", ok,
                   f"failed checks: {failed}" if failed else tab.status_label.text()[:80])


def _check(ok_list, n, value):
    ok_list.append((n, bool(value)))


def run_all():
    return [test_store_and_model(), test_tab_and_spreadsheet()]


if __name__ == "__main__":
    import sys
    sys.exit(0 if all(run_all()) else 1)

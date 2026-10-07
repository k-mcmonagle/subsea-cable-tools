# -*- coding: utf-8 -*-
"""QGIS-backed checks for the RPL comparison panel's Events tab.

Builds the real RevisionComparePanel against a small fake workbench store
whose RPL layers are memory layers: the default design-vs-as-laid pair, the
event table, a dropdown re-pairing, filters, CSV/HTML export, the offset
layer, saving corrections with the project, and the comparison dialog.

Requires the QGIS API (run via tests/run_qgis_smoke_tests.py).
"""

from __future__ import annotations

import math
import os
import tempfile

from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.core import QgsFeature, QgsField, QgsGeometry, QgsPointXY, QgsProject, QgsVectorLayer

from ..qgis_compat import FIELD_TYPE_DOUBLE, FIELD_TYPE_INT, FIELD_TYPE_STRING
from ..workbench import event_compare as ec
from ..workbench.compare_panel import RevisionComparePanel, RplCompareDialog, default_pair
from ..workbench.event_compare_panel import (
    COL_A, COL_B, PROJECT_KEY, PROJECT_SCOPE, build_offset_layer, write_csv,
)

REQUIRES_QGIS = True

LAT0 = 50.0
DESIGN = {0: "BMH A", 3: "RPTR 1", 5: "AC 1", 8: "RPTR 2", 11: "JT-3", 14: "RPTR 3", 20: "BMH B"}
AS_LAID = {**DESIGN, 3: "Repeater 1 S/N 4471", 8: "RTPR 2", 6: "AC 1A"}


def _result(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    return ok


def _rpl_layer(name, events, shift=None):
    m_lat, m_lon = ec._local_scale(LAT0)
    layer = QgsVectorLayer("Point?crs=EPSG:4326", name, "memory")
    layer.dataProvider().addAttributes([
        QgsField("SeqNo", FIELD_TYPE_INT), QgsField("PosNo", FIELD_TYPE_INT),
        QgsField("Event", FIELD_TYPE_STRING), QgsField("DistCumulative", FIELD_TYPE_DOUBLE),
        QgsField("CableDistCumulative", FIELD_TYPE_DOUBLE), QgsField("Latitude", FIELD_TYPE_DOUBLE),
        QgsField("Longitude", FIELD_TYPE_DOUBLE), QgsField("ApproxDepth", FIELD_TYPE_DOUBLE),
        QgsField("Remarks", FIELD_TYPE_STRING)])
    layer.updateFields()
    step_km = math.radians(0.05) * m_lon / 1000.0
    features = []
    for i in range(21):
        lat, lon = LAT0, i * 0.05
        east, north = (shift or {}).get(i, (0.0, 0.0))
        lat += math.degrees(north / m_lat)
        lon += math.degrees(east / m_lon)
        feature = QgsFeature(layer.fields())
        feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(lon, lat)))
        feature.setAttributes([i + 1, i + 1, events.get(i, ""), i * step_km, i * step_km * 1.01,
                               lat, lon, 100.0 + i, ""])
        features.append(feature)
    layer.dataProvider().addFeatures(features)
    return layer


class _FakeStore:
    gpkg_path = ""

    def __init__(self):
        self.layers = {
            "design_pts": _rpl_layer("design", DESIGN),
            "aslaid_pts": _rpl_layer("aslaid", AS_LAID, shift={3: (20.0, 30.0), 8: (-12.0, -45.0)}),
        }
        self.rpls = [
            {"rpl_id": "d1", "route_id": "r1", "rev_label": "Design C", "kind": "planned",
             "status": "issued", "points_layer": "design_pts", "lines_layer": ""},
            {"rpl_id": "a1", "route_id": "r1", "rev_label": "As-laid", "kind": "as_laid",
             "status": "draft", "points_layer": "aslaid_pts", "lines_layer": ""},
            {"rpl_id": "x9", "route_id": "r2", "rev_label": "Other", "kind": "planned",
             "status": "draft", "points_layer": "design_pts", "lines_layer": ""},
        ]

    def exists(self):
        return False

    def revisions_of_route(self, route_id):
        return [r for r in self.rpls if r["route_id"] == route_id]

    def list_rpls(self):
        return list(self.rpls)

    def list_routes(self):
        return [{"route_id": "r1", "name": "Seg 1"}, {"route_id": "r2", "name": "Seg 2"}]

    def get_rpl(self, rpl_id):
        return next((r for r in self.rpls if r["rpl_id"] == rpl_id), None)

    def open_layer(self, name):
        return self.layers.get(name)


def _row_index(widget, a_event):
    for index, row in enumerate(widget._shown):
        if row.a is not None and row.a.event == a_event:
            return index
    return -1


def test_panel_events_tab_end_to_end():
    store = _FakeStore()
    panel = RevisionComparePanel()
    panel.load_segment(store, "r1")
    panel.set_visible_tab(True)
    QCoreApplication.processEvents()
    events = panel.events
    ok = (panel.combo_a.currentData() == "d1" and panel.combo_b.currentData() == "a1"
          and panel.combo_a.count() == 4                  # 2 revisions + separator + 1 other
          and panel.tabs.tabText(0) == "Events")
    rows = {r.a.event: r for r in events.rows() if r.a is not None}
    ok = ok and (rows["RPTR 1"].b.event == "Repeater 1 S/N 4471"
                 and abs(rows["RPTR 1"].along_m - 20.0) < 0.05
                 and abs(rows["RPTR 1"].cross_m + 30.0) < 0.05
                 and rows["RPTR 2"].b.event == "RTPR 2"
                 and events.table.rowCount() == len(events.rows()))
    # Only-in-B row is listed and editable from its A cell.
    only_b = [i for i, r in enumerate(events._shown) if r.a is None]
    ok = ok and len(only_b) == 1 and bool(
        events.table.item(only_b[0], COL_A).flags() & Qt.ItemFlag.ItemIsEditable)

    # Re-pair RPTR 3 to "no match" through the delegate's API.
    index = _row_index(events, "RPTR 3")
    choices = events.partner_choices(index, COL_B)
    ok = ok and choices[0][1] == -1 and "RPTR 3" in choices[1][0]   # nearest first
    events.apply_partner(index, COL_B, -1)
    QCoreApplication.processEvents()
    rows = {r.a.event: r for r in events.rows() if r.a is not None}
    ok = ok and rows["RPTR 3"].b is None and events.mapping().how(
        events.mapping().events_a.index(rows["RPTR 3"].a)) == ""

    # Filter to repeaters, untick one, exports follow the selection.
    events.preset.setCurrentIndex([k for k, *_ in ec.FILTER_PRESETS].index("repeaters"))
    QCoreApplication.processEvents()
    shown = [r.a.event for r in events._shown if r.a is not None]
    ok = ok and shown == ["RPTR 1", "RPTR 2", "RPTR 3"]
    events.table.item(_row_index(events, "RPTR 2"), COL_A).setCheckState(Qt.CheckState.Unchecked)
    selected = [r.a.event for r in events.selected_rows() if r.a is not None]
    ok = ok and selected == ["RPTR 1", "RPTR 3"]
    ok = ok and "1 matched" in events.summary.text()

    folder = tempfile.mkdtemp(prefix="sct_evt_")
    csv_path = os.path.join(folder, "cmp.csv")
    write_csv(csv_path, events.selected_rows(), "Design C", "As-laid", 50.0, "Repeaters")
    with open(csv_path, encoding="utf-8-sig") as handle:
        text = handle.read()
    ok = ok and "Repeater 1 S/N 4471" in text and "RTPR 2" not in text
    layer = build_offset_layer([r for r in events.selected_rows() if r.matched], "offsets", 50.0)
    ok = ok and layer.isValid() and layer.featureCount() == 1
    first = next(layer.getFeatures()) if layer.featureCount() else None
    ok = ok and first is not None and first["a_event"] == "RPTR 1" and first["in_target"] == "yes"
    panel.deleteLater()
    return _result("compare panel Events tab: pairing, re-pairing, filters, exports", ok)


def test_corrections_saved_with_project():
    store = _FakeStore()
    key = "test_d1__a1"
    project = QgsProject.instance()
    project.removeEntry(PROJECT_SCOPE, PROJECT_KEY.format(key=key))
    widget_points = {name: None for name in ("a", "b")}
    from ..workbench.rpl_summary import read_point_rows

    widget_points["a"] = read_point_rows(store.layers["design_pts"])
    widget_points["b"] = read_point_rows(store.layers["aslaid_pts"])
    from ..workbench.event_compare_panel import EventComparisonWidget

    first = EventComparisonWidget()
    first.set_data(widget_points["a"], widget_points["b"], key=key)
    index = _row_index(first, "JT-3")
    first.apply_partner(index, COL_B, -1)
    saved, ok_read = project.readEntry(PROJECT_SCOPE, PROJECT_KEY.format(key=key), "")
    second = EventComparisonWidget()
    second.set_data(widget_points["a"], widget_points["b"], key=key)
    rows = {r.a.event: r for r in second.rows() if r.a is not None}
    ok = ok_read and "JT-3" in saved and rows["JT-3"].b is None
    project.removeEntry(PROJECT_SCOPE, PROJECT_KEY.format(key=key))
    first.deleteLater()
    second.deleteLater()
    return _result("manual corrections are saved with the project and re-applied", ok)


def test_default_pair_and_dialog():
    rows = [{"rpl_id": "p1", "kind": "planned"}, {"rpl_id": "p2", "kind": "planned"},
            {"rpl_id": "l1", "kind": "as_laid"}, {"rpl_id": "p3", "kind": "planned"}]
    ok = (default_pair(rows) == ("p3", "l1")
          and default_pair(rows[:2]) == ("p1", "p2")
          and default_pair(rows[:1]) == ("", ""))
    dialog = RplCompareDialog(_FakeStore(), "r1", rpl_a="", rpl_b="d1")
    QCoreApplication.processEvents()
    ok = ok and dialog.panel.combo_b.currentData() == "d1" and \
        dialog.panel.combo_a.currentData() != "d1"
    dialog.deleteLater()
    return _result("design vs as-laid is the default pair; comparison dialog opens", ok)


def run_all():
    return [
        test_panel_events_tab_end_to_end(),
        test_corrections_saved_with_project(),
        test_default_pair_and_dialog(),
    ]

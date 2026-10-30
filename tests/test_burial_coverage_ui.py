"""Coverage tables and draggable name dividers under QGIS 3 / Qt5 and 4 / Qt6."""

from contextlib import contextmanager

from qgis.PyQt.QtCore import QEvent, QPoint, QPointF, QSettings, Qt
from qgis.PyQt.QtGui import QMouseEvent
from qgis.PyQt.QtTest import QTest
from qgis.PyQt.QtWidgets import QApplication, QTableWidget, QTableWidgetItem

from ..burial import analysis_state, generation, schema, ui_helpers
from ..burial.plan_model import PlanModel
from ..burial.tabs.risk_tab import RiskTab
from ..burial.tabs.rules_tab import RulesTab
from ..qgis_compat import HEADER_RESIZE_MODE_CONTENTS, HEADER_RESIZE_MODE_STRETCH
from ..workbench import rules_engine as eng

REQUIRES_QGIS = True


class _Dock:
    def __getattr__(self, name):
        return lambda *a, **kw: None


@contextmanager
def _settings(*keys):
    settings = QSettings()
    saved = {key: settings.value(key) for key in keys if settings.contains(key)}
    for key in keys:
        settings.remove(key)
    try:
        yield
    finally:
        for key in keys:
            settings.remove(key)
        for key, value in saved.items():
            settings.setValue(key, value)


def _events():
    for _ in range(4):
        QApplication.processEvents()


def _model():
    model = PlanModel(object())
    model.plan = {"plan_id": "coverage", "scope_start_kp": 10, "scope_end_kp": 20,
                  "method": "plough"}
    return model


def test_exclusion_totals_unique_and_cross_criterion_currency():
    model = _model()
    model.rules = [dict(rule_id=key, name=key, enabled=1, action="exclude",
                        kind="manual", criterion_class="project")
                   for key in ("Depth", "Slope", "Screening", "Missing data", "Disabled", "Other method")]
    model.rules[2]["criterion_class"] = "screening"
    model.rules[3]["kind"] = schema.RULE_KIND_COVERAGE
    model.rules[4]["enabled"] = 0
    model.rules[5]["methods_json"] = '["trencher"]'
    model.context = generation.ResolutionContext(
        rule_hits={r["rule_id"]: [eng.Interval(10, 20)] for r in model.rules},
        excluded=[eng.RangeVerdict(10, 18, eng.STATUS_EXCLUDED, 0)])
    model.context.rule_hits["Depth"] = [eng.Interval(10, 15)]
    model.context.rule_hits["Slope"] = [eng.Interval(13, 18)]
    status = dict(source="recompute", reasons=[], rule_state={
        r["rule_id"]: analysis_state.STATE_CURRENT for r in model.rules})
    model.analysis_status = lambda: status
    tab = RulesTab(model, _Dock())
    try:
        assert tab.rule_table.item(0, 3).text() == "5.000 km · 50.0%"
        assert tab.rule_table.item(0, 4).text() == "3.000 km · 30.0%"
        assert tab.rule_table.item(1, 4).text() == "3.000 km · 30.0%"
        assert [tab.rule_table.item(i, 4).text() for i in range(2, 6)] == ["n/a", "n/a", "off", "n/a"]
        status["rule_state"]["Slope"] = analysis_state.STATE_CHANGED
        tab.refresh()
        assert tab.rule_table.item(0, 4).text() == "—"
        assert "Recompute" in tab.rule_table.item(0, 4).toolTip()
        model.rules[1]["enabled"] = 0
        tab.refresh()
        assert tab.rule_table.item(0, 4).text() == "5.000 km · 50.0%"
        # Switching to a new plan must not retain the previous figures.
        model.plan = {"plan_id": "empty"}
        model.rules = []
        model.context = generation.ResolutionContext()
        tab.refresh()
        assert tab.rule_table.rowCount() == 0
    finally:
        tab.deleteLater()


def test_allow_exception_currency_and_disabling_the_exception():
    model = _model()
    model.rules = [dict(rule_id=key, name=key, enabled=1, action=action,
                        kind="manual", criterion_class="project")
                   for key, action in (("Depth", "exclude"), ("Exception", "allow"))]
    model.context = generation.ResolutionContext(
        rule_hits={"Depth": [eng.Interval(10, 15)], "Exception": [eng.Interval(13, 15)]},
        excluded=[eng.RangeVerdict(10, 13, eng.STATUS_EXCLUDED, 0)])
    status = dict(source="recompute", reasons=[], rule_state={
        r["rule_id"]: analysis_state.STATE_CURRENT for r in model.rules})
    model.analysis_status = lambda: status
    tab = RulesTab(model, _Dock())
    try:
        assert tab.rule_table.item(0, 3).text() == "5.000 km · 50.0%"
        assert tab.rule_table.item(0, 4).text() == "3.000 km · 30.0%"
        status["rule_state"]["Exception"] = analysis_state.STATE_CHANGED
        tab.refresh()
        assert tab.rule_table.item(0, 4).text() == "—"
        model.rules[1]["enabled"] = 0
        tab.refresh()
        assert tab.rule_table.item(0, 4).text() == "5.000 km · 50.0%"
    finally:
        tab.deleteLater()


def test_risk_total_unique_disabled_and_unscanned_checks():
    model = _model()
    model.risk_checks = [dict(check_id=key, name=key, enabled=1) for key in ("A", "B", "Off")]
    model.risk_checks[2]["enabled"] = 0
    model.hazards = [dict(hazard_id=key, check_id=key, kp=start, end_kp=end)
                     for key, start, end in (("A", 8, 15), ("B", 13, 18), ("Off", 10, 20), ("", 10, 20))]
    runs = {key: {"state": analysis_state.STATE_CURRENT} for key in ("A", "B", "Off")}
    model.risk_status = lambda: runs
    tab = RiskTab(model, _Dock())
    try:
        assert tab.check_table.item(0, 3).text() == "1 · 5.000 km · 50.0%"
        assert tab.check_table.item(0, 4).text() == "3.000 km · 30.0%"
        assert tab.check_table.item(1, 4).text() == "3.000 km · 30.0%"
        assert tab.check_table.item(2, 4).text() == "off"
        runs["B"]["state"] = analysis_state.STATE_NEW
        tab.refresh()
        assert tab.check_table.item(0, 4).text() == "—"
        model.risk_checks[1]["enabled"] = 0
        tab.refresh()
        assert tab.check_table.item(0, 4).text() == "5.000 km · 50.0%"
    finally:
        tab.deleteLater()


def test_name_divider_drag_persistence_and_double_click_fit():
    key = "SubseaCableTools/Tests/coverage_name_width"
    with _settings(key):
        table = QTableWidget(1, 3)
        table.setHorizontalHeaderLabels(["On", "Criterion", "Route"])
        table.setItem(0, 0, QTableWidgetItem("On"))
        table.setItem(0, 1, QTableWidgetItem("Water depth"))
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, HEADER_RESIZE_MODE_CONTENTS)
        header.setSectionResizeMode(2, HEADER_RESIZE_MODE_STRETCH)
        helper = ui_helpers.ContentSizedColumn(table, 1, 2, key)
        table.resize(1000, 240)
        table.show()
        _events()
        try:
            fitted = table.columnWidth(1)
            assert fitted < table.columnWidth(2)
            handle = QPoint(header.sectionViewportPosition(1) + fitted - 1, header.height() // 2)
            QTest.mousePress(header.viewport(), Qt.MouseButton.LeftButton, pos=handle)
            # Qt5's offscreen QTest.mouseMove does not preserve pressed
            # buttons. Send the same drag event with its button state on
            # both bindings, between the real press and release events.
            position = handle + QPoint(90, 0)
            QApplication.sendEvent(header.viewport(), QMouseEvent(
                QEvent.Type.MouseMove, QPointF(position),
                QPointF(header.viewport().mapToGlobal(position)),
                Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier))
            QTest.mouseRelease(header.viewport(), Qt.MouseButton.LeftButton, pos=handle + QPoint(90, 0))
            _events()
            dragged = table.columnWidth(1)
            assert dragged > fitted + 50, (fitted, dragged)
            helper.fit()
            assert table.columnWidth(1) == dragged
            assert int(QSettings().value(key)) == dragged
            other = QTableWidget(0, 3)
            restored = ui_helpers.ContentSizedColumn(other, 1, 2, key)
            assert other.columnWidth(1) == dragged
            assert restored._width == dragged
            other.deleteLater()
            handle.setX(header.sectionViewportPosition(1) + dragged - 1)
            QTest.mouseDClick(header.viewport(), Qt.MouseButton.LeftButton, pos=handle)
            _events()
            assert table.columnWidth(1) == fitted, (table.columnWidth(1), fitted)
            assert not QSettings().contains(key)
            table.setItem(0, 1, QTableWidgetItem("A much longer criterion name"))
            helper.fit()
            assert table.columnWidth(1) > fitted
            table.resize(400, 240)
            _events()
            assert table.columnWidth(2) >= 150
        finally:
            table.close()
            table.deleteLater()

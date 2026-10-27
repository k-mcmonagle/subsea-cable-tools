# -*- coding: utf-8 -*-
"""Editing saved comment positions: precision, saved KP basis and map refresh."""

from __future__ import annotations

import os
import sqlite3
import tempfile

from qgis.core import QgsCoordinateReferenceSystem, QgsGeometry, QgsPointXY, QgsProject
from qgis.PyQt.QtCore import QCoreApplication, QSettings, Qt
from qgis.PyQt.QtWidgets import QLabel, QLineEdit

from .. import kp_range_utils
from ..qgis_compat import BUTTON_BOX_OK
from ..route_comments import dock as dock_module
from ..route_comments import map_layers, model
from ..route_comments.dialogs import EditPositionDialog, NewCommentDialog
from ..route_comments.route_ref import CaptureRoute, ReferenceChecker, location_without_route
from ..route_comments.store import CommentStore
from .test_route_comments_qgis import START_KP, _Iface, _close, _gpkg_route, _png, _pt, _route_layer

REQUIRES_QGIS = True


def test_position_form():
    layer = _route_layer()
    capture = CaptureRoute.from_layer(layer, start_kp_km=START_KP, use_cartesian=False)
    dialogs = []

    def form(values, route=capture):
        dialog = EditPositionDialog(None, values, route)
        dialogs.append(dialog)
        assert dialog.result_values() == {}, "Opening an editor must not round or move a comment"
        return dialog

    try:
        point = capture.point_values(START_KP + 1.23456789)
        dialog = form(point)
        labels = " ".join(label.text() for label in dialog.findChildren(QLabel))
        assert f"KP {capture.start_kp_km:.3f} to {capture.end_kp_km:.3f}" in labels
        assert f"{capture.end_kp_km:.6f}" not in labels
        assert dialog.editors["kp_km"].decimals() == 3
        dialog.editors["kp_km"].setValue(START_KP + 3)
        moved = dialog.result_values()
        assert moved["kp_km"] == START_KP + 3
        assert _pt(moved["wkt"]).distance(capture.route.point_at_kp(START_KP + 3)) < 1e-9
        assert _pt(moved["wkt"]).distance(QgsPointXY(moved["lon"], moved["lat"])) < 1e-11
        assert "text" not in moved and "route_hash" not in moved and "comment_id" not in moved
        dialog.editors["kp_km"].setValue(START_KP - 1)
        assert not dialog.buttons.button(BUTTON_BOX_OK).isEnabled()
        assert f"{capture.end_kp_km:.3f}" in dialog.error.text()
        assert f"{capture.end_kp_km:.6f}" not in dialog.error.text()
        try:
            dialog.result_values()
            raise AssertionError("A KP outside the route was silently clamped")
        except ValueError:
            pass
        dialog.editors["kp_km"].setValue(round(point["kp_km"], 3))
        assert dialog.result_values() == {}, "Changing back must preserve exact stored precision"

        # The true endpoint usually lies between the displayed metre values.
        form(capture.point_values(capture.end_kp_km))
        original = capture.range_values(START_KP + 1.123456789, capture.end_kp_km)
        dialog = form(original)
        dialog.editors["kp_km"].setValue(START_KP + 2)
        moved = dialog.result_values()
        assert moved["kp_end_km"] == original["kp_end_km"], "Untouched range endpoint lost precision"
        vertices = QgsGeometry.fromWkt(moved["wkt"]).asPolyline()
        assert len(vertices) > 2
        assert vertices[0].distance(capture.route.point_at_kp(START_KP + 2)) < 1e-9
        assert vertices[-1].distance(capture.route.point_at_kp(capture.end_kp_km)) < 1e-9
        dialog.editors["kp_end_km"].setValue(START_KP + 2)
        dialog.accept()
        assert not dialog.result(), "Zero-length range was accepted"
        dialog.editors["kp_end_km"].setValue(START_KP + 1)
        moved = dialog.result_values()
        assert (moved["kp_km"], moved["kp_end_km"]) == (START_KP + 1, START_KP + 2)

        original = capture.location_from_wgs84(55.0123456789, 3.0123456789)
        dialog = form(original)
        dialog.editors["lon"].setValue(3.04)
        moved = dialog.result_values()
        assert _close(moved["lat"], original["lat"], 1e-12), "Untouched latitude lost precision"
        assert moved["lon"] == 3.04
        expected = capture.location_from_wgs84(original["lat"], 3.04)
        assert _close(moved["kp_km"], expected["kp_km"], 1e-9)
        assert _close(moved["dcc_m"], expected["dcc_m"], 1e-6)
        assert moved["kp_km"] != original["kp_km"]

        dialog = form(location_without_route(55.123456789, 3.123456789), None)
        dialog.editors["lat"].setValue(56)
        moved = dialog.result_values()
        assert moved["lat"] == 56 and moved["lon"] == 3.123456789
        assert moved["kp_km"] is None and moved["dcc_m"] is None
        assert _pt(moved["wkt"]) == QgsPointXY(moved["lon"], 56)

        # Some older/imported rows have only geometry, including at zero longitude.
        dialog = form({"ref_kind": model.REF_LOCATION, "wkt": "POINT(0 55)"}, None)
        assert dialog.editors["lat"].value() == 55 and dialog.editors["lon"].value() == 0
        dialog.editors["lat"].lineEdit().setText("56")
        dialog.accept()  # Commit typed text even when focus has not left the editor.
        assert dialog.result() and dialog.result_values()["wkt"] == "POINT(0.0 56.0)"
    finally:
        for dialog in dialogs:
            dialog.deleteLater()
        QCoreApplication.processEvents()


def test_new_comment_precision():
    """KP Mouse Tool and Span Analysis both use the same new-comment form."""
    layer = _route_layer()
    capture = CaptureRoute.from_layer(layer, start_kp_km=START_KP, use_cartesian=False)
    values = (capture.point_values(START_KP + 1.23456789),
              capture.range_values(START_KP + 1.123456789, capture.end_kp_km),
              capture.location_from_wgs84(55.0123456789, 3.0123456789))
    for initial in values:
        dialog = NewCommentDialog(None, initial, capture, ["Open"], ["Normal"], [], "Open", "Normal", "Test")
        try:
            assert dialog.location_values() == initial, "Creating a comment must not round its captured position"
            if dialog.kp is not None:
                dialog.kp.setValue(START_KP - 1)
                try:
                    dialog.location_values()
                    raise AssertionError("New-comment form silently clamped a KP outside the route")
                except ValueError:
                    pass
                dialog.kp.setValue(START_KP + 2)
                result = dialog.location_values()
                if dialog.kp_end is not None:
                    assert result["kp_end_km"] == initial["kp_end_km"]
                    # A rounded route boundary displayed to the user must be usable.
                    dialog.kp_end.setValue(START_KP + 3)
                    dialog.kp_end.setValue(float(f"{capture.end_kp_km:.3f}"))
                    assert dialog.location_values()["kp_end_km"] == capture.end_kp_km
                else:
                    dialog.kp.setValue(float(f"{capture.end_kp_km:.3f}"))
                    result = dialog.location_values()
                    assert abs(result["kp_km"] - capture.end_kp_km) <= 0.0005
            else:
                dialog.lon.setValue(3.04)
                assert _close(dialog.location_values()["lat"], initial["lat"], 1e-12)
        finally:
            dialog.deleteLater()
    QCoreApplication.processEvents()


def test_inline_kps(folder):
    from qgis.PyQt.QtTest import QTest

    iface = _Iface()
    project = QgsProject.instance()
    layer = _route_layer()
    project.addMapLayer(layer)
    capture = CaptureRoute.from_layer(layer, start_kp_km=START_KP, use_cartesian=False)
    path = os.path.join(folder, "inline.gpkg")
    store = CommentStore.create(path)
    set_id = store.list_sets()[0]["set_id"]
    comments = [store.add_comment(set_id, dict(values, text="Keep text", custom_json='{"extra":"keep"}'))
                for values in (capture.point_values(START_KP + 1.23456789),
                               capture.range_values(START_KP + 1.123456789, START_KP + 4),
                               capture.location_from_wgs84(55.02, 3.02), location_without_route(55.02, 3.02))]
    store.close()
    map_layers.set_project_register(path, set_id)
    dock = dock_module.RouteCommentsDock(iface, start_kp_for=lambda _layer: START_KP)
    original_question = dock_module.QMessageBox.question
    original_exec = dock_module.qt_exec
    asked, errors = [], []
    reply = [dock_module.MESSAGEBOX_YES]
    hook = [None]
    dock._error = lambda title, exc: errors.append(str(exc))

    def confirm(*args):
        asked.append(args[2])
        if args[1] == "Move route comment":
            assert args[-1] == dock_module.MESSAGEBOX_NO
            assert "From:" in args[2] and "To:" in args[2]
            if dock._kp_preview is not None:
                assert dock._kp_preview.overlay.valid, "Confirmation must keep the proposed position visible"
            if hook[0]:
                hook[0]()
            return reply[0]
        return dock_module.QMessageBox.StandardButton.Save

    dock_module.QMessageBox.question = staticmethod(confirm)

    def cell(comment):
        return dock.proxy.mapFromSource(dock.table_model.index(
            dock.table_model.row_of(comment["comment_id"]), dock.table_model.column_keys().index("kp")))

    def change(comment, value):
        result = dock.proxy.setData(cell(comment), value, Qt.ItemDataRole.EditRole)
        QTest.qWait(20)  # Delegate commit and its deferred map/table refresh are separate events.
        return result

    try:
        iface.window.show()
        dock.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        dock.show()
        dock.table.sortByColumn(1, Qt.SortOrder.DescendingOrder)
        point, section, location, unlinked = comments
        for comment in (point, section):
            assert cell(comment).flags() & Qt.ItemFlag.ItemIsEditable
        for comment in (location, unlinked):
            assert not cell(comment).flags() & Qt.ItemFlag.ItemIsEditable
            assert "calculated" in cell(comment).data(Qt.ItemDataRole.ToolTipRole)
            assert not change(comment, "15.000")
        assert not asked

        # Typing the displayed value again preserves full precision without a prompt.
        assert not change(point, f"{point['kp_km']:.3f}") and not asked
        assert not change(section, f"{section['kp_km']:.3f} to {section['kp_end_km']:.3f}") and not asked
        for invalid in ("nan", "inf", "abc", "14.1234", "99999", "", "12 – 14"):
            assert not change(point, invalid)
        assert not change(section, "15.000 – 15.000")
        assert not change(section, "15.000")
        assert not asked and dock.store.get_comment(point["comment_id"]) == point

        reply[0] = dock_module.MESSAGEBOX_NO
        assert not change(point, "15.000")
        assert dock.store.get_comment(point["comment_id"]) == point
        reply[0] = dock_module.MESSAGEBOX_YES
        assert change(point, "15.000")
        saved = dock.store.get_comment(point["comment_id"])
        assert saved["kp_km"] == 15 and saved["wkt"] != point["wkt"]
        assert saved["text"] == point["text"] and saved["custom_json"] == point["custom_json"]
        assert _pt(saved["wkt"]).distance(capture.route.point_at_kp(15)) < 1e-9
        feature = next(f for f in dock.layers.find("points").getFeatures() if f["comment_id"] == point["comment_id"])
        assert feature.geometry().equals(QgsGeometry.fromWkt(saved["wkt"]))

        assert change(section, f"{section['kp_km']:.3f} – 18.000")
        saved_range = dock.store.get_comment(section["comment_id"])
        assert saved_range["kp_km"] == section["kp_km"] and saved_range["kp_end_km"] == 18
        assert change(section, "18.000 - 15.000")
        saved_range = dock.store.get_comment(section["comment_id"])
        assert (saved_range["kp_km"], saved_range["kp_end_km"]) == (15, 18)
        assert not change(section, "18 to 15"), "Reversing an unchanged range should not write or prompt"

        # Range previews follow both edited endpoints without changing stored geometry.
        dock._select_ids([section["comment_id"]])
        dock.table.setCurrentIndex(cell(section))
        dock.table.setFocus()
        QTest.keyClick(dock.table, Qt.Key.Key_F2)
        QTest.qWait(160)
        editor = next(e for e in dock.table.findChildren(QLineEdit) if e.isVisible())
        editor.setText("16.000 – 18.000")
        QTest.qWait(160)
        assert dock._kp_preview.overlay.valid and len(dock._kp_preview.overlay.overlay.bands) == 6
        assert dock._kp_preview.overlay.wkt == capture.range_values(16, 18)["wkt"]
        assert dock.store.get_comment(section["comment_id"])["kp_km"] == 15
        QTest.keyClick(editor, Qt.Key.Key_Escape)
        QTest.qWait(20)
        assert dock._kp_preview is None

        # Exercise the real table editor: F2, Enter, confirmation, and Esc.
        dock._select_ids([point["comment_id"]])
        dock.table.setCurrentIndex(cell(point))
        dock.table.setFocus()
        QTest.keyClick(dock.table, Qt.Key.Key_F2)
        QTest.qWait(20)
        editor = next(e for e in dock.table.findChildren(QLineEdit) if e.isVisible())
        assert editor.text() == "15.000"
        editor.setText("unfinished")
        QTest.qWait(160)
        assert not dock._kp_preview.overlay.valid
        editor.setText("16.000")
        QTest.qWait(160)
        assert dock._kp_preview.overlay.wkt == capture.point_values(16)["wkt"]
        assert dock.store.get_comment(point["comment_id"])["kp_km"] == 15
        QTest.keyClick(editor, Qt.Key.Key_Return)
        QTest.qWait(20)
        assert dock.store.get_comment(point["comment_id"])["kp_km"] == 16
        assert dock._kp_preview is None
        dock.table.setCurrentIndex(cell(point))
        QTest.keyClick(dock.table, Qt.Key.Key_F2)
        QTest.qWait(20)
        editor = next(e for e in dock.table.findChildren(QLineEdit) if e.isVisible())
        editor.setText("17.000")
        count = len(asked)
        QTest.keyClick(editor, Qt.Key.Key_Escape)
        QTest.qWait(20)
        assert dock.store.get_comment(point["comment_id"])["kp_km"] == 16 and len(asked) == count
        assert dock._kp_preview is None

        # Saving pending Details may reset/sort the model during the commit.
        dock.form.text.setPlainText("Pending details")
        dock.table.setCurrentIndex(cell(point))
        QTest.keyClick(dock.table, Qt.Key.Key_F2)
        QTest.qWait(20)
        editor = next(e for e in dock.table.findChildren(QLineEdit) if e.isVisible())
        editor.setText("14.000")
        QTest.keyClick(editor, Qt.Key.Key_Return)
        QTest.qWait(20)
        assert dock.store.get_comment(point["comment_id"])["text"] == "Pending details"
        assert dock.store.get_comment(point["comment_id"])["kp_km"] == 14
        assert dock.by_id[section["comment_id"]]["kp_km"] == 15

        dock.thread.edit_reply(None)
        dock.thread._editing.editor.setPlainText("Pending thread text")
        assert change(point, "14.500")
        assert dock.store.get_comment(point["comment_id"])["text"] == "Pending thread text"
        assert not dock.thread.is_editing()

        # Double-click a derived nearest KP opens coordinate editing instead.
        opened = []
        dock_module.qt_exec = lambda dialog, *_a: opened.append(dialog.kind) or 0
        dock._select_ids([location["comment_id"]])
        dock._on_double_click(cell(location))
        assert opened == [model.REF_LOCATION]

        # Another writer changes a comment while confirmation is open.
        before = dock.store.get_comment(point["comment_id"])
        hook[0] = lambda: dock.store.update_comment(point["comment_id"], {"text": "Other writer"})
        assert not change(point, "16.000")
        after = dock.store.get_comment(point["comment_id"])
        assert after["wkt"] == before["wkt"] and after["text"] == "Other writer"
        assert "changed elsewhere" in errors[-1]
        hook[0] = None
        dock.reload()

        # A route change during confirmation must also prevent a move.
        feature = next(layer.getFeatures())
        hook[0] = lambda: layer.dataProvider().changeGeometryValues({
            feature.id(): QgsGeometry.fromWkt("LINESTRING(3 55, 4 56)")})
        assert not change(point, "16.000")
        assert "geometry has changed" in errors[-1]
        assert dock.store.get_comment(point["comment_id"])["wkt"] == before["wkt"]
    finally:
        dock_module.QMessageBox.question = original_question
        dock_module.qt_exec = original_exec
        dock.shutdown()
        dock.layers.remove()
        dock.deleteLater()
        project.clear()
        map_layers.set_project_register("", "")
        iface.close()
        QCoreApplication.processEvents()


def test_saved_route_basis(folder):
    project = QgsProject.instance()
    layer = _route_layer()
    layer_id = layer.id()
    project.addMapLayer(layer)
    checker = ReferenceChecker(start_kp_for=lambda _layer: 200)
    settings = kp_range_utils.kp_distance_mode(), kp_range_utils.kp_grid_crs_setting()
    try:
        for cartesian in (False, True):
            capture = CaptureRoute.from_layer(layer, QgsCoordinateReferenceSystem("EPSG:32631"),
                                              start_kp_km=START_KP, use_cartesian=cartesian,
                                              grid_crs="EPSG:32631")
            comment = capture.point_values(START_KP + 2.3456789)
            kp_range_utils.set_kp_distance_settings(kp_range_utils.KP_MODE_CARTESIAN, "EPSG:32630")
            restored = checker.capture_for_edit(comment)
            assert restored.start_kp_km == START_KP
            assert restored.crs.authid() == "EPSG:32631"
            assert restored.identity()["kp_convention"] == comment["kp_convention"]
            assert restored.point_values(START_KP + 3)["wkt"] == capture.point_values(START_KP + 3)["wkt"]
            assert kp_range_utils.kp_grid_crs_setting() == "EPSG:32630", "Editing changed global KP settings"

        feature = next(layer.getFeatures())
        layer.dataProvider().changeGeometryValues({feature.id(): QgsGeometry.fromWkt("LINESTRING(3 55, 4 56)")})
        try:
            checker.capture_for_edit(comment)
            raise AssertionError("Changed geometry was used to move an existing comment")
        except ValueError as exc:
            assert "changed" in str(exc)
        project.removeMapLayer(layer_id)
        try:
            checker.capture_for_edit(comment)
            raise AssertionError("A missing route was substituted")
        except ValueError as exc:
            assert "unavailable" in str(exc)
        assert checker.capture_for_edit(location_without_route(55, 3)) is None

        # Saved routes remain editable after removal from the project.
        file_layer, _path = _gpkg_route(folder)
        comment = CaptureRoute.from_layer(file_layer, start_kp_km=START_KP,
                                           use_cartesian=False).point_values(START_KP + 1)
        restored = checker.capture_for_edit(comment)
        assert restored.point_values(START_KP + 2)["kp_km"] == START_KP + 2
        # Earlier/imported rows without optional reference metadata still work.
        legacy = dict(comment, route_hash="", kp_convention="", geom_crs="")
        assert checker.capture_for_edit(legacy).start_kp_km == START_KP
    finally:
        if project.mapLayer(layer_id):
            project.removeMapLayer(layer_id)
        kp_range_utils.set_kp_distance_settings(*settings)


def test_dock_position_save(folder):
    iface = _Iface()
    project = QgsProject.instance()
    layer = _route_layer()
    project.addMapLayer(layer)
    cap = CaptureRoute.from_layer(layer, start_kp_km=START_KP, use_cartesian=False)
    path = os.path.join(folder, "positions.gpkg")
    store = CommentStore.create(path)
    set_id = store.list_sets()[0]["set_id"]
    comments = [store.add_comment(set_id, dict(values, text="Keep me", author="Reviewer", ext_id="external",
                                               tags="tag", custom_json='{"extra": "keep"}'))
                for values in (cap.point_values(START_KP + 1.23456789),
                               cap.range_values(START_KP + 1, START_KP + 4),
                               cap.location_from_wgs84(55.02, 3.02), location_without_route(55.02, 3.02))]
    cid = comments[0]["comment_id"]
    store.add_reply(cid, "Reviewer", "Keep the reply")
    attachment = store.add_attachments(cid, [_png(os.path.join(folder, "image.png"))])[0]
    store.set_map_image(cid, attachment["attachment_id"])
    store.close()
    # A row from an older schema can have NULL in the later version column.
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE rc_comment SET version = NULL WHERE comment_id = ?",
                           (comments[-1]["comment_id"],))
    map_layers.set_project_register(path, set_id)
    dock = dock_module.RouteCommentsDock(iface, start_kp_for=lambda _layer: START_KP)
    original_exec = dock_module.qt_exec
    errors = []
    dock._error = lambda title, exc: errors.append(str(exc))
    try:
        assert not dock.position_button.isEnabled()
        for index, (original, changes) in enumerate(zip(comments, (
                {"kp_km": START_KP + 3}, {"kp_km": START_KP + 2, "kp_end_km": START_KP + 5},
                {"lon": 3.04}, {"lat": 55.04}))):
            cid = original["comment_id"]
            dock._select_ids([cid])
            before = dock.store.get_comment(cid)
            assert dock.position_button.isEnabled()

            def enter(dialog, *_args, changes=changes):
                for key, value in changes.items():
                    dialog.editors[key].setValue(value)
                dialog.accept()
                return dialog.result()

            dock_module.qt_exec = enter
            assert dock.edit_position(), errors
            after = dock.store.get_comment(cid)
            assert after["wkt"] != before["wkt"] and after["version"] == (before["version"] or 0) + 1
            for key in ("comment_id", "set_id", "ref_no", "ext_id", "text", "status", "priority", "author",
                        "created_utc", "custom_json", "tags", "route_hash", "kp_convention", "map_image"):
                assert after[key] == before[key], key
            if index == 0:
                assert len(dock.store.list_replies(cid)) == 1
                assert len(dock.store.list_attachments(cid)) == 1
            display = dock.layers.find("ranges" if index == 1 else "points")
            feature = next(f for f in display.getFeatures() if f["comment_id"] == cid)
            assert feature.geometry().equals(QgsGeometry.fromWkt(after["wkt"]))
            assert dock.by_id[cid]["kp_km"] == after["kp_km"]
            assert model.location_text(after) in dock.editor_title.text()
            dock_module.qt_exec = lambda dialog, *_a: 1
            assert dock.edit_position() and dock.store.get_comment(cid)["version"] == after["version"]
            def cancel(dialog, *_args, changes=changes):
                key = next(iter(changes))
                dialog.editors[key].setValue(changes[key] + 0.01)
                dialog.reject()
                return dialog.result()

            dock_module.qt_exec = cancel
            assert not dock.edit_position() and dock.store.get_comment(cid) == after

        # Pending Details edits are saved before moving, and kept by the move.
        cid = comments[0]["comment_id"]
        dock._select_ids([cid])
        original_question = dock_module.QMessageBox.question
        dock_module.QMessageBox.question = staticmethod(lambda *_a, **_k: dock_module.QMessageBox.StandardButton.Save)
        try:
            dock.form.text.setPlainText("Edited details")
            dock_module.qt_exec = lambda dialog, *_a: enter(dialog, changes={"kp_km": START_KP + 3.5})
            assert dock.edit_position(), errors
            after = dock.store.get_comment(cid)
            assert after["text"] == "Edited details" and after["kp_km"] == START_KP + 3.5
        finally:
            dock_module.QMessageBox.question = original_question

        # A concurrent edit must not be overwritten or move the feature.
        cid = comments[0]["comment_id"]
        dock._select_ids([cid])
        before = dock.store.get_comment(cid)

        def conflict(dialog, *_args):
            dialog.editors["kp_km"].setValue(START_KP + 4)
            dock.store.update_comment(cid, {"text": "Saved elsewhere"})
            dialog.accept()
            return dialog.result()

        dock_module.qt_exec = conflict
        assert not dock.edit_position()
        after = dock.store.get_comment(cid)
        assert after["wkt"] == before["wkt"] and after["kp_km"] == before["kp_km"]
        assert after["text"] == "Saved elsewhere" and "changed elsewhere" in errors[-1]
        assert not dock._editing_position

        # Even a route edit made during the dialog must leave the comment alone.
        dock.reload()
        before = dock.store.get_comment(cid)

        def route_changed(dialog, *_args):
            dialog.editors["kp_km"].setValue(START_KP + 4)
            feature = next(layer.getFeatures())
            layer.dataProvider().changeGeometryValues({
                feature.id(): QgsGeometry.fromWkt("LINESTRING(3 55, 4 56)")})
            dialog.accept()
            return dialog.result()

        dock_module.qt_exec = route_changed
        assert not dock.edit_position()
        assert dock.store.get_comment(cid) == before and "geometry has changed" in errors[-1]
    finally:
        dock_module.qt_exec = original_exec
        dock.shutdown()
        dock.layers.remove()
        dock.deleteLater()
        project.clear()
        map_layers.set_project_register("", "")
        iface.close()
        QCoreApplication.processEvents()


def run_all():
    old_format = QSettings.defaultFormat()
    results = []
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, folder)
        try:
            for name, test in (("position form, validation and precision", test_position_form),
                               ("new-comment capture precision and KP validation", test_new_comment_precision),
                               ("inline KP editing, confirmation, keyboard and conflicts", lambda: test_inline_kps(folder)),
                               ("saved route basis and legacy references", lambda: test_saved_route_basis(folder)),
                               ("dock saves, map geometry, cancellation and conflicts",
                                lambda: test_dock_position_save(folder))):
                try:
                    test()
                    results.append(True)
                    print("[PASS]", name)
                except Exception:
                    import traceback
                    traceback.print_exc()
                    results.append(False)
        finally:
            QSettings.setDefaultFormat(old_format)
            from ..route_comments.store import close_connection
            close_connection()
    return results

# -*- coding: utf-8 -*-
"""Route Comments in QGIS: map images that follow the register, adding
comments without the KP Mouse Tool, general comments, and copy / move /
duplicate / renumber.

* Map images are found beside the file the comment layer reads: they draw
  with the project variable missing or naming another user's folder, from
  a folder whose name has spaces, '&' and '#'. Labels made by earlier
  versions are upgraded in place (images, and steps that include their own
  scale) and keep the user's other changes; the upgrade runs once. Images
  show at the detail scale itself (a map set to 1:50,000).
* A project route layer with the comment's layer ID, opened from another
  folder (a synced folder under another user name), is the comment's route.
* The Add map tool: a click is a route point at the nearest KP, Shift+click
  the position itself, a drag along the route a KP range (previewed and
  removed afterwards); Esc and a right-click stop it; without a route a
  click is a map position.
* The dock: Add keeps the tool on and restores the previous map tool when
  stopped; a KP typed from the map centre; general comments (not on the
  map) given a location later by a click; copy or move selected comments
  to another set or a new set; duplicate a set and the register; renumber
  by KP; an import with map images makes the labels able to show them; the
  tool stops when the project closes.

QSettings are redirected to a temporary folder.
"""

from __future__ import annotations

import csv
import os
import shutil
import tempfile

from qgis.core import QgsExpressionContextUtils, QgsPalLayerSettings, QgsPointXY, QgsProject, QgsRectangle
from qgis.PyQt.QtCore import QCoreApplication, QEvent, QPoint, QSettings, Qt
from qgis.PyQt.QtGui import QColor, QImage, QKeyEvent

from ..route_comments import dock as dock_module
from ..route_comments import map_layers, model
from ..route_comments.place import CommentPlaceTool
from ..route_comments.route_ref import CaptureRoute, ReferenceChecker
from ..route_comments.store import CommentStore, close_connection, is_register
from .temp_utils import temporary_directory
from .test_route_comments_qgis import (START_KP, _AcceptingNewComment, _close, _gpkg_route, _Iface, _red_pixels,
                                       _render_comment, _route_layer)

REQUIRES_QGIS = True
ELSEWHERE = "file:///C:/Users/someone-else/Synced/register_attachments"


def _result(name, ok, detail=""):
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""))
    return bool(ok)


def _red_png(path, size=(160, 80)):
    image = QImage(size[0], size[1], QImage.Format.Format_RGB32)
    image.fill(QColor(255, 0, 0))
    image.save(path, "PNG")
    return path


def _legacy_labeling(settings, width_pt):
    """Labels as versions before this one made them: the image from the project
    variable, step scales exactly as set; the details text enlarged by hand."""
    labeling = map_layers.build_labeling(settings, False)
    for rule in labeling.rootRule().children():
        for get, put in ((rule.minimumScale, rule.setMinimumScale), (rule.maximumScale, rule.setMaximumScale)):
            if get():
                put(float(round(get() / 1.001)))
        pal = QgsPalLayerSettings(rule.settings())
        pal.fieldName = pal.fieldName.replace(map_layers.image_html(settings.image_mm),
                                              map_layers._legacy_image_html_pt(str(width_pt)))
        if rule.description() == "Details":
            fmt = pal.format()
            fmt.setSize(11.0)
            pal.setFormat(fmt)
        rule.setSettings(pal)
    return labeling


def test_images_follow_the_register(folder):
    project = QgsProject.instance()
    layer = _route_layer()
    project.addMapLayer(layer)
    register = os.path.join(folder, "Synced - Team & Co #2", "review.gpkg")
    os.makedirs(os.path.dirname(register))
    store = CommentStore.create(register)
    set_id = store.list_sets()[0]["set_id"]
    cap = CaptureRoute.from_layer(layer, layer.crs(), start_kp_km=START_KP, use_cartesian=False)
    comment = store.add_comments(set_id, [dict(cap.point_values(START_KP + 1.0), text="Report page",
                                               attach_paths=[_red_png(os.path.join(folder, "page.png"))],
                                               attach_on_map=True)])[0]
    layers = map_layers.CommentLayers()
    layers.show(register, set_id, "Comments")
    points = layers.find("points")
    var = map_layers.VAR_ATTACHMENTS
    checks = {}

    def drawn(scale=5000):
        return _red_pixels(_render_comment(points, comment["wkt"], scale=scale)) > 300

    try:
        checks["drawn"] = drawn()
        QgsExpressionContextUtils.setProjectVariable(project, var, ELSEWHERE)
        checks["drawn while the variable names another user's folder"] = drawn()
        QgsExpressionContextUtils.removeProjectVariable(project, var)
        checks["drawn without the variable"] = drawn()
        checks["drawn at the detail scale itself (1:50,000)"] = drawn(50000)
        checks["not at 1:52,000 (the short label)"] = not drawn(52000)

        settings = map_layers.LabelSettings()
        width = max(1, int(round(settings.image_mm * map_layers.PT_PER_MM)))
        points.setLabeling(_legacy_labeling(settings, width))
        points.setCustomProperty(map_layers.LABELS_PROP, settings.to_json())
        QgsExpressionContextUtils.setProjectVariable(project, var, ELSEWHERE)
        checks["earlier labels: lost when the variable is another user's"] = not drawn()
        changed = map_layers.upgrade_labels(points)
        rules = {r.description(): r for r in points.labeling().rootRule().children()}
        details = rules["Details"]
        checks["earlier labels upgraded"] = changed and "layer_property" in details.settings().fieldName and \
            _close(details.minimumScale(), 50000 * 1.001, 1e-6) and \
            _close(rules["Short"].maximumScale(), 50000 * 1.001, 1e-6) and \
            _close(rules["Number"].minimumScale(), 2000000 * 1.001, 1e-6)
        checks["the user's own change kept"] = _close(details.settings().format().size(), 11.0, 1e-9)
        checks["upgraded once"] = not map_layers.upgrade_labels(points)
        checks["earlier labels drawn after the upgrade"] = drawn() and drawn(50000)
        checks["labels_show_images"] = map_layers.labels_show_images(points)
        # The dock's show() upgrades too (a layer opened from an older project).
        points.setLabeling(_legacy_labeling(settings, width))
        layers.show(register, set_id, "Comments")
        checks["show() upgrades earlier labels"] = "layer_property" in \
            {r.description(): r for r in points.labeling().rootRule().children()}["Details"].settings().fieldName
    finally:
        QgsExpressionContextUtils.removeProjectVariable(project, var)
        layers.remove()
        store.close()
        project.removeMapLayer(layer.id())
    failed = [k for k, v in checks.items() if not v]
    return _result("map images follow the register; earlier labels upgraded in place", not failed, ", ".join(failed))


def test_route_from_another_folder(folder):
    project = QgsProject.instance()
    route, path = _gpkg_route(folder, "route_a")
    project.addMapLayer(route)
    checks = {}
    try:
        cap = CaptureRoute.from_layer(route, route.crs(), start_kp_km=START_KP, use_cartesian=False)
        comment = dict(cap.point_values(START_KP + 1.0), text="x", comment_id="c-1")
        checker = ReferenceChecker(start_kp_for=lambda _layer: START_KP, workbench_store=lambda: None)
        checks["found where it was made"] = checker.check(comment)[0] == model.COND_OK
        source = comment["route_source"]
        saved_path = source.split("|", 1)[0]
        elsewhere = dict(comment, route_source=source.replace(saved_path, "C:/Users/someone-else/Project/route_a.gpkg"))
        checker.clear()
        checks["found when saved under another user's folder"] = checker.check(elsewhere)[0] == model.COND_OK
        checks["position editable from there"] = checker.capture_for_edit(elsewhere) is not None
        other_name = dict(comment, route_source=source.replace(saved_path, "C:/Users/someone-else/Project/other.gpkg"))
        checker.clear()
        checks["another file name is not taken for it"] = checker.check(other_name)[0] == model.COND_UNAVAILABLE
        copy = os.path.join(folder, "copy", "route_a.gpkg")
        os.makedirs(os.path.dirname(copy))
        shutil.copy2(path, copy)
        existing = dict(comment, route_source=source.replace(saved_path, copy))
        checker.clear()
        # (Opened from its file, its start KP is the file's own: a non-zero one shows as a change.)
        checks["a saved file that is there is used, not swapped for the project layer"] = \
            checker.route_state(existing).how == "file" and checker.route_state(elsewhere).how == "project"
    finally:
        project.removeMapLayer(route.id())
    failed = [k for k, v in checks.items() if not v]
    return _result("a route opened from another user's folder is still the comment's route", not failed,
                   ", ".join(failed))


def _mouse(canvas, kind, pos, button=Qt.MouseButton.LeftButton, modifiers=Qt.KeyboardModifier.NoModifier):
    from qgis.gui import QgsMapMouseEvent
    buttons = button if kind != QEvent.Type.MouseButtonRelease else Qt.MouseButton.NoButton
    return QgsMapMouseEvent(canvas, kind, pos, button, buttons, modifiers)


def test_place_tool():
    iface = _Iface()
    canvas = iface.canvas
    layer = _route_layer()
    QgsProject.instance().addMapLayer(layer)
    iface.window.show()            # (off screen) so the canvas has its size for pixel positions
    iface.window.resize(900, 700)
    QCoreApplication.processEvents()
    canvas.setExtent(QgsRectangle(3.00, 55.00, 3.06, 55.04))
    QCoreApplication.processEvents()
    hints, placed, stopped = [], [], []
    tool = CommentPlaceTool(canvas, lambda: CaptureRoute.from_layer(layer, canvas.mapSettings().destinationCrs(),
                                                                    START_KP, use_cartesian=False), hints.append)
    tool.placed.connect(lambda kind, data: placed.append((kind, data)))
    tool.stopped.connect(lambda: stopped.append(True))
    checks = {}
    move, press, release = QEvent.Type.MouseMove, QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease
    try:
        canvas.setMapTool(tool)
        route = tool.capture.route

        def at(kp, dx=0, dy=0):
            pixel = canvas.getCoordinateTransform().transform(route.point_at_kp(kp))
            return QPoint(int(round(pixel.x())) + dx, int(round(pixel.y())) + dy)

        off = at(START_KP + 1.0, 0, 12)
        tool.canvasMoveEvent(_mouse(canvas, move, off, Qt.MouseButton.NoButton))
        checks["hover marks the route position"] = tool._marker is not None and tool._marker.isVisible() and \
            "KP" in hints[-1]
        tool.canvasPressEvent(_mouse(canvas, press, off))
        tool.canvasReleaseEvent(_mouse(canvas, release, off))
        nearest = route.kp_at_point(tool.toMapCoordinates(off)).kp_km
        checks["click: route point at the nearest KP"] = placed[-1][0] == model.REF_ROUTE_POINT and \
            _close(placed[-1][1]["kp_km"], nearest, 1e-9)
        shift = Qt.KeyboardModifier.ShiftModifier
        tool.canvasPressEvent(_mouse(canvas, press, off, modifiers=shift))
        tool.canvasReleaseEvent(_mouse(canvas, release, off, modifiers=shift))
        point = placed[-1][1].get("point")
        checks["Shift+click: the position itself"] = placed[-1][0] == model.REF_LOCATION and point is not None and \
            point.distance(tool.toMapCoordinates(off)) < 1e-9
        a, b = at(START_KP + 1.0), at(START_KP + 2.0)
        tool.canvasPressEvent(_mouse(canvas, press, a))
        tool.canvasMoveEvent(_mouse(canvas, move, QPoint((a.x() + b.x()) // 2, (a.y() + b.y()) // 2)))
        tool.canvasMoveEvent(_mouse(canvas, move, b))
        picker = tool._picker
        band = picker.band if picker is not None else None
        drawn = band is not None and band.asGeometry().length() > 0
        tool.canvasReleaseEvent(_mouse(canvas, release, b))
        kind, data = placed[-1]
        checks["drag: a KP range, previewed then removed"] = drawn and kind == model.REF_KP_RANGE and \
            abs(data["kps"][0] - (START_KP + 1.0)) < 0.05 and abs(data["kps"][1] - (START_KP + 2.0)) < 0.05 and \
            tool._picker is None and band.scene() is None
        count = len(placed)
        tool.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))
        tool.canvasReleaseEvent(_mouse(canvas, release, a, Qt.MouseButton.RightButton))
        checks["Esc and right-click stop"] = len(stopped) == 2 and len(placed) == count
        marker = tool._marker
        canvas.unsetMapTool(tool)
        checks["leaving removes the marker"] = tool._marker is None and marker.scene() is None and hints[-1] == ""

        bare = CommentPlaceTool(canvas, lambda: None)
        bare.placed.connect(lambda kind, data: placed.append((kind, data)))
        canvas.setMapTool(bare)
        bare.canvasPressEvent(_mouse(canvas, press, off))
        bare.canvasReleaseEvent(_mouse(canvas, release, off))
        checks["no route: a map position"] = placed[-1][0] == model.REF_LOCATION and "no KP" in bare.hint_text()
        canvas.unsetMapTool(bare)
    finally:
        canvas.unsetMapTool(tool)
        QgsProject.instance().removeMapLayer(layer.id())
        iface.close()
    failed = [k for k, v in checks.items() if not v]
    return _result("Add map tool: click, Shift+click, drag for a range, Esc / right-click", not failed,
                   ", ".join(failed))


def test_dock_tools(folder):
    from qgis.gui import QgsMapToolPan
    from ..route_comments.dialogs import ImportDialog
    project = QgsProject.instance()
    iface = _Iface()
    canvas = iface.canvas
    layer = _route_layer()
    project.addMapLayer(layer)
    register = os.path.join(folder, "tools.gpkg")
    store = CommentStore.create(register)
    set_id = store.list_sets()[0]["set_id"]
    cap = CaptureRoute.from_layer(layer, layer.crs(), start_kp_km=START_KP, use_cartesian=False)
    for i, kp in enumerate((3.0, 1.0, 2.0)):
        store.add_comment(set_id, dict(cap.point_values(START_KP + kp), text=f"Point {i}"))
    store.close()
    map_layers.set_project_register(register, set_id)
    asked = []
    copy_path = os.path.join(folder, "copies", "tools copy.gpkg")
    patches = [(dock_module, "NewCommentDialog", _AcceptingNewComment),
               (dock_module, "qt_exec", lambda obj, *a: 1),
               (dock_module.QMessageBox, "question",
                staticmethod(lambda *a, **k: asked.append(str(a[2]) if len(a) > 2 else "") or dock_module.MESSAGEBOX_YES)),
               (dock_module.QMessageBox, "information", staticmethod(lambda *a, **k: None)),
               (dock_module.QMessageBox, "warning", staticmethod(lambda *a, **k: None)),
               (dock_module.QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (copy_path, "")))]
    saved = [(obj, name, getattr(obj, name)) for obj, name, _v in patches]
    for obj, name, value in patches:
        setattr(obj, name, value)
    dock = dock_module.RouteCommentsDock(iface, start_kp_for=lambda _l: START_KP, reference_layer=lambda: layer)
    dock.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    checks = {}

    def by_kind(kind):
        return [c for c in dock.comments if c.get("ref_kind") == kind]

    try:
        iface.window.show()
        dock.show()
        canvas.setExtent(QgsRectangle(2.99, 54.99, 3.11, 55.08))
        pan = QgsMapToolPan(canvas)
        canvas.setMapTool(pan)

        # Add on the map: the tool stays on; stopping brings the previous tool back.
        dock.act_place.setChecked(True)
        tool = dock.place_tool
        checks["Add turns the map tool on"] = canvas.mapTool() is tool and dock.banner.isVisible() and \
            bool(dock.banner.property("placing"))
        before = len(dock.comments)
        tool.placed.emit(model.REF_ROUTE_POINT, {"kp_km": START_KP + 0.5})
        added = dock.by_id.get(dock.current_id or "", {})
        checks["a click adds a route point, the tool stays on"] = len(dock.comments) == before + 1 and \
            _close(added.get("kp_km"), START_KP + 0.5, 1e-9) and canvas.mapTool() is tool
        tool.placed.emit(model.REF_LOCATION, {"point": QgsPointXY(3.03, 55.03)})
        added = dock.by_id.get(dock.current_id or "", {})
        checks["Shift+click adds a position with its nearest KP"] = added.get("ref_kind") == model.REF_LOCATION \
            and added.get("kp_km") is not None and _close(added.get("lat"), 55.03, 1e-9)
        tool.stopped.emit()
        checks["stopping restores the previous map tool"] = canvas.mapTool() is pan and \
            not dock.act_place.isChecked() and not dock.banner.property("placing")

        # At a KP typed from the map centre.
        canvas.setCenter(cap.route.point_at_kp(START_KP + 2.5))
        typed = dock.add_at_kp(model.REF_ROUTE_POINT)
        ranged = dock.add_at_kp(model.REF_KP_RANGE)
        checks["at a KP / KP range from the map centre"] = typed is not None and \
            abs(typed["kp_km"] - (START_KP + 2.5)) < 0.01 and ranged is not None and \
            ranged["ref_kind"] == model.REF_KP_RANGE and _close(ranged["kp_end_km"] - ranged["kp_km"], 0.1, 1e-6)

        # A general comment: listed, not on the map; placed later by a click.
        general = dock.add_general()
        gid = general["comment_id"]
        index = dock.layers.index
        kind_col = dock.table_model.column_keys().index("kind")
        row = dock.table_model.row_of(gid)
        checks["general comment: listed, not on the map"] = general["ref_kind"] == model.REF_GENERAL and \
            gid not in index and len(index) == len(dock.comments) - 1 and \
            dock.table_model.data(dock.table_model.index(row, kind_col)) == "General" and \
            "general (not on the map)" in dock.status.text()
        dock._select_ids([gid])
        checks["its position button places it"] = dock.position_button.text() == "Place on the map…"
        dock.edit_position()
        checks["Edit position starts placing it"] = dock.act_place.isChecked() and dock._place_target == gid
        dock.place_tool.placed.emit(model.REF_ROUTE_POINT, {"kp_km": START_KP + 1.5})
        placed = dock.by_id[gid]
        checks["placed by a click, after asking"] = any("Place comment" in q for q in asked) and \
            placed["ref_kind"] == model.REF_ROUTE_POINT and _close(placed["kp_km"], START_KP + 1.5, 1e-9) and \
            placed["route_layer_id"] == layer.id() and gid in dock.layers.index and not dock.act_place.isChecked()

        # Copy / move to another set and to a new set.
        first_two = [c["comment_id"] for c in sorted(dock.comments, key=lambda c: c["ref_no"])[:2]]
        third = sorted(dock.comments, key=lambda c: c["ref_no"])[2]["comment_id"]
        count = len(dock.comments)
        result = dock.copy_to_set(None, ids=first_two, new_name="Selection")
        sets = {s["name"]: s["set_id"] for s in dock.store.list_sets()}
        target = sets.get("Selection")
        copies = dock.store.list_comments(target) if target else []
        checks["copy to a new set"] = result is not None and [c["ref_no"] for c in copies] == [1, 2] and \
            len(dock.comments) == count and dock.set_row["set_id"] == set_id and \
            "Copied 2 comment(s)" in dock.banner.text() and f"set:{target}" in dock.banner.text() and \
            dock.set_combo.findData(target) >= 0
        dock.copy_to_set(target, move=True, ids=[third])
        checks["move to another set"] = len(dock.comments) == count - 1 and third not in dock.by_id and \
            dock.store.get_comment(third)["set_id"] == target and dock.store.get_comment(third)["ref_no"] == 3 and \
            any("Move " in q for q in asked)
        dock._banner_link(f"set:{target}")
        checks["the banner link shows that set"] = dock.set_row["set_id"] == target and len(dock.comments) == 3
        dock.set_combo.setCurrentIndex(dock.set_combo.findData(set_id))

        # Duplicate the set (the dialog's defaults: everything).
        copy_set = dock.duplicate_set()
        checks["duplicate set"] = copy_set is not None and dock.set_row["set_id"] == copy_set["set_id"] and \
            len(dock.comments) == count - 1 and copy_set["name"] == "Comments (copy)"
        dock.set_combo.setCurrentIndex(dock.set_combo.findData(set_id))

        # Renumber by KP.
        numbered = dock.renumber_by_kp()
        ordered = sorted(dock.comments, key=lambda c: c["ref_no"])
        kps = [min(c["kp_km"], c.get("kp_end_km") or c["kp_km"]) for c in ordered]
        field = next((f for f in dock.fields if f.name == model.PREVIOUS_NUMBER_FIELD), None)
        column = dock.table_model.column_keys().index(f"cf:{field.field_id}") if field else -1
        checks["renumber by KP"] = numbered and kps == sorted(kps) and \
            [c["ref_no"] for c in ordered] == list(range(1, len(ordered) + 1)) and field is not None and \
            not dock.table.isColumnHidden(column)

        # Import with map images into labels made before images: they are updated on request.
        old = map_layers.LabelSettings().to_json().replace('"images": true, ', "").replace('"image_mm": 30.0, ', "")
        for lyr in dock.layers.layers():
            lyr.setCustomProperty(map_layers.LABELS_PROP, old)
            lyr.setLabeling(map_layers.build_labeling(map_layers.LabelSettings(images=False), False))
        picture = _red_png(os.path.join(folder, "page 1.png"))
        table = os.path.join(folder, "pictures.csv")
        with open(table, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Filename", "KP"])
            writer.writerow([os.path.basename(picture), START_KP + 1.2])
            writer.writerow(["", ""])

        class _Import(ImportDialog):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.load_file(table)
                self._combos["text"].setCurrentIndex(1)
                self._combos["images"].setCurrentIndex(1)
                self.images_on_map.setChecked(True)
                ImportDialog.accept(self)

        dock_module.ImportDialog = _Import
        asked.clear()
        try:
            dock.import_comments()
        finally:
            dock_module.ImportDialog = ImportDialog
        points = dock.layers.find("points")
        imported = [c for c in dock.comments if c.get("map_image")]
        checks["import with map images: labels updated to show them"] = len(imported) == 1 and \
            any("not set to show images" in q for q in asked) and map_layers.labels_show_images(points) and \
            _red_pixels(_render_comment(points, imported[0]["wkt"])) > 300

        # Duplicate the register: keep the original, then open the copy.
        path = dock.duplicate_register()
        checks["duplicate register"] = path == copy_path and is_register(copy_path) and \
            os.path.normcase(dock.store.path) == os.path.normcase(register) and \
            os.path.isdir(os.path.join(folder, "copies", "tools copy_attachments"))
        dock.open_register(copy_path)
        sources = {lyr.source().split("|", 1)[0] for lyr in dock.layers.layers()}
        checks["the copy opened, layers pointed at it"] = \
            os.path.normcase(dock.store.path) == os.path.normcase(os.path.abspath(copy_path)) and \
            {os.path.normcase(s) for s in sources} == {os.path.normcase(os.path.abspath(copy_path))}

        # The project closes while adding on the map: the tool lets go.
        dock.act_place.setChecked(True)
        tool = dock.place_tool
        project.clear()
        QCoreApplication.processEvents()
        checks["project closed: Add stops"] = canvas.mapTool() is not tool and not dock.act_place.isChecked()
    finally:
        dock.shutdown()
        dock.layers.remove()
        dock.deleteLater()
        for obj, name, value in saved:
            setattr(obj, name, value)
        project.clear()
        map_layers.set_project_register("", "")
        iface.close()
        QCoreApplication.processEvents()
    failed = [k for k, v in checks.items() if not v]
    return _result("dock: Add on the map, typed KP, general comments, copy / move, duplicate, renumber, import",
                   not failed, ", ".join(failed))


def run_all():
    results = []
    old_format = QSettings.defaultFormat()
    with temporary_directory() as temp:
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, temp)
        try:
            for test in (lambda: test_images_follow_the_register(tempfile.mkdtemp(dir=temp)),
                         lambda: test_route_from_another_folder(tempfile.mkdtemp(dir=temp)),
                         test_place_tool,
                         lambda: test_dock_tools(tempfile.mkdtemp(dir=temp))):
                try:
                    results.append(test())
                except Exception as exc:  # report, keep going
                    import traceback
                    traceback.print_exc()
                    results.append(_result(getattr(test, "__name__", "test"), False, repr(exc)))
        finally:
            close_connection()
            QSettings.setDefaultFormat(old_format)
    return results


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run via tests/run_qgis_smoke_tests.py (needs QGIS Python).")

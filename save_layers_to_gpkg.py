# -*- coding: utf-8 -*-
"""Save Layers to GeoPackage.

Writes the selected vector layers into one GeoPackage and points each project
layer at its saved table, keeping its name, style and place in the Layers
panel. It is QGIS's per-layer "Make Permanent" for many layers at once, and
works for any vector layer (temporary, scratch, shapefile, ...).
"""

import os
from dataclasses import dataclass, field
from typing import Dict, List

from qgis.PyQt.QtCore import QCoreApplication, QSettings
from qgis.PyQt.QtWidgets import QFileDialog, QMessageBox, QProgressDialog
from qgis.core import (
    QgsDataProvider,
    QgsFeedback,
    QgsLayerTreeGroup,
    QgsProject,
    QgsProviderRegistry,
    QgsVectorLayer,
)

from .gpkg_writer import (
    gpkg_layer_uri,
    gpkg_table_name,
    gpkg_table_names,
    stored_field_indexes,
    write_layer_to_gpkg,
)
from .qgis_compat import (
    FILE_DIALOG_DONT_CONFIRM_OVERWRITE,
    MESSAGE_SUCCESS,
    MESSAGE_WARNING,
    MESSAGEBOX_NO,
    MESSAGEBOX_YES,
    qt_exec,
)

SETTINGS_LAST_DIR = "subsea_cable_tools/save_layers_gpkg/last_dir"
TITLE = "Save Layers to GeoPackage"


@dataclass
class LayerSave:
    """One layer's place in a save: its target table, or why it is skipped."""
    layer: object
    table: str = ""
    skip_reason: str = ""
    error: str = ""
    renamed_fields: Dict[str, str] = field(default_factory=dict)

    @property
    def saved(self):
        return bool(self.table) and not self.skip_reason and not self.error


def _layer_gpkg_path(layer):
    """The GeoPackage file ``layer`` reads from ('' if it is not a GeoPackage layer)."""
    if layer.providerType() != "ogr":
        return ""
    parts = QgsProviderRegistry.instance().decodeUri("ogr", layer.source())
    path = parts.get("path") or ""
    return path if path.lower().endswith(".gpkg") else ""


def _same_file(a, b):
    return bool(a) and bool(b) and os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def plan_save(layers, gpkg_path) -> List[LayerSave]:
    """Choose a table name for each layer, or the reason it cannot be saved."""
    plan = []
    taken = []
    for layer in layers:
        entry = LayerSave(layer)
        if not isinstance(layer, QgsVectorLayer):
            entry.skip_reason = "not a vector layer"
        elif not layer.isValid():
            entry.skip_reason = "layer is broken"
        elif layer.isEditable():
            entry.skip_reason = "in edit mode - save or discard its edits first"
        elif _same_file(_layer_gpkg_path(layer), gpkg_path):
            entry.skip_reason = "already stored in this GeoPackage"
        else:
            entry.table = gpkg_table_name(layer.name(), taken)
            taken.append(entry.table)
        plan.append(entry)
    return plan


def _relink(layer, gpkg_path, table):
    """Point ``layer`` at the saved table, keeping its name and style."""
    uri = gpkg_layer_uri(gpkg_path, table)
    try:
        layer.setDataSource(uri, layer.name(), "ogr", QgsDataProvider.ProviderOptions())
    except TypeError:
        layer.setDataSource(uri, layer.name(), "ogr")
    return layer.isValid()


def save_layers(plan, gpkg_path, transform_context, relink=True, feedback=None):
    """Write each planned layer to ``gpkg_path`` and (optionally) relink it.

    ``feedback`` receives overall progress (0-100) and is checked for cancel
    between layers. Fills ``error`` / ``renamed_fields`` on the plan entries.
    """
    todo = [entry for entry in plan if entry.table and not entry.skip_reason]
    for index, entry in enumerate(todo):
        if feedback is not None and feedback.isCanceled():
            entry.error = "canceled"
            continue
        layer = entry.layer
        layer_feedback = None
        if feedback is not None:
            layer_feedback = QgsFeedback()
            layer_feedback.progressChanged.connect(
                lambda p, i=index: feedback.setProgress((i + p / 100.0) * 100.0 / len(todo)))
            feedback.canceled.connect(layer_feedback.cancel)
        indexes = stored_field_indexes(layer)
        error, entry.renamed_fields = write_layer_to_gpkg(
            layer,
            gpkg_path,
            entry.table,
            transform_context,
            feedback=layer_feedback,
            attributes=None if len(indexes) == layer.fields().count() else indexes,
        )
        if feedback is not None:
            feedback.canceled.disconnect(layer_feedback.cancel)
        if error:
            entry.error = error
            continue
        if relink and not _relink(layer, gpkg_path, entry.table):
            entry.error = "saved, but the layer could not be re-pointed at the GeoPackage"
    if feedback is not None:
        feedback.setProgress(100)
    return plan


# -- UI ----------------------------------------------------------------------
def _tr(text):
    return QCoreApplication.translate("SaveLayersToGpkg", text)


def selected_layers(iface):
    """Layers selected in the Layers panel, including those inside selected groups."""
    view = iface.layerTreeView()
    if view is None:
        return []
    getter = getattr(view, "selectedLayersRecursive", None) or view.selectedLayers
    seen = set()
    layers = []
    for layer in getter():
        if layer is not None and layer.id() not in seen:
            seen.add(layer.id())
            layers.append(layer)
    return layers


def _suggested_name(layers, project):
    """A file name from the layers' shared group (e.g. an MDB import group), else the first layer."""
    root = project.layerTreeRoot()
    parents = set()
    for layer in layers:
        node = root.findLayer(layer.id())
        parent = node.parent() if node is not None else None
        is_group = isinstance(parent, QgsLayerTreeGroup) and parent.parent() is not None
        parents.add(parent.name() if is_group else "")
    base = parents.pop() if len(parents) == 1 else ""
    base = os.path.splitext(base)[0] if base.lower().endswith((".mdb", ".accdb")) else base
    return gpkg_table_name(base or layers[0].name()) + ".gpkg"


def run_save_layers_dialog(iface, layers=None):
    """Ask for a GeoPackage, save the selected vector layers into it and relink them."""
    parent = iface.mainWindow()
    project = QgsProject.instance()
    layers = [l for l in (layers if layers is not None else selected_layers(iface))
              if isinstance(l, QgsVectorLayer)]
    if not layers:
        iface.messageBar().pushMessage(
            TITLE, _tr("Select one or more vector layers (or groups) in the Layers panel first."),
            level=MESSAGE_WARNING, duration=6)
        return

    settings = QSettings()
    start_dir = settings.value(SETTINGS_LAST_DIR, "") or project.homePath() or os.path.expanduser("~")
    gpkg_path, _ = QFileDialog.getSaveFileName(
        parent,
        _tr("Save {n} layer(s) to GeoPackage").format(n=len(layers)),
        os.path.join(start_dir, _suggested_name(layers, project)),
        _tr("GeoPackage (*.gpkg)"),
        "",
        FILE_DIALOG_DONT_CONFIRM_OVERWRITE,
    )
    if not gpkg_path:
        return
    if not gpkg_path.lower().endswith(".gpkg"):
        gpkg_path += ".gpkg"
    settings.setValue(SETTINGS_LAST_DIR, os.path.dirname(gpkg_path))

    plan = plan_save(layers, gpkg_path)
    existing = {name.casefold() for name in gpkg_table_names(gpkg_path)}
    clashes = [e.table for e in plan if e.table and not e.skip_reason and e.table.casefold() in existing]
    if clashes:
        answer = QMessageBox.question(
            parent, TITLE,
            _tr("{file} already contains {n} table(s) with these names:\n\n{names}\n\n"
                "Replace them? Other tables in the file are kept.").format(
                file=os.path.basename(gpkg_path), n=len(clashes), names="\n".join(clashes)),
            MESSAGEBOX_YES | MESSAGEBOX_NO, MESSAGEBOX_NO)
        if answer != MESSAGEBOX_YES:
            return

    progress = QProgressDialog(_tr("Saving layers to {file}…").format(
        file=os.path.basename(gpkg_path)), _tr("Cancel"), 0, 100, parent)
    progress.setWindowTitle(TITLE)
    progress.setModal(True)
    progress.setMinimumDuration(500)
    feedback = QgsFeedback()
    progress.canceled.connect(feedback.cancel)

    def _on_progress(value):
        progress.setValue(int(value))
        QCoreApplication.processEvents()

    feedback.progressChanged.connect(_on_progress)
    try:
        save_layers(plan, gpkg_path, project.transformContext(), relink=True, feedback=feedback)
    finally:
        progress.close()

    _report(iface, plan, gpkg_path)


def _report(iface, plan, gpkg_path):
    saved = [e for e in plan if e.saved]
    problems = [f"{e.layer.name()}: {e.skip_reason or e.error}" for e in plan if not e.saved]
    renames = [f"{e.layer.name()}: " + ", ".join(f"{a} -> {b}" for a, b in sorted(e.renamed_fields.items()))
               for e in saved if e.renamed_fields]
    file_name = os.path.basename(gpkg_path)
    if saved and not problems:
        iface.messageBar().pushMessage(
            TITLE, _tr("Saved {n} layer(s) to {file}.").format(n=len(saved), file=file_name),
            level=MESSAGE_SUCCESS, duration=6)
    if problems or renames:
        lines = [_tr("Saved {n} of {total} layer(s) to {path}.").format(
            n=len(saved), total=len(plan), path=gpkg_path)]
        if problems:
            lines += ["", _tr("Not saved:")] + problems
        if renames:
            lines += ["", _tr("Fields renamed for GeoPackage (names are case-insensitive; 'fid' is reserved):")] + renames
        box = QMessageBox(iface.mainWindow())
        box.setWindowTitle(TITLE)
        box.setText("\n".join(lines))
        qt_exec(box)

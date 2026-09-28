# -*- coding: utf-8 -*-
"""QGIS-runtime checks for Save Layers to GeoPackage and the MDB GeoPackage output."""

import gc
import os
import tempfile

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsProject,
    QgsVectorLayer,
)
from qgis.PyQt.QtGui import QColor

from ..gpkg_writer import gpkg_table_name, gpkg_table_names
from ..processing import import_mdb_algorithm as mdb_import
from ..processing.import_mdb_algorithm import ImportMdbAlgorithm
from ..qgis_compat import FIELD_TYPE_STRING
from ..save_layers_to_gpkg import plan_save, save_layers


def _result(name, ok, detail=""):
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" - " + detail) if detail else ""))
    return ok


def _memory_layer(name, fields="field=name:string", count=2):
    layer = QgsVectorLayer(f"Point?crs=EPSG:4326&{fields}", name, "memory")
    features = []
    for i in range(count):
        feature = QgsFeature(layer.fields())
        feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(i, i)))
        feature.setAttributes([f"f{i}"] + [None] * (layer.fields().count() - 1))
        features.append(feature)
    layer.dataProvider().addFeatures(features)
    return layer


def test_table_names_are_tidy_and_unique():
    ok = (
        gpkg_table_name("Survey_A - Soundings (Point)") == "Survey_A_-_Soundings_Point"
        and gpkg_table_name("Contours", ["contours"]) == "Contours_2"
        and gpkg_table_name("  ") == "layer"
    )
    return _result("GeoPackage table names are tidy and unique", ok)


def test_saves_relinks_and_keeps_style():
    temp_dir = tempfile.mkdtemp()
    gpkg = os.path.join(temp_dir, "out.gpkg")
    project = QgsProject.instance()
    contours = _memory_layer("Survey_A - Contours", "field=name:string&field=fid:string")
    contours.addExpressionField("upper(\"name\")", QgsField("name_upper", FIELD_TYPE_STRING))
    contours.renderer().symbol().setColor(QColor("#123456"))
    points = _memory_layer("Survey_A - Fixes", count=3)
    project.addMapLayers([contours, points])
    try:
        plan = save_layers(plan_save([contours, points], gpkg), gpkg, project.transformContext())
        names = contours.fields().names()
        # A source 'fid' field must not shift values (QGIS 3.x maps it onto the key).
        values = sorted(f["name"] for f in contours.getFeatures())
        ok = (
            values == ["f0", "f1"] and
            all(entry.saved for entry in plan)
            and contours.providerType() == "ogr" and points.providerType() == "ogr"
            and os.path.normcase(gpkg) in os.path.normcase(contours.source())
            and contours.name() == "Survey_A - Contours"
            and contours.featureCount() == 2 and points.featureCount() == 3
            and contours.renderer().symbol().color().name() == "#123456"
            and names.count("name_upper") == 1
            and "source_fid" in names
            and sorted(gpkg_table_names(gpkg)) == ["Survey_A_-_Contours", "Survey_A_-_Fixes"]
        )
        detail = "" if ok else f"fields={names} tables={gpkg_table_names(gpkg)} " + \
            "; ".join(f"{e.layer.name()}:{e.skip_reason or e.error}" for e in plan)
    finally:
        project.removeMapLayers([contours.id(), points.id()])
        gc.collect()
    return _result("layers save into one GeoPackage and keep name, style and expression fields", ok, detail)


def test_plan_skips_editing_and_same_file_layers_and_keeps_other_tables():
    temp_dir = tempfile.mkdtemp()
    gpkg = os.path.join(temp_dir, "out.gpkg")
    context = QgsProject.instance().transformContext()
    first = _memory_layer("First")
    save_layers(plan_save([first], gpkg), gpkg, context)
    editing = _memory_layer("Editing")
    editing.startEditing()
    second = _memory_layer("Second")
    plan = plan_save([first, editing, second], gpkg)
    reasons = [entry.skip_reason for entry in plan]
    save_layers(plan, gpkg, context)
    editing.rollBack()
    ok = (
        "already stored" in reasons[0]
        and "edit mode" in reasons[1]
        and reasons[2] == ""
        and sorted(gpkg_table_names(gpkg)) == ["First", "Second"]
    )
    first = second = editing = None
    gc.collect()
    return _result("plan skips edited/same-file layers; adding keeps existing tables", ok, str(reasons))


def test_mdb_output_paths_are_one_per_database():
    temp_dir = tempfile.mkdtemp()
    paths = ImportMdbAlgorithm._output_gpkg_paths(
        [r"C:\a\Survey.mdb", r"C:\b\survey.mdb", r"C:\a\Other.accdb"], temp_dir)
    names = [os.path.basename(p) for p in paths.values()]
    ok = (
        names == ["Survey.gpkg", "survey_2.gpkg", "Other.gpkg"]
        and ImportMdbAlgorithm._output_gpkg_paths(["x.mdb"], "") == {}
    )
    return _result("MDB import maps each database to its own GeoPackage", ok, str(names))


def test_mdb_layers_share_one_geopackage():
    temp_dir = tempfile.mkdtemp()
    gpkg = os.path.join(temp_dir, "Survey_A.gpkg")
    context = QgsProcessingContext()
    feedback = QgsProcessingFeedback()
    crs = QgsCoordinateReferenceSystem("EPSG:4326")
    layers = [
        mdb_import._write_mdb_layer(
            _memory_layer("src"), f"Survey_A - {table}", crs, gpkg, table, context, feedback)
        for table in ("Soundings", "Contours")
    ]
    ok = (
        all(layer is not None and layer.isValid() for layer in layers)
        and sorted(gpkg_table_names(gpkg)) == ["Contours", "Soundings"]
        and layers[0].name() == "Survey_A - Soundings"
    )
    layers = None
    gc.collect()
    return _result("MDB tables write into one GeoPackage per database", ok)


def run_all():
    return [
        test_table_names_are_tidy_and_unique(),
        test_saves_relinks_and_keeps_style(),
        test_plan_skips_editing_and_same_file_layers_and_keeps_other_tables(),
        test_mdb_output_paths_are_one_per_database(),
        test_mdb_layers_share_one_geopackage(),
    ]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)

# -*- coding: utf-8 -*-
"""Focused QGIS-runtime checks for the MDB processing algorithm."""

import os
import tempfile
import gc

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsProcessing,
    QgsProcessingContext,
    QgsProcessingException,
    QgsProcessingFeedback,
    QgsProcessingParameterMultipleLayers,
    QgsProject,
    QgsVectorLayer,
    QgsVectorFileWriter,
)

from ..processing import import_mdb_algorithm as mdb_import
from ..processing.import_mdb_algorithm import ImportMdbAlgorithm


def _result(name, ok, detail=""):
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" - " + detail) if detail else ""))
    return ok


class _Feedback(QgsProcessingFeedback):
    def __init__(self):
        super().__init__()
        self.errors = []

    def reportError(self, error, fatalError=False):
        self.errors.append(str(error))
        super().reportError(error, fatalError)


def test_multi_file_parameter():
    algorithm = ImportMdbAlgorithm()
    algorithm.initAlgorithm()
    parameter = algorithm.parameterDefinition(algorithm.INPUT_MDB)
    ok = (
        isinstance(parameter, QgsProcessingParameterMultipleLayers)
        and parameter.layerType() == QgsProcessing.TypeFile
    )
    return _result("MDB input uses native multi-file picker", ok)


def test_output_layer_details():
    project = QgsProject()
    context = QgsProcessingContext()
    context.setProject(project)
    layer = QgsVectorLayer("Point?crs=EPSG:4326", "old", "memory")

    ImportMdbAlgorithm._register_output_layer(
        context,
        layer,
        "Survey_A - Soundings",
        "Survey_A.mdb",
    )
    details = context.layerToLoadOnCompletionDetails(layer.id())
    ok = layer.name() == "Survey_A - Soundings" and details.name == layer.name() and details.forceName
    if hasattr(details, "groupName"):
        ok = ok and details.groupName == "Survey_A.mdb"
    return _result("MDB outputs carry filename and group details", ok)


def test_isolated_output_is_disk_backed():
    context = QgsProcessingContext()
    source = QgsVectorLayer(
        "Point?crs=EPSG:4326&field=fid:integer&field=source_fid:integer",
        "source",
        "memory",
    )
    source_feature = QgsFeature(source.fields())
    source_feature.setAttributes([11, 22])
    source.dataProvider().addFeature(source_feature)
    layer = mdb_import._write_to_temporary_gpkg(
        source,
        "Survey_A - Soundings",
        QgsCoordinateReferenceSystem("EPSG:4326"),
        context,
        _Feedback(),
    )
    ok = (
        layer is not None
        and layer.providerType() == "ogr"
        and ".gpkg|" in layer.source().lower()
        and "fid" not in {field.name().casefold() for field in layer.fields()}
        and "__subsea_fid" in layer.fields().names()
        and "source_fid" in layer.fields().names()
        and "source_fid_2" in layer.fields().names()
    )
    if ok:
        iterator = layer.getFeatures()
        imported_feature = next(iterator)
        ok = imported_feature["source_fid_2"] == 11 and imported_feature["source_fid"] == 22
        iterator.close()
    return _result("isolated MDB outputs use a merge-safe GeoPackage key", ok)


def test_cancel_terminates_worker():
    class CancelFeedback(_Feedback):
        def __init__(self):
            super().__init__()
            self.cancel_checks = 0

        def isCanceled(self):
            self.cancel_checks += 1
            return self.cancel_checks > 1

    class FakeProcess:
        def __init__(self):
            self.returncode = None
            self.terminated = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -1

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

        def communicate(self):
            return "", ""

    fake_process = FakeProcess()
    original_popen = mdb_import.subprocess.Popen
    mdb_import.subprocess.Popen = lambda *args, **kwargs: fake_process
    try:
        try:
            ImportMdbAlgorithm()._run_worker([], CancelFeedback())
        except QgsProcessingException as exc:
            canceled = "canceled" in str(exc).lower()
        else:
            canceled = False
    finally:
        mdb_import.subprocess.Popen = original_popen
    return _result("cancel terminates the active MDB worker", canceled and fake_process.terminated)


def test_merged_internal_ids_save_to_geopackage():
    with tempfile.TemporaryDirectory() as temp_dir:
        merged = QgsVectorLayer(
            "Point?crs=EPSG:4326&field=__subsea_fid:integer",
            "combined",
            "memory",
        )
        features = []
        for _ in range(2):
            feature = QgsFeature(merged.fields())
            feature.setAttributes([1])
            features.append(feature)
        merged.dataProvider().addFeatures(features)

        output_path = os.path.join(temp_dir, "combined.gpkg")
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.layerName = "combined"
        result = QgsVectorFileWriter.writeAsVectorFormatV3(
            merged,
            output_path,
            QgsProject.instance().transformContext(),
            options,
        )
        writer_error = result[0] if isinstance(result, tuple) else result
        error_scope = getattr(QgsVectorFileWriter, "WriterError", QgsVectorFileWriter)
        saved = QgsVectorLayer(f"{output_path}|layername=combined", "combined", "ogr")
        ok = (
            writer_error == getattr(error_scope, "NoError")
            and saved.isValid()
            and saved.featureCount() == 2
            and "fid" in saved.fields().names()
            and "__subsea_fid" in saved.fields().names()
        )
        saved = None
        merged = None
        gc.collect()
    return _result("merged duplicate internal IDs save with a fresh GeoPackage fid", ok)


def test_multi_file_dispatch_and_failure_isolation():
    class FakeAlgorithm(ImportMdbAlgorithm):
        def __init__(self, files):
            super().__init__()
            self.files = files
            self.calls = []

        def parameterAsFileList(self, parameters, name, context):
            return self.files

        def parameterAsCrs(self, parameters, name, context):
            return QgsCoordinateReferenceSystem("EPSG:4326")

        def _process_mdb_file(self, mdb_file, target_crs, context, feedback, **options):
            self.calls.append(os.path.basename(mdb_file))
            if os.path.basename(mdb_file) == "broken.mdb":
                raise QgsProcessingException("test failure")
            return {f"{os.path.basename(mdb_file)}::Table": os.path.basename(mdb_file)}

    with tempfile.TemporaryDirectory() as temp_dir:
        good_file = os.path.join(temp_dir, "good.mdb")
        broken_file = os.path.join(temp_dir, "broken.mdb")
        open(good_file, "wb").close()
        open(broken_file, "wb").close()

        algorithm = FakeAlgorithm([broken_file, good_file, good_file])
        feedback = _Feedback()
        result = algorithm.processAlgorithm({}, QgsProcessingContext(), feedback)

    outputs = result[algorithm.OUTPUT_LAYERS]
    ok = (
        algorithm.calls == ["broken.mdb", "good.mdb"]
        and outputs == {"good.mdb::Table": "good.mdb"}
        and any("broken.mdb: test failure" in error for error in feedback.errors)
    )
    return _result("MDB files dispatch once and failures do not block others", ok, str(algorithm.calls))


def run_all():
    return [
        test_multi_file_parameter(),
        test_output_layer_details(),
        test_isolated_output_is_disk_backed(),
        test_cancel_terminates_worker(),
        test_merged_internal_ids_save_to_geopackage(),
        test_multi_file_dispatch_and_failure_isolation(),
    ]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)
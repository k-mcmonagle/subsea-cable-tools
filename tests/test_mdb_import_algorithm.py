# -*- coding: utf-8 -*-
"""Focused QGIS-runtime checks for the MDB processing algorithm."""

import os
import math
import tempfile
import gc

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsProcessing,
    QgsProcessingContext,
    QgsProcessingException,
    QgsProcessingFeedback,
    QgsProcessingParameterMultipleLayers,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
    QgsVectorFileWriter,
)

from ..processing import import_mdb_algorithm as mdb_import
from ..processing.geomedia_crs import analyse_coordinate_systems
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

        def communicate(self, timeout=None):
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


_WGS84_CS = {
    "CSGUID": "{11111111-2222-3333-4444-555555555555}", "BaseStorageType": 1,
    "Stor2CompMatrix1": math.pi / 180.0, "Stor2CompMatrix6": math.pi / 180.0,
    "EquatorialRadius": 6378137.0, "InverseFlattening": 298.257223563, "GeodeticDatum": 17,
}
_UNKNOWN_CS = {
    "CSGUID": "{99999999-8888-7777-6666-555555555555}", "BaseStorageType": 0,
    "Stor2CompMatrix1": 1.0, "Stor2CompMatrix6": 1.0, "ProjAlgorithm": 12,
    "EquatorialRadius": 6378388.0, "InverseFlattening": 297.0,
}


def _listing(cs_row, tables=("Table",)):
    report = analyse_coordinate_systems([cs_row])
    return {
        "tables": {name: {"geom_field_name": "Geometry", "geometry_type_code": 1}
                   for name in tables},
        "non_spatial": [],
        "crs": report.to_dict(),
    }


class _FakeImport(ImportMdbAlgorithm):
    """Runs processAlgorithm with canned pre-flight listings per file."""

    def __init__(self, files, listings=None, user_crs="EPSG:4326", force=False):
        super().__init__()
        self.files = files
        self.listings = listings or {}
        self.user_crs = user_crs
        self.force = force
        self.calls = []
        self.assigned = {}
        self.preflighted = []

    def parameterAsFileList(self, parameters, name, context):
        return self.files

    def parameterAsCrs(self, parameters, name, context):
        return QgsCoordinateReferenceSystem(self.user_crs or "")

    def parameterAsEnum(self, parameters, name, context):
        return self.CRS_MODE_FORCE if self.force else self.CRS_MODE_FALLBACK

    def _preflight_file(self, mdb_file, feedback, isolate=True, schema_discovery=False):
        self.preflighted.append(os.path.basename(mdb_file))
        return self.listings.get(os.path.basename(mdb_file), _listing(_WGS84_CS))

    def _process_mdb_file(self, mdb_file, target_crs, context, feedback, **options):
        name = os.path.basename(mdb_file)
        self.calls.append(name)
        self.assigned[name] = {
            table: crs.authid() for table, crs in (options.get("table_crs") or {}).items()}
        self.assigned[name]["(default)"] = target_crs.authid() if target_crs else ""
        return {f"{name}::Table": name}


def _empty_files(temp_dir, *names):
    paths = []
    for name in names:
        path = os.path.join(temp_dir, name)
        open(path, "wb").close()
        paths.append(path)
    return paths


def test_crs_detected_from_file_without_user_crs():
    with tempfile.TemporaryDirectory() as temp_dir:
        files = _empty_files(temp_dir, "a.mdb")
        algorithm = _FakeImport(files, user_crs=None)
        algorithm.processAlgorithm({}, QgsProcessingContext(), _Feedback())
    assigned = algorithm.assigned.get("a.mdb", {})
    ok = assigned.get("Table") == "EPSG:4326" and assigned.get("(default)") == "EPSG:4326"
    return _result("MDB CRS is auto-detected when no Source CRS is set", ok, str(assigned))


def test_undetected_crs_fails_before_any_import():
    with tempfile.TemporaryDirectory() as temp_dir:
        files = _empty_files(temp_dir, "good.mdb", "odd.mdb")
        algorithm = _FakeImport(files, {"odd.mdb": _listing(_UNKNOWN_CS)}, user_crs=None)
        message = ""
        try:
            algorithm.processAlgorithm({}, QgsProcessingContext(), _Feedback())
        except QgsProcessingException as exc:
            message = str(exc)
    ok = (
        algorithm.preflighted == ["good.mdb", "odd.mdb"]
        and algorithm.calls == []                       # nothing imported
        and "odd.mdb:" in message
        and "Detected without problems: good.mdb" in message
        and "could not be detected" in message
        and "Source CRS" in message
    )
    return _result("undetectable MDB CRS stops the run before importing anything", ok, message)


def test_user_crs_is_fallback_for_undetected_files_only():
    with tempfile.TemporaryDirectory() as temp_dir:
        files = _empty_files(temp_dir, "good.mdb", "odd.mdb")
        algorithm = _FakeImport(files, {"odd.mdb": _listing(_UNKNOWN_CS)}, user_crs="EPSG:23031")
        feedback = _Feedback()
        algorithm.processAlgorithm({}, QgsProcessingContext(), feedback)
    ok = (
        algorithm.calls == ["good.mdb", "odd.mdb"]
        and algorithm.assigned["good.mdb"]["Table"] == "EPSG:4326"   # the file wins
        and algorithm.assigned["odd.mdb"]["Table"] == "EPSG:23031"   # fallback
    )
    return _result("Source CRS fills in only where detection fails", ok, str(algorithm.assigned))


def test_force_mode_ignores_file_crs():
    with tempfile.TemporaryDirectory() as temp_dir:
        files = _empty_files(temp_dir, "good.mdb")
        algorithm = _FakeImport(files, user_crs="EPSG:32631", force=True)
        algorithm.processAlgorithm({}, QgsProcessingContext(), _Feedback())
        force_without_crs = _FakeImport(files, user_crs=None, force=True)
        refused = False
        try:
            force_without_crs.processAlgorithm({}, QgsProcessingContext(), _Feedback())
        except QgsProcessingException:
            refused = True
    ok = (algorithm.assigned["good.mdb"]["(default)"] == "EPSG:32631"
          and "Table" not in algorithm.assigned["good.mdb"] and refused)
    return _result("'Always' mode assigns the Source CRS and requires one", ok,
                   str(algorithm.assigned))


def test_extent_crs_warning():
    wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    utm31 = QgsCoordinateReferenceSystem("EPSG:32631")
    degrees = QgsRectangle(2.0, 51.0, 3.0, 52.0)
    metres = QgsRectangle(430000.0, 5650000.0, 500000.0, 5760000.0)
    ok = (
        mdb_import.extent_crs_warning(degrees, wgs84) == ""
        and "cannot be longitude/latitude" in mdb_import.extent_crs_warning(metres, wgs84)
        and mdb_import.extent_crs_warning(metres, utm31) == ""
        and "look like degrees" in mdb_import.extent_crs_warning(degrees, utm31)
    )
    return _result("extent check flags a CRS that cannot fit the data", ok)


def test_multi_file_dispatch_and_failure_isolation():
    class FakeAlgorithm(_FakeImport):
        def __init__(self, files):
            super().__init__(files)

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


def test_source_crs_parameter_wording():
    algorithm = ImportMdbAlgorithm()
    algorithm.initAlgorithm()
    parameter = algorithm.parameterDefinition(algorithm.SOURCE_CRS)
    ok = (
        parameter is not None
        # The stored key stays TARGET_CRS so existing models keep working.
        and algorithm.SOURCE_CRS == "TARGET_CRS"
        and "Source CRS" in parameter.description()
        and "MDB" in parameter.description()
    )
    return _result("MDB CRS parameter is described as the source CRS", ok)


def test_worker_listing_envelope_and_legacy_shape():
    envelope = {
        "tables": {"A": {"geom_field_name": "Geometry", "geometry_type_code": 1}},
        "non_spatial": [{"table": "Sediment_Classification", "row_count": 12,
                         "reason": "no geometry field and no recognised coordinate pair"}],
    }
    tables, non_spatial = ImportMdbAlgorithm._read_listing(envelope)
    ok = list(tables) == ["A"] and len(non_spatial) == 1

    legacy = {"B": {"geom_field_name": "Geom", "geometry_type_code": 2}}
    tables, non_spatial = ImportMdbAlgorithm._read_listing(legacy)
    ok = ok and list(tables) == ["B"] and non_spatial == []
    ok = ok and ImportMdbAlgorithm._read_listing(None) == ({}, [])
    return _result("MDB worker listing accepts both response shapes", ok)


def test_multipoint_filtering_keeps_environment_behaviour():
    should_load = ImportMdbAlgorithm._should_load_geometry_type
    ok = (
        should_load("LineString", False)
        and should_load("Polygon", False)
        and should_load("Point", False)
        and not should_load("MultiPoint", False)
        and not should_load("MultiPolygon", False)
        and should_load("MultiPoint", True)
    )
    return _result("MultiPoint layers stay behind SUBSEA_MDB_LOAD_ALL_GEOMS", ok)


def test_populated_table_without_geometry_is_reported_as_an_error():
    feedback = _Feedback()
    ImportMdbAlgorithm._report_table_result(
        "Mag_Contact_ID",
        {
            "table": "Mag_Contact_ID", "status": "parse_failed", "row_count": 17,
            "non_null_geometry_count": 17, "blob_decoded_count": 0,
            "xy_fallback_count": 0, "invalid_geometry_count": 17,
            "outputs": {}, "message": "no geometry BLOB could be decoded",
        },
        feedback,
    )
    ok = len(feedback.errors) == 1 and "rows=17" in feedback.errors[0]

    empty_feedback = _Feedback()
    ImportMdbAlgorithm._report_table_result(
        "Subcropping_ROCK",
        {"table": "Subcropping_ROCK", "status": "empty", "row_count": 0,
         "outputs": {}, "message": "table is empty"},
        empty_feedback,
    )
    ok = ok and not empty_feedback.errors
    return _result("populated tables with no geometry are escalated, empty ones are not", ok)


def test_table_summary_reports_geometry_sources():
    summary = ImportMdbAlgorithm._summarise_table_result({
        "table": "Mag_Contact_ID", "row_count": 17, "non_null_geometry_count": 17,
        "blob_decoded_count": 0, "xy_fallback_count": 17,
        "invalid_geometry_count": 0, "outputs": {"Point": "/tmp/x.geojson"},
        "message": "",
    })
    ok = (
        "rows=17" in summary
        and "non-null BLOBs=17" in summary
        and "BLOB decoded=0" in summary
        and "XY fallback=17" in summary
        and "outputs=Point" in summary
    )
    return _result("MDB table summary shows BLOB and fallback counts", ok, summary)


def test_non_spatial_tables_are_reported_without_geometry():
    feedback = _Feedback()
    ImportMdbAlgorithm._report_non_spatial_tables(
        [{"table": "Sediment_Classification", "row_count": 12,
          "classification": "non_spatial",
          "reason": "no geometry field and no recognised coordinate pair"}],
        feedback,
    )
    ok = not feedback.errors
    return _result("non-spatial MDB tables are reported, not loaded", ok)


def test_temporary_gpkg_path_with_spaces():
    context = QgsProcessingContext()
    source = QgsVectorLayer("Point?crs=EPSG:4326&field=name:string", "source", "memory")
    feature = QgsFeature(source.fields())
    feature.setAttributes(["a"])
    source.dataProvider().addFeature(feature)
    layer = mdb_import._write_to_temporary_gpkg(
        source,
        "Survey Data 2024 - Mag Contact ID (Point)",
        QgsCoordinateReferenceSystem("EPSG:4326"),
        context,
        _Feedback(),
    )
    ok = layer is not None and layer.isValid() and layer.featureCount() == 1
    return _result("MDB output names containing spaces still write to GeoPackage", ok)


def test_case_insensitive_field_names_are_deduplicated():
    def _fields(names):
        fields = QgsFields()
        for name in names:
            fields.append(QgsField(name, mdb_import.FIELD_TYPE_DOUBLE))
        return list(fields)

    renamed, reserved = mdb_import.resolve_gpkg_field_names(
        _fields(("Depth", "Easting", "depth", "source")))
    ok = renamed == {"depth": "depth_2"} and "easting" in reserved

    renamed, _ = mdb_import.resolve_gpkg_field_names(_fields(("fid", "source_fid")))
    ok = ok and renamed == {"fid": "source_fid_2"}
    return _result("GeoPackage field names are de-duplicated case-insensitively", ok, str(renamed))


def test_source_depth_column_does_not_break_geopackage_output():
    """A source 'Depth' column and the derived 'depth' collide in GeoPackage."""
    context = QgsProcessingContext()
    source = QgsVectorLayer(
        "Point?crs=EPSG:4326&field=Depth:double&field=Easting:double"
        "&field=depth:double&field=source:string",
        "source",
        "memory",
    )
    feature = QgsFeature(source.fields())
    feature.setAttributes([12.5, 500100.0, 34.5, "survey.mdb"])
    source.dataProvider().addFeature(feature)

    feedback = _Feedback()
    layer = mdb_import._write_to_temporary_gpkg(
        source,
        "Survey - Sonar_Contact (Point)",
        QgsCoordinateReferenceSystem("EPSG:4326"),
        context,
        feedback,
    )
    ok = layer is not None and layer.isValid() and layer.featureCount() == 1
    if ok:
        names = layer.fields().names()
        ok = "Depth" in names and "depth_2" in names and not feedback.errors
        imported = next(layer.getFeatures())
        ok = ok and imported["Depth"] == 12.5 and imported["depth_2"] == 34.5
    return _result("source Depth column no longer blocks GeoPackage creation", ok)


def test_table_summary_reports_secondary_geometry():
    summary = ImportMdbAlgorithm._summarise_table_result({
        "table": "Description_Leader", "row_count": 254,
        "non_null_geometry_count": 114, "blob_decoded_count": 114,
        "secondary_blob_decoded_count": 140, "xy_fallback_count": 0,
        "invalid_geometry_count": 0,
        "geometry_fields_used": ["LinearGeometry", "CoordGeocodePoint"],
        "outputs": {"LineString": "a", "Point": "b"}, "message": "",
    })
    ok = (
        "secondary BLOB decoded=140" in summary
        and "geometry fields=LinearGeometry, CoordGeocodePoint" in summary
    )
    return _result("summary reports secondary geometry columns", ok, summary)


def run_all():
    return [
        test_multi_file_parameter(),
        test_output_layer_details(),
        test_isolated_output_is_disk_backed(),
        test_cancel_terminates_worker(),
        test_merged_internal_ids_save_to_geopackage(),
        test_multi_file_dispatch_and_failure_isolation(),
        test_crs_detected_from_file_without_user_crs(),
        test_undetected_crs_fails_before_any_import(),
        test_user_crs_is_fallback_for_undetected_files_only(),
        test_force_mode_ignores_file_crs(),
        test_extent_crs_warning(),
        test_source_crs_parameter_wording(),
        test_worker_listing_envelope_and_legacy_shape(),
        test_multipoint_filtering_keeps_environment_behaviour(),
        test_populated_table_without_geometry_is_reported_as_an_error(),
        test_table_summary_reports_geometry_sources(),
        test_non_spatial_tables_are_reported_without_geometry(),
        test_temporary_gpkg_path_with_spaces(),
        test_case_insensitive_field_names_are_deduplicated(),
        test_source_depth_column_does_not_break_geopackage_output(),
        test_table_summary_reports_secondary_geometry(),
    ]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)
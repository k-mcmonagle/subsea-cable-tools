# -*- coding: utf-8 -*-
"""Checks for the cable-lay management ops and the Recompute ISO Time tool.

Covers the ``import_log`` provenance rows written by the importers, the
in-place ISO_Time recompute (including the source-file filter and the
duplicate cleanup after a double import under two different start dates), and
the ``edit_log`` audit rows.

Requires the QGIS API (run via tests/run_qgis_smoke_tests.py).
"""

from __future__ import annotations

import os
import tempfile
from typing import List, Optional

from qgis.core import (
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsProject,
    QgsVectorLayer,
)

from ..processing import cable_lay_manage_ops as ops
from ..processing import cable_lay_parsers as clp
from ..processing.import_cable_lay_algorithm import ImportCableLayAlgorithm
from ..processing.recompute_iso_time_algorithm import RecomputeIsoTimeAlgorithm


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


_CABLE_LAY_FILE = (
    "Time,Ship Latitude,Ship Longitude,Ship KP\n"
    "dd:hh:mm:ss,dms,dms,km\n"
    '"1,14:00:00","17 09.7399N","169 30.1234W",0.000\n'
    '"1,14:00:01","17 09.8000N","169 30.2000W",0.025\n'
    '"2,02:30:00","17 09.9000N","169 30.3000W",0.050\n'
)


def _temp_dir() -> str:
    """Per-process scratch dir so parallel QGIS 3 / QGIS 4 runs never share files."""
    path = os.path.join(tempfile.gettempdir(), f"sct_mgmt_tests_{os.getpid()}")
    os.makedirs(path, exist_ok=True)
    return path


def _write_temp(name: str, content: str) -> str:
    path = os.path.join(_temp_dir(), name)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(content)
    return path


def _fresh_gpkg(name: str) -> str:
    path = os.path.join(_temp_dir(), name)
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            os.remove(path + suffix)
        except OSError:
            pass
    return path


def _open(gpkg_path: str, layer_name: str) -> Optional[QgsVectorLayer]:
    vl = QgsVectorLayer(clp.gpkg_layer_uri(gpkg_path, layer_name), layer_name, "ogr")
    return vl if vl.isValid() else None


def _context():
    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())
    return context


def _import(files: List[str], gpkg_path: str, start_date: str):
    alg = ImportCableLayAlgorithm()
    alg.initAlgorithm()
    alg.processAlgorithm(
        {"INPUT": files, "GEOPACKAGE": gpkg_path, "START_DATE": start_date},
        _context(),
        QgsProcessingFeedback(),
    )
    return _open(gpkg_path, clp.prefixed_layer_name(gpkg_path, "cable_lay"))


def _recompute(gpkg_path: str, start_date: str, extra: dict = None):
    name = clp.prefixed_layer_name(gpkg_path, "cable_lay")
    alg = RecomputeIsoTimeAlgorithm()
    alg.initAlgorithm()
    params = {
        "TARGET_LAYER": clp.gpkg_layer_uri(gpkg_path, name),
        "START_DATE": start_date,
    }
    if extra:
        params.update(extra)
    result = alg.processAlgorithm(params, _context(), QgsProcessingFeedback())
    return result, _open(gpkg_path, name)


def _iso_times(layer) -> List[str]:
    return sorted(str(f["ISO_Time"]) for f in layer.getFeatures())


# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------
def test_layer_type_for_name() -> bool:
    ok = ops.layer_type_for_name("ProjectX_cable_lay") == "cable_lay"
    ok = ok and ops.layer_type_for_name("plough_data") == "plough_data"
    ok = ok and ops.layer_type_for_name("something_else") is None
    return _result("layer_type_for_name (prefixed + bare)", ok)


# ---------------------------------------------------------------------------
# End-to-end management tests
# ---------------------------------------------------------------------------
def test_import_log_written() -> bool:
    path = _write_temp("sct_mgmt_import.csv", _CABLE_LAY_FILE)
    gpkg = _fresh_gpkg("sct_mgmt_import.gpkg")
    try:
        layer = _import([path], gpkg, "2024-01-01")
    except Exception as exc:
        return _result("import writes import_log", False, repr(exc))
    if layer is None or layer.featureCount() != 3:
        return _result("import writes import_log", False, "import itself failed")
    log = _open(gpkg, clp.prefixed_layer_name(gpkg, "import_log"))
    if log is None:
        return _result("import writes import_log", False, "no import_log table")
    entries = list(log.getFeatures())
    ok = len(entries) == 1
    if ok:
        entry = entries[0]
        ok = (
            str(entry["source_file"]) == "sct_mgmt_import.csv"
            and str(entry["start_date"]) == "2024-01-01"
            and int(entry["rows_parsed"]) == 3
            and str(entry["algorithm"]) == "import_cable_lay"
        )
    return _result("import writes import_log", ok, f"entries={len(entries)}")


def test_recompute_shifts_times() -> bool:
    path = _write_temp("sct_mgmt_fix.csv", _CABLE_LAY_FILE)
    gpkg = _fresh_gpkg("sct_mgmt_fix.gpkg")
    try:
        _import([path], gpkg, "2024-01-01")
        result, layer = _recompute(gpkg, "2024-02-01")
    except Exception as exc:
        return _result("recompute ISO_Time (in place)", False, repr(exc))
    if layer is None:
        return _result("recompute ISO_Time (in place)", False, "layer missing")
    times = _iso_times(layer)
    expected = ["2024-02-01T14:00:00", "2024-02-01T14:00:01", "2024-02-02T02:30:00"]
    ok = times == expected and result.get("UPDATED") == 3
    if ok:
        edit_log = _open(gpkg, clp.prefixed_layer_name(gpkg, "edit_log"))
        entries = [] if edit_log is None else list(edit_log.getFeatures())
        ok = len(entries) == 1 and str(entries[0]["operation"]) == "recompute_iso_time"
    return _result("recompute ISO_Time (in place)", ok, f"times={times}")


def test_recompute_source_filter() -> bool:
    a = _write_temp("sct_mgmt_a.csv", _CABLE_LAY_FILE)
    b = _write_temp("sct_mgmt_b.csv", _CABLE_LAY_FILE)
    gpkg = _fresh_gpkg("sct_mgmt_filter.gpkg")
    try:
        _import([a, b], gpkg, "2024-01-01")
        _, layer = _recompute(
            gpkg, "2024-03-01", {"SOURCE_FILES": "sct_mgmt_a.csv"}
        )
    except Exception as exc:
        return _result("recompute honours source-file filter", False, repr(exc))
    if layer is None or layer.featureCount() != 6:
        return _result("recompute honours source-file filter", False, "unexpected count")
    by_file = {}
    for feature in layer.getFeatures():
        by_file.setdefault(str(feature["source_file"]), set()).add(
            str(feature["ISO_Time"])[:7]
        )
    ok = by_file.get("sct_mgmt_a.csv") == {"2024-03"} and by_file.get(
        "sct_mgmt_b.csv"
    ) == {"2024-01"}
    return _result("recompute honours source-file filter", ok, f"{by_file}")


def test_recompute_dedupes_double_import() -> bool:
    """The same file imported under two start dates collapses after the fix."""
    path = _write_temp("sct_mgmt_dupe.csv", _CABLE_LAY_FILE)
    gpkg = _fresh_gpkg("sct_mgmt_dupe.gpkg")
    try:
        _import([path], gpkg, "2024-01-01")
        layer = _import([path], gpkg, "2024-02-01")  # different ISO_Time -> no dedupe
        if layer is None or layer.featureCount() != 6:
            return _result(
                "recompute dedupes double import",
                False,
                f"pre-fix count={None if layer is None else layer.featureCount()} (expected 6)",
            )
        result, layer = _recompute(gpkg, "2024-02-01")
    except Exception as exc:
        return _result("recompute dedupes double import", False, repr(exc))
    ok = (
        layer is not None
        and layer.featureCount() == 3
        and result.get("DUPLICATES_REMOVED") == 3
        and _iso_times(layer)[0] == "2024-02-01T14:00:00"
    )
    return _result(
        "recompute dedupes double import",
        ok,
        f"count={None if layer is None else layer.featureCount()} (expected 3)",
    )


def test_gap_math_pure() -> bool:
    epochs = [0.0, 10.0, 100.0, 110.0, float("nan")]
    gaps = ops.find_gaps_in_epochs(epochs, 60.0)
    ok = gaps == [(10.0, 100.0)]
    ok = ok and ops.epoch_in_gaps(50.0, gaps) and not ops.epoch_in_gaps(10.0, gaps)
    ok = ok and ops.classify_gap_fill([50.0, 200.0], gaps) == [
        ops.STATUS_ACTIVE, ops.STATUS_STANDBY,
    ]
    return _result("gap math (find/classify, pure)", ok, f"gaps={gaps}")


def test_fast_append_and_relog() -> bool:
    """A second import into an existing schema appends (and still dedupes)."""
    a = _write_temp("sct_mgmt_fast_a.csv", _CABLE_LAY_FILE)
    b = _write_temp("sct_mgmt_fast_b.csv", _CABLE_LAY_FILE)
    gpkg = _fresh_gpkg("sct_mgmt_fast.gpkg")
    try:
        first = _import([a], gpkg, "2024-01-01")
        if first is None or first.featureCount() != 3:
            return _result("provider fast-append + dedupe", False, "initial import failed")
        second = _import([b], gpkg, "2024-01-01")  # fast path: same schema
        if second is None or second.featureCount() != 6:
            return _result(
                "provider fast-append + dedupe", False,
                f"append count={None if second is None else second.featureCount()} (expected 6)",
            )
        third = _import([a, b], gpkg, "2024-01-01")  # all duplicates
    except Exception as exc:
        return _result("provider fast-append + dedupe", False, repr(exc))
    log = _open(gpkg, clp.prefixed_layer_name(gpkg, "import_log"))
    log_count = 0 if log is None else log.featureCount()
    ok = third is not None and third.featureCount() == 6 and log_count == 4
    return _result(
        "provider fast-append + dedupe",
        ok,
        f"count={None if third is None else third.featureCount()} (expected 6), "
        f"import_log rows={log_count} (expected 4)",
    )


_PRIMARY_FILE = (
    "Time,Ship Latitude,Ship Longitude,Ship KP\n"
    "dd:hh:mm:ss,dms,dms,km\n"
    '"1,14:00:00","17 09.7399N","169 30.1234W",0.000\n'
    '"1,14:00:01","17 09.8000N","169 30.2000W",0.025\n'
    '"1,14:10:00","17 09.9000N","169 30.3000W",0.050\n'
)

_SECONDARY_FILE = (
    "Time,Ship Latitude,Ship Longitude,Ship KP\n"
    "dd:hh:mm:ss,dms,dms,km\n"
    '"1,14:05:00","17 09.8100N","169 30.2100W",0.030\n'
    '"1,14:20:00","17 09.9500N","169 30.3500W",0.060\n'
)


class _Controller:
    """Minimal stand-in for the explorer window used by ManagePanel."""

    def __init__(self, layer, gpkg_path):
        self._layer = layer
        self._gpkg = gpkg_path

    @property
    def layer(self):
        return self._layer

    def gpkg_path(self):
        return self._gpkg

    def layer_name(self):
        return self._layer.name() if self._layer is not None else None

    def transform_context(self):
        return QgsProject.instance().transformContext()

    def reload_dataset(self):
        pass


def test_status_ops_and_filter() -> bool:
    a = _write_temp("sct_mgmt_status_a.csv", _PRIMARY_FILE)
    b = _write_temp("sct_mgmt_status_b.csv", _SECONDARY_FILE)
    gpkg = _fresh_gpkg("sct_mgmt_status.gpkg")
    try:
        layer = _import([a, b], gpkg, "2024-01-01")
        if layer is None or layer.featureCount() != 5:
            return _result("record_status ops + active filter", False, "import failed")
        changed = ops.set_source_status(layer, ops.STATUS_EXCLUDED, ["sct_mgmt_status_b.csv"])
        if changed != 2:
            return _result("record_status ops + active filter", False, f"changed={changed}")
        layer.setSubsetString(ops.active_subset_expression())
        active_count = layer.featureCount()
        layer.setSubsetString("")
        deleted = ops.delete_source_rows(layer, ["sct_mgmt_status_b.csv"])
    except Exception as exc:
        return _result("record_status ops + active filter", False, repr(exc))
    ok = active_count == 3 and deleted == 2 and layer.featureCount() == 3
    return _result(
        "record_status ops + active filter",
        ok,
        f"active={active_count} (expected 3), deleted={deleted} (expected 2)",
    )


def test_manage_panel_gap_fill() -> bool:
    """End-to-end: ManagePanel summarises sources and applies a gap fill."""
    try:
        from ..explorer.panels.manage_panel import ManagePanel
        from ..laydata import LayDataset
    except Exception as exc:
        return _result("Manage panel gap fill (end-to-end)", False, f"import: {exc!r}")

    a = _write_temp("sct_mgmt_panel_a.csv", _PRIMARY_FILE)
    b = _write_temp("sct_mgmt_panel_b.csv", _SECONDARY_FILE)
    gpkg = _fresh_gpkg("sct_mgmt_panel.gpkg")
    try:
        layer = _import([a, b], gpkg, "2024-01-01")
        if layer is None or layer.featureCount() != 5:
            return _result("Manage panel gap fill (end-to-end)", False, "import failed")
        ManagePanel.run_async = False  # edits run inline (still on a private layer)
        panel = ManagePanel(_Controller(layer, gpkg))
        panel.set_dataset(LayDataset.from_qgis_layer(layer))
        panel.refresh_now()

        if panel.sources_table.rowCount() != 2:
            return _result(
                "Manage panel gap fill (end-to-end)", False,
                f"sources rows={panel.sources_table.rowCount()} (expected 2)",
            )
        start_dates = {
            panel.sources_table.item(r, 0).text(): panel.sources_table.item(r, 4).text()
            for r in range(panel.sources_table.rowCount())
        }
        if set(start_dates.values()) != {"2024-01-01"}:
            return _result(
                "Manage panel gap fill (end-to-end)", False,
                f"import_log start dates not shown: {start_dates}",
            )

        panel.primary_combo.setCurrentText("sct_mgmt_panel_a.csv")
        panel.secondary_combo.setCurrentText("sct_mgmt_panel_b.csv")
        panel.threshold_spin.setValue(60)
        panel.find_gaps()
        if panel.gaps_table.rowCount() != 1:
            return _result(
                "Manage panel gap fill (end-to-end)", False,
                f"gaps found={panel.gaps_table.rowCount()} (expected 1)",
            )
        panel.apply_gap_fill()

        # The edit went through a private connection; re-open to see the new column.
        layer = _open(gpkg, clp.prefixed_layer_name(gpkg, "cable_lay"))
        status_by_time = {}
        for feature in layer.getFeatures():
            if str(feature["source_file"]) == "sct_mgmt_panel_b.csv":
                status_by_time[str(feature["ISO_Time"])] = str(feature["record_status"])
        ok = (
            status_by_time.get("2024-01-01T14:05:00") == ops.STATUS_ACTIVE
            and status_by_time.get("2024-01-01T14:20:00") == ops.STATUS_STANDBY
        )
        if ok:
            edit_log = _open(gpkg, clp.prefixed_layer_name(gpkg, "edit_log"))
            operations = (
                [] if edit_log is None
                else [str(f["operation"]) for f in edit_log.getFeatures()]
            )
            ok = "gap_fill" in operations
    except Exception as exc:
        return _result("Manage panel gap fill (end-to-end)", False, repr(exc))
    return _result(
        "Manage panel gap fill (end-to-end)", ok, f"secondary statuses={status_by_time}"
    )


def test_manage_panel_edits_ignore_map_filter() -> bool:
    """Regression: a provider filter on the project layer must not hide rows
    from the Manage panel's edits (they run on a private layer)."""
    name = "Manage panel edits ignore map filter"
    try:
        from ..explorer.panels.manage_panel import ManagePanel
        from ..laydata import LayDataset
    except Exception as exc:
        return _result(name, False, f"import: {exc!r}")

    a = _write_temp("sct_mgmt_pfilter_a.csv", _PRIMARY_FILE)
    b = _write_temp("sct_mgmt_pfilter_b.csv", _SECONDARY_FILE)
    gpkg = _fresh_gpkg("sct_mgmt_pfilter.gpkg")
    try:
        layer = _import([a, b], gpkg, "2024-01-01")
        if layer is None or layer.featureCount() != 5:
            return _result(
                name, False,
                f"import failed: count={None if layer is None else layer.featureCount()}",
            )
        # One row of file a goes to standby, then the map filter hides it.
        first_a = next(
            f.id() for f in layer.getFeatures()
            if str(f["source_file"]) == "sct_mgmt_pfilter_a.csv"
        )
        ops.apply_status(layer, {first_a: ops.STATUS_STANDBY})
        layer.setSubsetString(ops.active_subset_expression())
        visible = layer.featureCount()

        ManagePanel.run_async = False
        panel = ManagePanel(_Controller(layer, gpkg))
        panel.set_dataset(LayDataset.from_qgis_layer(layer))
        panel.refresh_now()
        row = next(
            r for r in range(panel.sources_table.rowCount())
            if panel.sources_table.item(r, 0).text() == "sct_mgmt_pfilter_a.csv"
        )
        panel.sources_table.selectRow(row)
        panel.status_combo.setCurrentText(ops.STATUS_EXCLUDED)
        panel.set_status()
        layer.setSubsetString("")
        reopened = _open(gpkg, clp.prefixed_layer_name(gpkg, "cable_lay"))
        excluded_a = sum(
            1 for f in reopened.getFeatures()
            if str(f["source_file"]) == "sct_mgmt_pfilter_a.csv"
            and str(f["record_status"]) == ops.STATUS_EXCLUDED
        )
    except Exception as exc:
        return _result(name, False, repr(exc))
    ok = visible == 4 and excluded_a == 3
    return _result(name, ok, f"visible with filter={visible} (expected 4), "
                             f"excluded rows of a={excluded_a} (expected 3)")


def test_key_value_normalisation() -> bool:
    """Parser-typed and provider-typed dedupe keys must compare equal."""
    name = "dedupe key normalisation (int/float/null)"
    ok = (
        clp.key_value(1) == "1" == clp.key_value(1.0)
        and clp.key_value(None) == ""
        and clp.key_value(float("nan")) == ""
        and clp.key_value("NULL") == "NULL"
        and clp.key_value(0.025) == clp.key_value(0.025)
        and clp.key_value("1,14:00:00") == "1,14:00:00"
    )
    detail = "pure checks"
    if ok:
        try:
            from qgis.core import QgsFeature

            mem = QgsVectorLayer("None?field=KP1:double&field=source_file:string", "m", "memory")
            feature = QgsFeature(mem.fields())
            feature.setAttributes([1, "x.csv"])  # stored as double 1.0
            mem.dataProvider().addFeatures([feature])
            stored = clp.read_key_set(mem, ["KP1", "source_file"])
            fresh = clp.row_key({"KP1": 1, "source_file": "x.csv"}, ["KP1", "source_file"])
            ok = fresh in stored
            detail = f"stored={stored} fresh={fresh}"
        except Exception as exc:
            return _result(name, False, repr(exc))
    return _result(name, ok, detail)


def test_dataset_views() -> bool:
    """LayDataset.active_mask / subset / source_masks."""
    name = "LayDataset active view + source masks"
    try:
        from ..laydata import LayDataset

        dataset = LayDataset(
            columns={
                "ISO_Time": ["2024-01-01T00:00:00", "2024-01-01T00:00:01",
                             "2024-01-01T00:00:02", "2024-01-01T00:00:03",
                             "2024-01-01T00:00:04"],
                "source_file": ["a", "a", "b", "b", None],
                "record_status": [None, "active", "standby", "excluded", "NULL"],
            },
            fids=[10, 11, 12, 13, 14],
        )
        mask = dataset.active_mask().tolist()
        view = dataset.active_view()
        masks = dataset.source_masks()
        ok = (
            mask == [True, True, False, False, True]
            and view.row_count == 3
            and view.fids.tolist() == [10, 11, 14]
            and view.time_epoch is not None and len(view.time_epoch) == 3
            and sorted(masks) == ["a", "b"]
            and masks["b"].tolist() == [False, False, True, True, False]
            and dataset.active_view() is not dataset
            and view.active_view() is view
        )
    except Exception as exc:
        return _result(name, False, repr(exc))
    return _result(name, ok, f"mask={mask} view_rows={view.row_count}")


def test_gap_math_vectorised() -> bool:
    """Vectorised gap classification agrees with the scalar reference."""
    name = "gap math (vectorised == scalar reference)"
    try:
        import random

        import numpy as np

        rng = random.Random(7)
        primary = sorted(rng.uniform(0, 100000) for _ in range(3000))
        gaps = ops.find_gaps_in_epochs(primary, 60.0)
        secondary = [rng.uniform(-100, 100100) for _ in range(5000)]
        secondary += [float("nan"), gaps[0][0], gaps[0][1]] if gaps else [float("nan")]
        expected = [ops.STATUS_ACTIVE if ops.epoch_in_gaps(t, gaps) else ops.STATUS_STANDBY
                    for t in secondary]
        got = ops.classify_gap_fill(secondary, gaps)
        counts = ops.count_in_gaps(secondary, gaps)
        expected_counts = [sum(1 for t in secondary if t == t and s < t < e) for s, e in gaps]
        ok = got == expected and counts == expected_counts and len(gaps) > 0
        ok = ok and ops.count_in_gaps(np.array([]), gaps) == [0] * len(gaps)
        ok = ok and ops.find_gaps_in_epochs([], 10) == [] and ops.count_in_gaps([1.0], []) == []
    except Exception as exc:
        return _result(name, False, repr(exc))
    return _result(name, ok, f"gaps={len(gaps)} rows={len(secondary)}")


def test_vacuum() -> bool:
    path = _write_temp("sct_mgmt_vac.csv", _CABLE_LAY_FILE)
    gpkg = _fresh_gpkg("sct_mgmt_vac.gpkg")
    try:
        layer = _import([path], gpkg, "2024-01-01")
        if layer is None:
            return _result("vacuum_gpkg", False, "import failed")
        del layer
        before, after = ops.vacuum_gpkg(gpkg)
        reopened = _open(gpkg, clp.prefixed_layer_name(gpkg, "cable_lay"))
    except Exception as exc:
        return _result("vacuum_gpkg", False, repr(exc))
    ok = before > 0 and after > 0 and reopened is not None and reopened.featureCount() == 3
    return _result("vacuum_gpkg", ok, f"{before} -> {after} bytes")


def run_all() -> List[bool]:
    results = [
        test_layer_type_for_name(),
        test_import_log_written(),
        test_recompute_shifts_times(),
        test_recompute_source_filter(),
        test_recompute_dedupes_double_import(),
        test_gap_math_pure(),
        test_fast_append_and_relog(),
        test_status_ops_and_filter(),
        test_manage_panel_gap_fill(),
        test_manage_panel_edits_ignore_map_filter(),
        test_key_value_normalisation(),
        test_dataset_views(),
        test_gap_math_vectorised(),
        test_vacuum(),
    ]
    print("")
    print(f"{sum(results)}/{len(results)} passed")
    return results


if __name__ == "__main__":  # pragma: no cover
    run_all()

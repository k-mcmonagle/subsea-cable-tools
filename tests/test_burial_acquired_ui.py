# -*- coding: utf-8 -*-
"""Acquired burial data in the Burial Planner: the wizard, the Acquired Data tab, the Assessment tab.

On a plan whose route is the sample route (a line layer) with a 1 m target:

* the import wizard takes the three sample plough days through files,
  dataset, columns (suggested roles plus the CRS) and review, and imports
  them in the background: one dataset, three files, two overlaps; the data
  file is created beside the plan file and named in the project only then;
* the Acquired Data tab lists the dataset and its files;
* the Assessment tab builds in the background when shown: the summary, the
  below-target ranges (the hard ground), the listing and the readout naming
  the file and line behind a value;
* a re-delivered day imported into the dataset replaces the original;
* leaving the project lets go of the data file and empties the tabs.

Screenshots of the wizard's columns and review pages and of both tabs are
saved for a look (path printed).
"""

from __future__ import annotations

import os
import tempfile
import time

from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsProject, QgsVectorLayer
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtWidgets import QMessageBox

from ..burial import store as store_module
from ..burial.acquired import location
from ..burial.acquired import samples as S
from ..burial.acquired.controller import AcquiredController
from ..burial.acquired.import_wizard import ImportWizard
from ..burial.plan_model import PlanModel
from ..burial.store import BurialStore
from ..burial.tabs.acquired_tab import AcquiredDataTab
from ..burial.tabs.assessment_tab import AssessmentTab
from ..burial.tabs.inputs_tab import InputsTab

REQUIRES_QGIS = True


def _result(name: str, ok: bool, detail: str = "") -> bool:
    ok = bool(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def _check(name: str, checks: dict) -> bool:
    failed = [k for k, v in checks.items() if not v]
    return _result(name, not failed, ", ".join(failed))


def _wait(condition, timeout: float = 60.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        QCoreApplication.processEvents()
        if condition():
            return True
        time.sleep(0.02)
    return False


def _write(folder: str, name: str, content: bytes) -> str:
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    with open(path, "wb") as handle:
        handle.write(content)
    return path


class _Setup:
    """A plan on the sample route with a 1 m target, in a temporary plan file."""

    def __enter__(self):
        self._boxes = {n: getattr(QMessageBox, n) for n in ("warning", "information", "question")}
        for name in self._boxes:
            setattr(QMessageBox, name, staticmethod(lambda *a, **k: 0))
        QgsProject.instance().clear()
        self.folder = tempfile.mkdtemp(prefix="ba_ui_")
        self.shots = os.environ.get("SHOT_DIR") or os.path.join(self.folder, "shots")
        os.makedirs(self.shots, exist_ok=True)
        self.store = BurialStore(os.path.join(self.folder, "Job_burial_plans.gpkg"))
        self.store.migrate()
        store_module.set_project_gpkg_path(self.store.gpkg_path)
        self.model = PlanModel(self.store)
        self.model.create_plan("Main lay", "plough")
        self.layer = QgsVectorLayer("LineString?crs=EPSG:4326", "Sample RPL", "memory")
        feature = QgsFeature()
        feature.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(x, y) for x, y in S.ROUTE_LONLAT]))
        self.layer.dataProvider().addFeatures([feature])
        QgsProject.instance().addMapLayer(self.layer)
        self.inputs = InputsTab(self.model, lambda: None)
        self.inputs.route_layer_radio.setChecked(True)
        self.inputs.fallback_combo.setLayer(self.layer)
        self.inputs.apply_rpl_button.click()
        self.inputs.full_route_button.click()
        self.inputs.apply_scope_button.click()
        self.model.update_targets(S.TARGET_DOL_M, [])
        self.controller = AcquiredController(self.model)
        self.controller.follow_project()
        return self

    def __exit__(self, *_exc):
        try:
            self.controller.leave_project()
            self.inputs.deleteLater()
            self.model.close_plan()
            QgsProject.instance().removeMapLayer(self.layer.id())
            QCoreApplication.processEvents()
            self.store.close()
        finally:
            for name, value in self._boxes.items():
                setattr(QMessageBox, name, value)
            QgsProject.instance().clear()
        return False


def _import(setup, paths, dataset_id=None, name="Plough log", crs=f"EPSG:{S.UTM_EPSG}", shots=""):
    wizard = ImportWizard(setup.controller, setup.model, dataset_id, paths)
    wizard.resize(1100, 720)
    wizard.show()
    QCoreApplication.processEvents()
    wizard.next()                                    # files -> dataset
    if dataset_id is None:
        wizard.dataset_page.name.setText(name)
    wizard.next()                                    # dataset -> columns
    mapping = wizard.columns_page.mapping
    if crs and mapping.problems():
        mapping._set_profile_value("crs", crs)
    QCoreApplication.processEvents()
    if shots:
        wizard.grab().save(os.path.join(setup.shots, f"wizard_columns{shots}.png"))
    problems = mapping.problems()
    wizard.next()                                    # columns -> review (review runs in the background)
    reviewed = _wait(lambda: wizard.review_page._ready, 60)
    if shots:
        wizard.grab().save(os.path.join(setup.shots, f"wizard_review{shots}.png"))
    summary = wizard.review_page.summary.text()
    wizard.next()                                    # Import (commit)
    imported = _wait(lambda: wizard.result is not None or (not wizard.review_page._importing
                                                            and not setup.controller.busy), 120)
    _wait(lambda: not setup.controller.busy, 30)
    result = wizard.result
    wizard.hide()
    wizard.deleteLater()
    return problems, reviewed, summary, imported, result


def test_wizard_tabs_and_assessment() -> bool:
    run = S.plough_run(rate_hz=0.1)
    with _Setup() as setup:
        folder = os.path.join(setup.folder, "deliveries")
        paths = [_write(folder, n, c) for n, c in S.daily_files(run)]
        entry_before = location.project_data_path()
        problems, reviewed, summary, imported, result = _import(setup, paths, shots="_1")
        store = setup.controller.store
        dataset_id = result.dataset_id if result else None
        files = store.list_files(dataset_id) if store is not None and dataset_id else []
        overlaps = store.list_overlaps(dataset_id) if store is not None and dataset_id else []

        acquired = AcquiredDataTab(setup.controller, setup.model)
        acquired.resize(1100, 520)
        acquired.show()
        QCoreApplication.processEvents()
        acquired.grab().save(os.path.join(setup.shots, "acquired_tab.png"))
        listed = acquired.dataset_list.count()
        rows = acquired.files_table.rowCount()
        verdict = acquired.check_kps()
        checked_logged = any(e["action"] == "check_kps" for e in setup.controller.store.log(dataset_id))

        assessment = AssessmentTab(setup.controller, setup.model, persist=False)
        assessment.resize(1150, 900)
        assessment.show()
        built = _wait(lambda: assessment.result is not None, 90)
        QCoreApplication.processEvents()
        assessment.grab().save(os.path.join(setup.shots, "assessment_tab.png"))
        ranges = assessment.ranges_table.rowCount()
        readout = assessment._readout(14.36)
        hard_ground = assessment.result is not None and any(
            r["kp_from"] <= 14.36 < r["kp_to"] for r in assessment.result.ranges)
        listing_rows = assessment.listing_table.rowCount()
        summary_text = assessment.summary.text()

        name2, content2 = S.redelivered_day(run)
        resent = _write(os.path.join(setup.folder, "resent"), name2, content2)
        _p, reviewed2, summary2, imported2, result2 = _import(setup, [resent], dataset_id=dataset_id, shots="_2")
        statuses = sorted(f["status"] for f in setup.controller.store.list_files(dataset_id))
        rebuilt = _wait(lambda: assessment.result is not None and not assessment._dirty
                        and not setup.controller.busy, 60)
        data_path = setup.controller.store.path

        setup.controller.leave_project()
        QCoreApplication.processEvents()
        emptied = acquired.dataset_list.count() == 0 and setup.controller.store is None
        assessment.hide()
        acquired.hide()
        assessment.deleteLater()
        acquired.deleteLater()
        print(f"  screenshots: {setup.shots}")
        return _check("wizard, Acquired Data and Assessment on the sample route", {
            "mapping complete": problems == [],
            "reviewed": reviewed and "3 file(s)" in summary and "3 rows rejected" in summary,
            "imported": imported and result is not None and len(result.imported) == 3,
            "no entry before": entry_before is None,
            "file beside the plans": data_path == os.path.join(setup.folder, "Job_burial_data.gpkg")
            and location.project_data_path() == data_path,
            "files and overlaps": len(files) == 3 and len(overlaps) == 2,
            "acquired tab": listed == 1 and rows == 3,
            "kp check": verdict is not None and verdict.level == "ok" and verdict.compared > 1000
            and abs(verdict.median_diff_m) < 0.2 and 0.0 < verdict.dcc_median_m < 3.0 and checked_logged,
            "assessment built": built and "below target" in summary_text,
            "hard ground found": hard_ground and ranges >= 3,
            "readout": "PLG_" in readout and "line" in readout,
            "listing": listing_rows > 1000,
            "re-delivery replaces": reviewed2 and imported2 and result2 is not None
            and statuses == ["current", "current", "current", "replaced"] and "replacing a current file" in summary2,
            "rebuilt": rebuilt,
            "left": emptied,
        })


def run_all() -> list:
    return [test_wizard_tabs_and_assessment()]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)

# -*- coding: utf-8 -*-
"""The Reporting tab: burial graph pages from the current assessment.

On the sample route with three imported plough days:

* shown on its own, the tab gets the assessment built and previews page 1;
* page settings change the pages (KP per page), a channel panel can be
  added, the template saves as a version in the data file;
* a custom KP range narrows the report;
* the PDF is written in the background, a real PDF with every page.

A screenshot of the tab is saved for a look (path printed).
"""

from __future__ import annotations

import os

from qgis.PyQt.QtCore import QCoreApplication

from ..burial.acquired import graph_page as G
from ..burial.acquired import report_template as T
from ..burial.acquired import samples as S
from ..burial.tabs.assessment_tab import AssessmentTab
from ..burial.tabs.reporting_tab import ReportingTab
from .test_burial_acquired_ui import _import, _Setup, _wait, _write

REQUIRES_QGIS = True


def _result(name: str, ok: bool, detail: str = "") -> bool:
    ok = bool(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def _check(name: str, checks: dict) -> bool:
    failed = [k for k, v in checks.items() if not v]
    return _result(name, not failed, ", ".join(failed))


def test_reporting_tab() -> bool:
    run = S.plough_run(rate_hz=0.1)
    with _Setup() as setup:
        folder = os.path.join(setup.folder, "deliveries")
        paths = [_write(folder, n, c) for n, c in S.daily_files(run)]
        _import(setup, paths)
        assessment = AssessmentTab(setup.controller, setup.model, persist=False)
        reporting = ReportingTab(setup.controller, setup.model, assessment, persist=False)
        reporting.resize(1250, 820)
        reporting.show()
        built = _wait(lambda: reporting.report is not None, 90)
        first_pages = reporting.report.page_count if reporting.report else 0
        windows_5km = len(reporting.report.windows) if reporting.report else 0
        preview = reporting.page_view.pixmap()
        has_preview = preview is not None and not preview.isNull() and preview.width() > 300
        page_text = reporting.page_label.text()
        reporting.kp_per_page.setValue(2.0)
        _wait(lambda: reporting.report is not None and len(reporting.report.windows) != windows_5km, 10)
        windows_2km = len(reporting.report.windows)
        reporting._add_panel(T.PANEL_CHANNEL)
        reporting.panel_channel.setCurrentIndex(max(0, reporting.panel_channel.findData("pitch")))
        _wait(lambda: reporting.template["panels"][-1].get("channel") == "pitch", 5)
        QCoreApplication.processEvents()
        panel_channel = reporting.template["panels"][-1].get("channel")
        reporting.template["name"] = "Plough A3"
        reporting.template_source = ("saved", "")
        store = setup.controller.store
        template_id = store.save_template("Plough A3", reporting.template)
        reporting._refresh_templates()
        saved = [t["name"] for t in store.list_templates()]
        reporting._custom_range = (2.0, 6.0)
        reporting._rebuild()
        custom_windows = reporting.report.windows if reporting.report else []
        reporting._custom_range = None
        reporting._rebuild()
        QCoreApplication.processEvents()
        reporting.grab().save(os.path.join(setup.shots, "reporting_tab.png"))
        pdf = os.path.join(setup.folder, "graph.pdf")
        started = reporting.export_to(pdf, lambda report, path, progress: G.export_pdf(report, path, progress))
        written = _wait(lambda: not setup.controller.busy and os.path.isfile(pdf), 60)
        with open(pdf, "rb") as handle:
            head = handle.read(5)
        pages_written = reporting.last_export
        reporting.hide()
        reporting.deleteLater()
        assessment.deleteLater()
        print(f"  screenshot: {setup.shots}")
        return _check("Reporting tab: preview, settings, templates, range, PDF", {
            "built": built and first_pages > windows_5km >= 6,
            "preview": has_preview and page_text.startswith("Page 1 of"),
            "kp per page": windows_2km > windows_5km,
            "channel panel": panel_channel == "pitch",
            "template saved": template_id and "Plough A3" in saved
            and reporting.template_combo.findText("Plough A3 (v1)") >= 0,
            "custom range": [(round(a, 2), round(b, 2)) for a, b in custom_windows][:1] == [(2.0, 4.0)]
            and custom_windows[-1][1] >= 6.0,
            "pdf": started and written and head == b"%PDF-" and pages_written == reporting.report.page_count,
        })


def run_all() -> list:
    return [test_reporting_tab()]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)

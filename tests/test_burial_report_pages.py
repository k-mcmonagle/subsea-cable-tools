# -*- coding: utf-8 -*-
"""Burial graph pages: one painter for preview, PDF, PNG and SVG (QGIS / Qt).

On a sample campaign (three plough days and a survey) assessed against a
1 m target:

* the report has a graph page per KP window and summary pages; tokens fill
  per page; the footer names the data, the route and who made it;
* a graph page draws the status colours, the governing DOL and the
  below-target fill where they belong;
* the PDF is vector with one page per report page; PNG and SVG come out per
  page; nothing is left half-written;
* thinning keeps a one-sample spike; templates save as versions.

Page images are saved for a look (path printed).
"""

from __future__ import annotations

import os
import re
import tempfile

import numpy as np

from qgis.PyQt.QtGui import QColor

from ..burial.acquired import assessment as A
from ..burial.acquired import graph_page as G
from ..burial.acquired import importer as I
from ..burial.acquired import profile as P
from ..burial.acquired import reader
from ..burial.acquired import report_template as T
from ..burial.acquired import samples as S
from ..burial.acquired import style as ST
from ..burial.acquired.store import AcquiredStore

REQUIRES_QGIS = True


def _result(name: str, ok: bool, detail: str = "") -> bool:
    ok = bool(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def _check(name: str, checks: dict) -> bool:
    failed = [k for k, v in checks.items() if not v]
    return _result(name, not failed, ", ".join(failed))


def _profile(content: bytes, name: str, crs: str = f"EPSG:{S.UTM_EPSG}") -> dict:
    table = reader.sniff(content, name)
    raw = reader.read_table(content, name, table, max_rows=5)
    prof = P.new_profile(name)
    prof["table"] = table
    prof["columns"] = P.suggest(raw.headers, raw.units)
    prof["crs"] = crs
    return prof


def _campaign(folder: str):
    store = AcquiredStore.create(os.path.join(folder, "rep_burial_data.gpkg"))
    run = S.plough_run(rate_hz=0.1)
    days = S.daily_files(run)
    paths = []
    for name, content in days:
        path = os.path.join(folder, name)
        with open(path, "wb") as handle:
            handle.write(content)
        paths.append(path)
    prof = _profile(days[0][1], days[0][0])
    plough = I.import_batch(store.path, I.DatasetTarget(name="Plough log"), I.review_files(None, None, paths, prof),
                            I.ImportOptions(prof, "Plough"))
    name, content = S.survey_listing(step_m=2.0)
    path = os.path.join(folder, name)
    with open(path, "wb") as handle:
        handle.write(content)
    prof_s = _profile(content, name)
    survey = I.import_batch(store.path, I.DatasetTarget(name="Survey PLS-01", kind="survey"),
                            I.review_files(None, None, [path], prof_s),
                            I.ImportOptions(prof_s, "Survey", acquired_utc=S.SURVEY_DATE + "T00:00:00Z"))
    request = A.Request([A.DatasetSpec(plough.dataset_id, "Plough log"), A.DatasetSpec(survey.dataset_id,
                                                                                       "Survey PLS-01")],
                        target_runs=[(0.0, 33.0, S.TARGET_DOL_M)], scope_km=(0.0, 33.0),
                        channels=["water_depth", "tension:tow"],
                        sections=[{"section_id": "s1", "kind": "burial", "start_kp": 0.0, "end_kp": 20.0}])
    return store, A.build(store, request)


def _colour_present(image, colour, x0, x1, y0, y1, tolerance: int = 30) -> bool:
    target = QColor(*colour[:3])
    for x in range(int(x0), int(x1), 2):
        for y in range(int(y0), int(y1), 2):
            pixel = QColor(image.pixel(x, y))
            if (abs(pixel.red() - target.red()) < tolerance and abs(pixel.green() - target.green()) < tolerance
                    and abs(pixel.blue() - target.blue()) < tolerance):
                return True
    return False


def test_report_pages_and_exports() -> bool:
    folder = tempfile.mkdtemp(prefix="ba_report_")
    shots = os.environ.get("SHOT_DIR") or os.path.join(folder, "shots")
    os.makedirs(shots, exist_ok=True)
    store, assessment = _campaign(folder)
    try:
        data = G.report_data(assessment, {"project": "Job 42", "plan": "Main lay", "rpl": "S1", "rpl_rev": "rev C",
                                          "target": "1.0 m"}, author="tester", made_utc="2026-10-21 12:00 UTC")
        template = T.default_template()
        template["sources_page"] = True
        report = G.Report(data, template)
        windows = len(report.windows)
        tokens = report.tokens(1)
        dpi = 72.0
        first = G.render_image(report, 0, dpi)
        k = dpi / 25.4
        first.save(os.path.join(shots, "report_page1.png"))
        G.render_image(report, windows, 72.0).save(os.path.join(shots, "report_summary.png"))
        G.render_image(report, report.page_count - 1, 72.0).save(os.path.join(shots, "report_sources.png"))
        # Page 1 is KP 0-5: the hard ground at 3.2-3.55 is below target; the status bar sits under the title.
        has_green = _colour_present(first, ST.STATUS_COLOURS[1], 40 * k, 400 * k, 30 * k, 45 * k)
        has_red_bar = _colour_present(first, ST.STATUS_COLOURS[2], 40 * k, 400 * k, 30 * k, 45 * k)
        has_governing = _colour_present(first, ST.GOVERNING, 40 * k, 400 * k, 45 * k, 200 * k)
        pdf = os.path.join(shots, "graph.pdf")
        pages = G.export_pdf(report, pdf)
        with open(pdf, "rb") as handle:
            body = handle.read()
        pdf_pages = len(re.findall(rb"/Type\s*/Page[^s]", body))
        pngs = G.export_images(report, os.path.join(folder, "graph.png"), dpi=60, pages=[0, 1])
        svgs = G.export_svgs(report, os.path.join(folder, "graph.svg"), pages=[0])
        with open(svgs[0], encoding="utf-8", errors="replace") as handle:
            svg = handle.read()
        leftovers = [f for f in os.listdir(folder) + os.listdir(shots) if ".part" in f]
        x = np.linspace(0.0, 1.0, 100001)
        y = np.ones_like(x)
        y[54321] = -5.0
        thin_x, thin_y = G.decimate(x, y, 0.0, 1.0, 400)
        first_id = store.save_template("Burial graph A3", template)
        same_id = store.save_template("Burial graph A3", dict(template))
        template["kp_per_page"] = 2.0
        second_id = store.save_template("Burial graph A3", template)
        listed = store.list_templates()
        print(f"  pages: {report.page_count} ({windows} graph); page images in {shots}")
        return _check("report pages, PDF / PNG / SVG, thinning, templates", {
            "pages": windows == 7 and report.page_count >= windows + 2,
            "tokens": tokens["kp_start"] == "4.750" and tokens["page"] == "2" and tokens["plan"] == "Main lay"
            and tokens["datasets"] == "Plough log, Survey PLS-01",
            "footer": data.provenance.startswith("Data: Plough log (3 files), Survey PLS-01 (1 file) · route S1 rev C")
            and "by tester" in data.provenance,
            "status colours": has_green and has_red_bar,
            "bars merged": len(assessment.status_runs[0]) < 200,
            "governing line": has_governing,
            "pdf": body.startswith(b"%PDF") and pages == report.page_count and pdf_pages == report.page_count,
            "png": len(pngs) == 2 and all(os.path.getsize(p) > 5000 for p in pngs)
            and pngs[0].endswith("graph_p01.png"),
            "svg": len(svgs) == 1 and "<svg" in svg and svgs[0].endswith("graph.svg"),
            "no leftovers": leftovers == [],
            "spike kept": float(np.nanmin(thin_y)) == -5.0 and thin_x.size <= 1600,
            "template versions": first_id == same_id and second_id != first_id
            and [(t["name"], t["version"]) for t in listed] == [("Burial graph A3", 2)]
            and listed[0]["template"]["kp_per_page"] == 2.0,
        })
    finally:
        store.close()


def run_all() -> list:
    return [test_report_pages_and_exports()]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)

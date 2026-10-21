# -*- coding: utf-8 -*-
"""Help for acquired burial data (the Acquired Data and Assessment tabs)."""

from __future__ import annotations

from qgis.PyQt.QtWidgets import QDialog, QDialogButtonBox, QTextBrowser, QVBoxLayout

from ...qgis_compat import BUTTON_BOX_CLOSE, qt_exec

HELP_HTML = """
<h3>Acquired burial data (beta: check outputs)</h3>
<p>Bring in burial data as it is delivered (trencher or plough logs, burial surveys), keep every delivery
exactly as received, and compare the depth of lowering with the plan's target depth along the plan's KPs.</p>

<h4>Datasets and files</h4>
<p>A <b>dataset</b> is one stream of data: one plough's log, one survey. Each delivered <b>file</b> is kept
byte for byte in the project's burial data file (beside the plan file), with its checksum, who imported it and
when. The same file imported again is skipped. A file delivered again under the same name replaces the earlier
one, which is kept and marked replaced (you can choose to keep both). <i>File ▾</i> saves any original as
delivered, lists its rejected rows, or withdraws or restores it with a reason. <i>History</i> lists everything
done.</p>

<h4>Importing (Add data)</h4>
<ol>
<li><b>Files</b>: choose or drop them; they import in the listed order (a later file is the newer delivery).</li>
<li><b>Dataset</b>: add to one, or start one and say which RPL its KPs were quoted against.</li>
<li><b>Columns</b>: each column gets a role from the chip above it (KP, DOL, Timestamp, Latitude, Easting, Water
depth, Tension…) and its units from the chip below that. Suggested roles are tinted until you move on. Values
that will not read are red, with the reason. Unmapped columns of numbers are kept as extra channels. The mapping
is saved and applies itself to the next delivery in the same format. <i>Reading ▸</i> sets the delimiter,
header and units lines, decimal comma, missing values and encoding.</li>
<li><b>Review</b>: what will happen to each file, before anything is written. Rows that cannot be read are
listed with their line and reason (the original keeps them). Overlaps in time with other files of the dataset
are found here.</li>
</ol>
<p>Times are stored in UTC: give the UTC offset of the delivered times if they are not UTC. A file without a time
column needs the date it was acquired.</p>

<h4>Overlaps</h4>
<p>Where two deliveries of a dataset hold records for the same times (a daily file running past midnight, a
re-sent half day), the <b>newer delivery wins</b> by default. You can keep the older file's records, keep both,
or split at a time. Each decision is recorded; changing one needs a reason and keeps the old one.</p>

<h4>Assessment</h4>
<p>DOL (depth of lowering: metres below mean seabed level, positive down) is assessed on 1 m bins along the
plan's KPs. Each bin takes the <b>shallowest</b> value of the records around it (the worst case). Records
support half the way to their neighbours, so a survey every 5 m fills its 1 m bins, while a gap wider than the
bridging limit (3x the typical spacing, at least 1 m) is <b>no data</b>, which is never counted as below
target. Where datasets overlap, the <b>latest data wins</b> (by acquisition time; ties go to the dataset listed
first in Acquired Data), or choose <i>Worst case</i>. The bar under the plot shows which dataset is used where.</p>
<p>Lengths and percentages are of the assessed length. Hover the plot for the value, the target, and the file,
line and time behind it; the map marks the KP. Click a below-target range to zoom to it. The listing gives one
row per 1, 5 or 10 m: the shallowest (or mean) DOL there and the record behind it.</p>

<h4>KPs</h4>
<p>KPs are used as delivered when they refer to the plan's RPL. KPs quoted on another RPL (an earlier revision,
the as-laid RPL) are moved onto the plan's route by seabed position, and the assessment says so.
<i>Check KPs</i> compares the delivered KPs with the records' positions on their RPL and reports the agreement,
a constant shift (another start KP) or a difference growing along the route (grid chainage).</p>
"""


def show_help(parent=None) -> None:
    dialog = QDialog(parent)
    dialog.setWindowTitle("Acquired burial data: help")
    dialog.resize(760, 640)
    layout = QVBoxLayout(dialog)
    browser = QTextBrowser()
    browser.setOpenExternalLinks(True)
    browser.setHtml(HELP_HTML)
    layout.addWidget(browser, 1)
    buttons = QDialogButtonBox(BUTTON_BOX_CLOSE)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    qt_exec(dialog)

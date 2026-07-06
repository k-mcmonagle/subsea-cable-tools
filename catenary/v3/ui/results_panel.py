# -*- coding: utf-8 -*-
"""HTML rendering of Cable Lay Simulator results (no Qt imports)."""

from __future__ import annotations

import html
from typing import Dict, List


def _table(rows: Dict[str, str]) -> str:
    body = "".join(
        f"<tr><td style='padding:1px 10px 1px 0; color:#555;'>{html.escape(k)}</td>"
        f"<td style='padding:1px 0;'><b>{html.escape(v)}</b></td></tr>"
        for k, v in rows.items()
    )
    return f"<table cellspacing='0' cellpadding='0'>{body}</table>"


def render_results_html(out) -> str:
    """RunOutput -> results-pane HTML."""
    parts: List[str] = []
    if out is None:
        return "<i>No solution yet.</i>"
    if out.error:
        if out.error == "cancelled":
            return "<i>Solve cancelled.</i>"
        return (
            "<div style='color:#b00020;'><b>Solve failed.</b><br>"
            f"<pre style='white-space:pre-wrap;'>{html.escape(out.error)}</pre></div>"
        )

    mode_titles = {
        "static": "Static hang",
        "steady": "Steady lay (stationary configuration, ship frame)",
        "operation": "Operation simulation",
    }
    parts.append(f"<b>{mode_titles.get(out.mode, out.mode)}</b>")

    severe = [w for w in out.warnings if "VIOLATED" in w or "failed" in w.lower()]
    normal = [w for w in out.warnings if w not in severe]
    if severe:
        parts.append(
            "<div style='background:#fdecea; color:#b00020; padding:4px 6px;'>"
            + "<br>".join(html.escape(w) for w in severe) + "</div>"
        )

    if out.facts:
        parts.append(_table(out.facts))
    if out.quick:
        parts.append(
            "<div style='margin-top:6px; color:#555;'><b>Zajac closed-form checks</b> "
            "(same physics, independent of the numerical solve):</div>"
        )
        parts.append(_table(out.quick))
    if normal:
        parts.append(
            "<div style='margin-top:6px; color:#8a6d3b;'>"
            + "<br>".join("&#9888; " + html.escape(w) for w in normal) + "</div>"
        )
    parts.append(
        "<div style='margin-top:8px; color:#888; font-size:small;'>"
        "Quasi-static engineering model — see V3 model notes for assumptions "
        "and validation status. Verify against your own methods before "
        "operational use.</div>"
    )
    return "<br>".join(parts)

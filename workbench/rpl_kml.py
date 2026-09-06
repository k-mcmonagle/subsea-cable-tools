# -*- coding: utf-8 -*-
"""KML export for a Cable Route Workbench RPL.

Produces a Google-Earth / GIS-ready KML document for one RPL revision that
carries *every* RPL attribute (the engine-owned fields plus any extra
attributes that rode in from the source workbook) and exposes both feature
types the RPL is made of:

- one ``LineString`` placemark for the whole route (route-level summary),
- one ``LineString`` placemark per point-to-point leg (bearing, distance,
  slack, cable distance and all leg attributes),
- one ``Point`` placemark per position (KP, cable distance, depth and all
  position attributes).

Pure python — no Qt/QGIS imports — so it stays unit-testable, mirroring
``rpl_sheet.py``. The in-memory ``rpl_engine.RplModel`` is the single source
of truth; geometry is taken from the point coordinates (EPSG:4326 lon/lat).
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence
from xml.sax.saxutils import escape

def _text(value) -> str:
    return "" if value is None else str(value)


def _number(value, decimals: int) -> str:
    if value is None:
        return ""
    try:
        return f"{float(value):.{decimals}f}"
    except (TypeError, ValueError):
        return _text(value)


def _data(name: str, value) -> str:
    """One ``<Data>`` element. Blank values are omitted to keep KML tidy."""
    text = _text(value)
    if text == "":
        return ""
    return (
        f'      <Data name="{escape(name)}">\n'
        f"        <value>{escape(text)}</value>\n"
        "      </Data>\n"
    )


def _extended_data(entries: Sequence) -> str:
    """Wrap a list of ``(label, value)`` pairs in ``<ExtendedData>``."""
    body = "".join(_data(label, value) for label, value in entries)
    if not body:
        return ""
    return "    <ExtendedData>\n" + body + "    </ExtendedData>\n"


def _point_geometry(lon: float, lat: float) -> str:
    return (
        "    <Point>\n"
        "      <coordinates>"
        f"{lon:.7f},{lat:.7f},0"
        "</coordinates>\n"
        "    </Point>\n"
    )


def _linestring_geometry(coords: Sequence) -> str:
    # KML coordinate list: "lon,lat,alt lon,lat,alt ..."
    joined = " ".join(f"{lon:.7f},{lat:.7f},0" for lon, lat in coords)
    return (
        "    <LineString>\n"
        "      <tessellate>1</tessellate>\n"
        f"      <coordinates>{joined}</coordinates>\n"
        "    </LineString>\n"
    )


def _placemark(name: str, description: str, geometry_xml: str,
               entries: Sequence) -> str:
    desc = escape(description) if description else ""
    return (
        "  <Placemark>\n"
        f"    <name>{escape(name)}</name>\n"
        + (f"    <description>{desc}</description>\n" if desc else "")
        + _extended_data(entries)
        + geometry_xml
        + "  </Placemark>\n"
    )


def _point_entries(point, extra_keys: Sequence[str]) -> List:
    """Ordered (label, value) pairs for a position: engine fields then extras."""
    entries = [
        ("Pos no.", _text(point.pos_no)),
        ("Event", point.event or ""),
        ("KP (km)", _number(point.dist_cum_km, 3)),
        ("Cable cum. (km)", _number(point.cable_dist_cum_km, 3)),
        ("Depth (m)", _number(point.depth_m, 1)),
    ]
    for key in extra_keys:
        entries.append((key, point.attrs.get(key)))
    return entries


def _leg_entries(seg, p0, p1, extra_keys: Sequence[str]) -> List:
    """Ordered (label, value) pairs for a leg: engine fields then extras."""
    entries = [
        ("From pos", _text(p0.pos_no)),
        ("To pos", _text(p1.pos_no)),
        ("From event", p0.event or ""),
        ("To event", p1.event or ""),
        ("Bearing (deg)", _number(seg.bearing_deg, 1)),
        ("Dist (km)", _number(seg.dist_km, 4)),
        ("Slack (%)", _number(seg.slack_pct, 3)),
        ("Cable (km)", _number(seg.cable_dist_km, 4)),
    ]
    for key in extra_keys:
        entries.append((key, seg.attrs.get(key)))
    return entries


def _route_entries(model, rpl_meta: Optional[Dict]) -> List:
    """Route-level summary attributes for the whole-route line."""
    meta = rpl_meta or {}
    entries = [
        ("RPL", meta.get("name") or ""),
        ("Kind", meta.get("kind") or ""),
        ("Revision", meta.get("rev_label") or ""),
        ("Status", meta.get("status") or ""),
        ("Positions", str(len(model.points))),
        ("Legs", str(len(model.segments))),
        ("Start KP (km)", _number(model.start_kp_km(), 3)),
        ("End KP (km)", _number(model.end_kp_km(), 3)),
        ("Route length (km)", _number(model.total_route_km(), 3)),
        ("Cable length (km)", _number(model.total_cable_km(), 3)),
        ("Source file", meta.get("source_file") or ""),
        ("Notes", meta.get("notes") or ""),
    ]
    return entries


def build_kml(model, rpl_name: str = "RPL",
              rpl_meta: Optional[Dict] = None) -> str:
    """Return a complete KML document string for one RPL model.

    ``rpl_meta`` is the ``wb_rpl`` registry row (name, kind, rev_label,
    status, source_file, notes, ...) used to label the route placemark.
    """
    rpl_meta = rpl_meta or {}
    name = (rpl_name or "RPL").strip() or "RPL"

    # Extra (non-engine) attribute keys, in a stable order, so every workbook
    # column is carried through to the KML.
    point_extra = _ordered_extra_keys((p.attrs for p in model.points))
    leg_extra = _ordered_extra_keys((s.attrs for s in model.segments))

    parts: List[str] = []
    parts.append('<?xml version="1.0" encoding="UTF-8"?>')
    parts.append('<kml xmlns="http://www.opengis.net/kml/2.2">')
    parts.append(f'  <Document>')
    parts.append(f'    <name>{escape(name)}</name>')
    parts.append(f'    <description>{escape("Cable Route Workbench RPL export")}</description>')
    parts.append(f'    <Style id="routeStyle">')
    parts.append(f'      <LineStyle><color>ff0066ff</color><width>3</width></LineStyle>')
    parts.append(f'    </Style>')
    parts.append(f'    <Style id="legStyle">')
    parts.append(f'      <LineStyle><color>ff00aaff</color><width>2</width></LineStyle>')
    parts.append(f'    </Style>')
    parts.append(f'    <Style id="pointStyle">')
    parts.append(f'      <IconStyle><scale>0.8</scale></IconStyle>')
    parts.append(f'    </Style>')

    # Whole-route line ------------------------------------------------------
    route_coords = [(p.lon, p.lat) for p in model.points]
    if route_coords:
        parts.append(_placemark(
            f"{name} — route",
            f"Full cable route: {len(model.points)} positions, "
            f"{_number(model.total_route_km(), 3)} km route, "
            f"{_number(model.total_cable_km(), 3)} km cable.",
            _linestring_geometry(route_coords),
            _route_entries(model, rpl_meta),
        ))

    # Per-leg lines ---------------------------------------------------------
    for i, seg in enumerate(model.segments):
        p0, p1 = model.points[i], model.points[i + 1]
        leg_name = f"Leg {i + 1}: {_text(p0.pos_no)} → {_text(p1.pos_no)}"
        if p0.event or p1.event:
            leg_name += f" ({p0.event or '…'} → {p1.event or '…'})"
        parts.append(_placemark(
            leg_name,
            f"Point-to-point leg {i + 1} of {len(model.segments)}.",
            _linestring_geometry([(p0.lon, p0.lat), (p1.lon, p1.lat)]),
            _leg_entries(seg, p0, p1, leg_extra),
        ))

    # Position points -------------------------------------------------------
    for point in model.points:
        label = f"Pos {_text(point.pos_no)}"
        if point.event:
            label += f" — {point.event}"
        parts.append(_placemark(
            label,
            f"Position {_text(point.pos_no)} at KP "
            f"{_number(point.dist_cum_km, 3)} km.",
            _point_geometry(point.lon, point.lat),
            _point_entries(point, point_extra),
        ))

    parts.append("  </Document>")
    parts.append("</kml>")
    return "\n".join(parts) + "\n"


def _ordered_extra_keys(attr_rows) -> List[str]:
    """Union of extra attribute keys across rows, first-seen order."""
    found: List[str] = []
    for attrs in attr_rows:
        for key in attrs:
            if key not in found:
                found.append(key)
    return found


def write_kml(path: str, model, rpl_name: str = "RPL",
              rpl_meta: Optional[Dict] = None) -> None:
    """Write the KML document for ``model`` to ``path`` (UTF-8)."""
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(build_kml(model, rpl_name, rpl_meta))

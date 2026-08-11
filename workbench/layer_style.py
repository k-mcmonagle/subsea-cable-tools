# -*- coding: utf-8 -*-
"""Standard symbology for Cable Route Workbench RPL layers.

Line layers are categorised by ``CableType`` with a stable colour per type:
well-known armour codes (LW, SA, DA, ...) get canonical colours and anything
else gets a colour picked deterministically from a fixed palette, so the same
cable type always looks the same across layers, projects, and sessions.
Point layers use a rule-based renderer that highlights positions with an
Event (joints, repeaters, landings) over plain alter points.

Styles are additionally saved into the GeoPackage's ``layer_styles`` table as
the layer default, so the layers come back styled even when added to a
project without this plugin.

Pure helpers (colour lookup, normalisation) have no qgis dependency so they
can be unit-tested headless; the ``apply_*`` functions import qgis lazily.
"""

from __future__ import annotations

import re
import zlib
from typing import List, Optional

CABLE_TYPE_FIELD = "CableType"
EVENT_FIELD = "Event"

STYLE_NAME = "subsea_cable_tools"

# Canonical colours for common cable protection/armour codes (Okabe-Ito based,
# ordered roughly light protection = cool, heavy protection = warm).
KNOWN_CABLE_TYPE_COLOURS = {
    "LW": "#56b4e9",    # lightweight
    "LWP": "#0072b2",   # lightweight protected
    "LWS": "#0072b2",   # lightweight screened
    "SA": "#009e73",    # single armour
    "SAL": "#8fce5a",   # single armour light
    "SAM": "#e69f00",   # single armour medium
    "SAH": "#d55e00",   # single armour heavy
    "DA": "#cc3311",    # double armour
    "RA": "#882255",    # rock armour
}

# Deterministic fallback palette for cable types not in the canonical map.
FALLBACK_PALETTE = [
    "#4477aa", "#66ccee", "#228833", "#ccbb44", "#ee6677", "#aa3377",
    "#bbbbbb", "#e69f00", "#009988", "#997700", "#6699cc", "#994455",
]

UNSET_COLOUR = "#7f8c99"     # segments with no cable type
LINE_WIDTH = "0.9"

POINT_PLAIN_COLOUR = "#33404d"
POINT_EVENT_COLOUR = "#d55e00"


def normalise_cable_type(value) -> str:
    """Uppercase alphanumeric token used for canonical colour lookup."""
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def colour_for_cable_type(value) -> str:
    """Stable hex colour for a cable type string (canonical or hashed)."""
    token = normalise_cable_type(value)
    if not token:
        return UNSET_COLOUR
    if token in KNOWN_CABLE_TYPE_COLOURS:
        return KNOWN_CABLE_TYPE_COLOURS[token]
    # crc32 is stable across Python runs (unlike hash()).
    index = zlib.crc32(token.encode("utf-8")) % len(FALLBACK_PALETTE)
    return FALLBACK_PALETTE[index]


def _unique_strings(layer, field_name: str) -> List[str]:
    idx = layer.fields().indexOf(field_name)
    if idx < 0:
        return []
    values = set()
    for value in layer.uniqueValues(idx):
        if value is None:
            continue
        if type(value).__name__ == "QVariant":  # Qt5 NULL
            if not value.isValid() or value.isNull():
                continue
            value = value.value()
        text = str(value).strip()
        if text and text.upper() != "NULL":
            values.add(text)
    return sorted(values)


def _line_symbol(colour: str):
    from qgis.core import QgsLineSymbol

    return QgsLineSymbol.createSimple({"color": colour, "width": LINE_WIDTH})


def apply_line_style(layer) -> None:
    """Categorise an RPL line layer by CableType with stable colours."""
    try:
        from qgis.core import QgsCategorizedSymbolRenderer, QgsRendererCategory
    except ImportError:  # pragma: no cover - headless
        return
    if layer is None or not layer.isValid():
        return
    categories = []
    for value in _unique_strings(layer, CABLE_TYPE_FIELD):
        categories.append(
            QgsRendererCategory(value, _line_symbol(colour_for_cable_type(value)), value))
    # Catch-all so newly typed values and unset segments still draw.
    try:
        categories.append(QgsRendererCategory(None, _line_symbol(UNSET_COLOUR), "(other)"))
    except TypeError:  # pragma: no cover - binding rejects None
        categories.append(QgsRendererCategory("", _line_symbol(UNSET_COLOUR), "(other)"))
    layer.setRenderer(QgsCategorizedSymbolRenderer(CABLE_TYPE_FIELD, categories))
    layer.triggerRepaint()


def refresh_line_categories(layer) -> None:
    """Add categories for CableType values that appeared since styling.

    Keeps any colour tweaks the user made to existing categories; only truly
    new values get a category. Falls back to a full restyle when the layer is
    not categorised on CableType.
    """
    try:
        from qgis.core import QgsCategorizedSymbolRenderer, QgsRendererCategory
    except ImportError:  # pragma: no cover - headless
        return
    if layer is None or not layer.isValid():
        return
    renderer = layer.renderer()
    if not isinstance(renderer, QgsCategorizedSymbolRenderer) \
            or renderer.classAttribute() != CABLE_TYPE_FIELD:
        apply_line_style(layer)
        return
    existing = {str(c.value()) for c in renderer.categories()}
    added = False
    for value in _unique_strings(layer, CABLE_TYPE_FIELD):
        if value not in existing:
            renderer.addCategory(
                QgsRendererCategory(value, _line_symbol(colour_for_cable_type(value)), value))
            added = True
    if added:
        layer.triggerRepaint()


def apply_point_style(layer) -> None:
    """Small dots for alter points, highlighted markers for event positions."""
    try:
        from qgis.core import QgsMarkerSymbol, QgsRuleBasedRenderer
    except ImportError:  # pragma: no cover - headless
        return
    if layer is None or not layer.isValid():
        return

    root = QgsRuleBasedRenderer.Rule(None)

    event_symbol = QgsMarkerSymbol.createSimple({
        "name": "circle",
        "color": POINT_EVENT_COLOUR,
        "outline_color": "#ffffff",
        "outline_width": "0.3",
        "size": "2.6",
    })
    event_rule = QgsRuleBasedRenderer.Rule(event_symbol)
    event_rule.setLabel("Event")
    event_rule.setFilterExpression(f'"{EVENT_FIELD}" IS NOT NULL AND trim("{EVENT_FIELD}") <> \'\'')
    root.appendChild(event_rule)

    plain_symbol = QgsMarkerSymbol.createSimple({
        "name": "circle",
        "color": POINT_PLAIN_COLOUR,
        "outline_color": "#ffffff",
        "outline_width": "0.2",
        "size": "1.6",
    })
    plain_rule = QgsRuleBasedRenderer.Rule(plain_symbol)
    plain_rule.setLabel("Position")
    try:
        plain_rule.setIsElse(True)
    except AttributeError:  # pragma: no cover - very old API
        plain_rule.setFilterExpression("ELSE")
    root.appendChild(plain_rule)

    layer.setRenderer(QgsRuleBasedRenderer(root))
    layer.triggerRepaint()


def save_default_style(layer) -> None:
    """Persist the current renderer into the GeoPackage as the default style.

    QGIS then applies it automatically whenever the layer is loaded, plugin or
    not. Best-effort: failures (read-only file, old provider) are ignored.
    """
    if layer is None or not layer.isValid():
        return
    try:
        layer.saveStyleToDatabase(
            STYLE_NAME, "Subsea Cable Tools standard style", True, "")
    except Exception:
        pass


def style_rpl_layer(layer, layer_name: Optional[str] = None, persist: bool = True) -> None:
    """Apply the standard style to one RPL layer based on its name suffix."""
    name = layer_name or (layer.name() if layer is not None else "")
    if not name:
        return
    if name.endswith("_points"):
        apply_point_style(layer)
    elif name.endswith("_lines"):
        apply_line_style(layer)
    else:
        return
    if persist:
        save_default_style(layer)

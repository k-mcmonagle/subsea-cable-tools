# -*- coding: utf-8 -*-
"""Standard symbology for Cable Route Workbench RPL layers.

Line layers are categorised by ``CableType`` with a stable colour per type:
the user's own palette wins, then well-known armour codes (LW, SA, DA, ...)
get canonical colours, and anything else gets a colour picked
deterministically from a fixed palette, so the same cable type always looks
the same across layers, projects, and sessions. Point layers use a
rule-based renderer that highlights positions with an Event (joints,
repeaters, landings) over plain alter points.

The user's palette lives in QGIS settings (not in the project or the
GeoPackage), so a company's cable-type colours follow the user across
projects; :func:`set_user_cable_type_colours` writes it and the *Cable type
colours...* dialog edits it.

Styles are additionally saved into the GeoPackage's ``layer_styles`` table as
the layer default, so the layers come back styled even when added to a
project without this plugin. Because of that, styling is **not** reapplied to
a layer that already carries an RPL-shaped renderer: a style the user saved
(or tweaked and saved as the layer default) survives being reloaded, and only
newly appearing cable types get a category added. Pass ``force=True`` to
deliberately restyle, which is what applying a palette change does.

Pure helpers (colour lookup, normalisation) have no qgis dependency so they
can be unit-tested headless; the ``apply_*`` functions import qgis lazily.
"""

from __future__ import annotations

import json
import re
import zlib
from typing import Dict, List, Optional

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

# QGIS settings key holding the user's cable-type palette (JSON object of
# normalised type token -> "#rrggbb"). Global on purpose: cable-type colours
# are a house convention, not a property of one project.
COLOUR_SETTING_KEY = "SubseaCableTools/Workbench/cable_type_colours"

_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# Fallback store for environments without QSettings (headless unit tests).
_MEMORY_PALETTE: Dict[str, str] = {}

POINT_PLAIN_COLOUR = "#33404d"
POINT_EVENT_COLOUR = "#d55e00"


def normalise_cable_type(value) -> str:
    """Uppercase alphanumeric token used for canonical colour lookup."""
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def normalise_colour(value) -> Optional[str]:
    """Return ``"#rrggbb"`` lowercase for a valid hex colour, else ``None``."""
    text = str(value or "").strip()
    if not text.startswith("#"):
        text = "#" + text
    return text.lower() if _HEX_RE.match(text) else None


def _settings():
    """QSettings, or None when the Qt bindings are unavailable."""
    try:
        from qgis.PyQt.QtCore import QSettings
    except ImportError:  # pragma: no cover - pure-Python test harness
        return None
    return QSettings()


def user_cable_type_colours() -> Dict[str, str]:
    """The user's cable-type palette: normalised token -> ``"#rrggbb"``."""
    settings = _settings()
    if settings is None:
        return dict(_MEMORY_PALETTE)
    raw = settings.value(COLOUR_SETTING_KEY, "")
    try:
        stored = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    if not isinstance(stored, dict):
        return {}
    palette = {}
    for key, value in stored.items():
        token = normalise_cable_type(key)
        colour = normalise_colour(value)
        if token and colour:
            palette[token] = colour
    return palette


def set_user_cable_type_colours(palette: Optional[Dict[str, str]]) -> Dict[str, str]:
    """Replace the user's palette; returns what was actually stored.

    Entries with an unusable type token or colour are dropped rather than
    stored as something that would later read back as a standard colour.
    """
    cleaned = {}
    for key, value in (palette or {}).items():
        token = normalise_cable_type(key)
        colour = normalise_colour(value)
        if token and colour:
            cleaned[token] = colour
    settings = _settings()
    if settings is None:
        _MEMORY_PALETTE.clear()
        _MEMORY_PALETTE.update(cleaned)
        return dict(cleaned)
    if cleaned:
        settings.setValue(COLOUR_SETTING_KEY, json.dumps(cleaned, sort_keys=True))
    else:
        settings.remove(COLOUR_SETTING_KEY)
    return dict(cleaned)


def standard_colour_for_cable_type(value) -> str:
    """The built-in colour for a cable type, ignoring the user's palette."""
    token = normalise_cable_type(value)
    if not token:
        return UNSET_COLOUR
    if token in KNOWN_CABLE_TYPE_COLOURS:
        return KNOWN_CABLE_TYPE_COLOURS[token]
    # crc32 is stable across Python runs (unlike hash()).
    index = zlib.crc32(token.encode("utf-8")) % len(FALLBACK_PALETTE)
    return FALLBACK_PALETTE[index]


def colour_for_cable_type(value, overrides: Optional[Dict[str, str]] = None) -> str:
    """Stable hex colour for a cable type: user palette, canonical, or hashed.

    ``overrides`` (normalised token -> hex) is read instead of the stored
    palette when given — pass ``{}`` for the built-in colours only, or a
    pending edit from the colour dialog.
    """
    token = normalise_cable_type(value)
    if not token:
        return UNSET_COLOUR
    palette = user_cable_type_colours() if overrides is None else overrides
    override = palette.get(token) if palette else None
    if override:
        return override
    return standard_colour_for_cable_type(token)


def unique_field_values(layer, field_name: str) -> List[str]:
    """Sorted distinct non-blank string values of one field."""
    return _unique_strings(layer, field_name)


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


def apply_line_style(layer, overrides: Optional[Dict[str, str]] = None) -> None:
    """Categorise an RPL line layer by CableType with stable colours."""
    try:
        from qgis.core import QgsCategorizedSymbolRenderer, QgsRendererCategory
    except ImportError:  # pragma: no cover - headless
        return
    if layer is None or not layer.isValid():
        return
    if overrides is None:
        overrides = user_cable_type_colours()
    categories = []
    for value in _unique_strings(layer, CABLE_TYPE_FIELD):
        categories.append(QgsRendererCategory(
            value, _line_symbol(colour_for_cable_type(value, overrides)), value))
    # Catch-all so newly typed values and unset segments still draw.
    try:
        categories.append(QgsRendererCategory(None, _line_symbol(UNSET_COLOUR), "(other)"))
    except TypeError:  # pragma: no cover - binding rejects None
        categories.append(QgsRendererCategory("", _line_symbol(UNSET_COLOUR), "(other)"))
    layer.setRenderer(QgsCategorizedSymbolRenderer(CABLE_TYPE_FIELD, categories))
    layer.triggerRepaint()


def is_cable_type_categorised(layer) -> bool:
    """True when ``layer`` already carries an RPL line renderer.

    Either the plugin applied one earlier in this session, or QGIS loaded
    one the user saved as the layer's default style - both mean "leave it
    alone".
    """
    try:
        from qgis.core import QgsCategorizedSymbolRenderer
    except ImportError:  # pragma: no cover - headless
        return False
    if layer is None or not layer.isValid():
        return False
    renderer = layer.renderer()
    return isinstance(renderer, QgsCategorizedSymbolRenderer) \
        and renderer.classAttribute() == CABLE_TYPE_FIELD


def is_rule_based(layer) -> bool:
    """True when ``layer`` already carries an RPL point renderer."""
    try:
        from qgis.core import QgsRuleBasedRenderer
    except ImportError:  # pragma: no cover - headless
        return False
    if layer is None or not layer.isValid():
        return False
    return isinstance(layer.renderer(), QgsRuleBasedRenderer)


def refresh_line_categories(layer, overrides: Optional[Dict[str, str]] = None) -> None:
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
        apply_line_style(layer, overrides)
        return
    if overrides is None:
        overrides = user_cable_type_colours()
    existing = {str(c.value()) for c in renderer.categories()}
    added = False
    for value in _unique_strings(layer, CABLE_TYPE_FIELD):
        if value not in existing:
            renderer.addCategory(QgsRendererCategory(
                value, _line_symbol(colour_for_cable_type(value, overrides)), value))
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


def style_rpl_layer(layer, layer_name: Optional[str] = None, persist: bool = True,
                    force: bool = False,
                    overrides: Optional[Dict[str, str]] = None) -> None:
    """Apply the standard style to one RPL layer based on its name suffix.

    A layer that already carries an RPL-shaped renderer keeps it: that is
    either a style the user saved as the GeoPackage default or one applied
    earlier this session, and silently overwriting it is how custom
    symbology used to get lost. Line layers still gain categories for cable
    types that appeared since. ``force=True`` restyles regardless, which is
    what applying a palette change does.
    """
    name = layer_name or (layer.name() if layer is not None else "")
    # Project layer names are display names, so fall back to the gpkg table.
    if not name.endswith(("_points", "_lines")) and layer is not None:
        name = source_layer_name(layer) or name
    if not name:
        return
    if name.endswith("_points"):
        if not force and is_rule_based(layer):
            return
        apply_point_style(layer)
    elif name.endswith("_lines"):
        if not force and is_cable_type_categorised(layer):
            refresh_line_categories(layer, overrides)
            return
        apply_line_style(layer, overrides)
    else:
        return
    if persist:
        save_default_style(layer)


def source_layer_name(layer) -> str:
    """The GeoPackage table behind ``layer``, from its provider URI."""
    try:
        parts = str(layer.source() or "").split("|")
    except Exception:  # pragma: no cover - exotic providers
        return ""
    for part in parts[1:]:
        key, sep, value = part.partition("=")
        if sep and key.lower() == "layername":
            return value
    return ""


def restyle_workbench_layers(project=None, gpkg_path: str = "",
                             overrides: Optional[Dict[str, str]] = None) -> int:
    """Reapply the standard style to every RPL layer in the project.

    Used after a palette change: unlike normal styling this is deliberate,
    so it forces the renderer and re-saves the GeoPackage default style.
    Returns the number of layers restyled.
    """
    try:
        from qgis.core import QgsProject, QgsVectorLayer
    except ImportError:  # pragma: no cover - headless
        return 0
    import os

    project = project or QgsProject.instance()
    if overrides is None:
        overrides = user_cable_type_colours()
    count = 0
    for layer in list(project.mapLayers().values()):
        if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
            continue
        name = source_layer_name(layer)
        if not name.endswith(("_points", "_lines")):
            continue
        if gpkg_path:
            source = str(layer.source() or "").split("|")[0]
            if os.path.normcase(os.path.abspath(source)) \
                    != os.path.normcase(os.path.abspath(gpkg_path)):
                continue
        style_rpl_layer(layer, name, persist=True, force=True, overrides=overrides)
        count += 1
    return count

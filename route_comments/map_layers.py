# -*- coding: utf-8 -*-
"""Route Comments on the map: display layers, labels, highlight and a pick tool.

The map layers read the comment register directly (the ``rc_comment``
GeoPackage table, filtered to the current set): one for route points and
map locations, one for KP ranges, in a *Route comments* group. There is no
copy of the data, so nothing to refill or clean up, and the layers are
saved with the project like any other layer. After each change the dock
reloads them.

Styling is the user's. A default style is applied once, when a layer is
created, and never again; change it in Layer Properties like any layer. The
default draws open comments amber, closed ones grey and comments whose
route reference changed or is unavailable red, with crowded points
clustered. Which comments are closed or need attention is passed to the
style through two project variables (``@route_comments_closed``,
``@route_comments_attention``), so the plugin never has to rewrite it.

Labels come in two tiers (:class:`LabelSettings`): the comment number at
middle scales, and the fields the user picks (or any expression) when
zoomed in further. Full text is never shown at small scales.

Overlapping or clustered comments stay reachable: :class:`CommentPickTool`
lists every comment under the cursor, and selecting features on these
layers selects the comments in the dock (and the other way round). The
layers are read-only so edits always go through the register's checks.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from qgis.core import (Qgis, QgsCategorizedSymbolRenderer, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsExpressionContextUtils, QgsFeatureRequest, QgsGeometry, QgsLineSymbol, QgsMarkerSymbol,
                       QgsPalLayerSettings, QgsPointClusterRenderer, QgsPointXY, QgsProject, QgsRectangle,
                       QgsRendererCategory, QgsRuleBasedLabeling, QgsSimpleLineSymbolLayer, QgsTextBackgroundSettings,
                       QgsTextBufferSettings, QgsTextFormat, QgsUnitTypes, QgsVectorLayer)
from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import QSettings, QSizeF, pyqtSignal
from qgis.PyQt.QtGui import QColor, QCursor, QFont
from qgis.PyQt.QtWidgets import QMenu

from ..maptools.canvas_items import remove_canvas_item
from ..qgis_compat import FEATURE_REQUEST_NO_GEOMETRY, GEOMETRY_LINE, GEOMETRY_POINT, qt_exec

LAYER_PROP = "subsea_cable_tools/route_comments"          # "points" | "ranges"
LABELS_PROP = "subsea_cable_tools/route_comments_labels"   # LabelSettings JSON
GROUP_NAME = "Route comments"
PROJECT_SCOPE = "SubseaCableTools"
PROJECT_KEY_REGISTER = "route_comments_register"
PROJECT_KEY_SET = "route_comments_set"
PROJECT_KEY_LAYER = "route_comments_layer_"                 # + kind: the layer id
VAR_CLOSED = "route_comments_closed"
VAR_ATTENTION = "route_comments_attention"
TABLE = "rc_comment"
WGS84 = "EPSG:4326"
TITLES = {"points": "Comments", "ranges": "Comment ranges"}

STATE_OPEN, STATE_CLOSED, STATE_ATTENTION = "open", "closed", "attention"
STATE_COLORS = {STATE_OPEN: "230,120,0", STATE_CLOSED: "150,150,150", STATE_ATTENTION: "200,40,40"}
STATE_LABELS = {STATE_OPEN: "Open", STATE_CLOSED: "Closed", STATE_ATTENTION: "Route changed / unavailable"}
STATE_EXPRESSION = (
    f"CASE WHEN strpos(coalesce(@{VAR_ATTENTION}, ''), '|' || \"comment_id\" || '|') > 0 THEN '{STATE_ATTENTION}' "
    f"WHEN strpos(coalesce(@{VAR_CLOSED}, ''), '|' || lower(\"status\") || '|') > 0 THEN '{STATE_CLOSED}' "
    f"ELSE '{STATE_OPEN}' END")
CLUSTER_MM = 4.0
PICK_PX = 14
HIGHLIGHT_KEY = "SubseaCableTools/route_comments/highlight_color"
DEFAULT_HIGHLIGHT = "#d4007a"


def _render_mm():
    try:
        return Qgis.RenderUnit.Millimeters
    except AttributeError:  # QGIS 3
        return QgsUnitTypes.RenderMillimeters


def _horizontal_placement():
    try:
        return Qgis.LabelPlacement.Horizontal
    except AttributeError:  # QGIS < 3.26
        return QgsPalLayerSettings.Horizontal


def highlight_color() -> QColor:
    color = QColor(str(QSettings().value(HIGHLIGHT_KEY, DEFAULT_HIGHLIGHT) or DEFAULT_HIGHLIGHT))
    return color if color.isValid() else QColor(DEFAULT_HIGHLIGHT)


# --------------------------------------------------------------------------- project entries
def project_register(project: Optional[QgsProject] = None) -> Tuple[str, str]:
    """``(register path, set_id)`` saved in the project ('' when none)."""
    project = project or QgsProject.instance()
    path, _ok = project.readEntry(PROJECT_SCOPE, PROJECT_KEY_REGISTER, "")
    set_id, _ok = project.readEntry(PROJECT_SCOPE, PROJECT_KEY_SET, "")
    return str(path or ""), str(set_id or "")


def set_project_register(path: str, set_id: str = "", project: Optional[QgsProject] = None) -> None:
    project = project or QgsProject.instance()
    if project_register(project) != (path or "", set_id or ""):
        project.writeEntry(PROJECT_SCOPE, PROJECT_KEY_REGISTER, path or "")
        project.writeEntry(PROJECT_SCOPE, PROJECT_KEY_SET, set_id or "")


def _set_variable(project: QgsProject, name: str, value: str) -> None:
    """Set a project variable only when it changes (setting marks the project modified)."""
    current = QgsExpressionContextUtils.projectScope(project).variable(name)
    if (current or "") != value:
        QgsExpressionContextUtils.setProjectVariable(project, name, value)


def _id_list(values: Iterable[str]) -> str:
    items = [str(v) for v in values if v]
    return "|" + "|".join(items) + "|" if items else ""


# --------------------------------------------------------------------------- labels
LABEL_PARTS = [  # key, menu text, expression
    ("number", "Number (#12)", "'#' || \"ref_no\""),
    ("kp", "KP or KP range",
     "CASE WHEN \"ref_kind\" = 'kp_range' AND \"kp_end_km\" IS NOT NULL "
     "THEN 'KP ' || format_number(\"kp_km\", 3) || '–' || format_number(\"kp_end_km\", 3) "
     "WHEN \"kp_km\" IS NOT NULL THEN 'KP ' || format_number(\"kp_km\", 3) END"),
    ("status", "Status", "\"status\""),
    ("priority", "Priority", "\"priority\""),
    ("author", "Author", "\"author\""),
    ("date", "Date", "\"comment_date\""),
    ("tags", "Tags", "\"tags\""),
    ("comment", "Comment (shortened)", None),
]
SEPARATORS = {"dot": " · ", "space": " ", "newline": "\\n"}


@dataclass
class LabelSettings:
    """What the comment labels show, and from which scale."""
    numbers: bool = True                 # the number tier (middle scales)
    number_scale: int = 100000           # numbers shown when zoomed in beyond 1:number_scale
    detail_scale: int = 20000            # details shown when zoomed in beyond 1:detail_scale
    parts: List[str] = field(default_factory=lambda: ["number", "comment"])
    separator: str = "space"
    comment_chars: int = 40
    custom: str = ""                     # an expression used instead of ``parts``
    size: float = 7.5
    background: bool = True              # a white tag behind the text (else a halo)

    @classmethod
    def from_json(cls, text: str) -> "LabelSettings":
        try:
            data = json.loads(text or "{}")
        except (TypeError, ValueError):
            data = {}
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        try:
            return cls(**known)
        except TypeError:
            return cls()

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    def comment_expression(self) -> str:
        n = max(5, int(self.comment_chars))
        first = "regexp_substr(\"text\", '^[^\\n]*')"
        return (f"CASE WHEN length({first}) > {n} THEN left({first}, {n - 1}) || '…' "
                f"ELSE {first} END")

    def detail_expression(self) -> str:
        if self.custom.strip():
            return self.custom.strip()
        parts = []
        for key, _text, expression in LABEL_PARTS:
            if key in self.parts:
                parts.append(self.comment_expression() if key == "comment" else expression)
        if not parts:
            return LABEL_PARTS[0][2]
        sep = SEPARATORS.get(self.separator, " ")
        joined = ", ".join(f"nullif({p}, '')" for p in parts)
        return f"array_to_string(array_filter(array({joined}), @element IS NOT NULL), '{sep}')"


def _pal(expression: str, settings: LabelSettings, line: bool) -> QgsPalLayerSettings:
    pal = QgsPalLayerSettings()
    pal.isExpression = True
    pal.fieldName = expression
    pal.enabled = True
    fmt = QgsTextFormat()
    font = QFont()
    font.setBold(True)
    fmt.setFont(font)
    fmt.setSize(float(settings.size))
    fmt.setColor(QColor(45, 45, 45))
    if settings.background:
        back = QgsTextBackgroundSettings()
        back.setEnabled(True)
        back.setSize(QSizeF(1.0, 0.5))
        back.setRadii(QSizeF(0.6, 0.6))
        back.setFillColor(QColor(255, 255, 255, 225))
        back.setStrokeColor(QColor(150, 150, 150))
        back.setStrokeWidth(0.15)
        fmt.setBackground(back)
    else:
        buffer = QgsTextBufferSettings()
        buffer.setEnabled(True)
        buffer.setSize(0.8)
        buffer.setColor(QColor(255, 255, 255))
        fmt.setBuffer(buffer)
    pal.setFormat(fmt)
    pal.dist = 1.2
    try:
        pal.autoWrapLength = 48
    except AttributeError:  # very old QGIS
        pass
    if line:
        pal.placement = _horizontal_placement()
    return pal


def build_labeling(settings: LabelSettings, line: bool) -> QgsRuleBasedLabeling:
    """Rule-based labels: numbers at middle scales, details when zoomed in."""
    root = QgsRuleBasedLabeling.Rule(None)
    detail = max(1, int(settings.detail_scale))
    if settings.numbers and int(settings.number_scale) > detail:
        root.appendChild(QgsRuleBasedLabeling.Rule(_pal(LABEL_PARTS[0][2], settings, line), detail,
                                                   int(settings.number_scale), "", "Number"))
    root.appendChild(QgsRuleBasedLabeling.Rule(_pal(settings.detail_expression(), settings, line), 0, detail, "",
                                               "Details"))
    return QgsRuleBasedLabeling(root)


def apply_labels(layers: Sequence[QgsVectorLayer], settings: LabelSettings, on: bool = True) -> None:
    for layer in layers:
        layer.setLabeling(build_labeling(settings, layer.customProperty(LAYER_PROP) == "ranges"))
        layer.setLabelsEnabled(bool(on))
        layer.setCustomProperty(LABELS_PROP, settings.to_json())
        layer.triggerRepaint()


def label_settings_of(layer: Optional[QgsVectorLayer]) -> LabelSettings:
    return LabelSettings.from_json(str(layer.customProperty(LABELS_PROP, "") or "")) if layer else LabelSettings()


# --------------------------------------------------------------------------- default style
def _point_renderer():
    categories = []
    for state in (STATE_OPEN, STATE_ATTENTION, STATE_CLOSED):
        symbol = QgsMarkerSymbol.createSimple({
            "name": "diamond" if state == STATE_ATTENTION else "circle",
            "size": "2.8" if state != STATE_CLOSED else "2.2", "color": STATE_COLORS[state],
            "outline_color": "255,255,255", "outline_width": "0.35"})
        categories.append(QgsRendererCategory(state, symbol, STATE_LABELS[state]))
    categorized = QgsCategorizedSymbolRenderer(STATE_EXPRESSION, categories)
    cluster = QgsPointClusterRenderer()
    cluster.setEmbeddedRenderer(categorized)
    cluster.setTolerance(CLUSTER_MM)
    cluster.setToleranceUnit(_render_mm())
    try:
        cluster.clusterSymbol().symbolLayer(0).setColor(QColor(90, 90, 90))
    except Exception:  # noqa: BLE001 - the default cluster symbol is fine too
        pass
    return cluster


def _range_symbol(state: str) -> QgsLineSymbol:
    """A wide translucent band (visible over a route line) with a dark core."""
    rgb = STATE_COLORS[state]
    symbol = QgsLineSymbol.createSimple({"line_color": rgb + ",120", "line_width": "2.8", "capstyle": "round",
                                         "joinstyle": "round"})
    core = QgsSimpleLineSymbolLayer(QColor(*[int(int(v) * 0.6) for v in rgb.split(",")]), 0.5)
    symbol.appendSymbolLayer(core)
    return symbol


def _line_renderer():
    categories = [QgsRendererCategory(state, _range_symbol(state), STATE_LABELS[state])
                  for state in (STATE_OPEN, STATE_ATTENTION, STATE_CLOSED)]
    return QgsCategorizedSymbolRenderer(STATE_EXPRESSION, categories)


def _uri(path: str, kind: str) -> str:
    geometry = "Point" if kind == "points" else "LineString"
    return f"{path}|layername={TABLE}|geometrytype={geometry}"


def _subset(set_id: str) -> str:
    return "\"set_id\" = '" + str(set_id).replace("'", "''") + "'"


def _same_file(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


# --------------------------------------------------------------------------- layers
class CommentLayers:
    """The two map layers of the current comment set."""

    def __init__(self, project: Optional[QgsProject] = None):
        self.project = project or QgsProject.instance()

    def find(self, kind: str) -> Optional[QgsVectorLayer]:
        layer_id, _ok = self.project.readEntry(PROJECT_SCOPE, PROJECT_KEY_LAYER + kind, "")
        layer = self.project.mapLayer(layer_id) if layer_id else None
        if isinstance(layer, QgsVectorLayer):
            return layer
        for candidate in self.project.mapLayers().values():   # e.g. the id entry was lost
            if isinstance(candidate, QgsVectorLayer) and candidate.customProperty(LAYER_PROP) == kind:
                return candidate
        return None

    def layers(self) -> List[QgsVectorLayer]:
        return [layer for layer in (self.find("points"), self.find("ranges")) if layer is not None]

    def _group(self):
        root = self.project.layerTreeRoot()
        group = root.findGroup(GROUP_NAME)
        if group is None:
            group = root.insertGroup(0, GROUP_NAME)
        return group

    def show(self, register_path: str, set_id: str, set_name: str, closed: Set[str] = (),
             attention: Iterable[str] = (), labels_on: Optional[bool] = None) -> List[QgsVectorLayer]:
        """Show ``set_id`` of the register: create the layers (default style) or
        repoint, refilter and reload the existing ones (style untouched)."""
        _set_variable(self.project, VAR_CLOSED, _id_list(sorted(closed)))
        _set_variable(self.project, VAR_ATTENTION, _id_list(sorted(attention)))
        out = []
        for kind in ("points", "ranges"):
            name = f"{TITLES[kind]} — {set_name}" if set_name else TITLES[kind]
            layer = self.find(kind)
            if layer is None:
                layer = QgsVectorLayer(_uri(register_path, kind), name, "ogr")
                if not layer.isValid():
                    continue
                layer.setCustomProperty(LAYER_PROP, kind)
                layer.setRenderer(_point_renderer() if kind == "points" else _line_renderer())
                apply_labels([layer], label_settings_of(self.find("points")) if kind == "ranges"
                             else LabelSettings(), True if labels_on is None else labels_on)
                layer.setSubsetString(_subset(set_id))
                self.project.addMapLayer(layer, False)
                self._group().insertLayer(0 if kind == "points" else 1, layer)
                self.project.writeEntry(PROJECT_SCOPE, PROJECT_KEY_LAYER + kind, layer.id())
            else:
                path = layer.source().split("|", 1)[0]
                if not layer.isValid() or not _same_file(path, register_path):
                    # The register moved, or another register was opened: repoint, keep the style.
                    layer.setDataSource(_uri(register_path, kind), layer.name(), "ogr")
                if layer.subsetString() != _subset(set_id):
                    layer.setSubsetString(_subset(set_id))
                if layer.name().startswith(TITLES[kind] + " — ") or layer.name() == TITLES[kind]:
                    if layer.name() != name:
                        layer.setName(name)
                layer.reload()
                layer.updateExtents()
                layer.triggerRepaint()
            layer.setReadOnly(True)
            out.append(layer)
        return out

    def set_labels(self, on: bool) -> None:
        for layer in self.layers():
            layer.setLabelsEnabled(bool(on))
            layer.triggerRepaint()

    @property
    def index(self) -> Dict[str, Tuple[str, int]]:
        """``{comment_id: (layer kind, feature id)}`` read from the layers."""
        out: Dict[str, Tuple[str, int]] = {}
        for kind in ("points", "ranges"):
            layer = self.find(kind)
            if layer is None or not layer.isValid():
                continue
            request = QgsFeatureRequest().setFlags(FEATURE_REQUEST_NO_GEOMETRY)
            request.setSubsetOfAttributes(["comment_id"], layer.fields())
            for feature in layer.getFeatures(request):
                out[str(feature["comment_id"])] = (kind, feature.id())
        return out

    def select(self, comment_ids: Iterable[str]) -> None:
        wanted = {"points": [], "ranges": []}
        index = self.index
        for cid in comment_ids:
            hit = index.get(cid)
            if hit is not None:
                wanted[hit[0]].append(hit[1])
        for kind in ("points", "ranges"):
            layer = self.find(kind)
            if layer is not None and layer.isValid() and \
                    sorted(layer.selectedFeatureIds()) != sorted(wanted[kind]):
                layer.selectByIds(wanted[kind])

    def selected_ids(self) -> List[str]:
        out = []
        for layer in self.layers():
            if layer.isValid() and layer.selectedFeatureCount():
                for feature in layer.getSelectedFeatures():
                    out.append(str(feature["comment_id"]))
        return out

    def remove(self) -> None:
        ids = [layer.id() for layer in self.layers()]
        if ids:
            self.project.removeMapLayers(ids)
        for kind in ("points", "ranges"):
            self.project.removeEntry(PROJECT_SCOPE, PROJECT_KEY_LAYER + kind)
        group = self.project.layerTreeRoot().findGroup(GROUP_NAME)
        if group is not None and not group.children():
            group.parent().removeChildNode(group)


def comments_at(canvas, map_point, layers: Sequence[QgsVectorLayer], tolerance_px: int = PICK_PX) -> List[Tuple[float, str]]:
    """``[(distance in map units, comment_id)]`` of every comment feature within
    ``tolerance_px`` of ``map_point``, nearest first (clusters included)."""
    tol = canvas.mapUnitsPerPixel() * tolerance_px
    map_crs = canvas.mapSettings().destinationCrs()
    probe = QgsGeometry.fromPointXY(QgsPointXY(map_point))
    rect = QgsRectangle(map_point.x() - tol, map_point.y() - tol, map_point.x() + tol, map_point.y() + tol)
    hits: Dict[str, float] = {}
    for layer in layers:
        if not layer.isValid():
            continue
        to_layer = QgsCoordinateTransform(map_crs, layer.crs(), QgsProject.instance())
        to_map = QgsCoordinateTransform(layer.crs(), map_crs, QgsProject.instance())
        try:
            search = to_layer.transformBoundingBox(rect)
        except Exception:  # noqa: BLE001 - outside the layer CRS
            continue
        for feature in layer.getFeatures(QgsFeatureRequest().setFilterRect(search)):
            geom = QgsGeometry(feature.geometry())
            try:
                geom.transform(to_map)
            except Exception:  # noqa: BLE001
                continue
            dist = geom.distance(probe)
            cid = str(feature["comment_id"])
            if dist <= tol and dist < hits.get(cid, float("inf")):
                hits[cid] = dist
    return sorted((d, cid) for cid, d in hits.items())


class CommentPickTool(QgsMapTool):
    """Click to pick comments; several under the cursor are listed to choose from."""

    picked = pyqtSignal(list)

    def __init__(self, canvas, layers_getter, describe):
        super().__init__(canvas)
        self._layers = layers_getter
        self._describe = describe       # comment_id -> menu text
        self.setCursor(QCursor())

    def canvasReleaseEvent(self, event):  # noqa: N802 - Qt API
        point = self.toMapCoordinates(event.pos())
        hits = comments_at(self.canvas(), point, self._layers())
        if not hits:
            self.picked.emit([])
            return
        if len(hits) == 1:
            self.picked.emit([hits[0][1]])
            return
        menu = QMenu(self.canvas())
        actions = {}
        for _dist, cid in hits[:40]:
            actions[menu.addAction(self._describe(cid))] = [cid]
        if len(hits) > 40:
            menu.addAction(f"… and {len(hits) - 40} more (use Select all)").setEnabled(False)
        menu.addSeparator()
        actions[menu.addAction(f"Select all {len(hits)}")] = [cid for _d, cid in hits]
        chosen = qt_exec(menu, QCursor.pos())
        if chosen in actions:
            self.picked.emit(actions[chosen])


def _circle_icon():
    return getattr(QgsRubberBand, "ICON_CIRCLE", None) or QgsRubberBand.IconType.ICON_CIRCLE


class Highlight:
    """Outline of the selected comments: a colour band with a white casing,
    so it stands out over route lines and the layer's own symbols."""

    def __init__(self, canvas):
        self.canvas = canvas
        self.bands: List[QgsRubberBand] = []

    def show(self, wkts: Sequence[str]) -> None:
        self.clear()
        crs = QgsCoordinateReferenceSystem(WGS84)
        color = highlight_color()
        for wkt in wkts:
            geom = QgsGeometry.fromWkt(wkt or "")
            if geom is None or geom.isNull():
                continue
            point = geom.type() == GEOMETRY_POINT
            for casing in (True, False):
                band = QgsRubberBand(self.canvas, GEOMETRY_POINT if point else GEOMETRY_LINE)
                band.setColor(QColor(255, 255, 255, 230) if casing else color)
                band.setWidth(7 if casing else 3)
                if point:
                    band.setIcon(_circle_icon())
                    band.setIconSize(18)
                    band.setFillColor(QColor(0, 0, 0, 0))
                band.setToGeometry(geom, crs)
                self.bands.append(band)

    def wkts_shown(self) -> int:
        return len(self.bands) // 2

    def clear(self) -> None:
        for band in self.bands:
            remove_canvas_item(band)
        self.bands = []

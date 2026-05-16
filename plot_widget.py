# -*- coding: utf-8 -*-
"""Matplotlib-shaped plotting helpers backed by bundled pyqtgraph."""

from __future__ import annotations

import math
import importlib
import os
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import pyqtgraph as pg
from qgis.PyQt import QtCore, QtGui, QtSvg
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QPainterPath
from qgis.PyQt.QtWidgets import QFileDialog, QGraphicsPathItem, QHBoxLayout, QPushButton, QVBoxLayout, QWidget


_PEN_STYLE = getattr(Qt, "PenStyle", Qt)
_MOUSE_BUTTON = getattr(Qt, "MouseButton", Qt)
__SUBSEA_PLOT_WIDGET_PATCH_VERSION__ = 4
_REQUIRED_SVG_EXPORTER_PATCH_VERSION = 4


_TAB10_COLORS = [
    "#1f77b4",
    "#ff7f0e",
    "#2ca02c",
    "#d62728",
    "#9467bd",
    "#8c564b",
    "#e377c2",
    "#7f7f7f",
    "#bcbd22",
    "#17becf",
]

_NAMED_COLORS = {
    "black": "#000000",
    "blue": "#1f77b4",
    "brown": "#8c564b",
    "k": "#000000",
    "lightblue": "#add8e6",
    "tab:blue": "#1f77b4",
    "tab:orange": "#ff7f0e",
    "tab:green": "#2ca02c",
    "tab:red": "#d62728",
    "tab:purple": "#9467bd",
    "tab:brown": "#8c564b",
    "tab:pink": "#e377c2",
    "tab:gray": "#7f7f7f",
    "tab:olive": "#bcbd22",
    "tab:cyan": "#17becf",
}


def get_tab10_color(index: int) -> str:
    return _TAB10_COLORS[int(index) % len(_TAB10_COLORS)]


def _as_plot_color(color: Any, default: str = "#1f77b4", alpha: Optional[float] = None) -> Any:
    value = default if color is None else color
    if isinstance(value, str):
        value = _NAMED_COLORS.get(value.lower(), value)
    if isinstance(value, (tuple, list)) and value:
        values = list(value)
        if all(isinstance(item, (int, float)) for item in values) and all(0 <= float(item) <= 1 for item in values):
            value = tuple(int(round(float(item) * 255)) for item in values)
    if alpha is None:
        return value
    try:
        qcolor = pg.mkColor(value)
        qcolor.setAlphaF(max(0.0, min(1.0, float(alpha))))
        return qcolor
    except Exception:
        return value


def _pen_style(linestyle: Optional[str]) -> Any:
    if linestyle in ("--", "dashed"):
        return _PEN_STYLE.DashLine
    if linestyle in (":", "dotted"):
        return _PEN_STYLE.DotLine
    return _PEN_STYLE.SolidLine


def _values(values: Iterable[Any]) -> List[float]:
    out: List[float] = []
    for value in values:
        if value is None:
            out.append(math.nan)
            continue
        try:
            out.append(float(value))
        except Exception:
            out.append(math.nan)
    return out


def _svg_exporter_class():
    module = importlib.import_module("pyqtgraph.exporters.SVGExporter")
    module_path = os.path.normcase(os.path.abspath(getattr(module, "__file__", "")))
    bundled_lib = os.path.normcase(os.path.abspath(os.path.join(os.path.dirname(__file__), "lib")))
    patch_version = int(getattr(module, "__SUBSEA_SVG_EXPORTER_PATCH_VERSION__", 0) or 0)
    if module_path.startswith(bundled_lib) and patch_version < _REQUIRED_SVG_EXPORTER_PATCH_VERSION:
        module = importlib.reload(module)
    try:
        exporter_base = importlib.import_module("pyqtgraph.exporters.Exporter").Exporter
        svg_class = module.SVGExporter
        svg_name = getattr(svg_class, "Name", "")
        exporter_base.Exporters = [
            exp for exp in exporter_base.Exporters
            if getattr(exp, "__name__", "") != "SVGExporter" and getattr(exp, "Name", "") != svg_name
        ]
        exporter_base.Exporters.append(svg_class)
    except Exception:
        pass
    return module.SVGExporter


class LineCollection:
    def __init__(self, segments: Sequence[Any], colors: Optional[Sequence[Any]] = None, linewidths: Any = 1, **_kwargs):
        self.segments = list(segments)
        self.colors = list(colors) if colors is not None else []
        self.linewidth = self._first_linewidth(linewidths)
        self.alpha = _kwargs.get("alpha")

    @staticmethod
    def _first_linewidth(linewidths: Any) -> float:
        if isinstance(linewidths, (tuple, list)) and linewidths:
            return float(linewidths[0])
        return float(linewidths or 1)


class PlotMouseEvent:
    def __init__(self, name: str, inaxes: Optional["PyQtGraphAxis"], xdata: Optional[float], ydata: Optional[float], button: Optional[int] = None):
        self.name = name
        self.inaxes = inaxes
        self.xdata = xdata
        self.ydata = ydata
        self.button = button


class PyQtGraphLine:
    def __init__(self, item: Any, xdata: Sequence[Any], ydata: Sequence[Any], label: Optional[str] = None, kind: str = "line"):
        self.item = item
        self._xdata = list(xdata)
        self._ydata = list(ydata)
        self._label = label or "_nolegend_"
        self.kind = kind

    def get_xdata(self):
        return self._xdata

    def get_ydata(self):
        return self._ydata

    def get_label(self):
        return self._label

    def set_xdata(self, xdata: Sequence[Any]):
        self._xdata = list(xdata)
        if self.kind == "vline" and self._xdata:
            self.item.setValue(float(self._xdata[0]))
        elif hasattr(self.item, "setData"):
            self.item.setData(_values(self._xdata), _values(self._ydata))

    def set_ydata(self, ydata: Sequence[Any]):
        self._ydata = list(ydata)
        if self.kind == "hline" and self._ydata:
            self.item.setValue(float(self._ydata[0]))
        elif hasattr(self.item, "setData"):
            self.item.setData(_values(self._xdata), _values(self._ydata))

    def set_visible(self, visible: bool):
        self.item.setVisible(bool(visible))

    def get_visible(self) -> bool:
        try:
            return bool(self.item.isVisible())
        except Exception:
            return False


class PyQtGraphFigure:
    def __init__(self, figsize: Optional[Tuple[float, float]] = None):
        self.figsize = figsize
        self.canvas: Optional[PyQtGraphCanvas] = None
        self.axes: List[PyQtGraphAxis] = []

    def clear(self):
        self.axes = []
        if self.canvas is not None:
            self.canvas.clear_axes()

    def add_subplot(self, spec=111, *args, **kwargs) -> "PyQtGraphAxis":
        if self.canvas is None:
            raise RuntimeError("Plot canvas has not been attached to the figure.")
        rows, cols, index = self._parse_subplot_spec(spec)
        sharex = kwargs.get("sharex")
        return self.canvas.ensure_axis(rows, cols, index, sharex=sharex)

    @staticmethod
    def _parse_subplot_spec(spec: Any) -> Tuple[int, int, int]:
        try:
            value = int(spec)
            if value >= 100:
                return value // 100, (value // 10) % 10, value % 10
        except Exception:
            pass
        return 1, 1, 1

    def get_axes(self) -> List["PyQtGraphAxis"]:
        return list(self.axes)

    def tight_layout(self):
        return None

    def savefig(self, path: str, format: str = "svg"):
        if self.canvas is None:
            raise RuntimeError("Plot canvas has not been attached to the figure.")
        if format and format.lower() != "svg":
            raise ValueError("Only SVG export is supported by the pyqtgraph plot widget.")
        self.canvas.export_svg(path)


class PyQtGraphCanvas(QWidget):
    def __init__(self, figure: PyQtGraphFigure):
        super().__init__()
        self.figure = figure
        self.figure.canvas = self
        self._callbacks: Dict[int, Tuple[str, Callable[[PlotMouseEvent], None]]] = {}
        self._next_callback_id = 1
        self._connected_scene_ids = set()
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(2)

    def clear_axes(self):
        self._connected_scene_ids = set()
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def ensure_axis(self, rows: int, cols: int, index: int, sharex: Optional["PyQtGraphAxis"] = None) -> "PyQtGraphAxis":
        if cols != 1:
            raise ValueError("Only single-column plot layouts are supported.")
        while len(self.figure.axes) < rows:
            axis_index = len(self.figure.axes) + 1
            plot_widget = pg.PlotWidget()
            plot_widget.setBackground("w")
            plot_widget.showGrid(x=False, y=False)
            plot_widget.getViewBox().setMouseEnabled(x=True, y=True)
            self._layout.addWidget(plot_widget, 1)
            axis = PyQtGraphAxis(self, plot_widget, axis_index)
            self.figure.axes.append(axis)
            self._connect_scene(plot_widget)
        axis = self.figure.axes[max(0, min(index - 1, len(self.figure.axes) - 1))]
        if sharex is not None:
            axis.plot_widget.setXLink(sharex.plot_widget)
            axis._shared_x_axes.append(sharex)
            sharex._shared_x_axes.append(axis)
        return axis

    def _connect_scene(self, plot_widget: pg.PlotWidget):
        scene = plot_widget.scene()
        scene_id = id(scene)
        if scene_id in self._connected_scene_ids:
            return
        scene.sigMouseMoved.connect(self._dispatch_mouse_move)
        scene.sigMouseClicked.connect(self._dispatch_mouse_click)
        self._connected_scene_ids.add(scene_id)

    def _axis_for_scene_pos(self, scene_pos: Any) -> Tuple[Optional["PyQtGraphAxis"], Optional[float], Optional[float]]:
        for axis in self.figure.axes:
            try:
                if axis.plot_item.vb.sceneBoundingRect().contains(scene_pos):
                    mapped = axis.plot_item.vb.mapSceneToView(scene_pos)
                    return axis, float(mapped.x()), float(mapped.y())
            except Exception:
                continue
        return None, None, None

    def _dispatch_mouse_move(self, scene_pos: Any):
        axis, xdata, ydata = self._axis_for_scene_pos(scene_pos)
        event = PlotMouseEvent("motion_notify_event", axis, xdata, ydata)
        self._emit("motion_notify_event", event)

    def _dispatch_mouse_click(self, mouse_event: Any):
        try:
            scene_pos = mouse_event.scenePos()
        except Exception:
            scene_pos = None
        axis, xdata, ydata = self._axis_for_scene_pos(scene_pos) if scene_pos is not None else (None, None, None)
        button = None
        try:
            qt_button = mouse_event.button()
            if qt_button == _MOUSE_BUTTON.RightButton:
                button = 3
            elif qt_button == _MOUSE_BUTTON.MiddleButton:
                button = 2
            elif qt_button == _MOUSE_BUTTON.LeftButton:
                button = 1
        except Exception:
            pass
        event = PlotMouseEvent("button_press_event", axis, xdata, ydata, button=button)
        self._emit("button_press_event", event)

    def _emit(self, event_name: str, event: PlotMouseEvent):
        for registered_name, callback in list(self._callbacks.values()):
            if registered_name != event_name:
                continue
            try:
                callback(event)
            except Exception:
                pass

    def mpl_connect(self, event_name: str, callback: Callable[[PlotMouseEvent], None]) -> int:
        callback_id = self._next_callback_id
        self._next_callback_id += 1
        self._callbacks[callback_id] = (event_name, callback)
        return callback_id

    def mpl_disconnect(self, callback_id: int):
        self._callbacks.pop(callback_id, None)

    def draw(self):
        self.update()

    def draw_idle(self):
        self.update()

    def export_svg(self, path: str):
        if not self.figure.axes:
            return
        try:
            exporter = _svg_exporter_class()(self.figure.axes[0].plot_item)
            exporter.export(path)
        except Exception as first_error:
            try:
                self._export_svg_with_qt(path)
            except Exception as fallback_error:
                raise RuntimeError(
                    f"SVG export failed with pyqtgraph ({first_error}) and Qt fallback ({fallback_error})."
                )

    def _export_svg_with_qt(self, path: str):
        if not self.figure.axes:
            return
        widget = self.figure.axes[0].plot_widget
        size = widget.size()
        if not size.isValid() or size.width() <= 0 or size.height() <= 0:
            size = QtCore.QSize(1200, 800)

        generator = QtSvg.QSvgGenerator()
        generator.setFileName(path)
        generator.setSize(size)
        generator.setViewBox(QtCore.QRect(0, 0, int(size.width()), int(size.height())))
        try:
            screen = QtGui.QGuiApplication.primaryScreen()
            if screen is not None:
                generator.setResolution(int(screen.logicalDotsPerInchX()))
        except Exception:
            pass

        painter = QtGui.QPainter()
        if not painter.begin(generator):
            raise RuntimeError("Could not start Qt SVG painter.")
        try:
            painter.fillRect(QtCore.QRect(0, 0, int(size.width()), int(size.height())), QtGui.QColor("white"))
            widget.render(painter)
        finally:
            painter.end()

    def show_export_dialog(self):
        if not self.figure.axes:
            return
        _svg_exporter_class()
        axis = self.figure.axes[0]
        scene = axis.plot_widget.scene()
        scene.contextMenuItem = axis.plot_item
        if hasattr(scene, "showExportDialog"):
            scene.showExportDialog()
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save SVG", "plot.svg", "SVG Files (*.svg)")
        if path:
            self.export_svg(path)

    def export_svg_dialog(self):
        self.show_export_dialog()

    def auto_range_all(self):
        for axis in self.figure.axes:
            axis.auto_range()


class PyQtGraphNavigationToolbar(QWidget):
    def __init__(self, canvas: PyQtGraphCanvas, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.canvas = canvas
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.reset_btn = QPushButton("Reset View")
        self.export_btn = QPushButton("Export Plot...")
        layout.addWidget(self.reset_btn)
        layout.addWidget(self.export_btn)
        layout.addStretch(1)
        self.reset_btn.clicked.connect(self.canvas.auto_range_all)
        self.export_btn.clicked.connect(self.canvas.show_export_dialog)


class PyQtGraphAxis:
    def __init__(self, canvas: PyQtGraphCanvas, plot_widget: pg.PlotWidget, index: int, view_box: Optional[pg.ViewBox] = None, is_twin: bool = False, parent_axis: Optional["PyQtGraphAxis"] = None):
        self.canvas = canvas
        self.plot_widget = plot_widget
        self.plot_item = plot_widget.getPlotItem()
        self.index = index
        self.view_box = view_box or self.plot_item.vb
        self.is_twin = is_twin
        self.parent_axis = parent_axis
        self._data_lines: List[PyQtGraphLine] = []
        self._legend_items: List[PyQtGraphLine] = []
        self._legend = None
        self._twinx: Optional[PyQtGraphAxis] = None
        self._default_color_index = 0
        self._shared_x_axes: List[PyQtGraphAxis] = []

    def _next_color(self) -> str:
        color = get_tab10_color(self._default_color_index)
        self._default_color_index += 1
        return color

    def _make_pen(self, color: Any = None, linewidth: Any = 1, linestyle: Optional[str] = None, alpha: Optional[float] = None):
        default = self._next_color() if color is None else "#1f77b4"
        return pg.mkPen(_as_plot_color(color, default, alpha=alpha), width=float(linewidth or 1), style=_pen_style(linestyle))

    def _add_item(self, item: Any):
        if self.is_twin:
            self.view_box.addItem(item)
            self.view_box.enableAutoRange()
        else:
            self.plot_item.addItem(item)

    def plot(self, x_values, y_values, *args, **kwargs):
        label = kwargs.pop("label", None)
        color = kwargs.pop("color", None)
        linewidth = kwargs.pop("linewidth", kwargs.pop("lw", 1))
        linestyle = kwargs.pop("linestyle", None)
        alpha = kwargs.pop("alpha", None)
        if color is None and args:
            color = args[0]
        xdata = list(x_values)
        ydata = list(y_values)
        item = pg.PlotDataItem(_values(xdata), _values(ydata), pen=self._make_pen(color, linewidth, linestyle, alpha))
        self._add_item(item)
        line = PyQtGraphLine(item, xdata, ydata, label=label)
        self._data_lines.append(line)
        if label:
            self._legend_items.append(line)
        return [line]

    def add_collection(self, collection: LineCollection):
        current_color = object()
        current_x: List[float] = []
        current_y: List[float] = []
        default_color = self._next_color() if not collection.colors else "#1f77b4"

        def flush_current():
            if len(current_x) < 2:
                return
            item = pg.PlotDataItem(
                current_x,
                current_y,
                pen=pg.mkPen(_as_plot_color(current_color, alpha=collection.alpha), width=collection.linewidth),
            )
            self._add_item(item)

        for segment_index, segment in enumerate(collection.segments):
            if len(segment) < 2:
                continue
            color = collection.colors[segment_index] if segment_index < len(collection.colors) else default_color
            x_values = [float(point[0]) for point in segment]
            y_values = [float(point[1]) for point in segment]
            if current_x and color == current_color:
                if current_x[-1] != x_values[0] or current_y[-1] != y_values[0]:
                    current_x.append(math.nan)
                    current_y.append(math.nan)
                    current_x.append(x_values[0])
                    current_y.append(y_values[0])
                current_x.append(x_values[-1])
                current_y.append(y_values[-1])
            else:
                flush_current()
                current_color = color
                current_x = list(x_values)
                current_y = list(y_values)

        flush_current()

    def axvline(self, x: float = 0, *args, **kwargs):
        color = kwargs.pop("color", "#000000")
        linestyle = kwargs.pop("linestyle", kwargs.pop("ls", None))
        linewidth = kwargs.pop("lw", kwargs.pop("linewidth", 1))
        line_item = pg.InfiniteLine(pos=float(x), angle=90, pen=self._make_pen(color, linewidth, linestyle))
        self._add_item(line_item)
        return PyQtGraphLine(line_item, [x, x], [], kind="vline")

    def axhline(self, y: float = 0, *args, **kwargs):
        label = kwargs.pop("label", None)
        color = kwargs.pop("color", "#7f7f7f")
        linewidth = kwargs.pop("linewidth", kwargs.pop("lw", 1))
        linestyle = kwargs.pop("linestyle", None)
        line_item = pg.InfiniteLine(pos=float(y), angle=0, pen=self._make_pen(color, linewidth, linestyle))
        self._add_item(line_item)
        line = PyQtGraphLine(line_item, [], [y, y], label=label, kind="hline")
        if label:
            self._legend_items.append(line)
        return line

    def scatter(self, x_values, y_values, *args, **kwargs):
        label = kwargs.pop("label", None)
        marker = str(kwargs.pop("marker", "o")).lower()
        size_arg = float(kwargs.pop("s", 36) or 36)
        color = kwargs.pop("color", None)
        facecolors = kwargs.pop("facecolors", color)
        edgecolors = kwargs.pop("edgecolors", color or "#000000")
        symbol = "d" if marker == "d" else marker
        size = max(6.0, math.sqrt(size_arg) * 1.6)
        brush = pg.mkBrush(0, 0, 0, 0) if facecolors == "none" else pg.mkBrush(_as_plot_color(facecolors, "#000000"))
        pen = pg.mkPen(_as_plot_color(edgecolors, "#000000"), width=1)
        item = pg.ScatterPlotItem(x=list(x_values), y=list(y_values), size=size, symbol=symbol, pen=pen, brush=brush)
        self._add_item(item)
        line = PyQtGraphLine(item, list(x_values), list(y_values), label=label)
        if label:
            self._legend_items.append(line)
        return item

    def fill(self, x_values, y_values, *args, **kwargs):
        label = kwargs.pop("label", None)
        facecolor = kwargs.pop("facecolor", kwargs.pop("color", None))
        edgecolor = kwargs.pop("edgecolor", facecolor or "#000000")
        linewidth = kwargs.pop("linewidth", kwargs.pop("lw", 1))
        alpha = kwargs.pop("alpha", None)
        zorder = kwargs.pop("zorder", None)
        if facecolor is None and args:
            facecolor = args[0]

        xdata = _values(x_values)
        ydata = _values(y_values)
        path = QPainterPath()
        started = False
        for x_val, y_val in zip(xdata, ydata):
            if math.isnan(x_val) or math.isnan(y_val):
                continue
            if not started:
                path.moveTo(float(x_val), float(y_val))
                started = True
            else:
                path.lineTo(float(x_val), float(y_val))

        if not started:
            return []

        path.closeSubpath()
        item = QGraphicsPathItem(path)
        item.setBrush(pg.mkBrush(_as_plot_color(facecolor, "#7f7f7f", alpha=alpha)))
        item.setPen(pg.mkPen(_as_plot_color(edgecolor, "#000000"), width=float(linewidth or 1)))
        if zorder is not None:
            try:
                item.setZValue(float(zorder))
            except Exception:
                pass
        self._add_item(item)

        line = PyQtGraphLine(item, xdata, ydata, label=label, kind="polygon")
        if label:
            self._legend_items.append(line)
        return [line]

    def twinx(self):
        if self._twinx is not None:
            return self._twinx
        view = pg.ViewBox()
        self.plot_item.showAxis("right")
        self.plot_item.scene().addItem(view)
        self.plot_item.getAxis("right").linkToView(view)
        view.setXLink(self.plot_item.vb)

        def update_views():
            view.setGeometry(self.plot_item.vb.sceneBoundingRect())
            view.linkedViewChanged(self.plot_item.vb, view.XAxis)

        self.plot_item.vb.sigResized.connect(update_views)
        update_views()
        self._twinx = PyQtGraphAxis(self.canvas, self.plot_widget, self.index, view_box=view, is_twin=True, parent_axis=self)
        return self._twinx

    def get_lines(self):
        return list(self._data_lines)

    def get_legend_handles_labels(self):
        handles: List[PyQtGraphLine] = []
        labels: List[str] = []
        for line in self._legend_items:
            label = line.get_label()
            if label and not label.startswith("_"):
                handles.append(line)
                labels.append(label)
        return handles, labels

    def legend(self, handles=None, labels=None, *args, **kwargs):
        if self._legend is None:
            self._legend = self.plot_item.addLegend(offset=(10, 10))
        if handles is None:
            handles, labels = self.get_legend_handles_labels()
        elif labels is None:
            labels = [handle.get_label() if hasattr(handle, "get_label") else "" for handle in handles]
        for handle, label in zip(handles or [], labels or []):
            if not label or str(label).startswith("_"):
                continue
            item = handle.item if hasattr(handle, "item") else handle
            try:
                if not hasattr(item, "opts"):
                    pen = getattr(item, "pen", None)
                    item = pg.PlotDataItem([], [], pen=pen)
                self._legend.addItem(item, str(label))
            except Exception:
                pass

    def set_xlabel(self, label: str):
        self.plot_item.setLabel("bottom", label)

    def set_ylabel(self, label: str):
        axis_name = "right" if self.is_twin else "left"
        self.plot_item.setLabel(axis_name, label)

    def set_title(self, title: str):
        self.plot_item.setTitle(title)

    def set_xlim(self, left: float, right: float):
        self.plot_widget.setXRange(float(left), float(right), padding=0)

    def set_ylim(self, bottom: float, top: float):
        self.view_box.setYRange(float(bottom), float(top), padding=0)

    def get_xlim(self):
        x_range = self.plot_item.vb.viewRange()[0]
        return float(x_range[0]), float(x_range[1])

    def get_ylim(self):
        y_range = self.view_box.viewRange()[1]
        return float(y_range[0]), float(y_range[1])

    def _set_invert_x(self, inverted: bool):
        self.plot_item.vb.invertX(bool(inverted))

    def invert_xaxis(self):
        self._set_invert_x(True)
        for axis in self._shared_x_axes:
            axis._set_invert_x(True)

    def invert_yaxis(self):
        self.view_box.invertY(True)

    def set_aspect(self, aspect: str, adjustable: Optional[str] = None):
        self.plot_item.vb.setAspectLocked(aspect == "equal", ratio=1)

    def grid(self, value: bool, alpha: float = 0.25):
        self.plot_item.showGrid(x=bool(value), y=bool(value), alpha=alpha)

    def auto_range(self):
        self.plot_item.enableAutoRange()
        if self._twinx is not None:
            self._twinx.view_box.enableAutoRange()


Figure = PyQtGraphFigure
FigureCanvas = PyQtGraphCanvas
NavigationToolbar = PyQtGraphNavigationToolbar

# -*- coding: utf-8 -*-
"""Compatibility imports for the catenary dialogs' plotting backend."""

import importlib

from .. import plot_widget as _plot_widget

if int(getattr(_plot_widget, "__SUBSEA_PLOT_WIDGET_PATCH_VERSION__", 0) or 0) < 7:
    _plot_widget = importlib.reload(_plot_widget)

CatenaryPlotCanvas = _plot_widget.FigureCanvas
CatenaryPlotFigure = _plot_widget.Figure
CatenaryLineCollection = _plot_widget.LineCollection
get_tab10_color = _plot_widget.get_tab10_color

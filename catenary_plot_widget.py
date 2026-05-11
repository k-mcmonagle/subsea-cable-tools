# -*- coding: utf-8 -*-
"""Compatibility imports for the catenary dialogs' plotting backend."""

from .plot_widget import (
    FigureCanvas as CatenaryPlotCanvas,
    Figure as CatenaryPlotFigure,
    LineCollection as CatenaryLineCollection,
    get_tab10_color,
)

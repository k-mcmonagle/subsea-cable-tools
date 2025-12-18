"""
Live Data Module

Subsea Cable Tools live data streaming and monitoring functionality.
Includes TCP data reception, map integration, and card-based/plot-based grid display.

Main components:
- LiveDataWorker: Threading worker for data reception
- LiveDataDockWidget: Main UI container (connection + map)
- LiveDataCardsDockWidget: Cards display in separate dockwidget
- LiveDataPlotsDockWidget: Plots (trend graphs) display in separate dockwidget
- CompactCardWidget: Individual card display widget
- CardGridWidget: Grid layout for multiple cards
- CardManagerDialog: Card configuration UI
- CardConfig: Card configuration data model
- QPlotWidget: Individual plot display widget
- PlotGridWidget: Grid layout for multiple plots
- PlotManagerDialog: Plot configuration UI
- PlotConfig: Plot configuration data model
- PlotDataBuffer: Time-series data buffer for plots
"""

from .live_data_worker import LiveDataWorker
from .live_data_dockwidget import LiveDataDockWidget
from .live_data_cards_dockwidget import LiveDataCardsDockWidget
from .live_data_plots_dockwidget import LiveDataPlotsDockWidget
from .card_config import CardConfig
from .compact_card_widget import CompactCardWidget
from .card_grid_widget import CardGridWidget
from .card_manager_dialog import CardManagerDialog
from .plot_config import PlotConfig
from .plot_widget import QPlotWidget
from .plot_grid_widget import PlotGridWidget
from .plot_manager_dialog import PlotManagerDialog
from .plot_data_buffer import PlotDataBuffer
from .utils import CardConfigManager, PlotConfigManager

__all__ = [
    'LiveDataWorker',
    'LiveDataDockWidget',
    'LiveDataCardsDockWidget',
    'LiveDataPlotsDockWidget',
    'CardConfig',
    'CompactCardWidget',
    'CardGridWidget',
    'CardManagerDialog',
    'CardConfigManager',
    'PlotConfig',
    'QPlotWidget',
    'PlotGridWidget',
    'PlotManagerDialog',
    'PlotDataBuffer',
    'PlotConfigManager',
]


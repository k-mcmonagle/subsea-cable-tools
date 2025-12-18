"""
Live Data Utilities

Helper functions for persistence, validation, and configuration management.
"""

import json
import os
from pathlib import Path
from typing import List, Dict, Any
from qgis.core import QgsProject
from qgis.PyQt.QtCore import QSettings

from .card_config import CardConfig
from .plot_config import PlotConfig
from .table_config import TableConfig


class CardConfigManager:
    """Manages loading and saving card configurations."""
    
    SETTINGS_KEY = "subsea_cable_tools/live_data/cards"
    PROJECT_KEY = "subsea_cable_tools_live_data_cards"
    
    @staticmethod
    def save_cards_to_project(cards: List[CardConfig]):
        """
        Save card configurations to the current QGIS project.
        
        Args:
            cards: List of CardConfig objects to save
        """
        project = QgsProject.instance()
        if not project:
            return
        
        try:
            # Serialize to JSON
            cards_data = [card.to_dict() for card in cards]
            json_str = json.dumps(cards_data, indent=2)
            
            # Store in project using customProperty (works with all QGIS versions)
            project.writeEntry("subsea_cable_tools", CardConfigManager.PROJECT_KEY, json_str)
        except Exception as e:
            print(f"Error saving cards to project: {e}")
    
    @staticmethod
    def load_cards_from_project() -> List[CardConfig]:
        """
        Load card configurations from the current QGIS project.
        
        Returns:
            List of CardConfig objects, or empty list if none found
        """
        project = QgsProject.instance()
        if not project:
            return []
        
        try:
            # Load from project using readEntry (works with all QGIS versions)
            json_str, success = project.readEntry("subsea_cable_tools", CardConfigManager.PROJECT_KEY, "")
            
            if not json_str or not success:
                return []
            
            cards_data = json.loads(json_str)
            return [CardConfig.from_dict(card) for card in cards_data]
        except (json.JSONDecodeError, KeyError, TypeError, Exception) as e:
            print(f"Error loading cards from project: {e}")
            return []
    
    @staticmethod
    def save_cards_to_settings(cards: List[CardConfig]):
        """
        Save card configurations to QSettings (per-user, persistent).
        
        Args:
            cards: List of CardConfig objects to save
        """
        settings = QSettings()
        
        # Serialize to JSON
        cards_data = [card.to_dict() for card in cards]
        json_str = json.dumps(cards_data, indent=2)
        
        # Store in settings
        settings.setValue(CardConfigManager.SETTINGS_KEY, json_str)
    
    @staticmethod
    def load_cards_from_settings() -> List[CardConfig]:
        """
        Load card configurations from QSettings.
        
        Returns:
            List of CardConfig objects, or empty list if none found
        """
        settings = QSettings()
        json_str = settings.value(CardConfigManager.SETTINGS_KEY, "")
        
        if not json_str:
            return []
        
        try:
            cards_data = json.loads(json_str)
            return [CardConfig.from_dict(card) for card in cards_data]
        except (json.JSONDecodeError, KeyError, TypeError):
            return []
    
    @staticmethod
    def save_cards(cards: List[CardConfig], to_project: bool = True):
        """
        Save cards to project or settings.
        
        Args:
            cards: List of CardConfig objects
            to_project: If True, save to project; if False, save to settings
        """
        if to_project:
            CardConfigManager.save_cards_to_project(cards)
        else:
            CardConfigManager.save_cards_to_settings(cards)
    
    @staticmethod
    def load_cards(from_project: bool = True) -> List[CardConfig]:
        """
        Load cards from project or settings.
        
        Args:
            from_project: If True, load from project; if False, load from settings
            
        Returns:
            List of CardConfig objects
        """
        if from_project:
            cards = CardConfigManager.load_cards_from_project()
            # Fallback to settings if project is empty
            if not cards:
                cards = CardConfigManager.load_cards_from_settings()
        else:
            cards = CardConfigManager.load_cards_from_settings()
        
        return cards


class PlotConfigManager:
    """Manages loading and saving plot configurations."""
    
    SETTINGS_KEY = "subsea_cable_tools/live_data/plots"
    PROJECT_KEY = "subsea_cable_tools_live_data_plots"
    
    @staticmethod
    def save_plots_to_project(plots: List[PlotConfig]):
        """
        Save plot configurations to the current QGIS project.
        
        Args:
            plots: List of PlotConfig objects to save
        """
        project = QgsProject.instance()
        if not project:
            return
        
        try:
            # Serialize to JSON
            plots_data = [plot.to_dict() for plot in plots]
            json_str = json.dumps(plots_data, indent=2)
            
            # Store in project
            project.writeEntry("subsea_cable_tools", PlotConfigManager.PROJECT_KEY, json_str)
        except Exception as e:
            print(f"Error saving plots to project: {e}")
    
    @staticmethod
    def load_plots_from_project() -> List[PlotConfig]:
        """
        Load plot configurations from the current QGIS project.
        
        Returns:
            List of PlotConfig objects, or empty list if none found
        """
        project = QgsProject.instance()
        if not project:
            return []
        
        try:
            # Load from project
            json_str, success = project.readEntry("subsea_cable_tools", PlotConfigManager.PROJECT_KEY, "")
            
            if not json_str or not success:
                return []
            
            plots_data = json.loads(json_str)
            return [PlotConfig.from_dict(plot) for plot in plots_data]
        except (json.JSONDecodeError, KeyError, TypeError, Exception) as e:
            print(f"Error loading plots from project: {e}")
            return []
    
    @staticmethod
    def save_plots_to_settings(plots: List[PlotConfig]):
        """
        Save plot configurations to QSettings (per-user, persistent).
        
        Args:
            plots: List of PlotConfig objects to save
        """
        settings = QSettings()
        
        # Serialize to JSON
        plots_data = [plot.to_dict() for plot in plots]
        json_str = json.dumps(plots_data, indent=2)
        
        # Store in settings
        settings.setValue(PlotConfigManager.SETTINGS_KEY, json_str)
    
    @staticmethod
    def load_plots_from_settings() -> List[PlotConfig]:
        """
        Load plot configurations from QSettings.
        
        Returns:
            List of PlotConfig objects, or empty list if none found
        """
        settings = QSettings()
        json_str = settings.value(PlotConfigManager.SETTINGS_KEY, "")
        
        if not json_str:
            return []
        
        try:
            plots_data = json.loads(json_str)
            return [PlotConfig.from_dict(plot) for plot in plots_data]
        except (json.JSONDecodeError, KeyError, TypeError):
            return []
    
    @staticmethod
    def save_plots(plots: List[PlotConfig], to_project: bool = True):
        """
        Save plots to project or settings.
        
        Args:
            plots: List of PlotConfig objects
            to_project: If True, save to project; if False, save to settings
        """
        if to_project:
            PlotConfigManager.save_plots_to_project(plots)
        else:
            PlotConfigManager.save_plots_to_settings(plots)
    
    @staticmethod
    def load_plots(from_project: bool = True) -> List[PlotConfig]:
        """
        Load plots from project or settings.
        
        Args:
            from_project: If True, load from project; if False, load from settings
            
        Returns:
            List of PlotConfig objects
        """
        if from_project:
            plots = PlotConfigManager.load_plots_from_project()
            # Fallback to settings if project is empty
            if not plots:
                plots = PlotConfigManager.load_plots_from_settings()
        else:
            plots = PlotConfigManager.load_plots_from_settings()
        
        return plots


def format_value_for_display(value: Any, decimal_places: int = -1) -> str:
    """
    Format a numeric value for display.
    
    Args:
        value: Value to format
        decimal_places: Number of decimal places (-1 for auto)
        
    Returns:
        Formatted string
    """
    try:
        if isinstance(value, str):
            value = float(value.strip())
        
        if decimal_places == -1:
            # Auto format: 3 significant figures
            return f"{float(value):.3g}"
        else:
            return f"{float(value):.{decimal_places}f}"
    except (ValueError, TypeError):
        return "--"


class TableConfigManager:
    """Manages loading and saving table configurations."""
    
    SETTINGS_KEY = "subsea_cable_tools/live_data/tables"
    PROJECT_KEY = "subsea_cable_tools_live_data_tables"
    
    @staticmethod
    def save_tables_to_project(tables: List[TableConfig]):
        """
        Save table configurations to the current QGIS project.
        
        Args:
            tables: List of TableConfig objects to save
        """
        project = QgsProject.instance()
        if not project:
            return
        
        try:
            # Serialize to JSON
            tables_data = [table.to_dict() for table in tables]
            json_str = json.dumps(tables_data, indent=2)
            
            # Store in project
            project.writeEntry("subsea_cable_tools", TableConfigManager.PROJECT_KEY, json_str)
        except Exception as e:
            print(f"Error saving tables to project: {e}")
    
    @staticmethod
    def load_tables_from_project() -> List[TableConfig]:
        """
        Load table configurations from the current QGIS project.
        
        Returns:
            List of TableConfig objects
        """
        project = QgsProject.instance()
        if not project:
            return []
        
        try:
            json_str, _ = project.readEntry("subsea_cable_tools", TableConfigManager.PROJECT_KEY, "")
            if not json_str:
                return []
            
            tables_data = json.loads(json_str)
            return [TableConfig.from_dict(table) for table in tables_data]
        except (json.JSONDecodeError, KeyError, TypeError):
            return []
    
    @staticmethod
    def save_tables_to_settings(tables: List[TableConfig]):
        """
        Save table configurations to QSettings.
        
        Args:
            tables: List of TableConfig objects to save
        """
        try:
            tables_data = [table.to_dict() for table in tables]
            json_str = json.dumps(tables_data, indent=2)
            
            settings = QSettings()
            settings.setValue(TableConfigManager.SETTINGS_KEY, json_str)
        except Exception as e:
            print(f"Error saving tables to settings: {e}")
    
    @staticmethod
    def load_tables_from_settings() -> List[TableConfig]:
        """
        Load table configurations from QSettings.
        
        Returns:
            List of TableConfig objects
        """
        try:
            settings = QSettings()
            json_str = settings.value(TableConfigManager.SETTINGS_KEY, "")
            
            if not json_str:
                return []
            
            tables_data = json.loads(json_str)
            return [TableConfig.from_dict(table) for table in tables_data]
        except (json.JSONDecodeError, KeyError, TypeError):
            return []
    
    @staticmethod
    def save_tables(tables: List[TableConfig], to_project: bool = True):
        """
        Save tables to project or settings.
        
        Args:
            tables: List of TableConfig objects
            to_project: If True, save to project; if False, save to settings
        """
        if to_project:
            TableConfigManager.save_tables_to_project(tables)
        else:
            TableConfigManager.save_tables_to_settings(tables)
    
    @staticmethod
    def load_tables(from_project: bool = True) -> List[TableConfig]:
        """
        Load tables from project or settings.
        
        Args:
            from_project: If True, load from project; if False, load from settings
            
        Returns:
            List of TableConfig objects
        """
        if from_project:
            tables = TableConfigManager.load_tables_from_project()
            # Fallback to settings if project is empty
            if not tables:
                tables = TableConfigManager.load_tables_from_settings()
        else:
            tables = TableConfigManager.load_tables_from_settings()
        
        return tables


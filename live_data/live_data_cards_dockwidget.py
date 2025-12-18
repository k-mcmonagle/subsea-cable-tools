"""
Live Data Cards Dock Widget

Separate dockable widget for displaying live data cards in grid layout.
Receives updates from LiveDataWorker and manages card display.
"""

from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QMessageBox, QLabel
)
from qgis.PyQt.QtCore import Qt, pyqtSignal, QSize
from typing import Dict

from .card_config import CardConfig
from .card_grid_widget import CardGridWidget
from .card_manager_dialog import CardManagerDialog
from .utils import CardConfigManager


class LiveDataCardsDockWidget(QDockWidget):
    """
    Separate dockable widget for displaying live data cards in grid.
    
    Receives:
    - Card configurations to display
    - Data updates to refresh values
    
    Provides:
    - Grid display of cards
    - Zoom control
    - Add/edit/delete card management
    """
    
    card_added = pyqtSignal(CardConfig)       # New card created
    card_edited = pyqtSignal(CardConfig)      # Existing card modified
    card_deleted = pyqtSignal(str)            # card_id deleted
    
    def __init__(self, parent=None):
        super().__init__("Live Data Cards", parent)
        self.setObjectName("LiveDataCardsDockWidget")
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea | Qt.BottomDockWidgetArea)
        
        self.cards: Dict[str, CardConfig] = {}  # card_id -> config
        self.available_headers: list = []
        self.connected = False
        
        self.setup_ui()
        self.load_cards()
    
    def setup_ui(self):
        """Build the dock widget UI."""
        self.widget = QWidget()
        self.setWidget(self.widget)
        layout = QVBoxLayout(self.widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        # Toolbar
        toolbar_layout = QHBoxLayout()
        toolbar_layout.setContentsMargins(4, 4, 4, 4)
        toolbar_layout.setSpacing(4)
        
        self.add_card_btn = QPushButton("+ Add Card")
        self.add_card_btn.clicked.connect(self.add_card)
        self.add_card_btn.setEnabled(False)  # Disabled until connected
        toolbar_layout.addWidget(self.add_card_btn)
        
        self.status_label = QLabel("No connection")
        self.status_label.setStyleSheet("color: #666666; font-size: 10px;")
        toolbar_layout.addWidget(self.status_label)
        
        toolbar_layout.addStretch()
        
        layout.addLayout(toolbar_layout)
        
        # Grid widget
        self.grid_widget = CardGridWidget()
        self.grid_widget.card_removed.connect(self.on_card_removed_from_grid)
        self.grid_widget.card_edit_requested.connect(self.edit_card)
        self.grid_widget.card_delete_requested.connect(self.on_card_deleted_from_context_menu)
        layout.addWidget(self.grid_widget)
    
    def set_available_headers(self, headers: list):
        """
        Set available data fields (called when headers received).
        
        Args:
            headers: List of field names from data stream
        """
        self.available_headers = headers
        self.set_connected(True)
    
    def set_connected(self, connected: bool):
        """Update connection status."""
        self.connected = connected
        self.add_card_btn.setEnabled(connected and len(self.available_headers) > 0)
        
        if connected:
            self.status_label.setText(f"Connected ({len(self.available_headers)} fields)")
            self.status_label.setStyleSheet("color: #00AA00; font-size: 10px;")
        else:
            self.status_label.setText("Disconnected")
            self.status_label.setStyleSheet("color: #666666; font-size: 10px;")
    
    def add_card(self):
        """Open dialog to add new card."""
        if not self.available_headers:
            QMessageBox.warning(self, "No Data", "Connect to data stream first")
            return
        
        dialog = CardManagerDialog(self.available_headers, parent=self)
        dialog.card_configured.connect(self.on_card_configured)
        dialog.exec_()
    
    def edit_card(self, card_id: str):
        """Open dialog to edit existing card."""
        if card_id not in self.cards:
            return
        
        config = self.cards[card_id]
        dialog = CardManagerDialog(self.available_headers, existing_config=config, parent=self)
        dialog.card_configured.connect(self.on_card_configured)
        dialog.exec_()
    
    def on_card_configured(self, config: CardConfig):
        """Handle card created or edited."""
        # Check if editing existing card
        is_new = config.card_id not in self.cards
        
        # Update our dict
        self.cards[config.card_id] = config
        
        if is_new:
            # Add to grid
            card_widget = self.grid_widget.add_card(config)
            self.card_added.emit(config)
        else:
            # Update existing widget in grid
            card_widget = self.grid_widget.get_card(config.card_id)
            if card_widget:
                # Recreate widget with new config
                self.grid_widget.remove_card(config.card_id)
                card_widget = self.grid_widget.add_card(config)
            self.card_edited.emit(config)
        
        # Save to project
        self.save_cards()
    
    def on_card_removed_from_grid(self, card_id: str):
        """Handle card deleted from grid (right-click delete)."""
        if card_id in self.cards:
            del self.cards[card_id]
            self.save_cards()
            self.card_deleted.emit(card_id)
    
    def on_card_deleted_from_context_menu(self, card_id: str):
        """Handle card deletion from context menu."""
        # Show confirmation dialog
        reply = QMessageBox.question(
            self, 
            "Delete Card",
            f"Are you sure you want to delete this card?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            self.on_card_removed_from_grid(card_id)
    
    def update_card_value(self, card_id: str, value):
        """
        Update card value from data stream.
        
        Args:
            card_id: ID of card to update
            value: New value to display
        """
        self.grid_widget.update_card_value(card_id, value)
    
    def update_all_cards(self, data_dict: dict):
        """
        Update all cards with values from data dictionary.
        
        Args:
            data_dict: Dictionary mapping field_name -> value
        """
        for card_id, config in self.cards.items():
            field_name = config.field_name
            value = data_dict.get(field_name)
            if value is not None:
                self.update_card_value(card_id, value)
    
    def display_loaded_cards(self):
        """
        Display previously saved cards (call after headers received).
        Only shows cards with valid field references.
        """
        if not self.available_headers:
            return
        
        for config in self.cards.values():
            # Check field exists
            if config.field_name in self.available_headers:
                self.grid_widget.add_card(config)
    
    def save_cards(self):
        """Save card configs to project."""
        cards_list = list(self.cards.values())
        CardConfigManager.save_cards_to_project(cards_list)
    
    def load_cards(self):
        """Load saved card configs from project."""
        loaded = CardConfigManager.load_cards_from_project()
        for config in loaded:
            self.cards[config.card_id] = config
    
    def clear_all_cards(self):
        """Clear all cards from display and storage."""
        self.grid_widget.clear_all_cards()
        self.cards.clear()
        self.save_cards()
    
    def closeEvent(self, event):
        """
        Handle widget close request.
        Hide instead of closing to allow reopening via checkbox.
        """
        # Save current state
        self.save_cards()
        # Hide the widget instead of closing it
        self.hide()
        event.ignore()  # Don't actually close/destroy the widget
    
    def force_cleanup(self):
        """
        Force cleanup of all resources.
        Called during plugin unload.
        """
        try:
            print("DEBUG: LiveDataCardsDockWidget.force_cleanup() called")
            # Block all signals
            self.blockSignals(True)
            # Clear cards
            self.clear_all_cards()
            print("DEBUG: LiveDataCardsDockWidget cleanup complete")
        except Exception as e:
            print(f"DEBUG: Error during LiveDataCardsDockWidget cleanup: {e}")

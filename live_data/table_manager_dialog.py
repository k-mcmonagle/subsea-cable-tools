"""
Live Data Table Manager Dialog

Dialog for creating and editing live data table configurations.
Allows selection and ordering of fields to display.
"""

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, 
    QListWidget, QListWidgetItem, QGroupBox, QMessageBox, QSpinBox
)
from qgis.PyQt.QtCore import Qt, pyqtSignal, QMimeData
from qgis.PyQt.QtGui import QIcon, QDrag
import uuid

from .table_config import TableConfig, TableFieldConfig
from .table_field_editor import TableFieldEditorDialog
from typing import List


class LiveDataTableManagerDialog(QDialog):
    """
    Dialog for creating and editing live data table configurations.
    
    Features:
    - Select which fields to include in the table
    - Drag-to-reorder fields
    - Set display names and units
    - Configure decimal places
    - Preview table configuration
    """
    
    table_configured = pyqtSignal(TableConfig)
    
    def __init__(self, available_headers: List[str], existing_config: TableConfig = None, parent=None):
        super().__init__(parent)
        self.available_headers = available_headers
        self.existing_config = existing_config
        self.selected_fields: List[TableFieldConfig] = []
        
        self.setWindowTitle("Configure Live Data Table")
        self.setMinimumWidth(600)
        self.setMinimumHeight(500)
        
        self.setup_ui()
        
        if existing_config:
            self.load_existing_config()
    
    def setup_ui(self):
        """Build the dialog UI."""
        layout = QVBoxLayout(self)
        
        # Name input
        name_layout = QHBoxLayout()
        name_layout.addWidget(QLabel("Table Name:"))
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("e.g., 'Vessel Status'")
        name_layout.addWidget(self.name_input)
        layout.addLayout(name_layout)
        
        # Fields selection
        fields_group = QGroupBox("Select Fields to Display (drag to reorder)")
        fields_layout = QVBoxLayout(fields_group)
        
        # Available fields list
        available_label = QLabel("Available Fields:")
        fields_layout.addWidget(available_label)
        
        self.available_list = QListWidget()
        self.available_list.setMaximumHeight(120)
        self.available_list.setDragDropMode(QListWidget.DragOnly)
        self.available_list.model().rowsMoved.connect(self.on_fields_reordered)
        
        for header in self.available_headers:
            item = QListWidgetItem(header)
            item.setFlags(item.flags() | Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsDragEnabled)
            self.available_list.addItem(item)
        
        fields_layout.addWidget(self.available_list)
        
        # Add button
        add_layout = QHBoxLayout()
        self.add_btn = QPushButton("Add Selected ➜")
        self.add_btn.clicked.connect(self.add_selected_field)
        add_layout.addStretch()
        add_layout.addWidget(self.add_btn)
        add_layout.addStretch()
        fields_layout.addLayout(add_layout)
        
        # Selected fields list
        selected_label = QLabel("Table Fields (drag to reorder, right-click to remove):")
        fields_layout.addWidget(selected_label)
        
        self.selected_list = QListWidget()
        self.selected_list.setDragDropMode(QListWidget.InternalMove)
        self.selected_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.selected_list.model().rowsMoved.connect(self.on_fields_reordered)
        self.selected_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.selected_list.customContextMenuRequested.connect(self.show_field_context_menu)
        
        fields_layout.addWidget(self.selected_list)
        
        layout.addWidget(fields_group)
        
        # Buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        
        self.ok_btn = QPushButton("OK")
        self.ok_btn.clicked.connect(self.accept)
        button_layout.addWidget(self.ok_btn)
        
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(self.cancel_btn)
        
        layout.addLayout(button_layout)
    
    def add_selected_field(self):
        """Add selected field from available list to table fields."""
        selected_items = self.available_list.selectedItems()
        if not selected_items:
            QMessageBox.information(self, "Select Field", "Please select a field to add.")
            return
        
        for item in selected_items:
            field_name = item.text()
            
            # Check if already added
            existing_fields = [fc.field_name for fc in self.selected_fields]
            if field_name in existing_fields:
                QMessageBox.warning(
                    self, 
                    "Field Already Added", 
                    f"'{field_name}' is already in the table."
                )
                continue
            
            # Create field config
            field_config = TableFieldConfig(
                field_name=field_name,
                display_name=field_name,
                unit="",
                decimal_places=-1,
                order=len(self.selected_fields),
                enabled=True
            )
            self.selected_fields.append(field_config)
            
            # Add to list widget
            item = QListWidgetItem(field_name)
            item.setFlags(item.flags() | Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsDragEnabled)
            self.selected_list.addItem(item)
    
    def show_field_context_menu(self, position):
        """Show context menu to edit or remove field."""
        item = self.selected_list.itemAt(position)
        if not item:
            return
        
        from qgis.PyQt.QtWidgets import QMenu
        menu = QMenu(self)
        
        edit_action = menu.addAction("Edit Field Properties")
        edit_action.triggered.connect(lambda: self.edit_field(item))
        
        remove_action = menu.addAction("Remove Field")
        remove_action.triggered.connect(lambda: self.remove_field(item))
        
        menu.exec_(self.selected_list.mapToGlobal(position))
    
    def edit_field(self, item):
        """Open dialog to edit field properties."""
        row = self.selected_list.row(item)
        if row < 0 or row >= len(self.selected_fields):
            return
        
        field_config = self.selected_fields[row]
        editor = TableFieldEditorDialog(field_config, parent=self)
        if editor.exec_() == QDialog.Accepted:
            # Update the list item display name if it changed
            self.selected_list.item(row).setText(
                field_config.display_name or field_config.field_name
            )
    
    def remove_field(self, item):
        """Remove a field from the selected list."""
        row = self.selected_list.row(item)
        if row >= 0:
            self.selected_list.takeItem(row)
            if row < len(self.selected_fields):
                del self.selected_fields[row]
            self.on_fields_reordered()
    
    def on_fields_reordered(self):
        """Update field order after reordering - preserves field properties."""
        # Create a map of field_name -> field_config for lookup
        field_map = {fc.field_name: fc for fc in self.selected_fields}
        
        # Rebuild selected_fields list based on current order in list widget
        new_fields = []
        for row in range(self.selected_list.count()):
            item = self.selected_list.item(row)
            field_name = item.text()
            
            # Get existing config if available, otherwise create new one
            if field_name in field_map:
                field_config = field_map[field_name]
            else:
                # This shouldn't happen, but create a default just in case
                field_config = TableFieldConfig(
                    field_name=field_name,
                    display_name=field_name,
                    unit="",
                    decimal_places=-1,
                    order=row,
                    enabled=True
                )
            
            # Update order
            field_config.order = row
            new_fields.append(field_config)
        
        self.selected_fields = new_fields
    
    def load_existing_config(self):
        """Load existing table configuration."""
        self.name_input.setText(self.existing_config.name)
        
        # Load selected fields
        for field_config in self.existing_config.get_enabled_fields():
            item = QListWidgetItem(field_config.field_name)
            item.setFlags(item.flags() | Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsDragEnabled)
            self.selected_list.addItem(item)
            self.selected_fields.append(field_config)
    
    def accept(self):
        """Validate and accept the dialog."""
        name = self.name_input.text().strip()
        if not name:
            QMessageBox.warning(self, "Missing Name", "Please enter a table name.")
            return
        
        if not self.selected_fields:
            QMessageBox.warning(
                self, 
                "No Fields Selected", 
                "Please add at least one field to the table."
            )
            return
        
        # Create or update config
        if self.existing_config:
            config = self.existing_config
            config.name = name
            config.field_configs = self.selected_fields
        else:
            config = TableConfig(
                table_id=str(uuid.uuid4()),
                name=name,
                field_configs=self.selected_fields
            )
        
        self.table_configured.emit(config)
        super().accept()

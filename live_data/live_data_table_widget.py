"""
Live Data Table Widget

Displays live data in a compact table format with multiple fields.
Shows Field Name, Value, and Unit in an easy-to-read table.
"""

from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem, QMenu, QHeaderView,
    QInputDialog, QDialog
)
from qgis.PyQt.QtCore import Qt, pyqtSignal, QTimer
from qgis.PyQt.QtGui import QFont, QColor, QBrush

from .table_config import TableConfig, TableFieldConfig
from typing import Dict, Any, Optional, List
import time


class LiveDataTableWidget(QWidget):
    """
    Display widget for live data in table format.
    
    Shows multiple fields in a compact table with:
    - Column headers: Field Name, Value, Unit
    - One row per field
    - Real-time value updates
    - Right-click context menu for edit/delete
    """
    
    table_edit_requested = pyqtSignal(str)  # table_id
    table_delete_requested = pyqtSignal(str)  # table_id
    
    def __init__(self, config: TableConfig, parent=None):
        super().__init__(parent)
        self.config = config
        self.current_values: Dict[str, Any] = {}
        self.last_update_time = time.time()
        self.min_update_interval = 0.1  # 100ms minimum between updates
        
        self.setup_ui()
        self.apply_styling()
        
    def setup_ui(self):
        """Build the table UI layout."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        
        # Create table widget
        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["Field", "Value", "Unit"])
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        
        # Set column widths
        self.table.setColumnWidth(0, 120)  # Field name
        self.table.setColumnWidth(1, 100)  # Value
        self.table.setColumnWidth(2, 60)   # Unit
        
        # Hide headers if configured
        if not self.config.show_headers:
            self.table.horizontalHeader().hide()
        
        # Set row height
        self.table.verticalHeader().setDefaultSectionSize(self.config.styling.row_height)
        self.table.verticalHeader().hide()
        
        # Add rows for each enabled field
        enabled_fields = self.config.get_enabled_fields()
        self.table.setRowCount(len(enabled_fields))
        
        for row, field_config in enumerate(enabled_fields):
            # Field name
            field_item = QTableWidgetItem(
                field_config.display_name or field_config.field_name
            )
            field_item.setFlags(field_item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row, 0, field_item)
            
            # Value (will be updated)
            value_item = QTableWidgetItem("--")
            value_item.setFlags(value_item.flags() & ~Qt.ItemIsEditable)
            value_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, 1, value_item)
            
            # Unit
            unit_item = QTableWidgetItem(field_config.unit)
            unit_item.setFlags(unit_item.flags() & ~Qt.ItemIsEditable)
            unit_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, 2, unit_item)
        
        # Make table read-only - we'll allow editing through context menu
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        
        # Enable context menu
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.show_context_menu)
        
        # Connect cell double-click to allow quick unit editing
        self.table.cellDoubleClicked.connect(self.on_cell_double_clicked)
        
        layout.addWidget(self.table)
        
        # Stretch to fill available space
        layout.setStretchFactor(self.table, 1)
        
        # Set minimum size
        self.setMinimumWidth(300)
        self.setMinimumHeight(100)
    
    def apply_styling(self):
        """Apply styling from config."""
        styling = self.config.styling
        
        # Set up stylesheet
        stylesheet = f"""
            QTableWidget {{
                background-color: {styling.row_background};
                alternate-background-color: {styling.row_alternate_background};
                border: 1px solid {styling.border_color};
            }}
            QTableWidget::item {{
                padding: 4px;
                border-bottom: 1px solid {styling.border_color};
                color: {styling.row_text_color};
            }}
            QHeaderView::section {{
                background-color: {styling.header_background};
                color: {styling.header_text_color};
                padding: 4px;
                border: 1px solid {styling.border_color};
                font-weight: bold;
            }}
        """
        
        self.table.setStyleSheet(stylesheet)
        self.table.setAlternatingRowColors(self.config.alternating_rows)
        
        # Set font size
        font = QFont()
        font.setPointSize(styling.font_size)
        self.table.setFont(font)
    
    def update_value(self, field_name: str, value: Any) -> None:
        """
        Update a field value in the table.
        
        Args:
            field_name: Name of the field to update
            value: New value to display
        """
        # Store value
        self.current_values[field_name] = value
        
        # Find and update row
        enabled_fields = self.config.get_enabled_fields()
        for row, field_config in enumerate(enabled_fields):
            if field_config.field_name == field_name:
                # Format value
                if value is None:
                    formatted = "--"
                else:
                    try:
                        if isinstance(value, str):
                            formatted = value.strip() if value.strip() else "--"
                        else:
                            # Numeric value
                            if field_config.decimal_places == -1:
                                formatted = f"{float(value):.3g}"
                            else:
                                formatted = f"{float(value):.{field_config.decimal_places}f}"
                    except (ValueError, TypeError):
                        formatted = "--"
                
                # Update cell
                item = self.table.item(row, 1)
                if item:
                    item.setText(formatted)
                break
    
    def update_all_values(self, data_dict: Dict[str, Any]) -> None:
        """
        Update multiple field values at once.
        
        Args:
            data_dict: Dictionary mapping field_name -> value
        """
        # Throttle updates
        current_time = time.time()
        if current_time - self.last_update_time < self.min_update_interval:
            return
        
        for field_name, value in data_dict.items():
            self.update_value(field_name, value)
        
        self.last_update_time = current_time
    
    def get_row_for_field(self, field_name: str) -> Optional[int]:
        """Get the row number for a field."""
        enabled_fields = self.config.get_enabled_fields()
        for row, field_config in enumerate(enabled_fields):
            if field_config.field_name == field_name:
                return row
        return None
    
    def show_context_menu(self, position):
        """Show context menu on right-click."""
        item = self.table.itemAt(position)
        if not item:
            return
        
        row = self.table.row(item)
        col = self.table.column(item)
        
        menu = QMenu(self)
        
        # If clicking on Unit column, offer quick edit
        if col == 2:  # Unit column
            edit_unit_action = menu.addAction("Edit Unit")
            edit_unit_action.triggered.connect(lambda: self.quick_edit_unit(row))
            menu.addSeparator()
        
        # Get field name for this row
        if col >= 0 and row >= 0:
            enabled_fields = self.config.get_enabled_fields()
            if row < len(enabled_fields):
                field_config = enabled_fields[row]
                
                edit_field_action = menu.addAction("Edit Field Properties...")
                edit_field_action.triggered.connect(lambda: self.edit_field_properties(row))
        
        menu.addSeparator()
        
        edit_action = menu.addAction("Edit Table")
        edit_action.triggered.connect(lambda: self.table_edit_requested.emit(self.config.table_id))
        
        delete_action = menu.addAction("Delete Table")
        delete_action.triggered.connect(lambda: self.table_delete_requested.emit(self.config.table_id))
        
        menu.exec_(self.table.mapToGlobal(position))
    
    def on_cell_double_clicked(self, row: int, col: int):
        """Handle double-click on cell - allow unit editing."""
        if col == 2:  # Unit column
            self.quick_edit_unit(row)
    
    def quick_edit_unit(self, row: int):
        """Quick edit the unit for a field."""
        if row < 0:
            return
        
        enabled_fields = self.config.get_enabled_fields()
        if row >= len(enabled_fields):
            return
        
        field_config = enabled_fields[row]
        current_unit = field_config.unit
        
        # Show input dialog
        new_unit, ok = QInputDialog.getText(
            self,
            "Edit Unit",
            f"Enter unit for '{field_config.display_name or field_config.field_name}':",
            text=current_unit
        )
        
        if ok:
            field_config.unit = new_unit.strip()
            # Update cell display
            unit_item = self.table.item(row, 2)
            if unit_item:
                unit_item.setText(field_config.unit)
    
    def edit_field_properties(self, row: int):
        """Open full field editor for field properties."""
        if row < 0:
            return
        
        enabled_fields = self.config.get_enabled_fields()
        if row >= len(enabled_fields):
            return
        
        field_config = enabled_fields[row]
        
        # Import and show the field editor dialog
        from .table_field_editor import TableFieldEditorDialog
        editor = TableFieldEditorDialog(field_config, parent=self)
        if editor.exec_() == QDialog.Accepted:
            # Update table display to reflect changes
            display_item = self.table.item(row, 0)
            if display_item:
                display_item.setText(field_config.display_name or field_config.field_name)
            unit_item = self.table.item(row, 2)
            if unit_item:
                unit_item.setText(field_config.unit)
    
    def set_field_value_alignment(self, alignment: Qt.Alignment = Qt.AlignCenter) -> None:
        """Set alignment of value column."""
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 1)
            if item:
                item.setTextAlignment(alignment)
    
    def highlight_field(self, field_name: str, highlight: bool = True) -> None:
        """Highlight a field row (e.g., for warnings)."""
        row = self.get_row_for_field(field_name)
        if row is not None:
            for col in range(3):
                item = self.table.item(row, col)
                if item:
                    if highlight:
                        item.setBackground(QBrush(QColor("#FFCCCC")))
                    else:
                        # Reset to styling color
                        if self.config.alternating_rows and row % 2:
                            item.setBackground(QBrush(
                                QColor(self.config.styling.row_alternate_background)
                            ))
                        else:
                            item.setBackground(QBrush(
                                QColor(self.config.styling.row_background)
                            ))
    
    def get_config(self) -> TableConfig:
        """Get the table configuration."""
        return self.config
    
    def set_config(self, config: TableConfig) -> None:
        """Update configuration and refresh display."""
        self.config = config
        # Rebuild table UI
        self.table.setParent(None)
        self.setup_ui()
        self.apply_styling()

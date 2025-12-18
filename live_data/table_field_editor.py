"""
Live Data Table Field Editor Dialog

Dialog for editing individual field properties in a table configuration.
Allows users to set display name, unit, and decimal places.
"""

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QSpinBox, QPushButton, QMessageBox
)
from qgis.PyQt.QtCore import Qt, pyqtSignal

from .table_config import TableFieldConfig


class TableFieldEditorDialog(QDialog):
    """
    Dialog for editing a field's properties.
    
    Allows editing:
    - Display Name: Custom name to show in table header
    - Unit: Unit of measurement
    - Decimal Places: Number of decimal places (-1 for auto)
    """
    
    field_edited = pyqtSignal(TableFieldConfig)
    
    def __init__(self, field_config: TableFieldConfig, parent=None):
        super().__init__(parent)
        self.field_config = field_config
        
        self.setWindowTitle(f"Edit Field: {field_config.field_name}")
        self.setMinimumWidth(400)
        self.setup_ui()
        
    def setup_ui(self):
        """Build the dialog UI."""
        layout = QVBoxLayout(self)
        
        # Field name (read-only)
        field_layout = QHBoxLayout()
        field_layout.addWidget(QLabel("Field Name:"))
        field_display = QLineEdit()
        field_display.setText(self.field_config.field_name)
        field_display.setReadOnly(True)
        field_layout.addWidget(field_display)
        layout.addLayout(field_layout)
        
        # Display name
        display_layout = QHBoxLayout()
        display_layout.addWidget(QLabel("Display Name:"))
        self.display_name_input = QLineEdit()
        self.display_name_input.setText(self.field_config.display_name or self.field_config.field_name)
        self.display_name_input.setToolTip("Custom name to show in table header (leave empty to use field name)")
        display_layout.addWidget(self.display_name_input)
        layout.addLayout(display_layout)
        
        # Unit
        unit_layout = QHBoxLayout()
        unit_layout.addWidget(QLabel("Unit:"))
        self.unit_input = QLineEdit()
        self.unit_input.setText(self.field_config.unit or "")
        self.unit_input.setPlaceholderText("e.g., m, kg, °C")
        self.unit_input.setToolTip("Unit of measurement to display in table")
        unit_layout.addWidget(self.unit_input)
        layout.addLayout(unit_layout)
        
        # Decimal places
        decimal_layout = QHBoxLayout()
        decimal_layout.addWidget(QLabel("Decimal Places:"))
        self.decimal_spinbox = QSpinBox()
        self.decimal_spinbox.setMinimum(-1)
        self.decimal_spinbox.setMaximum(10)
        self.decimal_spinbox.setValue(self.field_config.decimal_places)
        self.decimal_spinbox.setToolTip("-1 = auto (3 significant figures), 0+ = fixed decimal places")
        decimal_layout.addWidget(self.decimal_spinbox)
        decimal_layout.addStretch()
        layout.addLayout(decimal_layout)
        
        # Buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        
        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(self.accept)
        button_layout.addWidget(ok_btn)
        
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(cancel_btn)
        
        layout.addStretch()
        layout.addLayout(button_layout)
    
    def accept(self):
        """Accept changes and emit signal."""
        # Update field config
        self.field_config.display_name = self.display_name_input.text().strip() or self.field_config.field_name
        self.field_config.unit = self.unit_input.text().strip()
        self.field_config.decimal_places = self.decimal_spinbox.value()
        
        self.field_edited.emit(self.field_config)
        super().accept()

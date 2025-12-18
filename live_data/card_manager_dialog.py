"""
Card Manager Dialog

UI for creating, editing, and configuring live data cards.
Allows users to select fields, set formatting, and customize appearance.
"""

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QSpinBox, QDoubleSpinBox,
    QComboBox, QPushButton, QCheckBox, QColorDialog, QFormLayout, QGroupBox,
    QMessageBox, QTabWidget, QWidget
)
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QIcon
import uuid

from .card_config import CardConfig


class CardManagerDialog(QDialog):
    """
    Dialog for creating and editing card configurations.
    Provides UI for field selection, formatting, styling, and validation.
    """
    
    card_configured = pyqtSignal(CardConfig)  # Emitted when card is saved
    
    def __init__(self, available_fields: list, existing_config: CardConfig = None, parent=None):
        """
        Initialize the card manager dialog.
        
        Args:
            available_fields: List of available field names from data stream
            existing_config: If editing, the CardConfig to edit. If None, create new card.
            parent: Parent widget
        """
        super().__init__(parent)
        self.available_fields = available_fields
        self.existing_config = existing_config
        self.is_editing = existing_config is not None
        
        self.setWindowTitle("Edit Card" if self.is_editing else "New Card")
        self.setModal(True)
        self.setMinimumWidth(500)
        self.setMinimumHeight(400)
        
        self.setup_ui()
        if self.is_editing:
            self.load_config(existing_config)
    
    def setup_ui(self):
        """Build the dialog UI."""
        layout = QVBoxLayout(self)
        
        # Create tabs
        tabs = QTabWidget()
        
        # Basic tab
        basic_widget = self.create_basic_tab()
        tabs.addTab(basic_widget, "Basic")
        
        # Formatting tab
        format_widget = self.create_format_tab()
        tabs.addTab(format_widget, "Format")
        
        # Styling tab
        style_widget = self.create_style_tab()
        tabs.addTab(style_widget, "Styling")
        
        # Advanced tab
        advanced_widget = self.create_advanced_tab()
        tabs.addTab(advanced_widget, "Advanced")
        
        layout.addWidget(tabs)
        
        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        
        self.ok_btn = QPushButton("OK")
        self.ok_btn.clicked.connect(self.validate_and_save)
        btn_layout.addWidget(self.ok_btn)
        
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        
        layout.addLayout(btn_layout)
    
    def create_basic_tab(self) -> QWidget:
        """Create the Basic settings tab."""
        widget = QWidget()
        main_layout = QVBoxLayout(widget)
        layout = QFormLayout()
        
        # Card name
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g., Ship Depth")
        layout.addRow("Card Name:", self.name_edit)
        
        # Field selection
        self.field_combo = QComboBox()
        self.field_combo.addItems(self.available_fields)
        layout.addRow("Data Field:", self.field_combo)
        
        # Field type
        self.field_type_combo = QComboBox()
        self.field_type_combo.addItems(["numeric", "string"])
        self.field_type_combo.setToolTip("Select 'numeric' for numbers with formatting, 'string' for text values")
        layout.addRow("Field Type:", self.field_type_combo)
        
        # Unit (disabled for string fields)
        self.unit_edit = QLineEdit()
        self.unit_edit.setPlaceholderText("e.g., m, °, knots")
        layout.addRow("Unit:", self.unit_edit)
        
        # Update unit field enabled state when field type changes
        self.field_type_combo.currentTextChanged.connect(self.on_field_type_changed)
        
        main_layout.addLayout(layout)
        main_layout.addStretch()
        return widget
    
    def on_field_type_changed(self, field_type: str):
        """Handle field type change - disable unit for string fields."""
        self.unit_edit.setEnabled(field_type == "numeric")
    
    def create_format_tab(self) -> QWidget:
        """Create the Format settings tab."""
        widget = QWidget()
        main_layout = QVBoxLayout(widget)
        layout = QFormLayout()
        
        # Decimal places
        self.decimal_spinbox = QSpinBox()
        self.decimal_spinbox.setMinimum(-1)
        self.decimal_spinbox.setMaximum(10)
        self.decimal_spinbox.setValue(-1)
        self.decimal_spinbox.setToolTip("-1 = Auto format (3 significant figures)")
        layout.addRow("Decimal Places:", self.decimal_spinbox)
        
        # Prefix
        self.prefix_edit = QLineEdit()
        self.prefix_edit.setPlaceholderText("Optional text before value")
        layout.addRow("Prefix:", self.prefix_edit)
        
        # Suffix
        self.suffix_edit = QLineEdit()
        self.suffix_edit.setPlaceholderText("Optional text after value")
        layout.addRow("Suffix:", self.suffix_edit)
        
        # Font size
        self.font_size_spinbox = QSpinBox()
        self.font_size_spinbox.setMinimum(8)
        self.font_size_spinbox.setMaximum(48)
        self.font_size_spinbox.setValue(12)
        layout.addRow("Font Size (pt):", self.font_size_spinbox)
        
        main_layout.addLayout(layout)
        main_layout.addStretch()
        return widget
    
    def create_style_tab(self) -> QWidget:
        """Create the Styling settings tab."""
        widget = QWidget()
        main_layout = QVBoxLayout(widget)
        layout = QFormLayout()
        
        # Text color
        text_color_layout = QHBoxLayout()
        self.text_color_label = QLabel("█████")
        self.text_color_label.setFixedWidth(40)
        self.text_color_btn = QPushButton("Choose...")
        self.text_color_btn.clicked.connect(self.choose_text_color)
        text_color_layout.addWidget(self.text_color_label)
        text_color_layout.addWidget(self.text_color_btn)
        text_color_layout.addStretch()
        layout.addRow("Text Color:", text_color_layout)
        
        # Background color
        bg_color_layout = QHBoxLayout()
        self.bg_color_label = QLabel("█████")
        self.bg_color_label.setFixedWidth(40)
        self.bg_color_btn = QPushButton("Choose...")
        self.bg_color_btn.clicked.connect(self.choose_bg_color)
        bg_color_layout.addWidget(self.bg_color_label)
        bg_color_layout.addWidget(self.bg_color_btn)
        bg_color_layout.addStretch()
        layout.addRow("Background Color:", bg_color_layout)
        
        main_layout.addLayout(layout)
        main_layout.addStretch()
        return widget
    
    def create_advanced_tab(self) -> QWidget:
        """Create the Advanced settings tab."""
        widget = QWidget()
        main_layout = QVBoxLayout(widget)
        layout = QFormLayout()
        
        # Warning minimum
        self.warn_min_spinbox = QDoubleSpinBox()
        self.warn_min_spinbox.setMinimum(-999999.0)
        self.warn_min_spinbox.setMaximum(999999.0)
        self.warn_min_spinbox.setDecimals(2)
        self.warn_min_spinbox.setValue(-999999.0)
        layout.addRow("Warning Min (disabled if -999999):", self.warn_min_spinbox)
        
        # Warning maximum
        self.warn_max_spinbox = QDoubleSpinBox()
        self.warn_max_spinbox.setMinimum(-999999.0)
        self.warn_max_spinbox.setMaximum(999999.0)
        self.warn_max_spinbox.setDecimals(2)
        self.warn_max_spinbox.setValue(999999.0)
        layout.addRow("Warning Max (disabled if 999999):", self.warn_max_spinbox)
        
        # Warning color
        warn_color_layout = QHBoxLayout()
        self.warn_color_label = QLabel("█████")
        self.warn_color_label.setFixedWidth(40)
        self.warn_color_btn = QPushButton("Choose...")
        self.warn_color_btn.clicked.connect(self.choose_warn_color)
        warn_color_layout.addWidget(self.warn_color_label)
        warn_color_layout.addWidget(self.warn_color_btn)
        warn_color_layout.addStretch()
        layout.addRow("Warning Color:", warn_color_layout)
        
        # Alert on change
        self.alert_on_change_chk = QCheckBox("Animate on value change")
        layout.addRow(self.alert_on_change_chk)
        
        # Enabled
        self.enabled_chk = QCheckBox("Enabled")
        self.enabled_chk.setChecked(True)
        layout.addRow(self.enabled_chk)
        
        main_layout.addLayout(layout)
        main_layout.addStretch()
        return widget
    
    def choose_text_color(self):
        """Open color picker for text color."""
        color = QColorDialog.getColor(
            QColor(self.get_text_color()),
            self,
            "Choose Text Color"
        )
        if color.isValid():
            self.set_text_color(color.name())
    
    def choose_bg_color(self):
        """Open color picker for background color."""
        color = QColorDialog.getColor(
            QColor(self.get_bg_color()),
            self,
            "Choose Background Color"
        )
        if color.isValid():
            self.set_bg_color(color.name())
    
    def choose_warn_color(self):
        """Open color picker for warning color."""
        color = QColorDialog.getColor(
            QColor(self.get_warn_color()),
            self,
            "Choose Warning Color"
        )
        if color.isValid():
            self.set_warn_color(color.name())
    
    def get_text_color(self) -> str:
        """Get current text color hex."""
        return self.text_color_label.text() if hasattr(self, '_text_color') else "#000000"
    
    def set_text_color(self, hex_color: str):
        """Set text color and update UI."""
        self._text_color = hex_color
        self.text_color_label.setStyleSheet(f"color: {hex_color};")
    
    def get_bg_color(self) -> str:
        """Get current background color hex."""
        return self._bg_color if hasattr(self, '_bg_color') else "#FFFFFF"
    
    def set_bg_color(self, hex_color: str):
        """Set background color and update UI."""
        self._bg_color = hex_color
        self.bg_color_label.setStyleSheet(f"background-color: {hex_color};")
    
    def get_warn_color(self) -> str:
        """Get current warning color hex."""
        return self._warn_color if hasattr(self, '_warn_color') else "#FF6600"
    
    def set_warn_color(self, hex_color: str):
        """Set warning color and update UI."""
        self._warn_color = hex_color
        self.warn_color_label.setStyleSheet(f"background-color: {hex_color};")
    
    def load_config(self, config: CardConfig):
        """Load existing card configuration into UI."""
        self.name_edit.setText(config.name)
        
        idx = self.field_combo.findText(config.field_name)
        if idx >= 0:
            self.field_combo.setCurrentIndex(idx)
        
        # Load field type
        type_idx = self.field_type_combo.findText(config.field_type)
        if type_idx >= 0:
            self.field_type_combo.setCurrentIndex(type_idx)
        
        self.unit_edit.setText(config.unit)
        self.decimal_spinbox.setValue(config.decimal_places)
        self.prefix_edit.setText(config.prefix)
        self.suffix_edit.setText(config.suffix)
        self.font_size_spinbox.setValue(config.font_size)
        
        self.set_text_color(config.text_color)
        self.set_bg_color(config.background_color)
        self.set_warn_color(config.warning_color)
        
        warn_min = config.warning_min if config.warning_min is not None else -999999.0
        warn_max = config.warning_max if config.warning_max is not None else 999999.0
        self.warn_min_spinbox.setValue(warn_min)
        self.warn_max_spinbox.setValue(warn_max)
        
        self.alert_on_change_chk.setChecked(config.alert_on_change)
        self.enabled_chk.setChecked(config.enabled)
    
    def validate_and_save(self):
        """Validate configuration and save if valid."""
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Validation Error", "Please enter a card name")
            return
        
        field = self.field_combo.currentText()
        if not field:
            QMessageBox.warning(self, "Validation Error", "Please select a data field")
            return
        
        # Create configuration
        card_id = self.existing_config.card_id if self.is_editing else str(uuid.uuid4())
        
        warn_min = self.warn_min_spinbox.value()
        warn_max = self.warn_max_spinbox.value()
        
        # Disable warnings if using default values
        if warn_min <= -999998:
            warn_min = None
        if warn_max >= 999998:
            warn_max = None
        
        config = CardConfig(
            card_id=card_id,
            name=name,
            field_name=field,
            field_type=self.field_type_combo.currentText(),
            unit=self.unit_edit.text(),
            decimal_places=self.decimal_spinbox.value(),
            prefix=self.prefix_edit.text(),
            suffix=self.suffix_edit.text(),
            enabled=self.enabled_chk.isChecked(),
            text_color=self.get_text_color(),
            background_color=self.get_bg_color(),
            warning_min=warn_min,
            warning_max=warn_max,
            warning_color=self.get_warn_color(),
            alert_on_change=self.alert_on_change_chk.isChecked(),
            font_size=self.font_size_spinbox.value()
        )
        
        # Validate
        is_valid, error_msg = config.validate(self.available_fields)
        if not is_valid:
            QMessageBox.warning(self, "Validation Error", error_msg)
            return
        
        self.card_configured.emit(config)
        self.accept()

# -*- coding: utf-8 -*-
"""Compact import and processing editors for acquired burial data."""
from __future__ import annotations

import copy
import json

from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                                QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
                                QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout)

from ..laydata import burial_data as data


def combo(values, current=''):
    box = QComboBox()
    box.setEditable(True)
    box.addItems(values)
    if current:
        box.setCurrentText(str(current))
    return box


def spin(value, minimum=0, maximum=1000000, decimals=3):
    box = QDoubleSpinBox()
    box.setDecimals(decimals)
    box.setRange(minimum, maximum)
    box.setValue(float(value))
    return box


def buttons(dialog, layout):
    enum = getattr(QDialogButtonBox, 'StandardButton', QDialogButtonBox)
    box = QDialogButtonBox(enum.Ok | enum.Cancel)
    box.accepted.connect(dialog.accept)
    box.rejected.connect(dialog.reject)
    layout.addWidget(box)


class MappingDialog(QDialog):
    def __init__(self, headers, name, parent=None, spec=None, csv_content=None):
        super().__init__(parent)
        self.setWindowTitle('Import acquired burial data')
        self.resize(670, 710)
        self.headers, self.csv_content = headers, csv_content
        spec = spec or {}
        self._mapping = spec.get('mapping', {})
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.source = QLineEdit(name)
        self.source.setToolTip('Logical delivery identity. Reuse this name for corrected versions of the same delivery; use another name for new daily files.')
        self.supplier = QLineEdit(spec.get('supplier', ''))
        self.definition = combo(['Depressor position', 'Cable tracker burial depth', 'Depth of Cover'], spec.get('definition', 'Depressor position'))
        self.label = combo(['Burial Depth', 'Depth of Cover'], spec.get('depth_label', 'Burial Depth'))
        self.crs = QLineEdit(spec.get('crs', 'EPSG:4326'))
        self.zone = combo(['UTC', 'UTC+01:00', 'UTC-05:00', 'Europe/London'], spec.get('timezone', 'UTC'))
        self.time_format = combo(['ISO', 'day,time', 'epoch', '%d/%m/%Y %H:%M:%S'], spec.get('time_format', 'ISO'))
        self.start_date = QLineEdit(spec.get('start_date', ''))
        self.nulls = QLineEdit(','.join(spec.get('null_values', ['NULL', 'NaN', '-9999'])))
        for title, widget in [('Logical source', self.source), ('Supplier', self.supplier), ('Depth label', self.label),
                              ('Depth definition', self.definition), ('Position CRS', self.crs), ('Source timezone', self.zone),
                              ('Time format', self.time_format), ('Day 1 date (day,time only)', self.start_date), ('Missing values', self.nulls)]:
            form.addRow(title, widget)
        layout.addLayout(form)
        self.units = QCheckBox('Skip units row immediately after header')
        self.units.setChecked(bool(spec.get('units_row')))
        self.delimiter = combo(['Auto', ',', ';', '\t', '|'], spec.get('delimiter') or 'Auto')
        self.skip = QSpinBox()
        self.skip.setRange(0, 100)
        self.skip.setValue(spec.get('header_rows', 0))
        if csv_content is not None:
            options = QHBoxLayout()
            options.addWidget(self.units)
            options.addWidget(QLabel('Delimiter'))
            options.addWidget(self.delimiter)
            options.addWidget(QLabel('Lines before header'))
            options.addWidget(self.skip)
            reload_button = QPushButton('Read columns')
            reload_button.clicked.connect(self.reload_headers)
            options.addWidget(reload_button)
            layout.addLayout(options)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(['Channel', 'Source column', 'Multiplier to output units'])
        layout.addWidget(self.table, 1)
        hint = QLabel('Depth in metres; KP in kilometres. Use 0.001 for source KP in metres. Extra channels retain their chosen units.')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.extra = QLineEdit()
        self.extra.setText(", ".join(f"{k}={v}" for k, v in self._mapping.items()
                                    if k not in ("time", "kp", "depth", "x", "y", "pass", "state", "event", "pitch", "roll", "tension")))
        self.extra.setPlaceholderText('Extra numeric channels: torque=Torque, altitude=Altitude')
        layout.addWidget(self.extra)
        self.error = QLabel('')
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        self.populate(headers, spec)
        buttons(self, layout)
        self.value = None

    def populate(self, headers, spec):
        fields = ['time', 'kp', 'depth', 'x', 'y', 'pass', 'state', 'event', 'pitch', 'roll', 'tension']
        aliases = {'time': ['iso_time', 'time', 'datetime'], 'kp': ['kp', 'kp_km', 'designkp'],
                   'depth': ['burial_depth', 'burial depth', 'depth', 'depressordepth', 'depressor depth'],
                   'x': ['lon_dd', 'longitude', 'easting', '__x'], 'y': ['lat_dd', 'latitude', 'northing', '__y'],
                   'pass': ['pass', 'pass_id'], 'state': ['state', 'status'], 'event': ['event']}
        self.table.setRowCount(len(fields))
        self.field_boxes, self.factors = {}, {}
        for i, key in enumerate(fields):
            self.table.setItem(i, 0, QTableWidgetItem(data.CHANNELS.get(key, key)))
            box = QComboBox()
            box.addItem('(not mapped)', '')
            for h in headers:
                box.addItem(h, h)
            selected = spec.get('mapping', {}).get(key)
            if not selected:
                selected = next((h for h in headers if h.lower() in aliases.get(key, [key])), '')
            box.setCurrentIndex(max(0, box.findData(selected)))
            self.table.setCellWidget(i, 1, box)
            self.field_boxes[key] = box
            factor = spin(spec.get('factors', {}).get(key, spec.get('kp_factor', 1) if key == 'kp' else 1), -1000000)
            self.table.setCellWidget(i, 2, factor)
            self.factors[key] = factor
        self.table.resizeColumnsToContents()

    def reload_headers(self):
        try:
            spec = self.settings()
            headers, _ = data.read_csv(self.csv_content, spec)
            self.headers = headers
            self.populate(headers, spec)
            self.error.clear()
        except Exception as exc:
            self.error.setText(str(exc))

    def settings(self):
        mapping = {k: v.currentData() for k, v in self.field_boxes.items() if v.currentData()}
        for item in self.extra.text().split(','):
            if item.strip():
                key, value = item.strip().split('=', 1)
                if key.strip() in mapping:
                    raise ValueError('Extra channel duplicates a mapped channel')
                mapping[key.strip()] = value.strip()
        return {'mapping': mapping, 'supplier': self.supplier.text().strip(), 'definition': self.definition.currentText().strip(),
                'depth_label': self.label.currentText().strip(), 'crs': self.crs.text().strip(),
                'timezone': self.zone.currentText(), 'time_format': self.time_format.currentText(),
                'start_date': self.start_date.text().strip(), 'units_row': self.units.isChecked(),
                'delimiter': None if self.delimiter.currentText() == 'Auto' else self.delimiter.currentText(),
                'header_rows': self.skip.value(), 'null_values': ['', *self.nulls.text().split(',')],
                'kp_factor': self.factors['kp'].value(),
                'factors': {k: v.value() for k, v in self.factors.items() if k != 'kp'}}

    def accept(self):
        try:
            self.value = self.settings()
            if not self.source.text().strip():
                raise ValueError('Enter a logical source name')
            data.time_zone(self.value['timezone'])
            data.normalise([], self.value)
            if any(v not in self.headers for v in self.value['mapping'].values()):
                raise ValueError('A mapped column is not present in this file')
        except Exception as exc:
            self.error.setText(str(exc))
            return
        super().accept()


class ProcessingDialog(QDialog):
    def __init__(self, parent=None, recipe=None):
        super().__init__(parent)
        self.setWindowTitle('Process burial observations')
        recipe = recipe or {}
        self.original = copy.deepcopy(recipe)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.mode = QComboBox()
        self.mode.addItem('Calculate from positions and design RPL', 'position')
        self.mode.addItem('Use supplied KP on this design RPL', 'supplied')
        self.mode.setCurrentIndex(max(0, self.mode.findData(recipe.get('kp_mode', 'position'))))
        self.confirm = QCheckBox('Supplied KPs are referenced to this plan’s design RPL')
        self.confirm.setChecked(bool(recipe.get('supplied_kp_confirmed')))
        self.gap = spin(recipe.get('time_gap_s', 300), 0.001)
        self.reversal = spin(recipe.get('reversal_m', 10), .001)
        self.step = spin(recipe.get('max_step_m', 0))
        self.offset = spin(recipe.get('max_offset_m', 0))
        self.ambiguity = spin(recipe.get('ambiguity_m', 0))
        self.smooth = QSpinBox()
        self.smooth.setRange(1, 301)
        self.smooth.setSingleStep(2)
        self.smooth.setValue(recipe.get('smooth_window', 1))
        self.time_offset = spin(recipe.get('time_offset_s', 0), -86400, 86400)
        self.states = QLineEdit(','.join(recipe.get('working_states', ['burial', 'working', '1'])))
        self.reason = QLineEdit(recipe.get('reason', ''))
        for label, widget in [('KP reference', self.mode), ('', self.confirm), ('Split passes at time gap (s)', self.gap),
                              ('Direction reversal threshold (m)', self.reversal), ('Flag KP step above (m; 0 off)', self.step),
                              ('Flag cross-track offset above (m; 0 off)', self.offset),
                              ('Ambiguous route match tolerance (m; 0 off)', self.ambiguity),
                              ('Position median window (odd; 1 off)', self.smooth), ('Time correction (seconds)', self.time_offset),
                              ('Working states', self.states), ('Reason for changes', self.reason)]:
            form.addRow(label, widget)
        layout.addLayout(form)
        layout.addWidget(QLabel('Each run creates a new revision. Originals and earlier processing remain available.'))
        self.error = QLabel('')
        layout.addWidget(self.error)
        buttons(self, layout)
        self.value = None

    def accept(self):
        if self.mode.currentData() == 'supplied' and not self.confirm.isChecked():
            self.error.setText('Confirm the source KP reference or calculate KP from positions.')
            return
        if self.smooth.value() % 2 == 0:
            self.error.setText('Use an odd median window.')
            return
        if (self.smooth.value() > 1 or self.time_offset.value()) and not self.reason.text().strip():
            self.error.setText('Record a reason for the correction.')
            return
        self.value = dict(self.original, kp_mode=self.mode.currentData(), supplied_kp_confirmed=self.confirm.isChecked(),
                          time_gap_s=self.gap.value(), reversal_m=self.reversal.value(), max_step_m=self.step.value(),
                          max_offset_m=self.offset.value(), ambiguity_m=self.ambiguity.value(), smooth_window=self.smooth.value(),
                          time_offset_s=self.time_offset.value(), reason=self.reason.text().strip(),
                          working_states=[s.strip().lower() for s in self.states.text().split(',') if s.strip()])
        super().accept()


class JsonDialog(QDialog):
    """Advanced template/recipe editor with validation before accepting."""
    def __init__(self, title, value, parent=None, validate=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(720, 570)
        self.validate = validate
        layout = QVBoxLayout(self)
        self.editor = QPlainTextEdit(json.dumps(value, ensure_ascii=False, indent=2))
        layout.addWidget(self.editor)
        self.error = QLabel('')
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        buttons(self, layout)
        self.value = None

    def accept(self):
        try:
            self.value = json.loads(self.editor.toPlainText())
            if self.validate:
                self.validate(self.value)
        except Exception as exc:
            self.error.setText(str(exc))
            return
        super().accept()

# -*- coding: utf-8 -*-
"""Compatibility aliases for QGIS 3/Qt5 and QGIS 4/Qt6."""

from qgis.PyQt.QtCore import QMetaType
from qgis.PyQt.QtWidgets import QAbstractItemView, QDialog, QSizePolicy

try:
    from qgis.PyQt.QtCore import QVariant
except ImportError:  # pragma: no cover - PyQt6
    QVariant = None

try:
    from qgis.PyQt.QtGui import QAction
except ImportError:  # pragma: no cover - QGIS 3 / Qt5
    from qgis.PyQt.QtWidgets import QAction

from qgis.core import (
    Qgis,
    QgsMapLayer,
    QgsProcessingParameterField,
    QgsProcessingParameterNumber,
    QgsUnitTypes,
    QgsWkbTypes,
)


def _scoped_member(parent, scope_name, member_name, fallback_name=None):
    scope = getattr(parent, scope_name, None)
    if scope is not None and hasattr(scope, member_name):
        return getattr(scope, member_name)
    return getattr(parent, fallback_name or member_name)


def _member_from_scopes(parent, member_name, scope_names, fallback_name=None):
    for scope_name in scope_names:
        scope = getattr(parent, scope_name, None)
        if scope is not None and hasattr(scope, member_name):
            return getattr(scope, member_name)
    return getattr(parent, fallback_name or member_name)


def _field_type(qmeta_member_name, qvariant_member_name):
    if QVariant is not None and hasattr(QVariant, qvariant_member_name):
        return getattr(QVariant, qvariant_member_name)
    type_scope = getattr(QMetaType, "Type", None)
    if type_scope is not None and hasattr(type_scope, qmeta_member_name):
        return getattr(type_scope, qmeta_member_name)
    if hasattr(QMetaType, qmeta_member_name):
        return getattr(QMetaType, qmeta_member_name)
    raise AttributeError(qmeta_member_name)


def qt_exec(obj, *args, **kwargs):
    exec_method = getattr(obj, "exec", None)
    if exec_method is None:
        exec_method = getattr(obj, "exec_")
    return exec_method(*args, **kwargs)


DIALOG_ACCEPTED = _scoped_member(QDialog, "DialogCode", "Accepted")
DIALOG_REJECTED = _scoped_member(QDialog, "DialogCode", "Rejected")

SIZE_POLICY_EXPANDING = _scoped_member(QSizePolicy, "Policy", "Expanding")

SELECTION_MODE_EXTENDED = _scoped_member(QAbstractItemView, "SelectionMode", "ExtendedSelection")
SELECTION_MODE_SINGLE = _scoped_member(QAbstractItemView, "SelectionMode", "SingleSelection")

MESSAGE_INFO = _scoped_member(Qgis, "MessageLevel", "Info")
MESSAGE_WARNING = _scoped_member(Qgis, "MessageLevel", "Warning")
MESSAGE_CRITICAL = _scoped_member(Qgis, "MessageLevel", "Critical")
MESSAGE_SUCCESS = _scoped_member(Qgis, "MessageLevel", "Success")

GEOMETRY_POINT = _scoped_member(QgsWkbTypes, "GeometryType", "PointGeometry")
GEOMETRY_LINE = _scoped_member(QgsWkbTypes, "GeometryType", "LineGeometry")
GEOMETRY_POLYGON = _scoped_member(QgsWkbTypes, "GeometryType", "PolygonGeometry")
GEOMETRY_NULL = _scoped_member(QgsWkbTypes, "GeometryType", "NullGeometry")

LAYER_VECTOR = _scoped_member(QgsMapLayer, "LayerType", "VectorLayer")
LAYER_RASTER = _scoped_member(QgsMapLayer, "LayerType", "RasterLayer")

DISTANCE_METERS = _scoped_member(QgsUnitTypes, "DistanceUnit", "DistanceMeters")

PROCESSING_NUMBER_DOUBLE = _member_from_scopes(
    QgsProcessingParameterNumber,
    "Double",
    ("Type", "NumberType"),
)
PROCESSING_NUMBER_INTEGER = _member_from_scopes(
    QgsProcessingParameterNumber,
    "Integer",
    ("Type", "NumberType"),
)
PROCESSING_FIELD_NUMERIC = _member_from_scopes(
    QgsProcessingParameterField,
    "Numeric",
    ("DataType", "FieldType", "Type"),
)
PROCESSING_FIELD_ANY = _member_from_scopes(
    QgsProcessingParameterField,
    "Any",
    ("DataType", "FieldType", "Type"),
)

FIELD_TYPE_STRING = _field_type("QString", "String")
FIELD_TYPE_DOUBLE = _field_type("Double", "Double")
FIELD_TYPE_INT = _field_type("Int", "Int")
FIELD_TYPE_LONG_LONG = _field_type("LongLong", "LongLong")
FIELD_TYPE_BOOL = _field_type("Bool", "Bool")

# -*- coding: utf-8 -*-
"""Compatibility aliases for QGIS 3/Qt5 and QGIS 4/Qt6."""

from qgis.PyQt.QtCore import QMetaType, Qt
from qgis.PyQt.QtGui import QCursor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHeaderView, QMessageBox,
    QSizePolicy, QToolButton,
)

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
    QgsMapLayerProxyModel,
    QgsProcessingParameterField,
    QgsProcessingParameterNumber,
    QgsSnappingConfig,
    QgsTolerance,
    QgsUnitTypes,
    QgsVectorFileWriter,
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


def _layer_filter(member_name):
    """QGIS 4 moved QgsMapLayerProxyModel filters to Qgis.LayerFilter."""
    for scope_name in ("LayerFilter", "LayerFilters"):
        scope = getattr(Qgis, scope_name, None)
        if scope is not None and hasattr(scope, member_name):
            return getattr(scope, member_name)
    return _scoped_member(QgsMapLayerProxyModel, "Filter", member_name)


def _wkb_type(member_name):
    scope = getattr(Qgis, "WkbType", None)
    if scope is not None and hasattr(scope, member_name):
        return getattr(scope, member_name)
    return _scoped_member(QgsWkbTypes, "Type", member_name)


def _snapping_member(member_name, scope_names):
    """Resolve snapping enums moved from legacy classes to Qgis in QGIS 4."""
    for parent in (QgsSnappingConfig, QgsTolerance, Qgis):
        for scope_name in scope_names:
            scope = getattr(parent, scope_name, None)
            if scope is not None and hasattr(scope, member_name):
                return getattr(scope, member_name)
        if hasattr(parent, member_name):
            return getattr(parent, member_name)
    raise AttributeError(member_name)


def snapping_type_flags(*members):
    """Build the QFlags wrapper required by setTypeFlag on QGIS 3."""
    value = 0
    for member in members:
        value |= int(member)
    for parent in (Qgis, QgsSnappingConfig):
        wrapper = getattr(parent, "SnappingTypes", None)
        if wrapper is not None:
            return wrapper(value)
    return value


def layer_filters(*members):
    """Combine layer-filter members into the QFlags wrapper setFilters() wants.

    A bare ORed value can match the deprecated
    QgsMapLayerProxyModel.Filters overload on QGIS >= 3.34, logging a
    DeprecationWarning; wrapping in Qgis.LayerFilters selects the new one.
    """
    combined = members[0]
    for member in members[1:]:
        combined = combined | member
    for parent, name in ((Qgis, "LayerFilters"), (QgsMapLayerProxyModel, "Filters")):
        wrapper = getattr(parent, name, None)
        if wrapper is not None:
            try:
                return wrapper(int(combined))
            except (TypeError, ValueError):
                return combined
    return combined


def qt_exec(obj, *args, **kwargs):
    exec_method = getattr(obj, "exec", None)
    if exec_method is None:
        exec_method = getattr(obj, "exec_")
    return exec_method(*args, **kwargs)


DIALOG_ACCEPTED = _scoped_member(QDialog, "DialogCode", "Accepted")
DIALOG_REJECTED = _scoped_member(QDialog, "DialogCode", "Rejected")

SIZE_POLICY_EXPANDING = _scoped_member(QSizePolicy, "Policy", "Expanding")
SIZE_POLICY_IGNORED = _scoped_member(QSizePolicy, "Policy", "Ignored")
SIZE_POLICY_PREFERRED = _scoped_member(QSizePolicy, "Policy", "Preferred")
TEXT_ELIDE_RIGHT = _scoped_member(Qt, "TextElideMode", "ElideRight")
CONTEXT_MENU_POLICY_CUSTOM = _scoped_member(Qt, "ContextMenuPolicy", "CustomContextMenu")
MOUSE_BUTTON_LEFT = _scoped_member(Qt, "MouseButton", "LeftButton")
KEYBOARD_MODIFIER_NONE = _scoped_member(Qt, "KeyboardModifier", "NoModifier")

SELECTION_MODE_EXTENDED = _scoped_member(QAbstractItemView, "SelectionMode", "ExtendedSelection")
SELECTION_MODE_SINGLE = _scoped_member(QAbstractItemView, "SelectionMode", "SingleSelection")
SELECTION_BEHAVIOR_SELECT_ROWS = _scoped_member(QAbstractItemView, "SelectionBehavior", "SelectRows")
EDIT_TRIGGER_NONE = _scoped_member(QAbstractItemView, "EditTrigger", "NoEditTriggers")
EDIT_TRIGGER_DOUBLE_CLICKED = _scoped_member(QAbstractItemView, "EditTrigger", "DoubleClicked")
EDIT_TRIGGER_SELECTED_CLICKED = _scoped_member(QAbstractItemView, "EditTrigger", "SelectedClicked")
EDIT_TRIGGER_EDIT_KEY_PRESSED = _scoped_member(QAbstractItemView, "EditTrigger", "EditKeyPressed")
DRAG_DROP_MODE_INTERNAL_MOVE = _scoped_member(QAbstractItemView, "DragDropMode", "InternalMove")
DROP_ACTION_MOVE = _scoped_member(Qt, "DropAction", "MoveAction")
DROP_ACTION_IGNORE = _scoped_member(Qt, "DropAction", "IgnoreAction")

SNAPPING_MODE_ALL_LAYERS = _snapping_member("AllLayers", ("SnappingMode", "Mode"))
SNAPPING_MODE_PER_LAYER = _snapping_member(
    "AdvancedConfiguration", ("SnappingMode", "Mode")) if (
        hasattr(getattr(Qgis, "SnappingMode", object), "AdvancedConfiguration") or
        hasattr(QgsSnappingConfig, "AdvancedConfiguration")
    ) else _snapping_member("PerLayer", ("SnappingMode", "Mode"))
SNAPPING_TYPE_VERTEX = _snapping_member("Vertex", ("SnappingType", "Type", "TypeFlag"))
SNAPPING_TYPE_SEGMENT = _snapping_member("Segment", ("SnappingType", "Type", "TypeFlag"))
SNAPPING_UNIT_PIXELS = _snapping_member("Pixels", ("MapToolUnit", "UnitType"))

HEADER_RESIZE_MODE_INTERACTIVE = _scoped_member(QHeaderView, "ResizeMode", "Interactive")
HEADER_RESIZE_MODE_STRETCH = _scoped_member(QHeaderView, "ResizeMode", "Stretch")
HEADER_RESIZE_MODE_FIXED = _scoped_member(QHeaderView, "ResizeMode", "Fixed")

MESSAGEBOX_YES = _scoped_member(QMessageBox, "StandardButton", "Yes")
MESSAGEBOX_NO = _scoped_member(QMessageBox, "StandardButton", "No")

TOOLBUTTON_POPUP_MODE_MENU_BUTTON = _scoped_member(
    QToolButton,
    "ToolButtonPopupMode",
    "MenuButtonPopup",
)
TOOLBUTTON_POPUP_MODE_INSTANT = _scoped_member(
    QToolButton,
    "ToolButtonPopupMode",
    "InstantPopup",
)

MESSAGE_INFO = _scoped_member(Qgis, "MessageLevel", "Info")
MESSAGE_WARNING = _scoped_member(Qgis, "MessageLevel", "Warning")
MESSAGE_CRITICAL = _scoped_member(Qgis, "MessageLevel", "Critical")
MESSAGE_SUCCESS = _scoped_member(Qgis, "MessageLevel", "Success")

GEOMETRY_POINT = _scoped_member(QgsWkbTypes, "GeometryType", "PointGeometry")
GEOMETRY_LINE = _scoped_member(QgsWkbTypes, "GeometryType", "LineGeometry")
GEOMETRY_POLYGON = _scoped_member(QgsWkbTypes, "GeometryType", "PolygonGeometry")
GEOMETRY_NULL = _scoped_member(QgsWkbTypes, "GeometryType", "NullGeometry")

WKB_POINT = _wkb_type("Point")
WKB_POINT_Z = _wkb_type("PointZ")
WKB_POINT_M = _wkb_type("PointM")
WKB_LINESTRING = _wkb_type("LineString")
WKB_NO_GEOMETRY = _wkb_type("NoGeometry")

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


def _processing_source_type(legacy_name, scoped_name):
    """QGIS 4 moved QgsProcessing.Type* to Qgis.ProcessingSourceType."""
    from qgis.core import QgsProcessing

    value = getattr(QgsProcessing, legacy_name, None)
    if value is not None:
        return value
    return getattr(Qgis.ProcessingSourceType, scoped_name)


PROCESSING_SOURCE_FILE = _processing_source_type("TypeFile", "File")
PROCESSING_SOURCE_RASTER = _processing_source_type("TypeRaster", "Raster")

FIELD_TYPE_STRING = _field_type("QString", "String")
FIELD_TYPE_DOUBLE = _field_type("Double", "Double")
FIELD_TYPE_INT = _field_type("Int", "Int")
FIELD_TYPE_LONG_LONG = _field_type("LongLong", "LongLong")
FIELD_TYPE_BOOL = _field_type("Bool", "Bool")

# QDialogButtonBox button constants - PyQt6 moved these under StandardButton scope
BUTTON_BOX_OK = _scoped_member(QDialogButtonBox, "StandardButton", "Ok")
BUTTON_BOX_CANCEL = _scoped_member(QDialogButtonBox, "StandardButton", "Cancel")
BUTTON_BOX_CLOSE = _scoped_member(QDialogButtonBox, "StandardButton", "Close")
BUTTON_BOX_HELP = _scoped_member(QDialogButtonBox, "StandardButton", "Help")
BUTTON_BOX_YES = _scoped_member(QDialogButtonBox, "StandardButton", "Yes")
BUTTON_BOX_NO = _scoped_member(QDialogButtonBox, "StandardButton", "No")
BUTTON_BOX_SAVE = _scoped_member(QDialogButtonBox, "StandardButton", "Save")
BUTTON_BOX_DISCARD = _scoped_member(QDialogButtonBox, "StandardButton", "Discard")

# QDialogButtonBox button roles
BUTTON_BOX_ACCEPT_ROLE = _scoped_member(QDialogButtonBox, "ButtonRole", "AcceptRole")

# Window flags used by floating planner docks.
WINDOW_TYPE_WINDOW = _scoped_member(Qt, "WindowType", "Window")
WINDOW_HINT_CUSTOMIZE = _scoped_member(Qt, "WindowType", "CustomizeWindowHint")
WINDOW_HINT_TITLE = _scoped_member(Qt, "WindowType", "WindowTitleHint")
WINDOW_HINT_MIN_MAX = _scoped_member(Qt, "WindowType", "WindowMinMaxButtonsHint")
WINDOW_HINT_CLOSE = _scoped_member(Qt, "WindowType", "WindowCloseButtonHint")

PEN_STYLE_DASH = _scoped_member(Qt, "PenStyle", "DashLine")
ITEM_FLAG_EDITABLE = _scoped_member(Qt, "ItemFlag", "ItemIsEditable")
ITEM_FLAG_USER_CHECKABLE = _scoped_member(Qt, "ItemFlag", "ItemIsUserCheckable")
ITEM_DATA_USER_ROLE = _scoped_member(Qt, "ItemDataRole", "UserRole")
ITEM_DATA_DISPLAY_ROLE = _scoped_member(Qt, "ItemDataRole", "DisplayRole")
ITEM_DATA_EDIT_ROLE = _scoped_member(Qt, "ItemDataRole", "EditRole")
CHECK_STATE_CHECKED = _scoped_member(Qt, "CheckState", "Checked")
CHECK_STATE_UNCHECKED = _scoped_member(Qt, "CheckState", "Unchecked")
WINDOW_TYPE_TOOL = _scoped_member(Qt, "WindowType", "Tool")
SELECTION_MODE_NONE = _scoped_member(QAbstractItemView, "SelectionMode", "NoSelection")

MAP_LAYER_FILTER_POINT = _layer_filter("PointLayer")
MAP_LAYER_FILTER_LINE = _layer_filter("LineLayer")
MAP_LAYER_FILTER_POLYGON = _layer_filter("PolygonLayer")
MAP_LAYER_FILTER_VECTOR = _layer_filter("VectorLayer")
MAP_LAYER_FILTER_RASTER = _layer_filter("RasterLayer")

# Older alias spelling kept for the Burial Planner / Cable Route Workbench.
MESSAGE_BOX_YES = MESSAGEBOX_YES
MESSAGE_BOX_NO = MESSAGEBOX_NO

def _feature_request_no_geometry():
    """``NoGeometry`` feature-request flag: QGIS 4 moved it from the
    QgsFeatureRequest enum to Qgis.FeatureRequestFlag."""
    from qgis.core import QgsFeatureRequest

    scope = getattr(Qgis, "FeatureRequestFlag", None)
    if scope is not None and hasattr(scope, "NoGeometry"):
        return scope.NoGeometry
    flag_scope = getattr(QgsFeatureRequest, "Flag", None)
    if flag_scope is not None and hasattr(flag_scope, "NoGeometry"):
        return flag_scope.NoGeometry
    return QgsFeatureRequest.NoGeometry


FEATURE_REQUEST_NO_GEOMETRY = _feature_request_no_geometry()

VECTOR_WRITER_OVERWRITE_LAYER = _scoped_member(
    QgsVectorFileWriter, "ActionOnExistingFile", "CreateOrOverwriteLayer")
VECTOR_WRITER_OVERWRITE_FILE = _scoped_member(
    QgsVectorFileWriter, "ActionOnExistingFile", "CreateOrOverwriteFile")
VECTOR_WRITER_NO_ERROR = _scoped_member(
    QgsVectorFileWriter, "WriterError", "NoError")


def get_event_global_pos(event):
    """
    Get global screen position from a QGIS map mouse event.
    
    In QGIS 3 (Qt5), QgsMapMouseEvent has globalPos().
    In QGIS 4 (Qt6), QgsMapMouseEvent does not have globalPos(), so we use QCursor.pos().
    
    Args:
        event: QgsMapMouseEvent object
    
    Returns:
        QPoint with global screen coordinates
    """
    if hasattr(event, 'globalPos'):
        return event.globalPos()
    else:
        # Fallback for QGIS 4 - use current cursor position
        return QCursor.pos()


def processing_temp_folder(context=None):
    """QgsProcessingUtils.tempFolder with the QGIS 3.32+ context overload.

    QGIS 3.32 added an optional QgsProcessingContext argument so temporary
    files are tracked (and cleaned) per processing run; earlier releases only
    accept the no-argument form. Falls back gracefully so callers can always
    pass their context.
    """
    from qgis.core import QgsProcessingUtils

    if context is not None:
        try:
            return QgsProcessingUtils.tempFolder(context)
        except TypeError:
            pass
    return QgsProcessingUtils.tempFolder()


def processing_generate_temp_filename(basename, context=None):
    """QgsProcessingUtils.generateTempFilename with the 3.32+ context overload.

    See processing_temp_folder for the version background.
    """
    from qgis.core import QgsProcessingUtils

    if context is not None:
        try:
            return QgsProcessingUtils.generateTempFilename(basename, context)
        except TypeError:
            pass
    return QgsProcessingUtils.generateTempFilename(basename)

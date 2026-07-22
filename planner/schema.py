# -*- coding: utf-8 -*-
"""Versioned GeoPackage schema for the Planner."""

from __future__ import annotations

import os
from typing import Dict, List, Tuple

from ..workbench.schema import new_id, sanitize_slug, unsaved_project_folder, utc_now_iso

SCHEMA_VERSION = 6

TABLE_META = "pow_meta"
TABLE_SCENARIO = "pow_scenario"
TABLE_RESOURCE = "pow_resource"
TABLE_TASK = "pow_task"
TABLE_TASK_POINT = "pow_task_point"
TABLE_TASK_LINE = "pow_task_line"

FieldSpec = Tuple[str, str]

META_FIELDS: List[FieldSpec] = [("key", "str"), ("value", "str")]

SCENARIO_FIELDS: List[FieldSpec] = [
    ("scenario_id", "str"), ("name", "str"), ("description", "str"),
    ("start_datetime", "str"), ("duplicated_from_id", "str"),
    ("settings_json", "str"), ("created_utc", "str"),
    ("modified_utc", "str"), ("notes", "str"),
]

# Since v5 resources are project-level and shared by every scenario; the
# scenario_id column is kept for file compatibility but written empty.
RESOURCE_FIELDS: List[FieldSpec] = [
    ("resource_id", "str"), ("scenario_id", "str"), ("name", "str"),
    ("kind", "str"), ("color_hex", "str"), ("default_speed_kn", "float"),
    ("start_offset_hours", "float"), ("fuel_unit", "str"),
    ("fuel_rate_transit", "float"), ("fuel_rate_dp", "float"),
    ("fuel_rate_anchor", "float"), ("fuel_rate_port", "float"),
    ("fuel_start", "float"), ("fuel_cost_per_unit", "float"),
    ("seq", "int"), ("notes", "str"),
]

TASK_FIELDS: List[FieldSpec] = [
    ("task_id", "str"), ("scenario_id", "str"), ("seq", "int"),
    ("name", "str"), ("description", "str"), ("operation_type", "str"),
    ("is_phase", "int"),
    ("outline_level", "int"), ("resource_id", "str"),
    ("duration_mode", "str"), ("duration_hours", "float"),
    ("predecessor_task_id", "str"), ("dependency_type", "str"),
    ("lag_hours", "float"),
    ("speed_knots", "float"), ("direction", "str"),
    ("location_mode", "str"), ("location_chainage_m", "float"),
    ("constraint_type", "str"), ("constraint_datetime", "str"),
    ("is_milestone", "int"),
    ("fuel_mode", "str"), ("bunker_amount", "float"), ("layer_id", "str"),
    ("layer_source", "str"), ("layer_name", "str"), ("feature_id", "str"),
    ("feature_label", "str"), ("geom_kind", "str"),
    ("linked_ref_json", "str"), ("progress_status", "str"),
    ("percent_complete", "float"), ("actual_start_datetime", "str"),
    ("actual_finish_datetime", "str"), ("remaining_duration_hours", "float"),
    ("progress_notes", "str"), ("actual_log_json", "str"),
    ("progress_updated_utc", "str"), ("created_utc", "str"),
    ("modified_utc", "str"), ("notes", "str"),
]

TASK_GEOMETRY_FIELDS: List[FieldSpec] = [
    ("geom_id", "str"), ("task_id", "str"), ("scenario_id", "str"),
    ("seq", "int"), ("name", "str"), ("resource_id", "str"),
    ("speed_knots", "float"), ("duration_hours", "float"),
    ("source_kind", "str"), ("source_ref_json", "str"),
    ("notes", "str"), ("created_utc", "str"), ("modified_utc", "str"),
]

SPATIAL_TABLES = {
    TABLE_TASK_POINT: TASK_GEOMETRY_FIELDS,
    TABLE_TASK_LINE: TASK_GEOMETRY_FIELDS,
}

REGISTRY_TABLES: Dict[str, List[FieldSpec]] = {
    TABLE_META: META_FIELDS,
    TABLE_SCENARIO: SCENARIO_FIELDS,
    TABLE_RESOURCE: RESOURCE_FIELDS,
    TABLE_TASK: TASK_FIELDS,
}

TABLE_KEYS = {
    TABLE_SCENARIO: "scenario_id",
    TABLE_RESOURCE: "resource_id",
    TABLE_TASK: "task_id",
}

DEFAULT_RESOURCE_NAME = "Vessel 1"
DEFAULT_RESOURCE_KIND = "vessel"
DEFAULT_RESOURCE_COLOR = "#1f78b4"
DEFAULT_SPEED_KN = 1.0
DEFAULT_FUEL_UNIT = "t"

# Per-24 h fuel rates a task can burn from its resource's profile.
FUEL_MODES = (("", "(none)"), ("transit", "Transit"), ("dp", "DP"),
              ("anchor", "Anchor"), ("port", "Port"))

OPERATION_TYPES = (
    ("", "(unspecified)"), ("lay", "Lay"), ("plgr", "PLGR"),
    ("plough", "Plough"), ("rov", "ROV"), ("recover", "Recover"),
    ("transit", "Transit"), ("mobilise", "Mobilise"),
    ("demobilise", "Demobilise"), ("port", "Port call"),
    ("vehicle_launch", "Vehicle launch"),
    ("vehicle_recover", "Vehicle recover"),
    ("midwater_transit", "Mid-water transit"),
    ("weather", "Weather/downtime"), ("other", "Other"),
)

DEPENDENCY_TYPES = (
    ("FS", "Finish-to-start"), ("SS", "Start-to-start"),
    ("FF", "Finish-to-finish"), ("SF", "Start-to-finish"),
)

CONSTRAINT_TYPES = (
    ("", "(none)"), ("snet", "Start no earlier than"),
    ("fnlt", "Finish no later than"), ("mso", "Must start on"),
    ("mfo", "Must finish on"),
)

PROGRESS_STATUSES = (
    ("not_started", "Not started"), ("in_progress", "In progress"),
    ("completed", "Completed"), ("on_hold", "On hold"),
    ("cancelled", "Cancelled"),
)


def default_gpkg_path(project_path: str, project_title: str = "") -> str:
    """Return the planner GeoPackage path beside the current project."""
    if project_path:
        folder = os.path.dirname(project_path)
        stem = os.path.splitext(os.path.basename(project_path))[0]
    else:
        folder = unsaved_project_folder()
        stem = sanitize_slug(project_title) if project_title else "project"
    return os.path.join(folder, "%s_planner.gpkg" % stem)

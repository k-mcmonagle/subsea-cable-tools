# -*- coding: utf-8 -*-
"""Burial Planner GeoPackage schema.

Declarative field specs for the ``bp_*`` registry tables plus the per-plan
spatial layer schemas, following the Workbench/Planner conventions:
``(name, type_str)`` field specs with ``str``/``float``/``int`` type strings,
geometryless registry tables, UUIDv7 ids, UTC ISO-8601 timestamps.

Table overview:

- bp_meta        key/value store (schema_version, created_utc)
- bp_plan        burial plan headers (method, RPL reference, scope, direction)
- bp_input       registered data inputs beyond the RPL (input-register style)
- bp_rule        the exclusion stack, plan-scoped; field-compatible with
                 wb_rule so rules copy losslessly between the tools
- bp_generation  one row per algorithm run (frozen snapshot + fingerprints)
- bp_event       burial events (PLDN/PLUP etc.); KP is the sole edit surface
- bp_section     derived sections (burial | skip | insufficient_info)
- bp_change_log  append-only change log with before/after JSON
- bp_tool        project-scoped Burial Tools registry (ploughs, trenchers…)
                 with per-tool configurations and an optional DXF footprint

No engineering values are shipped here: criteria values, buffers and limits
are user-entered, each with a source-reference field.
"""

from __future__ import annotations

import os
from typing import Dict, List, Tuple

# Shared primitives (UUIDv7 ids, UTC timestamps, slug/paths). Pure python.
from ..workbench.schema import (  # noqa: F401  (re-exported for the package)
    gpkg_folder_for,
    new_id,
    sanitize_slug,
    utc_now_iso,
)

SCHEMA_VERSION = 7

# Registry table names ------------------------------------------------------
TABLE_META = "bp_meta"
TABLE_PLAN = "bp_plan"
TABLE_INPUT = "bp_input"
TABLE_RULE = "bp_rule"
TABLE_GENERATION = "bp_generation"
TABLE_EVENT = "bp_event"
TABLE_SECTION = "bp_section"
TABLE_CHANGE_LOG = "bp_change_log"
TABLE_PROFILE = "bp_profile"
TABLE_RISK_CHECK = "bp_risk_check"
TABLE_HAZARD = "bp_hazard"
TABLE_TOOL = "bp_tool"

FieldSpec = Tuple[str, str]

META_FIELDS: List[FieldSpec] = [
    ("key", "str"),
    ("value", "str"),
]

# Methods -------------------------------------------------------------------
METHOD_PLOUGH = "plough"
METHOD_TRENCHER = "trencher"
# Legacy id: ROV jet trenching folded into the single Trencher method
# (schema v7). Kept as a constant so old data/exports keep resolving.
METHOD_ROV_JET = "rov_jet"
METHODS: List[str] = [METHOD_PLOUGH, METHOD_TRENCHER]  # enum open by design

METHOD_LABELS: Dict[str, str] = {
    METHOD_PLOUGH: "Plough",
    METHOD_TRENCHER: "Trencher",
}

# Method ids seen in older data / the Workbench Assessment tool, mapped to
# the Burial Planner vocabulary. Workbench DEFAULT_ASSESSMENT_METHODS uses
# "jet"; plans/rules written before v7 use "rov_jet". Both mean trencher
# burial here — a rule or plan must not change behaviour because of the
# spelling.
_METHOD_ALIASES: Dict[str, str] = {
    "jet": METHOD_TRENCHER,
    METHOD_ROV_JET: METHOD_TRENCHER,
}


def normalise_method(value: str) -> str:
    """Canonical method id for ``value`` (aliases mapped, unknowns kept)."""
    value = (value or "").strip()
    return _METHOD_ALIASES.get(value.lower(), value)


def normalise_methods(values) -> List[str]:
    """Canonicalise a method-id list, preserving order, dropping duplicates."""
    out: List[str] = []
    for value in values or []:
        method = normalise_method(str(value))
        if method and method not in out:
            out.append(method)
    return out

# Plan status ---------------------------------------------------------------
PLAN_STATUS_DRAFT = "draft"
PLAN_STATUS_STALE = "stale"
PLAN_STATUS_ISSUED = "issued"

PLAN_FIELDS: List[FieldSpec] = [
    ("plan_id", "str"),
    ("name", "str"),
    ("description", "str"),
    ("notes", "str"),
    ("method", "str"),               # one of METHODS (open enum)
    ("rpl_id", "str"),               # Workbench rpl_id ("" for a bare line layer)
    ("rpl_name", "str"),             # snapshot
    ("rpl_revision", "str"),         # Workbench rev_label snapshot
    ("rpl_gpkg_path", "str"),        # snapshot
    ("rpl_fingerprint", "str"),      # modified_utc/hash snapshot (stale detection)
    ("scope_start_kp", "float"),
    ("scope_end_kp", "float"),
    ("direction", "int"),            # +1 with increasing KP, -1 against
    ("target_burial_m", "float"),    # informational in v1
    ("params_json", "str"),          # plan-level post-processing params
    #                                  (min_section_km, sliver_tol_km,
    #                                   coarse_step_m, refine_tol_m)
    ("status", "str"),               # "" | draft | stale | issued
    ("rev_label", "str"),
    ("supersedes_id", "str"),
    ("created_utc", "str"),
    ("modified_utc", "str"),
]

# Input roles ---------------------------------------------------------------
INPUT_ROLE_BATHY = "bathy"
INPUT_ROLE_CROSSINGS_POINTS = "crossings_points"
INPUT_ROLE_CROSSINGS_LINES = "crossings_lines"
INPUT_ROLE_SOILS = "soils_polygons"
INPUT_ROLE_OTHER = "other"
INPUT_ROLES: List[str] = [
    INPUT_ROLE_BATHY,
    INPUT_ROLE_CROSSINGS_POINTS,
    INPUT_ROLE_CROSSINGS_LINES,
    INPUT_ROLE_SOILS,
    INPUT_ROLE_OTHER,
]

INPUT_ROLE_LABELS: Dict[str, str] = {
    INPUT_ROLE_BATHY: "Bathymetry",
    INPUT_ROLE_CROSSINGS_POINTS: "Crossings (points)",
    INPUT_ROLE_CROSSINGS_LINES: "Crossings (lines)",
    INPUT_ROLE_SOILS: "Seabed soils (polygons)",
    INPUT_ROLE_OTHER: "Other",
}

INPUT_FIELDS: List[FieldSpec] = [
    ("input_id", "str"),
    ("plan_id", "str"),
    ("role", "str"),
    ("layer_name", "str"),
    ("layer_source", "str"),         # normalised provider source path
    ("layer_id_hint", "str"),        # project layer id (hint only)
    ("config_json", "str"),          # role-specific; bathy = DepthSourceConfig dict
    # Input Data Register metadata (all optional)
    ("originator", "str"),
    ("revision", "str"),
    ("status", "str"),               # current | superseded
    ("received_utc", "str"),
    ("quality", "str"),              # high | moderate | low | insufficient | ""
    ("notes", "str"),
]

# Criterion classes (plough burial engineering guide terminology) -----------
CRITERION_NON_DEVIABLE = "non_deviable"
CRITERION_PROJECT = "project"
CRITERION_SCREENING = "screening"
CRITERION_CLASSES: List[str] = [
    CRITERION_NON_DEVIABLE, CRITERION_PROJECT, CRITERION_SCREENING,
]

CRITERION_LABELS: Dict[str, str] = {
    CRITERION_NON_DEVIABLE: "Non-Deviable Requirement",
    CRITERION_PROJECT: "Project Exclusion Criterion",
    CRITERION_SCREENING: "Screening Criterion",
}

# Rule kinds/actions are the shared engine's (workbench.schema RULE_KIND_* /
# RULE_ACTION_*). bp_rule is wb_rule plus plan_id, criterion_class and
# source_ref, so rules copy losslessly between the tools (extra columns
# dropped on copy to the Workbench, defaulted on copy from it).
RULE_FIELDS: List[FieldSpec] = [
    ("rule_id", "str"),
    ("plan_id", "str"),
    ("seq", "int"),
    ("name", "str"),
    ("enabled", "int"),
    ("kind", "str"),                 # threshold_profile | proximity | polygon_class | kp_range_table | manual
    ("action", "str"),               # exclude | risk | allow
    ("risk_level", "int"),
    ("criterion_class", "str"),      # non_deviable | project | screening
    ("source_ref", "str"),           # document + revision the value comes from
    ("methods_json", "str"),
    ("config_json", "str"),          # kind payload + scope_ranges / extend_m /
    #                                  influence_before_m / influence_after_m /
    #                                  buffer_field / slope + band keys / input_id
    ("notes", "str"),
]

GENERATION_FIELDS: List[FieldSpec] = [
    ("generation_id", "str"),
    ("plan_id", "str"),
    ("run_utc", "str"),
    ("active", "int"),
    ("rules_snapshot_json", "str"),
    ("params_json", "str"),
    ("inputs_fingerprint_json", "str"),
    ("summary_json", "str"),
    ("proposal_diff_json", "str"),
]

# Event types ---------------------------------------------------------------
EVENT_BURIAL_START = "BURIAL_START"
EVENT_BURIAL_END = "BURIAL_END"
# Reserved for the range-event roadmap; not emitted in v1.
EVENT_RESERVED_TYPES: List[str] = [
    "DEPLOY_START", "DEPLOY_END", "LIFT", "LOWER", "TRANSIT_START", "TRANSIT_END",
]

# Display labels: generic semantics, per-method vocabulary. Generic terms
# appear only in code/schema, never in plough-mode UI (spec D4). Consumed via
# ``events.event_label`` — kept here so every per-method vocabulary (labels,
# section codes, kind labels) lives in one module.
METHOD_EVENT_LABELS: Dict[str, Dict[str, str]] = {
    METHOD_PLOUGH: {
        EVENT_BURIAL_START: "PLDN",
        EVENT_BURIAL_END: "PLUP",
    },
    METHOD_TRENCHER: {
        EVENT_BURIAL_START: "TRENCH_START",
        EVENT_BURIAL_END: "TRENCH_END",
    },
}

EVENT_SOURCE_AUTO = "auto"
EVENT_SOURCE_MANUAL = "manual"
EVENT_SOURCE_IMPORT = "import"
EVENT_SOURCE_CLIENT = "client_proposal"

EVENT_STATUS_CANDIDATE = "candidate"
EVENT_STATUS_CONFIRMED = "confirmed"
EVENT_STATUS_CONFLICT = "conflict"

EVENT_FIELDS: List[FieldSpec] = [
    ("event_id", "str"),
    ("plan_id", "str"),
    ("generation_id", "str"),        # "" for manual/import events
    ("seq", "int"),
    ("event_type", "str"),
    ("kp", "float"),                 # authoritative position
    ("end_kp", "float"),             # reserved for range events; NULL in v1
    ("lat", "float"),                # derived WGS84 (never directly editable)
    ("lon", "float"),
    ("depth_m", "float"),            # sampled, nullable
    ("source", "str"),               # auto | manual | import | client_proposal
    ("status", "str"),               # candidate | confirmed | conflict
    ("locked", "int"),
    ("notes", "str"),
]

# Sections ------------------------------------------------------------------
SECTION_BURIAL = "burial"
SECTION_SKIP = "skip"
SECTION_INSUFFICIENT = "insufficient_info"

SECTION_STATE_CANDIDATE = "candidate"
SECTION_STATE_FINAL = "final"

CONCLUSION_NORMAL = "normal_envelope"
CONCLUSION_CONDITIONAL = "conditional_envelope"
CONCLUSION_OUTSIDE = "outside_envelope"
CONCLUSION_INSUFFICIENT = "insufficient_information"
CONCLUSION_NOT_SELECTED = "not_selected"
CONCLUSIONS: List[str] = [
    CONCLUSION_NORMAL, CONCLUSION_CONDITIONAL, CONCLUSION_OUTSIDE,
    CONCLUSION_INSUFFICIENT, CONCLUSION_NOT_SELECTED,
]

CONCLUSION_LABELS: Dict[str, str] = {
    "": "",
    CONCLUSION_NORMAL: "Within Normal Operating Envelope",
    CONCLUSION_CONDITIONAL: "Within Conditional Operating Envelope",
    CONCLUSION_OUTSIDE: "Outside Operating Envelope",
    CONCLUSION_INSUFFICIENT: "Insufficient Information",
    CONCLUSION_NOT_SELECTED: "Not Selected",
}

CONFIDENCE_VALUES: List[str] = ["high", "moderate", "low", "insufficient"]

# How a skip is executed operationally: recover the burial tool to deck, or
# transit with the tool suspended mid-water. Default "" = TBC.
SKIP_HANDLING_TBC = ""
SKIP_HANDLING_RECOVER = "recover_to_deck"
SKIP_HANDLING_MIDWATER = "midwater_transit"
SKIP_HANDLING_VALUES: List[str] = [
    SKIP_HANDLING_TBC, SKIP_HANDLING_RECOVER, SKIP_HANDLING_MIDWATER,
]
SKIP_HANDLING_LABELS: Dict[str, str] = {
    SKIP_HANDLING_TBC: "TBC",
    SKIP_HANDLING_RECOVER: "Recover to deck",
    SKIP_HANDLING_MIDWATER: "Mid-water transit",
}

# Working section references ("PS-01", "SK-02", …): a short, human-usable ID
# for tables, exports and documentation. Derived — never stored — so it always
# reflects the current partition: sections are numbered per kind in travel
# order (direction -1 counts from the high-KP end). Codes follow the method
# vocabulary (plough burial sections are Candidate Plough Sections -> PS).
_SECTION_REF_CODES_DEFAULT: Dict[str, str] = {
    SECTION_BURIAL: "BS",
    SECTION_SKIP: "SK",
    SECTION_INSUFFICIENT: "II",
}
_SECTION_REF_CODES_BY_METHOD: Dict[str, Dict[str, str]] = {
    METHOD_PLOUGH: {
        SECTION_BURIAL: "PS",
        SECTION_SKIP: "SK",
        SECTION_INSUFFICIENT: "II",
    },
    METHOD_TRENCHER: {
        SECTION_BURIAL: "TS",
        SECTION_SKIP: "SK",
        SECTION_INSUFFICIENT: "II",
    },
}

# Per-method section-kind display names. Insufficient Information is
# method-neutral by definition. The single source for the Plan Builder table,
# dialogs and the report (previously duplicated in both).
_SECTION_KIND_LABELS_DEFAULT: Dict[str, str] = {
    SECTION_BURIAL: "Burial section",
    SECTION_SKIP: "Skip",
    SECTION_INSUFFICIENT: "Insufficient Information",
}
_SECTION_KIND_LABELS_BY_METHOD: Dict[str, Dict[str, str]] = {
    METHOD_PLOUGH: {
        SECTION_BURIAL: "Candidate Plough Section",
        SECTION_SKIP: "Plough Skip",
        SECTION_INSUFFICIENT: "Insufficient Information",
    },
    METHOD_TRENCHER: {
        SECTION_BURIAL: "Candidate Trench Section",
        SECTION_SKIP: "Trench Skip",
        SECTION_INSUFFICIENT: "Insufficient Information",
    },
}


def section_kind_label(kind: str, method: str = "") -> str:
    """Method-correct display name for a section kind."""
    labels = _SECTION_KIND_LABELS_BY_METHOD.get(
        normalise_method(method), _SECTION_KIND_LABELS_DEFAULT)
    return labels.get(kind or "", _SECTION_KIND_LABELS_DEFAULT.get(
        kind or "", kind or ""))


def section_ref_code(kind: str, method: str = "") -> str:
    codes = _SECTION_REF_CODES_BY_METHOD.get(normalise_method(method),
                                             _SECTION_REF_CODES_DEFAULT)
    return codes.get(kind or "", "XX")


def section_ref_legend(method: str = "") -> str:
    """One-line legend for UI tooltips and report footnotes."""
    parts = [f"{section_ref_code(kind, method)} = {section_kind_label(kind, method)}"
             for kind in (SECTION_BURIAL, SECTION_SKIP, SECTION_INSUFFICIENT)]
    return ", ".join(parts) + " — numbered in travel order"


def section_refs(sections, direction: int = 1, method: str = "") -> Dict[str, str]:
    """Map ``section_id`` -> working reference (e.g. ``PS-03``).

    Numbering is positional (per kind, in travel order), so inserting or
    merging sections renumbers the ones after the edit; references become
    stable once the plan stops changing. Zero-padded to two digits so they
    sort naturally in documents up to 99 sections per kind.
    """
    def sort_key(section) -> Tuple[float, float]:
        try:
            start = float(section.get("start_kp") or 0.0)
            end = float(section.get("end_kp") or 0.0)
        except (TypeError, ValueError):
            start = end = 0.0
        return (start, end)

    ordered = sorted(sections, key=sort_key,
                     reverse=int(direction or 1) < 0)
    counters: Dict[str, int] = {}
    refs: Dict[str, str] = {}
    for section in ordered:
        code = section_ref_code(section.get("kind") or "", method)
        counters[code] = counters.get(code, 0) + 1
        refs[str(section.get("section_id") or "")] = \
            f"{code}-{counters[code]:02d}"
    return refs


SECTION_FIELDS: List[FieldSpec] = [
    ("section_id", "str"),
    ("plan_id", "str"),
    ("kind", "str"),                 # burial | skip | insufficient_info
    ("start_kp", "float"),
    ("end_kp", "float"),
    ("length_km", "float"),
    ("start_event_id", "str"),
    ("end_event_id", "str"),
    ("state", "str"),                # candidate | final
    ("conclusion", "str"),
    ("confidence", "str"),
    ("reason_json", "str"),
    # Per-section burial tool (bp_tool reference; "" = plan default).
    # ``method`` mirrors the assigned tool's type; generation still resolves
    # rules against the plan method until mixed-method generation lands.
    ("method", "str"),
    ("tool_id", "str"),
    ("tool_config_id", "str"),
    # Reserved for the grade roadmap (nullable, unused in v1)
    ("grade_in_m", "float"),
    ("grade_out_m", "float"),
    ("target_burial_m", "float"),
    ("skip_handling", "str"),        # "" (TBC) | recover_to_deck | midwater_transit
    ("notes", "str"),
]

# Burial Tools registry -----------------------------------------------------
# Project-scoped (no plan_id): tools are shared by every plan in the
# GeoPackage, like the Planner's vessels. No engineering values are shipped;
# every parameter is user-entered with a source-reference field.
# ``configs_json`` holds the tool's operating configurations as a JSON list
# of dicts (see ``tools.py``): each carries a stable ``config_id`` so
# per-section assignments survive relabelling.
# ``footprint_wkt`` is a body-fixed outline in metres, CRP at the origin,
# bow/front along +Y — the frame produced by Import Ship Outline (DXF).
TOOL_FIELDS: List[FieldSpec] = [
    ("tool_id", "str"),
    ("name", "str"),
    ("tool_type", "str"),            # method id: one of METHODS (open enum)
    ("source_ref", "str"),           # document + revision the values come from
    ("configs_json", "str"),
    ("footprint_wkt", "str"),
    ("footprint_source", "str"),     # e.g. the DXF file name
    ("footprint_scale", "float"),
    ("footprint_crp_x", "float"),
    ("footprint_crp_y", "float"),
    ("footprint_rotation_deg", "float"),
    ("length_m", "float"),           # overall dimensions (optional)
    ("width_m", "float"),
    ("notes", "str"),
    ("created_utc", "str"),
    ("modified_utc", "str"),
]

# Risk Profile --------------------------------------------------------------
# Feature-level hazard register: checks scan registered inputs (rocks,
# pockmarks, sandwaves, mag/sonar/linear contacts, …) for features on or
# near the route and record each interaction as a hazard with a risk level.
# Distinct from the Exclusion stack: hazards never remove burial length.
RISK_UNASSIGNED = ""
RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"
RISK_LEVELS: List[str] = [RISK_LOW, RISK_MEDIUM, RISK_HIGH]

RISK_LABELS: Dict[str, str] = {
    RISK_UNASSIGNED: "Unassigned",
    RISK_LOW: "Low",
    RISK_MEDIUM: "Medium",
    RISK_HIGH: "High",
}

# Severity order for aggregation ("" sorts lowest).
RISK_ORDER: Dict[str, int] = {
    RISK_UNASSIGNED: 0, RISK_LOW: 1, RISK_MEDIUM: 2, RISK_HIGH: 3,
}

HAZARD_STATUS_OPEN = "open"
HAZARD_STATUS_NOTED = "noted"
HAZARD_STATUS_ACCEPTED = "accepted"
HAZARD_STATUS_MITIGATED = "mitigated"
HAZARD_STATUSES: List[str] = [
    HAZARD_STATUS_OPEN, HAZARD_STATUS_NOTED,
    HAZARD_STATUS_ACCEPTED, HAZARD_STATUS_MITIGATED,
]
HAZARD_STATUS_LABELS: Dict[str, str] = {
    HAZARD_STATUS_OPEN: "Open",
    HAZARD_STATUS_NOTED: "Noted",
    HAZARD_STATUS_ACCEPTED: "Accepted",
    HAZARD_STATUS_MITIGATED: "Mitigated",
}

HAZARD_SOURCE_CHECK = "check"
HAZARD_SOURCE_MANUAL = "manual"

RISK_SOURCE_AUTO = "auto"
RISK_SOURCE_USER = "user"

RISK_CHECK_FIELDS: List[FieldSpec] = [
    ("check_id", "str"),
    ("plan_id", "str"),
    ("seq", "int"),
    ("name", "str"),
    ("enabled", "int"),
    ("config_json", "str"),          # input_id / distance_m / filter /
    #                                  label_attribute / band_*_m /
    #                                  default_risk / attribute /
    #                                  attribute_rules
    ("source_ref", "str"),           # document + revision the criteria follow
    ("notes", "str"),
]

HAZARD_FIELDS: List[FieldSpec] = [
    ("hazard_id", "str"),
    ("plan_id", "str"),
    ("check_id", "str"),             # "" for manual hazards
    ("feature_ref", "str"),          # source feature key (carry-over match)
    ("label", "str"),
    ("kp", "float"),
    ("end_kp", "float"),             # = kp for point interactions
    ("offset_m", "float"),           # nearest approach; 0 when crossing
    ("crossing", "int"),
    ("crossing_angle_deg", "float"),  # nullable; line crossings only
    ("lat", "float"),
    ("lon", "float"),
    ("risk", "str"),                 # "" | low | medium | high (effective)
    ("auto_risk", "str"),            # the check's assessment (audit)
    ("risk_source", "str"),          # auto | user
    ("status", "str"),               # open | noted | accepted | mitigated
    ("attributes_json", "str"),      # snapshot of the attributes used
    ("source", "str"),               # check | manual
    ("notes", "str"),
]

# One persisted sampling pass per plan (depth + optional cross-offset
# depths): derived data — rebuilt on demand, never change-logged.
PROFILE_FIELDS: List[FieldSpec] = [
    ("profile_id", "str"),
    ("plan_id", "str"),
    ("created_utc", "str"),
    ("params_json", "str"),          # step/cross offset/scope/fingerprints
    ("samples_json", "str"),         # kps/depths/port/stbd arrays
    ("sample_count", "int"),
]

CHANGE_LOG_FIELDS: List[FieldSpec] = [
    ("change_id", "str"),
    ("plan_id", "str"),
    ("seq", "int"),
    ("utc", "str"),
    ("user", "str"),
    ("action", "str"),
    ("target_id", "str"),
    ("before_json", "str"),
    ("after_json", "str"),
    ("reason", "str"),
]

REGISTRY_TABLES: Dict[str, List[FieldSpec]] = {
    TABLE_META: META_FIELDS,
    TABLE_PLAN: PLAN_FIELDS,
    TABLE_INPUT: INPUT_FIELDS,
    TABLE_RULE: RULE_FIELDS,
    TABLE_GENERATION: GENERATION_FIELDS,
    TABLE_EVENT: EVENT_FIELDS,
    TABLE_SECTION: SECTION_FIELDS,
    TABLE_CHANGE_LOG: CHANGE_LOG_FIELDS,
    TABLE_PROFILE: PROFILE_FIELDS,
    TABLE_RISK_CHECK: RISK_CHECK_FIELDS,
    TABLE_HAZARD: HAZARD_FIELDS,
    TABLE_TOOL: TOOL_FIELDS,
}

TABLE_KEYS: Dict[str, str] = {
    TABLE_PLAN: "plan_id",
    TABLE_INPUT: "input_id",
    TABLE_RULE: "rule_id",
    TABLE_GENERATION: "generation_id",
    TABLE_EVENT: "event_id",
    TABLE_SECTION: "section_id",
    TABLE_CHANGE_LOG: "change_id",
    TABLE_PROFILE: "profile_id",
    TABLE_RISK_CHECK: "check_id",
    TABLE_HAZARD: "hazard_id",
    # bp_tool is project-scoped: it never appears in a plan's change-log
    # payloads or rollbacks; the key is here for the store's generic upsert.
    TABLE_TOOL: "tool_id",
}

# Per-plan spatial layer schemas -------------------------------------------
SECTIONS_LAYER_FIELDS: List[FieldSpec] = [
    ("section_ref", "str"),
    ("section_id", "str"),
    ("plan_id", "str"),
    ("kind", "str"),
    ("start_kp", "float"),
    ("end_kp", "float"),
    ("length_km", "float"),
    ("state", "str"),
    ("conclusion", "str"),
    ("confidence", "str"),
    ("reasons", "str"),
    ("tool", "str"),
    ("skip_handling", "str"),
    ("notes", "str"),
]

EVENTS_LAYER_FIELDS: List[FieldSpec] = [
    ("event_id", "str"),
    ("plan_id", "str"),
    ("seq", "int"),
    ("event_type", "str"),
    ("label", "str"),
    ("kp", "float"),
    ("lat", "float"),
    ("lon", "float"),
    ("depth_m", "float"),
    ("source", "str"),
    ("status", "str"),
    ("locked", "int"),
    ("notes", "str"),
]

HAZARDS_LAYER_FIELDS: List[FieldSpec] = [
    ("hazard_id", "str"),
    ("plan_id", "str"),
    ("label", "str"),
    ("check", "str"),
    ("kp", "float"),
    ("end_kp", "float"),
    ("offset_m", "float"),
    ("crossing", "int"),
    ("crossing_angle_deg", "float"),
    ("risk", "str"),
    ("status", "str"),
    ("source", "str"),
    ("notes", "str"),
]


def plan_layer_base(plan_name: str, rev_label: str, plan_id: str) -> str:
    """Base fragment for a plan's spatial layer names.

    Includes the short plan id so two like-named plans stay distinct
    (the ``assessment_output`` convention).
    """
    parts = [sanitize_slug(plan_name or "plan")]
    if rev_label:
        parts.append(sanitize_slug(rev_label))
    parts.append((plan_id or "")[:8] or "x")
    return "bp_" + "_".join(parts)


def sections_layer_name(plan_name: str, rev_label: str, plan_id: str) -> str:
    return f"{plan_layer_base(plan_name, rev_label, plan_id)}_sections"


def events_layer_name(plan_name: str, rev_label: str, plan_id: str) -> str:
    return f"{plan_layer_base(plan_name, rev_label, plan_id)}_events"


def hazards_layer_name(plan_name: str, rev_label: str, plan_id: str) -> str:
    return f"{plan_layer_base(plan_name, rev_label, plan_id)}_hazards"


def default_gpkg_path(project_path: str, project_title: str = "") -> str:
    """Default Burial Planner GeoPackage path beside the project file."""
    if project_path:
        stem = os.path.splitext(os.path.basename(project_path))[0]
        # Keep pointing at an existing file even if the folder probe fails now.
        beside = os.path.join(os.path.dirname(project_path), f"{stem}_burial_plans.gpkg")
        if os.path.exists(beside):
            return beside
    else:
        stem = sanitize_slug(project_title) if project_title else "project"
    return os.path.join(gpkg_folder_for(project_path), f"{stem}_burial_plans.gpkg")


def format_kp(kp) -> str:
    """KP display/export format: 3 decimal places (1 m) everywhere."""
    try:
        return f"{float(kp):.3f}"
    except (TypeError, ValueError):
        return ""

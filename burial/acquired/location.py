# -*- coding: utf-8 -*-
"""Where a project's burial data file lives.

The file sits beside the project's plan file: ``X_burial_plans.gpkg`` gives
``X_burial_data.gpkg`` (another plan file name ``Y.gpkg`` gives
``Y_burial_data.gpkg``). Once created, its path is kept in the project
(``SubseaCableTools/burial_data_gpkg``) and found only from there: every
unsaved project shares one default plan folder, so a data file found there
by name could belong to another project. A project that never used
acquired data therefore gets no file and no project entry until the user
adds data or opens a data file (the plugin's rule for project changes).
"""

from __future__ import annotations

import os
from typing import Optional, Tuple

from qgis.core import QgsProject

from .. import store as plan_store
from .store import AcquiredStore, is_data_store

PROJECT_SCOPE = plan_store.PROJECT_SCOPE
PROJECT_KEY = "burial_data_gpkg"
STATE_NONE, STATE_OK, STATE_MISSING, STATE_INVALID = "none", "ok", "missing", "invalid"


def project_data_path(project: Optional[QgsProject] = None) -> Optional[str]:
    project = project or QgsProject.instance()
    value, ok = project.readEntry(PROJECT_SCOPE, PROJECT_KEY, "")
    return value if ok and value else None


def set_project_data_path(path: str, project: Optional[QgsProject] = None) -> None:
    project = project or QgsProject.instance()
    project.writeEntry(PROJECT_SCOPE, PROJECT_KEY, os.path.abspath(path))


def data_path_for_plans(plans_path: str) -> str:
    folder, base = os.path.split(os.path.abspath(plans_path))
    stem = os.path.splitext(base)[0]
    if stem.endswith("_burial_plans"):
        stem = stem[:-len("_burial_plans")]
    return os.path.join(folder, f"{stem}_burial_data.gpkg")


def default_data_path(project: Optional[QgsProject] = None) -> str:
    """Where a new data file would go (nothing is created)."""
    project = project or QgsProject.instance()
    plans = plan_store.project_gpkg_path(project) or plan_store.default_project_gpkg_path(project)
    return data_path_for_plans(plans)


def resolve(project: Optional[QgsProject] = None) -> Tuple[Optional[str], str]:
    """``(path, state)`` of the project's data file: none yet, ok, missing, or not a data file."""
    path = project_data_path(project)
    if not path:
        return None, STATE_NONE
    if not os.path.exists(path):
        return path, STATE_MISSING
    return path, STATE_OK if is_data_store(path) else STATE_INVALID


def open_or_create(project: Optional[QgsProject] = None, path: Optional[str] = None) -> AcquiredStore:
    """The project's data file, created (and recorded in the project) when the user first needs it."""
    project = project or QgsProject.instance()
    target = path or project_data_path(project) or default_data_path(project)
    store = AcquiredStore(target).open() if is_data_store(target) else AcquiredStore.create(target)
    if project_data_path(project) != os.path.abspath(target):
        set_project_data_path(target, project)
    return store

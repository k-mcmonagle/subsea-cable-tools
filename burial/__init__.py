# -*- coding: utf-8 -*-
"""Burial Planner (beta) — plough / ROV jet burial planning for cable routes.

Structured like ``planner/``: a dockable workflow UI over a dedicated
per-project GeoPackage, with the analysis built on the shared Workbench
rules engine (``workbench/rules_engine.py`` + ``workbench/rules_inputs.py``).

Headless-first: ``schema``, ``events``, ``generation``, ``change_log`` and
``io_csv`` run and are tested without a GUI.
"""

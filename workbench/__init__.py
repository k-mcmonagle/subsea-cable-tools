# -*- coding: utf-8 -*-
"""Cable Route Workbench.

Makes assemblies, RPLs, and cable systems first-class entities in a QGIS
project, backed by a per-project GeoPackage. See individual modules:

- schema / store: GeoPackage persistence
- rpl_engine: pure recompute engine (distances, bearings, slack, KP)
- assembly_model: assembly dataclasses + catenary JSON round-trip
- fit: assembly -> route fitting (body landing positions)
- system_topology: CRA-style component/port/connection graph
- depth_service: depth sampling for interactive tools
- UI: rpl_manager_dock, assembly_manager_dock, sld_widget, rpl_edit_maptool
"""

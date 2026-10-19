# -*- coding: utf-8 -*-
"""Route Comments: spatial comments on cable routes and other project features.

- ``model``      pure data rules: status/priority choices, custom fields,
                 search and filters, spreadsheet import validation and export rows
- ``store``      the comment register: a GeoPackage (sqlite3, no QGIS) with
                 named comment sets and an adjacent attachments folder
- ``route_ref``  route identity, KP references and reference condition checks
                 on the plugin's KP services (QGIS core, no widgets)
- ``map_layers`` the clustered map display, selection highlight and pick tool
- ``capture``    the KP-range picker the KP Mouse Tool uses
- ``dialogs`` / ``dock``  the user interface

``model`` and ``store`` import no QGIS so they are tested headlessly.
"""

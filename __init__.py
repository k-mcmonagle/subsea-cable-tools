# -*- coding: utf-8 -*-

# Make the bundled libraries in 'lib/' importable as a fallback. The folder is
# *appended* to sys.path, so a module the host QGIS Python already ships (e.g.
# openpyxl / et_xmlfile on QGIS 3.40) is still imported from the host for this
# and every other plugin; only modules missing from the host (pyqtgraph,
# access_parser, ...) come from 'lib/'. sys.path is shared by the whole QGIS
# session, which is why the plugin must never put 'lib/' in front of it.
import os
import sys

# QGIS and lxml can load incompatible libxml runtimes on Windows. openpyxl's
# optional lxml acceleration has caused native access violations when parsing
# workbooks, so default openpyxl to its thread-safe standard-library XML
# backend. The variable is process-wide and read once, when openpyxl is first
# imported. setdefault: an explicit OPENPYXL_LXML in the user's environment is
# a deliberate choice (only the value "True" enables lxml) and is respected.
os.environ.setdefault("OPENPYXL_LXML", "False")

_lib_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lib')
# Normalised so the processing modules' own "lib_dir not in sys.path" checks
# recognise the entry and do not add it a second time.
if os.path.isdir(_lib_dir) and _lib_dir not in sys.path:
    sys.path.append(_lib_dir)


# noinspection PyPep8Naming
def classFactory(iface):  # pylint: disable=invalid-name
    """Load SubseaCableTools class from file SubseaCableTools.

    :param iface: A QGIS interface instance.
    :type iface: QgsInterface
    """
    #
    from .subsea_cable_tools import SubseaCableTools
    return SubseaCableTools(iface)

# -*- coding: utf-8 -*-

# Make bundled libraries in 'lib/' importable, but only for modules that aren't
# already available in the host QGIS Python environment. This avoids polluting
# the global import path when QGIS already ships a compatible version.
import os
import sys
import importlib.util

_plugin_dir = os.path.dirname(__file__)
_lib_dir = os.path.join(_plugin_dir, 'lib')
_VENDORED_MODULES = ('openpyxl', 'pyqtgraph', 'et_xmlfile', 'OpenGL')
if os.path.isdir(_lib_dir) and _lib_dir not in sys.path:
    if any(importlib.util.find_spec(name) is None for name in _VENDORED_MODULES):
        sys.path.insert(0, _lib_dir)

# noinspection PyPep8Naming
def classFactory(iface):  # pylint: disable=invalid-name
    """Load SubseaCableTools class from file SubseaCableTools.

    :param iface: A QGIS interface instance.
    :type iface: QgsInterface
    """
    #
    from .subsea_cable_tools import SubseaCableTools
    return SubseaCableTools(iface)

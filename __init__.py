# -*- coding: utf-8 -*-

# Ensure bundled libraries in 'lib/' are available for import
import os
import sys
plugin_dir = os.path.dirname(__file__)
lib_dir = os.path.join(plugin_dir, 'lib')
if lib_dir not in sys.path:
    sys.path.insert(0, lib_dir)

# noinspection PyPep8Naming
def classFactory(iface):  # pylint: disable=invalid-name
    """Load SubseaCableTools class from file SubseaCableTools.

    :param iface: A QGIS interface instance.
    :type iface: QgsInterface
    """
    #
    from .subsea_cable_tools import SubseaCableTools
    return SubseaCableTools(iface)

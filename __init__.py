# -*- coding: utf-8 -*-

# noinspection PyPep8Naming
def classFactory(iface):  # pylint: disable=invalid-name
    """Load SubseaCableTools class from file SubseaCableTools.

    :param iface: A QGIS interface instance.
    :type iface: QgsInterface
    """
    #
    from .subsea_cable_tools import SubseaCableTools
    return SubseaCableTools(iface)

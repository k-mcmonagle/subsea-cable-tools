"""Project teardown signals shared by QGIS 3.22+ tools."""


def project_teardown_signal_names(project):
    """Signals for idempotent cleanup on New, Open and Close.

    aboutToBeCleared was added in QGIS 3.34. On older releases, removeAll
    releases references before layer deletion, while cleared also catches
    empty projects (removeAll does not fire for them). A populated project
    therefore invokes legacy cleanup twice; callbacks must be idempotent.
    The fallback also resets tools on an explicit removeAllMapLayers() call.
    """
    if hasattr(project, "aboutToBeCleared"):
        return ("aboutToBeCleared",)
    return ("removeAll", "cleared")


def project_teardown_signal_name(project):
    """The earliest available teardown signal (for receiver-count checks)."""
    return project_teardown_signal_names(project)[0]


def project_teardown_signal(project):
    """The earliest available signal; use slots() to include empty projects."""
    return getattr(project, project_teardown_signal_name(project))


def project_teardown_slots(project, slot):
    """Return the same signal/slot pairs for connection and disconnection."""
    return tuple((getattr(project, name), slot)
                 for name in project_teardown_signal_names(project))

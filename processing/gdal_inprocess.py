# -*- coding: utf-8 -*-
"""GDAL raster steps run inside QGIS, through the GDAL Python bindings QGIS ships.

The MBES tools used to run GDAL's command-line programs (gdal_rasterize,
gdal_grid, gdal_translate, gdalbuildvrt, gdal_fillnodata) through the
Processing GDAL provider, which starts each one as a separate program.
Some Windows computers refuse to start those programs: an application
control policy (reputation-based, or a managed code-integrity / allow-list
policy) blocks unsigned executables, so a step failed with "Process
gdal_translate failed to start". The bindings do the same work in QGIS's
own process, from the same command-line arguments, so results are the
same, with no extra program to start and no new dependency. Progress and
Cancel are passed to Processing, and the equivalent command is logged.

GDAL exceptions are turned on only around each call (``gdal.ExceptionMgr``,
GDAL 3.7+), never globally, so other code in QGIS keeps its own setting.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Sequence, Union

from qgis.core import QgsProcessingException

# step: (gdal function, options class, command it stands for)
_STEPS = {
    "rasterize": ("Rasterize", "RasterizeOptions", "gdal_rasterize"),
    "grid": ("Grid", "GridOptions", "gdal_grid"),
    "translate": ("Translate", "TranslateOptions", "gdal_translate"),
    "buildvrt": ("BuildVRT", "BuildVRTOptions", "gdalbuildvrt"),
}


def _gdal():
    try:
        from osgeo import gdal
    except Exception as exc:  # pragma: no cover - every QGIS install ships the bindings
        raise QgsProcessingException(
            f"The GDAL Python bindings (osgeo.gdal) could not be loaded: {exc}") from exc
    return gdal


@contextmanager
def _exceptions(gdal):
    manager = getattr(gdal, "ExceptionMgr", None)
    if manager is None:   # GDAL < 3.7: errors are read from the return value and the last message
        yield
        return
    with manager(useExceptions=True):
        yield


def _progress(feedback):
    def callback(complete, _message, _data):
        if feedback is None:
            return 1
        try:
            feedback.setProgress(max(0.0, min(100.0, 100.0 * float(complete))))
        except Exception:  # noqa: BLE001 - progress is best effort
            pass
        return 0 if feedback.isCanceled() else 1
    return callback


def _shown(arguments: Sequence[str]) -> str:
    return " ".join(f'"{a}"' if (" " in str(a) or not str(a)) else str(a) for a in arguments)


def _fail(gdal, program: str, feedback, detail: str = "") -> QgsProcessingException:
    if feedback is not None and feedback.isCanceled():
        return QgsProcessingException(f"{program} was cancelled.")
    message = detail or gdal.GetLastErrorMsg() or "no output was written"
    return QgsProcessingException(f"{program} failed: {message}")


def run(step: str, destination: str, source: Union[str, Sequence[str]], arguments: Sequence[str],
        feedback=None) -> str:
    """Run one GDAL step (``rasterize``, ``grid``, ``translate`` or ``buildvrt``)
    from ``source`` to ``destination`` with the program's command-line
    ``arguments`` (without the file names). Returns ``destination``; raises
    :class:`QgsProcessingException` with GDAL's message on failure."""
    gdal = _gdal()
    function_name, options_name, program = _STEPS[step]
    arguments = [str(a) for a in arguments]
    sources = list(source) if isinstance(source, (list, tuple)) else [source]
    if feedback is not None:
        feedback.pushCommandInfo(f"{program} {_shown(arguments + sources + [destination])}  (GDAL, in QGIS)")
    options = getattr(gdal, options_name)(options=arguments, callback=_progress(feedback))
    gdal.ErrorReset()
    try:
        with _exceptions(gdal):
            dataset = getattr(gdal, function_name)(
                destination, sources if step == "buildvrt" else sources[0], options=options)
    except RuntimeError as exc:
        raise _fail(gdal, program, feedback, str(exc)) from exc
    if dataset is None:
        raise _fail(gdal, program, feedback)
    dataset.FlushCache()
    dataset = None   # closes the file, so the next step reads it complete
    return destination


def fill_nodata(source: str, destination: str, max_distance: int, feedback=None) -> str:
    """``gdal_fillnodata -md max_distance -si 0 -b 1``: copy ``source`` to
    ``destination`` (GeoTIFF) and interpolate its NoData holes up to
    ``max_distance`` pixels across, using the band's own validity mask."""
    gdal = _gdal()
    program = "gdal_fillnodata"
    if feedback is not None:
        feedback.pushCommandInfo(f"{program} -md {int(max_distance)} -si 0 -b 1 "
                                 f"{_shown([source, destination])}  (GDAL, in QGIS)")
    gdal.ErrorReset()
    try:
        with _exceptions(gdal):
            source_ds = gdal.Open(source)
            if source_ds is None:
                raise _fail(gdal, program, feedback)
            target = gdal.GetDriverByName("GTiff").CreateCopy(destination, source_ds, 0)
            source_ds = None
            if target is None:
                raise _fail(gdal, program, feedback)
            band = target.GetRasterBand(1)
            status = gdal.FillNodata(band, band.GetMaskBand(), float(max_distance), 0, [],
                                     callback=_progress(feedback))
            band.FlushCache()
            target = None
    except RuntimeError as exc:
        raise _fail(gdal, program, feedback, str(exc)) from exc
    if status not in (None, 0) or (feedback is not None and feedback.isCanceled()):
        raise _fail(gdal, program, feedback)
    return destination


def driver_for(path: str) -> str:
    """The GDAL driver for an output file name (GeoTIFF unless QGIS knows the extension)."""
    import os

    from qgis.core import QgsRasterFileWriter

    extension = os.path.splitext(str(path))[1].lstrip(".")
    try:
        return QgsRasterFileWriter.driverForExtension(extension) or "GTiff"
    except Exception:  # noqa: BLE001
        return "GTiff"

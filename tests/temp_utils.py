"""Temporary test folders, tolerating QGIS's lingering Windows file handles."""
from contextlib import contextmanager
import shutil
import tempfile


@contextmanager
def temporary_directory():
    """Python 3.9 equivalent of TemporaryDirectory(ignore_cleanup_errors=True)."""
    folder = tempfile.mkdtemp()
    try:
        yield folder
    finally:
        shutil.rmtree(folder, ignore_errors=True)

"""Mock data playback worker.

Feeds a list of raw message strings through the same parser used for TCP data.
This removes the need for an external test server script for basic testing.
"""

from __future__ import annotations

import time
from typing import List, Optional

from qgis.PyQt.QtCore import QThread, pyqtSignal

from .message_parser import MessageFormatConfig, ParserState, parse_line, MessageParseError


class MockDataWorker(QThread):
    """Worker thread for replaying mock data lines at a fixed rate."""

    data_received = pyqtSignal(dict)
    status_changed = pyqtSignal(str)
    headers_received = pyqtSignal(list)
    raw_data_received = pyqtSignal(str)

    def __init__(
        self,
        lines: List[str],
        parser_config: MessageFormatConfig,
        interval_seconds: float = 1.0,
        loop: bool = True,
    ):
        super().__init__()
        self._lines = lines or []
        self._config = parser_config
        self._interval = max(0.01, float(interval_seconds))
        self._loop = bool(loop)
        self._running = True
        self._state = ParserState()
        self._last_error: Optional[str] = None

    def run(self):
        if not self._lines:
            self.status_changed.emit("Mock: No input lines")
            return

        self.status_changed.emit("Mock: Running")

        idx = 0
        while self._running:
            if idx >= len(self._lines):
                if self._loop:
                    idx = 0
                else:
                    break

            line = self._lines[idx]
            idx += 1

            if not self._running:
                break

            line = (line or "").rstrip("\r\n")
            if not line.strip():
                time.sleep(self._interval)
                continue

            self.raw_data_received.emit(line)

            try:
                values, new_headers = parse_line(line, self._config, self._state)
                if new_headers is not None:
                    self.headers_received.emit(new_headers)
                if values is not None:
                    self.data_received.emit(values)
                self._last_error = None
            except MessageParseError as e:
                msg = str(e)
                if msg != self._last_error:
                    self.status_changed.emit(f"Mock parse error: {msg}")
                    self._last_error = msg
            except Exception as e:
                msg = str(e)
                if msg != self._last_error:
                    self.status_changed.emit(f"Mock error: {msg}")
                    self._last_error = msg

            time.sleep(self._interval)

        self.status_changed.emit("Mock: Stopped")

    def stop(self):
        try:
            self.blockSignals(True)
        except Exception:
            pass
        self._running = False
        self.quit()
        self.wait(5000)

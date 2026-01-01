"""Live Data Worker

Threading worker that receives data from a TCP socket and emits signals.

The worker is **string-first**: it receives raw lines and then parses them via
`live_data.message_parser` using a user-configurable message format.
"""

from qgis.PyQt.QtCore import QThread, pyqtSignal

import socket
from typing import Optional

from .message_parser import (
    MessageFormatConfig,
    ParserState,
    parse_line,
    MessageParseError,
)


class LiveDataWorker(QThread):
    """
    Worker thread for receiving live data from a TCP server.
    
    Signals:
        data_received: Emitted with dict of parsed data
        status_changed: Emitted with status string
        headers_received: Emitted with list of header names
        raw_data_received: Emitted with raw data string
    """
    
    data_received = pyqtSignal(dict)
    status_changed = pyqtSignal(str)
    headers_received = pyqtSignal(list)
    raw_data_received = pyqtSignal(str)

    def __init__(
        self,
        host: str,
        port: int,
        lat_field: str,
        lon_field: str,
        persist: bool,
        parser_config: Optional[MessageFormatConfig] = None,
        encoding: str = "utf-8",
    ):
        """
        Initialize the live data worker.
        
        Args:
            host: Server hostname/IP
            port: Server port
            lat_field: Name of latitude field in data
            lon_field: Name of longitude field in data
            persist: Whether to persist points on map
        """
        super().__init__()
        self.host = host
        self.port = port
        self.lat_field = lat_field
        self.lon_field = lon_field
        self.persist = persist
        self.running = True

        self._parser_config = parser_config or MessageFormatConfig()
        self._parser_state = ParserState()
        self._encoding = encoding or "utf-8"
        self._last_error: Optional[str] = None

    def get_persist(self) -> bool:
        """Return the persist setting for this worker."""
        return self.persist

    def run(self):
        """Main thread loop - connects to server and receives data."""
        sock = None
        try:
            print(f"DEBUG: Attempting to connect to {self.host}:{self.port}")
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)  # 5 second timeout on socket operations
            sock.connect((self.host, self.port))
            print(f"DEBUG: Connected successfully to {self.host}:{self.port}")
            self.status_changed.emit("Connected")
            buffer = ""

            # Receive and process line-delimited messages.
            # We don't assume a header line exists; the parser config defines the behavior.
            while self.running:
                try:
                    data = sock.recv(1024)
                    if not data:
                        print(f"DEBUG: Connection closed by server")
                        break
                    buffer += data.decode(self._encoding, errors="replace")
                    lines = buffer.split('\n')
                    buffer = lines[-1]  # Keep incomplete line
                    for line in lines[:-1]:
                        line = (line or "").rstrip("\r")
                        if not line.strip():
                            continue

                        self.raw_data_received.emit(line)

                        try:
                            values, new_headers = parse_line(line, self._parser_config, self._parser_state)
                            if new_headers is not None:
                                self.headers_received.emit(new_headers)
                            if values is not None:
                                self.data_received.emit(values)
                            self._last_error = None
                        except MessageParseError as e:
                            msg = str(e)
                            if msg != self._last_error:
                                self.status_changed.emit(f"Parse error: {msg}")
                                self._last_error = msg
                        except Exception as e:
                            msg = str(e)
                            if msg != self._last_error:
                                self.status_changed.emit(f"Error: {msg}")
                                self._last_error = msg
                except socket.timeout:
                    print(f"DEBUG: Socket timeout while waiting for data")
                    break
        except socket.gaierror as e:
            print(f"DEBUG: Address resolution error: {str(e)}")
            self.status_changed.emit(f"Error: Cannot resolve host '{self.host}'")
        except ConnectionRefusedError as e:
            print(f"DEBUG: Connection refused: {str(e)}")
            self.status_changed.emit(f"Error: Connection refused on {self.host}:{self.port}")
        except socket.timeout as e:
            print(f"DEBUG: Connection timeout: {str(e)}")
            self.status_changed.emit(f"Error: Connection timeout to {self.host}:{self.port}")
        except Exception as e:
            print(f"DEBUG: Unexpected error: {type(e).__name__}: {str(e)}")
            self.status_changed.emit(f"Error: {str(e)}")
        finally:
            # Clean up socket
            if sock:
                try:
                    sock.close()
                    print(f"DEBUG: Socket closed")
                except Exception as e:
                    print(f"DEBUG: Error closing socket: {str(e)}")
            self.status_changed.emit("Disconnected")

    def stop(self):
        """Stop the worker thread gracefully."""
        try:
            self.blockSignals(True)  # Block any signals from being emitted
            self.running = False  # Signal the run loop to exit
            self.quit()  # Quit the event loop
            # Wait up to 5 seconds for thread to finish
            if not self.wait(5000):
                print(f"WARNING: LiveDataWorker thread did not stop within 5 seconds")
        except Exception as e:
            print(f"DEBUG: Error stopping LiveDataWorker: {e}")


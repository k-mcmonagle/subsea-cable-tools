"""
Live Data Worker

Threading worker that receives data from a TCP socket and emits signals.
Handles CSV parsing and header extraction.
"""

from qgis.PyQt.QtCore import QThread, pyqtSignal
import socket
import csv


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

    def __init__(self, host: str, port: int, lat_field: str, lon_field: str, persist: bool):
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
        self.headers = []

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

            # Receive headers first
            while '\n' not in buffer and self.running:
                try:
                    data = sock.recv(1024)
                    if not data:
                        print(f"DEBUG: No data received from server (headers)")
                        break
                    buffer += data.decode('utf-8')
                except socket.timeout:
                    print(f"DEBUG: Socket timeout while waiting for headers")
                    break
            
            if '\n' in buffer:
                header_line, buffer = buffer.split('\n', 1)
                header_reader = csv.reader([header_line])
                self.headers = list(header_reader)[0]
                print(f"DEBUG: Headers received: {self.headers}")
                self.headers_received.emit(self.headers)
            else:
                print(f"DEBUG: No headers received - buffer was: {buffer}")
                self.status_changed.emit("Error: No headers received")
                return

            # Then receive data
            while self.running:
                try:
                    data = sock.recv(1024)
                    if not data:
                        print(f"DEBUG: Connection closed by server")
                        break
                    buffer += data.decode('utf-8')
                    lines = buffer.split('\n')
                    buffer = lines[-1]  # Keep incomplete line
                    for line in lines[:-1]:
                        if line.strip():
                            self.raw_data_received.emit(line)
                            # Parse CSV line
                            reader = csv.reader([line])
                            row = next(reader)
                            data_dict = dict(zip(self.headers, row))
                            print(f"Parsed line: {line}, row: {row}, data_dict: {data_dict}")
                            self.data_received.emit(data_dict)
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


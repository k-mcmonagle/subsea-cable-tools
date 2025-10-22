#!/usr/bin/env python3
"""
Live Data Server for Subse            try:
                # Send headers
                headers_line = ','.join(headers) + '\n'
                client_socket.send(headers_line.encode('utf-8'))
                print(f"Sent headers: {headers_line.strip()}")
                time.sleep(0.1)  # Small delay to ensure headers are sent before data
                
                # Then send data
                index = 0
                while True:
                    if index >= len(data):
                        index = 0  # Loop back to beginning

                    # Send the current record
                    message = data[index] + '\n'
                    client_socket.send(message.encode('utf-8'))
                    print(f"Sent record {index + 1}/{len(data)}: {message.strip()}")

                    index += 1
                    time.sleep(1)  # Wait 1 secondn Testing

This script reads data from a CSV file and serves it over a TCP socket,
sending one record per second. It loops back to the beginning when reaching the end.

Usage:
    python live_data_server.py

The server listens on localhost:12345 by default.
"""

import socket
import time
import csv
import sys
import os

# Path to the sample CSV file
CSV_PATH = r"C:\Users\McMonaglek\OneDrive - Global Marine Systems Limited\Documents\PROJECTS\QGIS Live Ship Data Trial\SampleData\EMCS_S013_Lay_Data_cable_lay.csv"

HOST = 'localhost'
PORT = 12345

def load_csv_data(csv_path):
    """Load headers and all rows from CSV file."""
    if not os.path.exists(csv_path):
        print(f"Error: CSV file not found at {csv_path}")
        sys.exit(1)

    with open(csv_path, 'r', newline='', encoding='utf-8') as csvfile:
        reader = csv.reader(csvfile)
        headers = next(reader)
        data = []
        for row in reader:
            data.append(','.join(row))
    return headers, data

def start_server(headers, data):
    """Start the TCP server and send data."""
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind((HOST, PORT))
    server_socket.listen(1)

    print(f"Live Data Server listening on {HOST}:{PORT}")
    print(f"Loaded {len(data)} records from CSV")
    print("Waiting for client connection...")

    try:
        while True:
            client_socket, addr = server_socket.accept()
            print(f"Client connected from {addr}")

            try:
                # Send headers first
                headers_line = ','.join(headers) + '\n'
                client_socket.send(headers_line.encode('utf-8'))
                print(f"Sent headers: {headers_line.strip()}")

                # Then send data
                index = 0
                while True:
                    if index >= len(data):
                        index = 0  # Loop back to beginning

                    # Send the current record
                    message = data[index] + '\n'
                    client_socket.send(message.encode('utf-8'))
                    print(f"Sent record {index + 1}/{len(data)}: {message.strip()}")

                    index += 1
                    time.sleep(1)  # Wait 1 second

            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                print("Client disconnected")
            finally:
                client_socket.close()

    except KeyboardInterrupt:
        print("\nServer shutting down...")
    finally:
        server_socket.close()

if __name__ == "__main__":
    headers, data = load_csv_data(CSV_PATH)
    start_server(headers, data)
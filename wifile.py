"""
WiFile - A simple command-line tool for transferring files over a network.

This module provides functionality to send and receive files between devices
on the same network using TCP sockets. It operates in two modes:
- Server mode: sends a file to connected clients
- Client mode: receives a file from a server
"""
import socket
import argparse
import os
import sys


def get_local_ip():
    """Get the local IP address of the machine."""
    try:
        # Create a socket and connect to a remote address to determine local IP
        # This doesn't actually send data, just determines routing
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
        return local_ip
    except (socket.error, OSError):
        # Fallback to localhost if we can't determine the actual IP
        return "127.0.0.1"


def start_server(port, filepath):
    """Run the server to send a file to a client."""
    if not os.path.isfile(filepath):
        print(f"Error: File '{filepath}' does not exist.")
        sys.exit(1)

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.bind(('0.0.0.0', port))  # Listen on all interfaces
    server_socket.listen(1)

    local_ip = get_local_ip()
    print(f"Server listening on port {port}")
    print(f"Server IP address: {local_ip}")
    print(
        f"Clients can connect using: python wifile.py client --host {local_ip}")
    print("Waiting for connection...")

    conn = None
    try:
        conn, addr = server_socket.accept()
        print(f"Connected by {addr}")

        # Send file name and size first
        filename = os.path.basename(filepath)
        filesize = os.path.getsize(filepath)
        conn.send(f"{filename}:{filesize}".encode())

        # Send file content
        with open(filepath, 'rb') as f:
            while True:
                data = f.read(1024)  # Read in 1KB chunks
                if not data:
                    break
                conn.send(data)
        print(f"File '{filename}' sent successfully.")
    except (socket.error, OSError, IOError) as e:
        print(f"Server error: {e}")
    finally:
        if conn is not None:
            conn.close()
        server_socket.close()


def start_client(host, port, output_dir):
    """Run the client to receive a file from the server."""
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client_socket.connect((host, port))
        print(f"Connected to server {host}:{port}")

        # Receive file name and size
        data = client_socket.recv(1024).decode()
        filename, filesize = data.split(':')
        filesize = int(filesize)
        output_path = os.path.join(output_dir, filename)

        # Receive file content
        with open(output_path, 'wb') as f:
            received = 0
            while received < filesize:
                data = client_socket.recv(1024)
                if not data:
                    break
                f.write(data)
                received += len(data)
        print(f"File '{filename}' received and saved to '{output_path}'.")
    except (socket.error, OSError, IOError, ValueError) as e:
        print(f"Client error: {e}")
    finally:
        client_socket.close()


def main():
    """Parse command-line arguments and run the appropriate mode (server or client)."""
    parser = argparse.ArgumentParser(
        description="CLI tool for file transfer over WiFi network")
    parser.add_argument(
        'mode', choices=['server', 'client'], help="Run as server or client")
    parser.add_argument('--port', type=int, default=12345,
                        help="Port to use (default: 12345)")
    parser.add_argument(
        '--file', help="Path to the file to send (server mode)")
    parser.add_argument('--host', help="Server IP address (client mode)")
    parser.add_argument('--output-dir', default=".",
                        help="Directory to save received file (client mode)")

    args = parser.parse_args()

    if args.mode == 'server':
        if not args.file:
            print("Error: --file is required in server mode.")
            sys.exit(1)
        start_server(args.port, args.file)
    elif args.mode == 'client':
        if not args.host:
            print("Error: --host is required in client mode.")
            sys.exit(1)
        start_client(args.host, args.port, args.output_dir)


if __name__ == "__main__":
    main()

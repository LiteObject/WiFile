"""
WiFile - A simple command-line tool for transferring files over a network.

This module provides functionality to send and receive files between devices
on the same network using TCP sockets. It operates in two modes:
- Server mode: sends a file (or all files in a folder) to connected clients
- Client mode: receives a file or a batch of files from a server

Folder transfers are sent over a single connection, one file at a time,
using a batch protocol:
    1. Server sends:  WFILE_BATCH:<count>\n
    2. For each file: <relative_path>:<size>\n  ->  ACK\n  ->  file content
    3. Server finishes with:  WFILE_DONE\n
"""

from __future__ import annotations

import socket
import argparse
import os
import sys
import time


def format_bytes(bytes_value: float) -> str:
    """Convert bytes to human readable format."""
    for unit in ["B", "KB", "MB", "GB"]:
        if bytes_value < 1024.0:
            return f"{bytes_value:.1f} {unit}"
        bytes_value /= 1024.0
    return f"{bytes_value:.1f} TB"


def show_progress(
    current: float, total: float, start_time: float | None = None
) -> None:
    """Display a progress bar for file transfer."""
    if total == 0:
        return

    percent = (current / total) * 100
    bar_length = 50
    filled_length = int(bar_length * current // total)
    progress_bar = "█" * filled_length + "-" * (bar_length - filled_length)

    current_formatted = format_bytes(current)
    total_formatted = format_bytes(total)

    progress_line = (
        f"\r|{progress_bar}| {percent:.1f}% ({current_formatted}/{total_formatted})"
    )

    if start_time:
        elapsed = time.time() - start_time
        if elapsed > 0 and current > 0:
            speed = current / elapsed
            speed_formatted = format_bytes(speed)
            eta_seconds = (total - current) / speed if speed > 0 else 0
            eta_formatted = f"{int(eta_seconds)}s"
            progress_line += f" - {speed_formatted}/s - ETA: {eta_formatted}"

    print(progress_line, end="", flush=True)

    if current >= total:
        print()  # New line when complete


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


def collect_files(
    filepath: str | None = None, folder: str | None = None
) -> list[tuple[str, str]]:
    """Collect the list of files to send.

    Returns a list of (relative_name, absolute_path) tuples. When a single
    file is given, relative_name is just the base name. When a folder is
    given, it walks recursively and returns paths relative to the folder
    (using '/' separators).
    """
    files: list[tuple[str, str]] = []
    if filepath:
        files.append((os.path.basename(filepath), filepath))
    elif folder:
        for root, dirs, names in os.walk(folder):
            dirs.sort()
            for name in sorted(names):
                abs_path = os.path.join(root, name)
                rel_path = os.path.relpath(abs_path, folder)
                files.append((rel_path.replace(os.sep, "/"), abs_path))
    return files


def send_one_file(conn: socket.socket, display_name: str, filepath: str) -> bool:
    """Send a single file over an open connection. Returns True on success."""
    filesize = os.path.getsize(filepath)
    header = f"{display_name}:{filesize}\n".encode()
    conn.send(header)

    # Wait for client acknowledgment
    try:
        ack = conn.recv(4)  # Expect "ACK\n"
        if ack != b"ACK\n":
            print("Client did not acknowledge header properly")
            return False
    except socket.timeout:
        print("Timeout waiting for client acknowledgment")
        return False

    # Send file content with progress bar
    print(f"Sending '{display_name}' ({format_bytes(filesize)})...")
    sent_bytes = 0
    start_time = time.time()

    with open(filepath, "rb") as f:
        while True:
            data = f.read(1024)  # Read in 1KB chunks
            if not data:
                break
            try:
                conn.send(data)
                sent_bytes += len(data)
                show_progress(sent_bytes, filesize, start_time)
            except (
                socket.error,
                ConnectionResetError,
                ConnectionAbortedError,
                BrokenPipeError,
            ) as e:
                print(f"\nConnection lost during transfer: {e}")
                transfer_msg = (
                    f"Transfer incomplete: {format_bytes(sent_bytes)} "
                    f"of {format_bytes(filesize)} sent"
                )
                print(transfer_msg)
                return False

    print(f"File '{display_name}' sent successfully.")
    return True


def prompt_next_target(
    files: list[tuple[str, str]], batch_mode: bool, source: str
) -> tuple[list[tuple[str, str]], bool, str] | None:
    """Ask the server operator what to serve next after a transfer.

    Returns a (files, batch_mode, source) tuple for the next round, or None
    if the operator chose to quit the server.
    """
    try:
        while True:
            choice = (
                input(
                    "\nChoose next action: " "(s)end same, (n)ew file/folder, (e)xit: "
                )
                .strip()
                .lower()
            )
            if choice in ("s", "same"):
                return files, batch_mode, source
            if choice in ("e", "exit", "q", "quit"):
                return None
            if choice in ("n", "new"):
                path = input("Enter new file or folder path: ").strip().strip('"')
                if os.path.isfile(path):
                    return collect_files(filepath=path), False, path
                if os.path.isdir(path):
                    new_files = collect_files(folder=path)
                    if not new_files:
                        print(f"Error: No files found in folder '{path}'.")
                        continue
                    return new_files, True, path
                print(f"Error: '{path}' does not exist. Please try again.")
                continue
            print("Invalid choice. Enter 's', 'n', or 'e'.")
    except EOFError:
        return None


def start_server(
    port: int, filepath: str | None = None, folder: str | None = None
) -> None:
    """Run the server to send a file, or all files in a folder, to clients.

    The server keeps running after each transfer completes. After every
    transfer it asks the operator whether to serve the same file(s)/folder
    again, switch to a new file or folder, or quit. Press Ctrl+C to stop.
    """
    if filepath and not os.path.isfile(filepath):
        print(f"Error: File '{filepath}' does not exist.")
        sys.exit(1)
    if folder and not os.path.isdir(folder):
        print(f"Error: Folder '{folder}' does not exist.")
        sys.exit(1)

    files = collect_files(filepath, folder)
    if not files:
        print("Error: No files found to send.")
        sys.exit(1)

    batch_mode = folder is not None
    source = filepath if filepath else folder

    def print_ready() -> None:
        """Print what the server is about to send next."""
        if batch_mode:
            print(f"Ready to send {len(files)} file(s) from '{source}' (one by one)")
        else:
            print(f"Ready to send '{files[0][0]}'")

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.bind(("0.0.0.0", port))  # Listen on all interfaces
    server_socket.listen(1)

    local_ip = get_local_ip()
    print(f"Server listening on port {port}")
    print(f"Server IP address: {local_ip}")
    print(f"Clients can connect using: python wifile.py client --host {local_ip}")
    print_ready()
    print("Waiting for connection...")
    print("Press Ctrl+C to stop the server.")

    try:
        while True:
            conn = None
            try:
                conn, addr = server_socket.accept()
                conn.settimeout(30)  # 30 second timeout
                print(f"\nConnected by {addr}")

                if batch_mode:
                    # Tell the client how many files to expect
                    conn.send(f"WFILE_BATCH:{len(files)}\n".encode())
                    for display_name, abs_path in files:
                        if not send_one_file(conn, display_name, abs_path):
                            print("Transfer stopped due to a connection issue.")
                            break
                    else:
                        conn.send(b"WFILE_DONE\n")
                        print(f"All {len(files)} file(s) sent successfully.")
                else:
                    display_name, abs_path = files[0]
                    send_one_file(conn, display_name, abs_path)
            except (socket.error, OSError, IOError) as e:
                if "10054" in str(e) or "forcibly closed" in str(e).lower():
                    print(f"\nClient disconnected unexpectedly: {e}")
                    disconnect_msg = (
                        "This usually means the client closed the connection "
                        "or network was interrupted."
                    )
                    print(disconnect_msg)
                else:
                    print(f"Server error: {e}")
            finally:
                if conn is not None:
                    conn.close()
                next_target = prompt_next_target(files, batch_mode, source)
                if next_target is None:
                    print("Server stopped by user.")
                    return
                files, batch_mode, source = next_target
                print_ready()
                print("Waiting for next connection... (Ctrl+C to stop)")
    except KeyboardInterrupt:
        print("\nServer stopped by user.")
    finally:
        server_socket.close()


def receive_header(client_socket: socket.socket) -> str:
    """Read a single header line (terminated by '\n') from the socket."""
    header_data = b""
    while True:
        chunk = client_socket.recv(1)
        if not chunk:
            raise ConnectionError("Connection closed while receiving header")
        header_data += chunk
        if header_data.endswith(b"\n"):
            break
    return header_data.decode("utf-8").strip()


def drain_bytes(client_socket: socket.socket, count: int) -> None:
    """Read and discard bytes from the socket (used to stay in sync)."""
    received = 0
    while received < count:
        try:
            data = client_socket.recv(min(1024, count - received))
            if not data:
                break
            received += len(data)
        except socket.timeout:
            break


def receive_one_file(
    client_socket: socket.socket,
    output_dir: str,
    auto_overwrite: bool,
    auto_rename: bool,
    header_str: str,
) -> str:
    """Receive one file given its already-read header line.

    Returns:
        "file"      - a file was received (fully or partially)
        "cancelled" - the user chose to cancel this transfer
    """
    try:
        filename, filesize = header_str.split(":")
        filesize = int(filesize)
    except (UnicodeDecodeError, ValueError) as e:
        raise ValueError(f"Invalid header format: {e}") from e

    # Sanitize the relative path to prevent path traversal
    normalized = filename.replace("\\", "/")
    parts = [p for p in normalized.split("/") if p not in ("", ".", "..")]
    safe_name = os.path.join(*parts) if parts else "unnamed"
    if safe_name != normalized:
        print(f"Warning: Path sanitized from '{filename}' to '{safe_name}'")

    output_path = os.path.join(output_dir, safe_name)
    output_parent = os.path.dirname(output_path)
    if output_parent:
        os.makedirs(output_parent, exist_ok=True)

    # Send acknowledgment
    client_socket.send(b"ACK\n")

    # Handle file conflicts
    if os.path.exists(output_path):
        if auto_overwrite:
            print(f"Overwriting existing file '{safe_name}'...")
        elif auto_rename:
            base_name, ext = os.path.splitext(os.path.basename(output_path))
            counter = 1
            while True:
                new_filename = f"{base_name}_{counter}{ext}"
                new_output_path = os.path.join(output_parent, new_filename)
                if not os.path.exists(new_output_path):
                    output_path = new_output_path
                    new_rel = os.path.relpath(output_path, output_dir)
                    print(f"Saving as '{new_rel}' to avoid conflict...")
                    break
                counter += 1
        else:
            print(f"Warning: File '{safe_name}' already exists in '{output_dir}'")
            while True:
                choice = (
                    input("Choose action: (o)verwrite, (r)ename, (c)ancel: ")
                    .lower()
                    .strip()
                )
                if choice in ["o", "overwrite"]:
                    print(f"Overwriting existing file '{safe_name}'...")
                    break
                elif choice in ["r", "rename"]:
                    base_name, ext = os.path.splitext(os.path.basename(output_path))
                    counter = 1
                    while True:
                        new_filename = f"{base_name}_{counter}{ext}"
                        new_output_path = os.path.join(output_parent, new_filename)
                        if not os.path.exists(new_output_path):
                            output_path = new_output_path
                            new_rel = os.path.relpath(output_path, output_dir)
                            print(f"Saving as '{new_rel}' instead...")
                            break
                        counter += 1
                    break
                elif choice in ["c", "cancel"]:
                    print("Transfer cancelled by user.")
                    drain_bytes(client_socket, filesize)
                    return "cancelled"
                else:
                    print("Invalid choice. Please enter 'o', 'r', or 'c'.")

    # Receive file content with progress bar
    print(f"Receiving '{safe_name}' ({format_bytes(filesize)})...")
    received = 0
    start_time = time.time()

    with open(output_path, "wb") as f:
        while received < filesize:
            try:
                # Only request the remaining bytes so we never consume the
                # next file's header (or WFILE_DONE) that may follow.
                data = client_socket.recv(min(1024, filesize - received))
                if not data:
                    print("\nConnection closed by server. Transfer incomplete.")
                    received_msg = (
                        f"Received: {format_bytes(received)} "
                        f"of {format_bytes(filesize)}"
                    )
                    print(received_msg)
                    break
                f.write(data)
                received += len(data)
                show_progress(received, filesize, start_time)
            except (socket.error, ConnectionResetError, ConnectionAbortedError) as e:
                print(f"\nConnection lost during transfer: {e}")
                received_msg = (
                    f"Received: {format_bytes(received)} "
                    f"of {format_bytes(filesize)}"
                )
                print(received_msg)
                break

    if received == filesize:
        print(f"File '{safe_name}' received and saved to '{output_path}'.")
    else:
        incomplete_msg = (
            f"Transfer incomplete. File saved as '{output_path}' "
            "but may be corrupted."
        )
        print(incomplete_msg)

    return "file"


def start_client(
    host: str,
    port: int,
    output_dir: str,
    auto_overwrite: bool = False,
    auto_rename: bool = False,
) -> None:
    """Run the client to receive a file or a batch of files from the server."""
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client_socket.settimeout(30)  # 30 second timeout
    try:
        client_socket.connect((host, port))
        print(f"Connected to server {host}:{port}")

        first_header = receive_header(client_socket)

        if first_header.startswith("WFILE_BATCH:"):
            # Batch transfer: receive files one by one until WFILE_DONE
            try:
                total = int(first_header.split(":", 1)[1])
            except ValueError:
                total = 0
            print(f"Receiving batch of {total} file(s)...")
            received_count = 0
            while True:
                header_str = receive_header(client_socket)
                if header_str == "WFILE_DONE":
                    break
                result = receive_one_file(
                    client_socket, output_dir, auto_overwrite, auto_rename, header_str
                )
                if result == "cancelled":
                    print("Batch transfer cancelled.")
                    return
                received_count += 1
            print(f"Batch complete: {received_count} of {total} file(s) received.")
        else:
            # Single file transfer
            result = receive_one_file(
                client_socket, output_dir, auto_overwrite, auto_rename, first_header
            )
            if result == "cancelled":
                print("Transfer cancelled.")
    except (socket.error, OSError, IOError, ValueError) as e:
        if "10054" in str(e) or "forcibly closed" in str(e).lower():
            print(f"Server disconnected unexpectedly: {e}")
            disconnect_msg = (
                "This usually means the server closed the connection "
                "or network was interrupted."
            )
            print(disconnect_msg)
        else:
            print(f"Client error: {e}")
    finally:
        client_socket.close()


def prompt_for_source() -> tuple[str | None, str | None]:
    """Prompt the user for a file or folder path to send.

    Returns a (filepath, folder) tuple with exactly one of them set.
    Raises EOFError if input is closed before a valid path is given.
    """
    while True:
        path = input("Enter file or folder path to send: ").strip().strip('"')
        if os.path.isfile(path):
            return path, None
        if os.path.isdir(path):
            return None, path
        print(f"Error: '{path}' does not exist. Enter a valid file or folder path.")


def main():
    """Parse command-line arguments and run the appropriate mode (server or client)."""
    parser = argparse.ArgumentParser(
        description="CLI tool for file transfer over WiFi network"
    )
    parser.add_argument(
        "mode", choices=["server", "client"], help="Run as server or client"
    )
    parser.add_argument(
        "--port", type=int, default=12345, help="Port to use (default: 12345)"
    )
    parser.add_argument("--file", help="Path to the file to send (server mode)")
    parser.add_argument(
        "--folder",
        help="Path to the folder whose contents to send one by one (server mode)",
    )
    parser.add_argument("--host", help="Server IP address (client mode)")
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory to save received file(s) (client mode)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Automatically overwrite existing files (client mode)",
    )
    parser.add_argument(
        "--auto-rename",
        action="store_true",
        help="Automatically rename if file exists (client mode)",
    )

    args = parser.parse_args()

    if args.mode == "server":
        filepath, folder = args.file, args.folder
        if not filepath and not folder:
            # No target given on the command line: ask for one interactively.
            try:
                filepath, folder = prompt_for_source()
            except (EOFError, KeyboardInterrupt):
                print("\nNo path provided. Exiting.")
                sys.exit(1)
        start_server(args.port, filepath, folder)
    elif args.mode == "client":
        if not args.host:
            print("Error: --host is required in client mode.")
            sys.exit(1)
        start_client(
            args.host, args.port, args.output_dir, args.overwrite, args.auto_rename
        )


if __name__ == "__main__":
    main()

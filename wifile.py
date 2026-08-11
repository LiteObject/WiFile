"""
WiFile - A simple command-line tool for transferring files over a network.

This module provides functionality to send and receive files between devices
on the same network using TCP sockets. It operates in two modes:
- Server mode: sends a file (or all files in a folder) to connected clients
- Client mode: receives a file or a batch of files from a server

Folder transfers are sent over a single connection, one file at a time,
using a framed wire protocol (see send_frame/recv_frame):
    1. Server sends:  KIND_BATCH  -> payload: 4-byte big-endian file count
    2. For each file: KIND_FILE   -> payload: <utf-8 name>\0<8-byte size>
                       Client resolves conflicts, then sends KIND_ACK
                       Server streams the raw file content
                       Client sends KIND_RESULT (0=ok, 1=error)
    3. Server finishes with: KIND_DONE
"""

from __future__ import annotations

import argparse
import os
import socket
import struct
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

    try:
        print(progress_line, end="", flush=True)
    except UnicodeEncodeError:
        # Fall back to ASCII when the output stream cannot encode the block
        # characters (e.g. output redirected to a cp1252-encoded file).
        ascii_line = progress_line.replace("█", "#")
        print(ascii_line, end="", flush=True)

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
        walk_root: str = folder
        for root, dirs, names in os.walk(walk_root):
            dirs.sort()
            for name in sorted(names):
                abs_path = os.path.join(root, name)
                rel_path = os.path.relpath(abs_path, walk_root)
                files.append((rel_path.replace(os.sep, "/"), abs_path))
    return files


# Control message kinds used by the wire protocol
KIND_BATCH = b"B"  # server -> client: batch start, payload = 4-byte count
KIND_FILE = b"H"  # server -> client: file header, payload = <name>\0<8-byte size>
KIND_ACK = b"A"  # client -> server: ready to receive (after conflict resolution)
KIND_DONE = b"D"  # server -> client: end of batch
KIND_RESULT = b"R"  # client -> server: file result, payload = 1 byte (0=ok, 1=error)

# Upper bound for any control frame payload (file paths are well under this)
MAX_FRAME_PAYLOAD = 64 * 1024


def send_frame(sock: socket.socket, kind: bytes, payload: bytes = b"") -> None:
    """Send one framed control message.

    Frames are 1-byte kind + 4-byte big-endian length + payload. sendall()
    guarantees the whole frame is written, so partial writes cannot occur.
    """
    sock.sendall(kind + struct.pack(">I", len(payload)) + payload)


def recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly n bytes, raising ConnectionError on EOF."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Connection closed while receiving data")
        buf.extend(chunk)
    return bytes(buf)


def recv_frame(
    sock: socket.socket, max_payload: int = MAX_FRAME_PAYLOAD
) -> tuple[bytes, bytes]:
    """Read one framed control message and return (kind, payload).

    Payloads larger than max_payload are rejected so a misbehaving peer cannot
    make us buffer unbounded data.
    """
    head = recv_exact(sock, 5)
    kind = head[:1]
    length = struct.unpack(">I", head[1:5])[0]
    if length > max_payload:
        raise ValueError(f"Frame payload too large: {length} bytes (max {max_payload})")
    payload = recv_exact(sock, length) if length else b""
    return kind, payload


def send_one_file(conn: socket.socket, display_name: str, filepath: str) -> bool:
    """Send a single file over an open connection. Returns True on success."""
    filesize = os.path.getsize(filepath)
    payload = display_name.encode("utf-8") + b"\x00" + struct.pack(">Q", filesize)
    send_frame(conn, KIND_FILE, payload)

    # Wait until the client signals it is ready. The client resolves conflicts
    # first, so a slow prompt cannot stall the stream before we start sending.
    try:
        kind, _ = recv_frame(conn)
    except socket.timeout:
        print("Timeout waiting for client acknowledgment")
        return False
    if kind != KIND_ACK:
        print("Client did not acknowledge header properly")
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
                conn.sendall(data)
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

    # Wait for the receiver's completion status so we only report success
    # once the file was actually written on the other side.
    try:
        kind, payload = recv_frame(conn)
    except socket.timeout:
        print("Timeout waiting for receiver completion status")
        return False
    if kind != KIND_RESULT:
        print("Unexpected reply from client")
        return False
    if payload and payload[0] == 0:
        print(f"File '{display_name}' sent successfully.")
        return True
    print(f"Client reported an error receiving '{display_name}'.")
    return False


def start_server(
    port: int, filepath: str | None = None, folder: str | None = None
) -> None:
    """Run the server to send a file, or all files in a folder, to a client."""
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

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.bind(("0.0.0.0", port))  # Listen on all interfaces
    server_socket.listen(1)

    local_ip = get_local_ip()
    print(f"Server listening on port {port}")
    print(f"Server IP address: {local_ip}")
    print(f"Clients can connect using: python wifile.py client --host {local_ip}")
    if batch_mode:
        print(f"Ready to send {len(files)} file(s) from '{folder}' (one by one)")
    else:
        print(f"Ready to send '{files[0][0]}'")
    print("Waiting for connection...")

    conn = None
    try:
        conn, addr = server_socket.accept()
        conn.settimeout(30)  # 30 second timeout
        print(f"Connected by {addr}")

        if batch_mode:
            # Tell the client how many files to expect
            send_frame(conn, KIND_BATCH, struct.pack(">I", len(files)))
            for display_name, abs_path in files:
                if not send_one_file(conn, display_name, abs_path):
                    print("Transfer stopped.")
                    return
            send_frame(conn, KIND_DONE)
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
        server_socket.close()


def receive_one_file(
    client_socket: socket.socket,
    output_dir: str,
    auto_overwrite: bool,
    auto_rename: bool,
    header_payload: bytes,
) -> str:
    """Receive one file given its already-read KIND_FILE payload.

    Returns:
        "file"      - a file was received (fully or partially)
        "cancelled" - the user chose to cancel this transfer
    """
    try:
        name_bytes, size_bytes = header_payload.split(b"\x00", 1)
        filename = name_bytes.decode("utf-8")
        filesize = struct.unpack(">Q", size_bytes)[0]
    except (ValueError, UnicodeDecodeError, struct.error) as e:
        raise ValueError(f"Invalid file header: {e}") from e

    # Sanitize the relative path to prevent path traversal
    normalized = filename.replace("\\", "/")
    parts = [p for p in normalized.split("/") if p not in ("", ".", "..")]
    safe_name = os.path.join(*parts) if parts else "unnamed"
    if safe_name.replace(os.sep, "/") != normalized:
        print(f"Warning: Path sanitized from '{filename}' to '{safe_name}'")

    output_path = os.path.join(output_dir, safe_name)
    output_parent = os.path.dirname(output_path)
    if output_parent:
        os.makedirs(output_parent, exist_ok=True)

    # Handle file conflicts BEFORE acknowledging, so the server does not start
    # streaming while we are waiting on user input.
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
                    # The server is waiting for our ACK; report a declined
                    # transfer so it stops cleanly instead of streaming.
                    send_frame(client_socket, KIND_RESULT, b"\x01")
                    return "cancelled"
                else:
                    print("Invalid choice. Please enter 'o', 'r', or 'c'.")

    # Open the output file before signaling readiness so we only ACK once we
    # can actually accept the transfer.
    try:
        out_file = open(output_path, "wb")
    except OSError as e:
        print(f"\nError creating file '{output_path}': {e}")
        send_frame(client_socket, KIND_RESULT, b"\x01")
        return "file"

    try:
        send_frame(client_socket, KIND_ACK)
    except (socket.error, OSError):
        out_file.close()
        print("Connection lost before file transfer started.")
        return "file"

    # Receive file content with progress bar
    print(f"Receiving '{safe_name}' ({format_bytes(filesize)})...")
    received = 0
    start_time = time.time()

    try:
        with out_file as f:
            while received < filesize:
                # Only request the remaining bytes so we never consume the
                # next frame (or KIND_DONE) that may follow.
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
    except OSError as e:
        print(f"\nError during transfer: {e}")
        received_msg = f"Received: {format_bytes(received)} of {format_bytes(filesize)}"
        print(received_msg)

    if received == filesize:
        print(f"File '{safe_name}' received and saved to '{output_path}'.")
        transfer_ok = True
    else:
        incomplete_msg = (
            f"Transfer incomplete. File saved as '{output_path}' "
            "but may be corrupted."
        )
        print(incomplete_msg)
        transfer_ok = False

    # Confirm completion status so the server never reports success for a
    # file we failed to write.
    try:
        send_frame(client_socket, KIND_RESULT, b"\x00" if transfer_ok else b"\x01")
    except (socket.error, OSError):
        pass

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

        first_kind, first_payload = recv_frame(client_socket)

        if first_kind == KIND_BATCH:
            # Batch transfer: receive files one by one until KIND_DONE
            total = struct.unpack(">I", first_payload)[0]
            print(f"Receiving batch of {total} file(s)...")
            received_count = 0
            while True:
                kind, payload = recv_frame(client_socket)
                if kind == KIND_DONE:
                    break
                if kind != KIND_FILE:
                    raise ValueError(f"Unexpected message kind: {kind!r}")
                result = receive_one_file(
                    client_socket, output_dir, auto_overwrite, auto_rename, payload
                )
                if result == "cancelled":
                    print("Batch transfer cancelled.")
                    return
                received_count += 1
            print(f"Batch complete: {received_count} of {total} file(s) received.")
        elif first_kind == KIND_FILE:
            # Single file transfer
            result = receive_one_file(
                client_socket, output_dir, auto_overwrite, auto_rename, first_payload
            )
            if result == "cancelled":
                print("Transfer cancelled.")
        else:
            raise ValueError(f"Unexpected message kind: {first_kind!r}")
    except (socket.error, OSError, IOError, ValueError, struct.error) as e:
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

    # Server mode source: only one of --file / --folder may be given
    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument("--file", help="Path to the file to send (server mode)")
    source_group.add_argument(
        "--folder",
        help="Path to the folder whose contents to send one by one (server mode)",
    )

    parser.add_argument("--host", help="Server IP address (client mode)")
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory to save received file(s) (client mode)",
    )

    # Client conflict policy: only one of --overwrite / --auto-rename may be given
    conflict_group = parser.add_mutually_exclusive_group()
    conflict_group.add_argument(
        "--overwrite",
        action="store_true",
        help="Automatically overwrite existing files (client mode)",
    )
    conflict_group.add_argument(
        "--auto-rename",
        action="store_true",
        help="Automatically rename if file exists (client mode)",
    )

    args = parser.parse_args()

    if args.mode == "server":
        if not args.file and not args.folder:
            print("Error: --file or --folder is required in server mode.")
            sys.exit(1)
        start_server(args.port, args.file, args.folder)
    elif args.mode == "client":
        if not args.host:
            print("Error: --host is required in client mode.")
            sys.exit(1)
        start_client(
            args.host, args.port, args.output_dir, args.overwrite, args.auto_rename
        )


if __name__ == "__main__":
    main()

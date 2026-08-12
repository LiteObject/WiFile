"""
WiFile - A simple command-line tool for transferring files over a network.

This module provides functionality to send and receive files between devices
on the same network using TCP sockets. It operates in two modes:
- Server mode: sends a file (or all files in a folder) to connected clients
- Client mode: receives a file or a batch of files from a server

Wire protocol
-------------
All control messages use a framed format: a 1-byte kind, a 4-byte big-endian
length, then that many payload bytes. File content is streamed raw after an
ACK. A single-file transfer is:

    1. Server sends:  KIND_FILE frame (name + NUL + 8-byte size)
    2. Client resolves name conflicts, then sends a KIND_ACK frame
    3. Server streams the file content
    4. Client writes the file, then sends a KIND_RESULT frame
       (b"\\x00" success / b"\\x01" failure)

A batch transfer additionally starts with a KIND_BATCH frame carrying the
file count and finishes with a KIND_DONE frame. If the client declines a
file (for example the user cancels during conflict resolution), it sends a
KIND_RESULT failure frame instead of a KIND_ACK and the server stops the
batch.
"""

from __future__ import annotations

import argparse
import os
import re
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


def _write_progress(line: str) -> None:
    """Write a progress line, falling back to ASCII if stdout can't encode it."""
    try:
        sys.stdout.write(line)
        sys.stdout.flush()
    except UnicodeEncodeError:
        # Some redirected/piped stdout uses a legacy codepage (for example
        # cp1252) that cannot represent the block character.
        sys.stdout.write(line.replace("\u2588", "#"))
        sys.stdout.flush()


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

    _write_progress(progress_line)

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


# --- Wire protocol ---------------------------------------------------------

DEFAULT_PORT = 12345

KIND_FILE = b"\x01"  # payload: name + NUL + 8-byte big-endian size
KIND_ACK = b"\x02"  # payload: empty (client is ready for content)
KIND_RESULT = b"\x03"  # payload: b"\x00" success / b"\x01" failure
KIND_BATCH = b"\x04"  # payload: 4-byte big-endian file count
KIND_DONE = b"\x05"  # payload: empty (batch finished)

MAX_HEADER_PAYLOAD = 1 << 20  # 1 MiB cap for a file header (name + size)


def recv_exact(sock: socket.socket, count: int) -> bytes:
    """Read exactly ``count`` bytes, raising ConnectionError on early EOF."""
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("Connection closed while receiving data")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def send_frame(sock: socket.socket, kind: bytes, payload: bytes = b"") -> None:
    """Send one framed control message (1-byte kind + 4-byte length + payload)."""
    sock.sendall(kind + struct.pack(">I", len(payload)) + payload)


def recv_frame(
    sock: socket.socket, max_payload: int = MAX_HEADER_PAYLOAD
) -> tuple[bytes, bytes]:
    """Receive one framed control message, returning (kind, payload).

    Raises ValueError if the declared payload exceeds ``max_payload`` and
    ConnectionError if the peer closes before a full frame arrives.
    """
    header = recv_exact(sock, 5)
    kind = header[:1]
    length = struct.unpack(">I", header[1:5])[0]
    if length > max_payload:
        raise ValueError(f"Frame payload too large: {length} > {max_payload}")
    return kind, recv_exact(sock, length)


def sanitize_relative_path(path: str) -> str:
    """Clean a remote filename so it stays inside the output directory.

    Normalizes separators, strips drive/UNC prefixes and any leading
    slashes, removes traversal components ('..', '.', ''), and falls back
    to 'unnamed' when nothing remains. The result is always a plain
    relative path, so joining it onto the output directory cannot escape.
    """
    if not path:
        return "unnamed"
    normalized = path.replace("\\", "/")
    normalized = re.sub(r"^[A-Za-z]:", "", normalized)  # strip drive prefix
    normalized = normalized.lstrip("/")
    parts = [p for p in normalized.split("/") if p not in ("", ".", "..")]
    return os.path.join(*parts) if parts else "unnamed"


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
    """Send a single file over an open connection. Returns True on success.

    Sends a KIND_FILE header frame, waits for the client's decision
    (KIND_ACK to proceed, KIND_RESULT to decline), streams the content with
    ``sendall``, then waits for a KIND_RESULT completion frame so the sender
    learns whether the receiver actually wrote the file.
    """
    try:
        filesize = os.path.getsize(filepath)
    except OSError as e:
        print(f"Error reading '{filepath}': {e}")
        return False

    header_payload = (
        display_name.encode("utf-8") + b"\x00" + struct.pack(">Q", filesize)
    )
    try:
        send_frame(conn, KIND_FILE, header_payload)
    except (socket.error, OSError) as e:
        print(f"Connection lost while sending header: {e}")
        return False

    # Wait for the client's decision. The client may be prompting the user,
    # so do not impose the idle timeout while we wait.
    old_timeout = conn.gettimeout()
    conn.settimeout(None)
    try:
        kind, _ = recv_frame(conn)
    except (socket.error, OSError, ValueError) as e:
        print(f"Connection lost while waiting for acknowledgment: {e}")
        return False
    finally:
        conn.settimeout(old_timeout)

    if kind == KIND_RESULT:
        print(f"Client declined '{display_name}'.")
        return False
    if kind != KIND_ACK:
        print("Client did not acknowledge header properly.")
        return False

    # Send file content with progress bar
    print(f"Sending '{display_name}' ({format_bytes(filesize)})...")
    sent_bytes = 0
    start_time = time.time()

    try:
        with open(filepath, "rb") as f:
            while True:
                data = f.read(1024)  # Read in 1KB chunks
                if not data:
                    break
                conn.sendall(data)
                sent_bytes += len(data)
                show_progress(sent_bytes, filesize, start_time)
    except (socket.error, OSError) as e:
        print(f"\nConnection lost during transfer: {e}")
        transfer_msg = (
            f"Transfer incomplete: {format_bytes(sent_bytes)} "
            f"of {format_bytes(filesize)} sent"
        )
        print(transfer_msg)
        return False

    # Wait for the client's completion result.
    try:
        kind, result = recv_frame(conn)
    except (socket.error, OSError, ValueError) as e:
        print(f"Connection lost while waiting for completion result: {e}")
        return False

    if kind == KIND_RESULT and result == b"\x00":
        print(f"File '{display_name}' sent successfully.")
        return True
    if kind == KIND_RESULT:
        print(f"Client reported a problem saving '{display_name}'.")
        return False
    print("Unexpected response from client after transfer.")
    return False


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
                input("\nChoose next action: (s)end same, (n)ew file/folder, (e)xit: ")
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

    if filepath:
        files = collect_files(filepath=filepath)
        batch_mode = False
        source: str = filepath
    elif folder:
        files = collect_files(folder=folder)
        batch_mode = True
        source = folder
    else:
        # main() prompts for a source when neither option is given, so this
        # branch should not be reachable from the CLI.
        print("Error: No file or folder specified.")
        sys.exit(1)

    if not files:
        print("Error: No files found to send.")
        sys.exit(1)

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
    if port != DEFAULT_PORT:
        client_cmd = f"python wifile.py client --host {local_ip} --port {port}"
    else:
        client_cmd = f"python wifile.py client --host {local_ip}"
    print(f"Clients can connect using: {client_cmd}")
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
                    send_frame(conn, KIND_BATCH, struct.pack(">I", len(files)))
                    for display_name, abs_path in files:
                        if not send_one_file(conn, display_name, abs_path):
                            print("Transfer stopped.")
                            break
                    else:
                        send_frame(conn, KIND_DONE)
                        print(f"All {len(files)} file(s) sent successfully.")
                else:
                    display_name, abs_path = files[0]
                    send_one_file(conn, display_name, abs_path)
            except (socket.error, OSError, IOError, ValueError) as e:
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

            # After each round, ask the operator what to serve next.
            next_target = prompt_next_target(files, batch_mode, source)
            if next_target is None:
                print("Server stopped by user.")
                break
            files, batch_mode, source = next_target
            print_ready()
            print("Waiting for next connection... (Ctrl+C to stop)")
    except KeyboardInterrupt:
        print("\nServer stopped by user.")
    finally:
        server_socket.close()


def receive_one_file(
    client_socket: socket.socket,
    output_dir: str,
    auto_overwrite: bool,
    auto_rename: bool,
    header_payload: bytes,
) -> str:
    """Receive one file given its already-read KIND_FILE header payload.

    The header payload is ``name + b"\\x00" + 8-byte big-endian size``. The
    client resolves any name conflict first, then acknowledges, streams the
    content into a temporary file, and atomically replaces the destination
    only after the full transfer and close succeed.

    Returns:
        "file"      - handled (success or failure is reported over the wire)
        "cancelled" - the user chose to cancel this transfer
    """
    try:
        name_bytes, size_bytes = header_payload.split(b"\x00", 1)
        filename = name_bytes.decode("utf-8")
        filesize = struct.unpack(">Q", size_bytes)[0]
    except (ValueError, UnicodeDecodeError, struct.error) as e:
        raise ValueError(f"Invalid header payload: {e}") from e

    safe_name = sanitize_relative_path(filename)
    if safe_name != filename:
        print(f"Warning: Path sanitized from '{filename}' to '{safe_name}'")

    output_path = os.path.join(output_dir, safe_name)
    output_parent = os.path.dirname(output_path)
    if output_parent:
        os.makedirs(output_parent, exist_ok=True)

    # Resolve name conflicts BEFORE acknowledging, so the server never
    # starts streaming while the user is still deciding.
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
                    # Tell the server to stop; it will not stream content.
                    try:
                        send_frame(client_socket, KIND_RESULT, b"\x01")
                    except (socket.error, OSError):
                        pass
                    return "cancelled"
                else:
                    print("Invalid choice. Please enter 'o', 'r', or 'c'.")

    # The decision is made: acknowledge and start streaming.
    send_frame(client_socket, KIND_ACK)

    # Receive into a temporary file, then atomically replace the destination
    # only after the full transfer and clean close succeed.
    print(f"Receiving '{safe_name}' ({format_bytes(filesize)})...")
    received = 0
    start_time = time.time()
    temp_path = f"{output_path}.wifile-part"
    success = False
    try:
        with open(temp_path, "wb") as f:
            while received < filesize:
                # Only request the remaining bytes so we never consume the
                # next frame (header or KIND_DONE) that may follow.
                chunk = recv_exact(client_socket, min(1024, filesize - received))
                f.write(chunk)
                received += len(chunk)
                show_progress(received, filesize, start_time)
        # The whole file arrived and the temp file closed cleanly.
        if received == filesize:
            os.replace(temp_path, output_path)
            success = True
    except (socket.error, OSError, ConnectionError) as e:
        print(f"\nError while receiving '{safe_name}': {e}")
        received_msg = f"Received: {format_bytes(received)} of {format_bytes(filesize)}"
        print(received_msg)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass

    try:
        send_frame(client_socket, KIND_RESULT, b"\x00" if success else b"\x01")
    except (socket.error, OSError):
        pass

    if success:
        print(f"File '{safe_name}' received and saved to '{output_path}'.")
    else:
        print(f"Transfer incomplete. File '{output_path}' was not saved.")
    return "file"


def prompt_next_output(output_dir: str) -> str | None:
    """Ask the client operator what to do after a download.

    Returns the output directory for the next round, or None to exit the
    client.
    """
    try:
        while True:
            choice = (
                input(
                    "\nChoose next action: "
                    "(c)ontinue in current location, "
                    "(n)ew output location, (e)xit: "
                )
                .strip()
                .lower()
            )
            if choice in ("c", "continue", "same"):
                return output_dir
            if choice in ("e", "exit", "q", "quit"):
                return None
            if choice in ("n", "new"):
                new_dir = input("Enter new output directory: ").strip().strip('"')
                if not new_dir:
                    print("Invalid: directory path cannot be empty.")
                    continue
                try:
                    os.makedirs(new_dir, exist_ok=True)
                except OSError as e:
                    print(f"Error: cannot use '{new_dir}': {e}")
                    continue
                print(f"Saving to '{new_dir}'.")
                return new_dir
            print("Invalid choice. Enter 'c', 'n', or 'e'.")
    except EOFError:
        return None


def start_client(
    host: str,
    port: int,
    output_dir: str,
    auto_overwrite: bool = False,
    auto_rename: bool = False,
) -> None:
    """Run the client to receive a file or a batch of files from the server.

    The client keeps running after each download. After every batch it asks
    whether to keep saving to the current output location, switch to a new
    output location, or exit. Press Ctrl+C to stop.
    """
    try:
        while True:
            client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                client_socket.settimeout(30)  # 30 second timeout
                client_socket.connect((host, port))
                print(f"Connected to server {host}:{port}")

                first_kind, first_payload = recv_frame(client_socket)

                if first_kind == KIND_BATCH:
                    # Batch transfer: receive files one by one until KIND_DONE
                    try:
                        total = struct.unpack(">I", first_payload)[0]
                    except struct.error:
                        total = 0
                    print(f"Receiving batch of {total} file(s)...")
                    received_count = 0
                    cancelled = False
                    while True:
                        kind, header_payload = recv_frame(client_socket)
                        if kind == KIND_DONE:
                            break
                        if kind != KIND_FILE:
                            print(f"Unexpected message from server: {kind!r}")
                            break
                        result = receive_one_file(
                            client_socket,
                            output_dir,
                            auto_overwrite,
                            auto_rename,
                            header_payload,
                        )
                        if result == "cancelled":
                            cancelled = True
                            break
                        received_count += 1
                    if cancelled:
                        print("Batch transfer cancelled.")
                    else:
                        print(
                            f"Batch complete: {received_count} of {total} "
                            "file(s) received."
                        )
                else:
                    # Single file transfer
                    result = receive_one_file(
                        client_socket,
                        output_dir,
                        auto_overwrite,
                        auto_rename,
                        first_payload,
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

            # After each round, ask the operator what to do next
            next_output = prompt_next_output(output_dir)
            if next_output is None:
                print("Client stopped by user.")
                break
            output_dir = next_output
            print("Waiting for the next transfer... (Ctrl+C to stop)")
    except KeyboardInterrupt:
        print("\nClient stopped by user.")


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
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Port to use (default: {DEFAULT_PORT})",
    )
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

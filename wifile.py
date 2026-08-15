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

User interface
--------------
All operator interaction goes through a :class:`UI` implementation. The
default :class:`ConsoleUI` reproduces the original command-line behavior
(print + input) exactly, so the CLI is unchanged. A web or graphical front
end passes its own implementation to ``start_server`` / ``start_client`` and
receives structured events (messages, progress, prompts) instead.
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import struct
import sys
import time
from collections.abc import Sequence
from typing import Protocol


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


class UI(Protocol):
    """Interface through which the transfer engine talks to its operator.

    The CLI uses :class:`ConsoleUI`, which reproduces the original print/
    input behavior exactly. A web or graphical front end supplies its own
    implementation to receive structured events instead of parsing terminal
    output.

    ``choose`` presents ``prompt`` and returns one of ``options`` (the
    console also prints ``invalid`` and re-prompts on bad input; ``default``
    is only meaningful to non-console UIs). ``ask_text`` asks for free-form
    input such as a path. ``should_stop`` is polled between rounds so a UI
    can end a persistent server/client without Ctrl+C.
    """

    def message(self, text: str) -> None:
        """Show a plain message to the operator."""

    def progress(
        self, current: float, total: float, start_time: float | None = None
    ) -> None:
        """Report transfer progress (current/total bytes, optional start time)."""

    def choose(
        self, prompt: str, options: Sequence[str], default: str, invalid: str
    ) -> str:
        """Ask the operator to pick one of ``options`` and return it."""

    def ask_text(self, prompt: str) -> str:
        """Ask the operator for free-form text and return it."""

    def should_stop(self) -> bool:
        """Return True when the operator wants the engine to stop."""


class ConsoleUI:
    """Console implementation of the UI protocol: the original CLI behavior.

    Every method reproduces what the CLI did before the UI seam existed, so
    the command-line interface is byte-for-byte identical. ``input`` and
    ``print`` are resolved at call time, so tests that mock ``builtins.input``
    or redirect stdout keep working.
    """

    def message(self, text: str) -> None:
        """Print the message to the console."""
        print(text)

    def progress(
        self, current: float, total: float, start_time: float | None = None
    ) -> None:
        """Render a progress bar to the console."""
        show_progress(current, total, start_time)

    def choose(
        self, prompt: str, options: Sequence[str], default: str, invalid: str
    ) -> str:
        """Prompt on the console until a valid option is entered."""
        del default  # the console never auto-selects; only non-console UIs use it
        while True:
            choice = input(prompt).strip().lower()
            if choice in options:
                return choice
            print(invalid)

    def ask_text(self, prompt: str) -> str:
        """Read a line of free-form text from the console."""
        return input(prompt).strip().strip('"')

    def should_stop(self) -> bool:
        """The console never requests a stop (Ctrl+C handles it)."""
        return False


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

# How long the server waits for the client to accept or decline a file.
# Bounded so a silent client cannot hold the server's only connection
# forever, yet generous enough for a user answering an interactive prompt.
DECISION_TIMEOUT = 300  # seconds


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

    Normalizes separators, strips drive prefixes from every component (a
    drive-qualified component anywhere in the path, such as
    'foo/C:/bar.txt', is just as dangerous as one at the start), removes
    traversal components ('..', '.', ''), and falls back to 'unnamed' when
    nothing remains. The result is always a plain relative path, so joining
    it onto the output directory cannot escape.
    """
    if not path:
        return "unnamed"
    normalized = path.replace("\\", "/")
    parts: list[str] = []
    for part in normalized.split("/"):
        part = re.sub(r"^[A-Za-z]:", "", part)  # strip any drive prefix
        if part in ("", ".", ".."):
            continue
        parts.append(part)
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


def send_one_file(
    conn: socket.socket, display_name: str, filepath: str, *, ui: UI | None = None
) -> bool:
    """Send a single file over an open connection. Returns True on success.

    Sends a KIND_FILE header frame, waits for the client's decision
    (KIND_ACK to proceed, KIND_RESULT to decline), streams the content with
    ``sendall``, then waits for a KIND_RESULT completion frame so the sender
    learns whether the receiver actually wrote the file.
    """
    if ui is None:
        ui = ConsoleUI()

    try:
        filesize = os.path.getsize(filepath)
    except OSError as e:
        ui.message(f"Error reading '{filepath}': {e}")
        return False

    header_payload = (
        display_name.encode("utf-8") + b"\x00" + struct.pack(">Q", filesize)
    )
    try:
        send_frame(conn, KIND_FILE, header_payload)
    except (socket.error, OSError) as e:
        ui.message(f"Connection lost while sending header: {e}")
        return False

    # Wait for the client's decision (ACK to proceed, RESULT to decline).
    # The client may be prompting the user, so the wait is generous, but it
    # is still bounded so a silent client cannot hold the server's only
    # connection forever.
    old_timeout = conn.gettimeout()
    conn.settimeout(DECISION_TIMEOUT)
    try:
        kind, _ = recv_frame(conn)
    except socket.timeout:
        ui.message(
            f"Timeout ({DECISION_TIMEOUT}s) waiting for the client to accept "
            f"or decline '{display_name}'. Aborting transfer."
        )
        return False
    except (socket.error, OSError, ValueError) as e:
        ui.message(f"Connection lost while waiting for acknowledgment: {e}")
        return False
    finally:
        conn.settimeout(old_timeout)

    if kind == KIND_RESULT:
        ui.message(f"Client declined '{display_name}'.")
        return False
    if kind != KIND_ACK:
        ui.message("Client did not acknowledge header properly.")
        return False

    # Send file content with progress bar
    ui.message(f"Sending '{display_name}' ({format_bytes(filesize)})...")
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
                ui.progress(sent_bytes, filesize, start_time)
    except (socket.error, OSError) as e:
        ui.message(f"\nConnection lost during transfer: {e}")
        transfer_msg = (
            f"Transfer incomplete: {format_bytes(sent_bytes)} "
            f"of {format_bytes(filesize)} sent"
        )
        ui.message(transfer_msg)
        return False

    # Wait for the client's completion result.
    try:
        kind, result = recv_frame(conn)
    except (socket.error, OSError, ValueError) as e:
        ui.message(f"Connection lost while waiting for completion result: {e}")
        return False

    if kind == KIND_RESULT and result == b"\x00":
        ui.message(f"File '{display_name}' sent successfully.")
        return True
    if kind == KIND_RESULT:
        ui.message(f"Client reported a problem saving '{display_name}'.")
        return False
    ui.message("Unexpected response from client after transfer.")
    return False


def prompt_next_target(
    files: list[tuple[str, str]],
    batch_mode: bool,
    source: str,
    *,
    ui: UI | None = None,
) -> tuple[list[tuple[str, str]], bool, str] | None:
    """Ask the server operator what to serve next after a transfer.

    Returns a (files, batch_mode, source) tuple for the next round, or None
    if the operator chose to quit the server.
    """
    if ui is None:
        ui = ConsoleUI()
    try:
        while True:
            choice = ui.choose(
                "\nChoose next action: (s)end same, (n)ew file/folder, (e)xit: ",
                ["s", "same", "n", "new", "e", "exit", "q", "quit"],
                "s",
                "Invalid choice. Enter 's', 'n', or 'e'.",
            )
            if choice in ("s", "same"):
                return files, batch_mode, source
            if choice in ("e", "exit", "q", "quit"):
                return None
            path = ui.ask_text("Enter new file or folder path: ")
            if os.path.isfile(path):
                return collect_files(filepath=path), False, path
            if os.path.isdir(path):
                new_files = collect_files(folder=path)
                if not new_files:
                    ui.message(f"Error: No files found in folder '{path}'.")
                    continue
                return new_files, True, path
            ui.message(f"Error: '{path}' does not exist. Please try again.")
    except EOFError:
        return None


def start_server(
    port: int,
    filepath: str | None = None,
    folder: str | None = None,
    *,
    ui: UI | None = None,
) -> None:
    """Run the server to send a file, or all files in a folder, to clients.

    The server keeps running after each transfer completes. After every
    transfer it asks the operator whether to serve the same file(s)/folder
    again, switch to a new file or folder, or quit. Press Ctrl+C to stop.
    """
    if ui is None:
        ui = ConsoleUI()

    if filepath and not os.path.isfile(filepath):
        ui.message(f"Error: File '{filepath}' does not exist.")
        if isinstance(ui, ConsoleUI):
            sys.exit(1)
        raise ValueError(f"File '{filepath}' does not exist")
    if folder and not os.path.isdir(folder):
        ui.message(f"Error: Folder '{folder}' does not exist.")
        if isinstance(ui, ConsoleUI):
            sys.exit(1)
        raise ValueError(f"Folder '{folder}' does not exist")

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
        ui.message("Error: No file or folder specified.")
        if isinstance(ui, ConsoleUI):
            sys.exit(1)
        raise ValueError("No file or folder specified")

    if not files:
        ui.message("Error: No files found to send.")
        if isinstance(ui, ConsoleUI):
            sys.exit(1)
        raise ValueError("No files found to send")

    def print_ready() -> None:
        """Report what the server is about to send next."""
        if batch_mode:
            ui.message(
                f"Ready to send {len(files)} file(s) from '{source}' (one by one)"
            )
        else:
            ui.message(f"Ready to send '{files[0][0]}'")

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.bind(("0.0.0.0", port))  # Listen on all interfaces
    server_socket.listen(1)
    # Poll accept() so a UI's stop flag is honored while waiting for the
    # next connection; invisible to the console user.
    server_socket.settimeout(1.0)

    local_ip = get_local_ip()
    ui.message(f"Server listening on port {port}")
    ui.message(f"Server IP address: {local_ip}")
    if port != DEFAULT_PORT:
        client_cmd = f"python wifile.py client --host {local_ip} --port {port}"
    else:
        client_cmd = f"python wifile.py client --host {local_ip}"
    ui.message(f"Clients can connect using: {client_cmd}")
    print_ready()
    ui.message("Waiting for connection...")
    ui.message("Press Ctrl+C to stop the server.")

    try:
        while not ui.should_stop():
            conn = None
            try:
                try:
                    conn, addr = server_socket.accept()
                except socket.timeout:
                    continue
                conn.settimeout(30)  # 30 second timeout
                ui.message(f"\nConnected by {addr}")

                if batch_mode:
                    # Tell the client how many files to expect
                    send_frame(conn, KIND_BATCH, struct.pack(">I", len(files)))
                    for display_name, abs_path in files:
                        if not send_one_file(conn, display_name, abs_path, ui=ui):
                            ui.message("Transfer stopped.")
                            break
                    else:
                        send_frame(conn, KIND_DONE)
                        ui.message(f"All {len(files)} file(s) sent successfully.")
                else:
                    display_name, abs_path = files[0]
                    send_one_file(conn, display_name, abs_path, ui=ui)
            except (socket.error, OSError, IOError, ValueError) as e:
                if "10054" in str(e) or "forcibly closed" in str(e).lower():
                    ui.message(f"\nClient disconnected unexpectedly: {e}")
                    disconnect_msg = (
                        "This usually means the client closed the connection "
                        "or network was interrupted."
                    )
                    ui.message(disconnect_msg)
                else:
                    ui.message(f"Server error: {e}")
            finally:
                if conn is not None:
                    conn.close()

            # After each round, ask the operator what to serve next.
            next_target = prompt_next_target(files, batch_mode, source, ui=ui)
            if next_target is None:
                ui.message("Server stopped by user.")
                break
            files, batch_mode, source = next_target
            print_ready()
            ui.message("Waiting for next connection... (Ctrl+C to stop)")
    except KeyboardInterrupt:
        ui.message("\nServer stopped by user.")
    finally:
        server_socket.close()


def receive_one_file(
    client_socket: socket.socket,
    output_dir: str,
    auto_overwrite: bool,
    auto_rename: bool,
    header_payload: bytes,
    *,
    ui: UI | None = None,
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
    if ui is None:
        ui = ConsoleUI()

    try:
        name_bytes, size_bytes = header_payload.split(b"\x00", 1)
        filename = name_bytes.decode("utf-8")
        filesize = struct.unpack(">Q", size_bytes)[0]
    except (ValueError, UnicodeDecodeError, struct.error) as e:
        raise ValueError(f"Invalid header payload: {e}") from e

    safe_name = sanitize_relative_path(filename)
    if safe_name != filename:
        ui.message(f"Warning: Path sanitized from '{filename}' to '{safe_name}'")

    output_path = os.path.join(output_dir, safe_name)

    # Defense in depth: the sanitized name must resolve inside the output
    # directory. If it ever escapes (for example a drive-qualified component
    # slipping through), reject the transfer instead of writing outside.
    resolved_output = os.path.normcase(os.path.abspath(output_dir))
    resolved_path = os.path.normcase(os.path.abspath(output_path))
    if not (
        resolved_path == resolved_output
        or resolved_path.startswith(resolved_output + os.sep)
    ):
        ui.message(
            f"Warning: path '{safe_name}' resolves outside the output "
            f"directory '{output_dir}'; rejecting the transfer."
        )
        try:
            send_frame(client_socket, KIND_RESULT, b"\x01")
        except (socket.error, OSError):
            pass
        return "file"

    output_parent = os.path.dirname(output_path)
    if output_parent:
        os.makedirs(output_parent, exist_ok=True)

    # Resolve name conflicts BEFORE acknowledging, so the server never
    # starts streaming while the user is still deciding.
    if os.path.exists(output_path):
        if auto_overwrite:
            ui.message(f"Overwriting existing file '{safe_name}'...")
        elif auto_rename:
            base_name, ext = os.path.splitext(os.path.basename(output_path))
            counter = 1
            while True:
                new_filename = f"{base_name}_{counter}{ext}"
                new_output_path = os.path.join(output_parent, new_filename)
                if not os.path.exists(new_output_path):
                    output_path = new_output_path
                    new_rel = os.path.relpath(output_path, output_dir)
                    ui.message(f"Saving as '{new_rel}' to avoid conflict...")
                    break
                counter += 1
        else:
            ui.message(f"Warning: File '{safe_name}' already exists in '{output_dir}'")
            choice = ui.choose(
                "Choose action: (o)verwrite, (r)ename, (c)ancel: ",
                ["o", "overwrite", "r", "rename", "c", "cancel"],
                "o",
                "Invalid choice. Please enter 'o', 'r', or 'c'.",
            )
            if choice in ("o", "overwrite"):
                ui.message(f"Overwriting existing file '{safe_name}'...")
            elif choice in ("r", "rename"):
                base_name, ext = os.path.splitext(os.path.basename(output_path))
                counter = 1
                while True:
                    new_filename = f"{base_name}_{counter}{ext}"
                    new_output_path = os.path.join(output_parent, new_filename)
                    if not os.path.exists(new_output_path):
                        output_path = new_output_path
                        new_rel = os.path.relpath(output_path, output_dir)
                        ui.message(f"Saving as '{new_rel}' instead...")
                        break
                    counter += 1
            else:
                ui.message("Transfer cancelled by user.")
                # Tell the server to stop; it will not stream content.
                try:
                    send_frame(client_socket, KIND_RESULT, b"\x01")
                except (socket.error, OSError):
                    pass
                return "cancelled"

    # The decision is made: acknowledge and start streaming.
    send_frame(client_socket, KIND_ACK)

    # Receive into a temporary file, then atomically replace the destination
    # only after the full transfer and clean close succeed.
    ui.message(f"Receiving '{safe_name}' ({format_bytes(filesize)})...")
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
                ui.progress(received, filesize, start_time)
        # The whole file arrived and the temp file closed cleanly.
        if received == filesize:
            os.replace(temp_path, output_path)
            success = True
    except (socket.error, OSError, ConnectionError) as e:
        ui.message(f"\nError while receiving '{safe_name}': {e}")
        received_msg = f"Received: {format_bytes(received)} of {format_bytes(filesize)}"
        ui.message(received_msg)
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
        ui.message(f"File '{safe_name}' received and saved to '{output_path}'.")
    else:
        ui.message(f"Transfer incomplete. File '{output_path}' was not saved.")
    return "file"


def prompt_next_output(output_dir: str, *, ui: UI | None = None) -> str | None:
    """Ask the client operator what to do after a download.

    Returns the output directory for the next round, or None to exit the
    client.
    """
    if ui is None:
        ui = ConsoleUI()
    try:
        while True:
            choice = ui.choose(
                "\nChoose next action: "
                "(c)ontinue in current location, "
                "(n)ew output location, (e)xit: ",
                ["c", "continue", "same", "n", "new", "e", "exit", "q", "quit"],
                "c",
                "Invalid choice. Enter 'c', 'n', or 'e'.",
            )
            if choice in ("c", "continue", "same"):
                return output_dir
            if choice in ("e", "exit", "q", "quit"):
                return None
            new_dir = ui.ask_text("Enter new output directory: ")
            if not new_dir:
                ui.message("Invalid: directory path cannot be empty.")
                continue
            try:
                os.makedirs(new_dir, exist_ok=True)
            except OSError as e:
                ui.message(f"Error: cannot use '{new_dir}': {e}")
                continue
            ui.message(f"Saving to '{new_dir}'.")
            return new_dir
    except EOFError:
        return None


def receive_batch(
    client_socket: socket.socket,
    output_dir: str,
    auto_overwrite: bool,
    auto_rename: bool,
    batch_payload: bytes,
    *,
    ui: UI | None = None,
) -> None:
    """Receive a batch of files until KIND_DONE, validating completion.

    The batch payload carries the announced file count. The batch is only
    reported as complete when a KIND_DONE frame arrives and the number of
    files received matches the announced count. Unexpected frame kinds and
    files beyond the announced count abort the batch with an error message.
    """
    if ui is None:
        ui = ConsoleUI()
    try:
        total = struct.unpack(">I", batch_payload)[0]
    except struct.error:
        total = 0
    ui.message(f"Receiving batch of {total} file(s)...")
    received_count = 0
    cancelled = False
    done = False
    while True:
        kind, header_payload = recv_frame(client_socket)
        if kind == KIND_DONE:
            done = True
            break
        if kind != KIND_FILE or received_count >= total:
            ui.message(f"Unexpected message from server: {kind!r}")
            break
        result = receive_one_file(
            client_socket,
            output_dir,
            auto_overwrite,
            auto_rename,
            header_payload,
            ui=ui,
        )
        if result == "cancelled":
            cancelled = True
            break
        received_count += 1

    if cancelled:
        ui.message("Batch transfer cancelled.")
    elif done and received_count == total:
        ui.message(f"Batch complete: {received_count} of {total} file(s) received.")
    elif done:
        ui.message(
            f"Error: server ended the batch early "
            f"({received_count} of {total} file(s) received)."
        )
    else:
        ui.message(
            f"Error: batch ended unexpectedly "
            f"({received_count} of {total} file(s) received)."
        )


def start_client(
    host: str,
    port: int,
    output_dir: str,
    auto_overwrite: bool = False,
    auto_rename: bool = False,
    *,
    ui: UI | None = None,
) -> None:
    """Run the client to receive a file or a batch of files from the server.

    The client keeps running after each download. After every batch it asks
    whether to keep saving to the current output location, switch to a new
    output location, or exit. Press Ctrl+C to stop.
    """
    if ui is None:
        ui = ConsoleUI()
    try:
        while not ui.should_stop():
            client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                client_socket.settimeout(30)  # 30 second timeout
                client_socket.connect((host, port))
                ui.message(f"Connected to server {host}:{port}")

                first_kind, first_payload = recv_frame(client_socket)

                if first_kind == KIND_BATCH:
                    receive_batch(
                        client_socket,
                        output_dir,
                        auto_overwrite,
                        auto_rename,
                        first_payload,
                        ui=ui,
                    )
                else:
                    # Single file transfer
                    result = receive_one_file(
                        client_socket,
                        output_dir,
                        auto_overwrite,
                        auto_rename,
                        first_payload,
                        ui=ui,
                    )
                    if result == "cancelled":
                        ui.message("Transfer cancelled.")
            except (socket.error, OSError, IOError, ValueError) as e:
                if "10054" in str(e) or "forcibly closed" in str(e).lower():
                    ui.message(f"Server disconnected unexpectedly: {e}")
                    disconnect_msg = (
                        "This usually means the server closed the connection "
                        "or network was interrupted."
                    )
                    ui.message(disconnect_msg)
                else:
                    ui.message(f"Client error: {e}")
            finally:
                client_socket.close()

            # After each round, ask the operator what to do next
            next_output = prompt_next_output(output_dir, ui=ui)
            if next_output is None:
                ui.message("Client stopped by user.")
                break
            output_dir = next_output
            ui.message("Waiting for the next transfer... (Ctrl+C to stop)")
    except KeyboardInterrupt:
        ui.message("\nClient stopped by user.")


def prompt_for_source(*, ui: UI | None = None) -> tuple[str | None, str | None]:
    """Prompt the user for a file or folder path to send.

    Returns a (filepath, folder) tuple with exactly one of them set.
    Raises EOFError if input is closed before a valid path is given.
    """
    if ui is None:
        ui = ConsoleUI()
    while True:
        path = ui.ask_text("Enter file or folder path to send: ")
        if os.path.isfile(path):
            return path, None
        if os.path.isdir(path):
            return None, path
        ui.message(
            f"Error: '{path}' does not exist. Enter a valid file or folder path."
        )


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

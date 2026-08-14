"""Launch the WiFile web UI.

Usage:
    python webui.py [--port 8765] [--host 127.0.0.1]

The UI binds to 127.0.0.1 by default. Pass ``--host 0.0.0.0`` to make it
reachable from other devices on the network — be aware that anyone who can
reach the UI can then ask this machine to send arbitrary local files, and
no authentication is provided.
"""

import argparse
from typing import Any

from wifile_web.server import WEB_PORT, create_server


def bind_server(host: str, port: int) -> tuple[Any, OSError | None]:
    """Create the web server, falling back to an ephemeral port on failure.

    Windows sometimes blocks specific ports (Hyper-V exclusions, other
    services), which shows up as OSError 10013/10048. Returns
    ``(server, bind_error)`` where ``bind_error`` is None on a clean bind and
    otherwise the error that triggered the fallback.
    """
    try:
        return create_server(host, port), None
    except OSError as e:
        if port == 0:
            raise
        return create_server(host, 0), e


def main() -> None:
    parser = argparse.ArgumentParser(description="WiFile web UI")
    parser.add_argument(
        "--port", type=int, default=WEB_PORT, help=f"Web UI port (default: {WEB_PORT})"
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address (default: 127.0.0.1; use 0.0.0.0 for LAN access)",
    )
    args = parser.parse_args()

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: binding to a non-loopback address exposes the UI to the")
        print("network. Anyone who can reach it can read local files from this")
        print("machine. There is no authentication. Only do this on trusted")
        print("networks.")

    server, bind_error = bind_server(args.host, args.port)
    actual_port = server.server_address[1]
    if bind_error is not None:
        print(f"Warning: could not bind port {args.port} ({bind_error}).")
        print(f"Using a free port instead: {actual_port}")
    print(f"WiFile web UI running at http://{args.host}:{actual_port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping web UI.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

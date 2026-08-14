"""WiFile web UI backend.

Public pieces:
    - :class:`WebState` - thread-safe store shared by engine threads and HTTP
    - :class:`WebUI` - adapter from the ``wifile.UI`` protocol to the store
    - :func:`create_server` - build the HTTP server (state + runner attached)
"""

from .adapter import WebUI
from .server import WEB_PORT, create_server
from .state import WebState

__all__ = ["WebState", "WebUI", "create_server", "WEB_PORT"]

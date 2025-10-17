"""Core Socket.IO handlers and helpers.

This package replaces the previous single module `sockets.py` to avoid
name collision with the `sockets/` feature gateway directory.

Import side‑effects: importing this package registers all core events.
"""

from .core import *  # noqa: F401,F403

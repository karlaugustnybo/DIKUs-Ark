"""Compatibility import for tooling that still expects ``app.app``.

The request path is Litestar/ASGI. Start it with Granian as documented in the
repository README; this module deliberately contains no Flask runtime.
"""

from backend.app import app

__all__ = ["app"]

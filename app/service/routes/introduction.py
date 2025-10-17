"""Deprecated introduction route module.

This module is kept solely to provide a clear runtime error for any remaining
legacy imports. The warm-up workflow now lives in
``app.service.routes.warm_up``.
"""

raise RuntimeError(
    "app.service.routes.introduction has been removed. Use app.service.routes.warm_up instead."
)
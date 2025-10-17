from flask import Blueprint

# Primary administrative blueprint (HTML interface)
admin_bp = Blueprint(
    "admin",
    __name__,
    template_folder="templates",
    static_folder="static",
)

# Companion blueprint hosting the REST API under /admin/api
admin_api_bp = Blueprint(
    "admin_api",
    __name__,
    url_prefix="/admin/api",
)

# Import routes so their view functions attach to the blueprints defined above.
from . import routes  # noqa: E402,F401
from . import api  # noqa: E402,F401

__all__ = ["admin_bp", "admin_api_bp"]

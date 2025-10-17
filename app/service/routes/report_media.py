import os
from flask import Blueprint, send_from_directory, abort
from flask_login import login_required
from app.config import Config


def _ensure_report_dir():
    os.makedirs(Config.MEDIA_REPORTS_DIR, exist_ok=True)


reports_bp = Blueprint("reports_bp", __name__)


@reports_bp.route(f"{Config.MEDIA_REPORTS_URL_PREFIX}/<path:filename>")
@login_required
def media_reports(filename):
    """Serve generated report PDFs from instance/uploads/reports.

    For security, only files within the configured reports directory are served.
    """
    _ensure_report_dir()
    # Prevent path traversal
    safe_name = os.path.normpath(filename).replace("..", "")
    directory = Config.MEDIA_REPORTS_DIR
    fpath = os.path.join(directory, safe_name)
    if not os.path.isfile(fpath):
        abort(404)
    return send_from_directory(directory, safe_name, as_attachment=False)

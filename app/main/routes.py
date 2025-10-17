# app/main/routes.py
from flask import Blueprint, render_template, redirect, url_for
from flask_login import current_user

from .video_library import get_video_manifest

main_bp = Blueprint("main_bp", __name__, template_folder="templates")

@main_bp.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("account_bp.account"))
    video_manifest = get_video_manifest()
    return render_template("main_index.html", video_manifest=video_manifest)


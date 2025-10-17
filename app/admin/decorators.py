# app/admin/decorators.py
from functools import wraps
from flask import redirect, url_for, flash
from flask_login import current_user

from functools import wraps
from typing import Any, Callable, TypeVar

from flask import flash, redirect, url_for
from flask_login import current_user

F = TypeVar("F", bound=Callable[..., Any])


def admin_required(func: F) -> F:
    """Ensure the current user is authenticated and has the admin role."""

    @wraps(func)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            flash("Please sign in to access the admin console.", "warning")
            return redirect(url_for("auth_bp.login"))
        if getattr(current_user, "role", "user") != "admin":
            flash("Administrator access required.", "danger")
            return redirect(url_for("main_bp.index"))
        return func(*args, **kwargs)

    return wrapper  # type: ignore[return-value]
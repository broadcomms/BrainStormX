"""Document module package, exposing service pipeline and scheduler."""

from .queue import scheduler, init_scheduler

__all__ = ["scheduler", "init_scheduler"]

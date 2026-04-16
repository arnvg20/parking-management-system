from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .app import app as app

__all__ = ["app"]


def __getattr__(name: str):
    if name == "app":
        from .app import app

        return app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

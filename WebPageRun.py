from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _reload_enabled() -> bool:
    return os.getenv("RELOAD", "0").strip().lower() in {"1", "true", "yes", "on"}


if __name__ == "__main__":
    uvicorn.run(
        "live_site.app:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=_reload_enabled(),
    )

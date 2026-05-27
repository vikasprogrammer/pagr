#!/usr/bin/env python3
"""Fallback entrypoint for hosts that run `python main.py`.

Prefer `uvicorn app:app` (see Procfile); this just forwards to the same app and
binds the platform-provided $PORT on 0.0.0.0.
"""
import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
    )

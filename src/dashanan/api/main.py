"""Uvicorn entry point for the Dashanan FastAPI host (DASH-STORY-023, FR-014).

Run with: `python -m dashanan.api.main` (reads `DASHANAN_API_HOST`/
`DASHANAN_API_PORT`, defaulting to `0.0.0.0:8000` inside a container --
`docker-compose.yml` publishes no port for this host itself, must-not-
deviate item 3: "the FR-014 host application (DASH-STORY-023) is a
separate concern" from that file's own five backing services).
"""

from __future__ import annotations

import os

import uvicorn

from dashanan.api.app import create_app


def main() -> None:
    """Build and serve the app. Fails closed via `create_app`'s own startup checks."""
    host = os.environ.get("DASHANAN_API_HOST", "0.0.0.0")
    port = int(os.environ.get("DASHANAN_API_PORT", "8000"))
    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    main()

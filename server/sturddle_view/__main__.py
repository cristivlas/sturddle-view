from __future__ import annotations

import argparse
import os

import uvicorn

from .config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="sturddle-view")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--desktop", action="store_true", help="Open in PyWebView native window")
    parser.add_argument("--reload", action="store_true", help="Dev mode: auto-reload on changes")
    parser.add_argument("--engine", default=None, help="Path to UCI engine binary")
    args = parser.parse_args()

    # Push CLI overrides into env so the worker process's Settings() picks them up.
    if args.engine:
        os.environ["STURDDLE_ENGINE_PATH"] = args.engine
    if args.host:
        os.environ["STURDDLE_HOST"] = args.host
    if args.port:
        os.environ["STURDDLE_PORT"] = str(args.port)

    settings = Settings()
    host = settings.host
    port = settings.port

    if args.desktop:
        from .desktop import run_desktop

        run_desktop(host=host, port=port)
        return

    uvicorn.run(
        "sturddle_view.app:create_app",
        host=host,
        port=port,
        reload=args.reload,
        factory=True,
    )


if __name__ == "__main__":
    main()

"""Command-line entry point for the local bridge."""

from __future__ import annotations

import argparse
import logging
import sys

from .config import DEFAULT_DIR, initialize, load_config


def main() -> int:
    parser = argparse.ArgumentParser(prog="claude-lark-link")
    parser.add_argument("command", choices=("init", "check", "serve"))
    args = parser.parse_args()
    try:
        if args.command == "init":
            initialize()
            print(f"Configuration directory: {DEFAULT_DIR}")
            return 0
        config = load_config()
        if not config.enabled:
            print("Set the app ID, app secret, owner Open ID and at least one allowed workspace.", file=sys.stderr)
            return 2
        if args.command == "check":
            print(f"Configuration is valid ({config.domain}, {len(config.workspaces)} allowed workspace(s)).")
            return 0
        import uvicorn

        from .api import create_app

        logging.getLogger("httpx").setLevel(logging.WARNING)
        uvicorn.run(create_app(config), host="127.0.0.1", port=config.port, proxy_headers=False, access_log=False)
        return 0
    except (OSError, TypeError, ValueError, RuntimeError):
        print("Could not start Claude Lark Link. Check local configuration and pause state.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Launch the Sideye server."""

import os
import sys

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uvicorn
from app.config import Config


def main():
    print(f"\n  Sideye")
    print(f"  http://{Config.APP_HOST}:{Config.APP_PORT}\n")

    issues = Config.validate()
    if issues:
        print(f"  Warnings: {', '.join(issues)}")
        print(f"  Copy .env.example to .env and fill in your tokens.\n")

    uvicorn.run(
        "app.main:app",
        host=Config.APP_HOST,
        port=Config.APP_PORT,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()

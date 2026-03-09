"""Detect coding standards and conventions from repository config files."""

import os
import re
import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def detect_standards(repo_path: str) -> dict:
    """Analyze repo config files to detect coding standards and conventions."""
    p = Path(repo_path)
    standards = {
        "language": _detect_language(p),
        "linters": [],
        "formatters": [],
        "test_framework": "",
        "test_command": "",
        "build_tool": "",
        "min_version": "",
        "naming_conventions": {},
        "style_notes": [],
    }

    _parse_precommit(p, standards)
    _parse_pyproject(p, standards)
    _parse_gomod(p, standards)
    _parse_makefile(p, standards)
    _parse_contributing(p, standards)
    _detect_naming_from_files(p, standards)

    return standards


def _detect_language(p: Path) -> str:
    go_files = list(p.glob("**/*.go"))[:1]
    py_files = list(p.glob("**/*.py"))[:1]
    if go_files and py_files:
        return "mixed"
    if go_files:
        return "go"
    if py_files:
        return "python"
    return "unknown"


def _parse_precommit(p: Path, s: dict) -> None:
    cfg = p / ".pre-commit-config.yaml"
    if not cfg.exists():
        return
    try:
        text = cfg.read_text()
        # Simple pattern matching (avoid yaml dependency)
        if "ruff" in text:
            s["linters"].append("ruff")
            s["formatters"].append("ruff-format")
        if "black" in text:
            s["formatters"].append("black")
        if "mypy" in text:
            s["linters"].append("mypy")
        if "flake8" in text:
            s["linters"].append("flake8")
        if "isort" in text:
            s["formatters"].append("isort")
        if "golangci" in text:
            s["linters"].append("golangci-lint")
        if "clang-format" in text:
            s["formatters"].append("clang-format")
        if "typos" in text:
            s["linters"].append("typos")
    except Exception as e:
        logger.warning("Failed to parse pre-commit config: %s", e)


def _parse_pyproject(p: Path, s: dict) -> None:
    toml = p / "pyproject.toml"
    if not toml.exists():
        return
    try:
        text = toml.read_text()
        if "pytest" in text:
            s["test_framework"] = "pytest"
            s["test_command"] = "pytest"
        if "requires-python" in text:
            m = re.search(r'requires-python\s*=\s*"([^"]+)"', text)
            if m:
                s["min_version"] = f"python {m.group(1)}"
        if "[tool.ruff]" in text:
            s["linters"].append("ruff") if "ruff" not in s["linters"] else None
        if "[tool.mypy]" in text:
            s["linters"].append("mypy") if "mypy" not in s["linters"] else None
    except Exception as e:
        logger.warning("Failed to parse pyproject.toml: %s", e)


def _parse_gomod(p: Path, s: dict) -> None:
    gomod = p / "go.mod"
    if not gomod.exists():
        return
    try:
        text = gomod.read_text()
        m = re.search(r"^go\s+(\d+\.\d+)", text, re.MULTILINE)
        if m:
            s["min_version"] = f"go {m.group(1)}"
        s["test_framework"] = "go test"
        s["test_command"] = "go test ./..."
        s["build_tool"] = "go"
    except Exception as e:
        logger.warning("Failed to parse go.mod: %s", e)


def _parse_makefile(p: Path, s: dict) -> None:
    makefile = p / "Makefile"
    if not makefile.exists():
        return
    try:
        text = makefile.read_text()
        if "test:" in text or "test " in text:
            s["build_tool"] = "make"
            if not s["test_command"]:
                s["test_command"] = "make test"
        if "lint:" in text:
            s["style_notes"].append("Has 'make lint' target")
    except Exception as e:
        logger.warning("Failed to parse Makefile: %s", e)


def _parse_contributing(p: Path, s: dict) -> None:
    for name in ("CONTRIBUTING.md", "contributing.md", "CONTRIBUTING.rst"):
        f = p / name
        if f.exists():
            try:
                text = f.read_text()[:2000]
                s["style_notes"].append(f"Has {name} with guidelines")
                # Look for specific mentions
                if "sign off" in text.lower() or "dco" in text.lower():
                    s["style_notes"].append("Requires DCO/sign-off on commits")
            except Exception:
                pass
            break


def _detect_naming_from_files(p: Path, s: dict) -> None:
    """Sample a few source files to detect naming conventions."""
    lang = s["language"]
    if lang == "python":
        s["naming_conventions"] = {
            "functions": "snake_case",
            "classes": "PascalCase",
            "constants": "UPPER_SNAKE_CASE",
        }
    elif lang == "go":
        s["naming_conventions"] = {
            "exported": "PascalCase",
            "unexported": "camelCase",
            "packages": "lowercase",
        }

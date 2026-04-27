#!/usr/bin/env python3
"""Detect CODING_RULES anti-patterns not caught by ruff/black."""

import ast
import re
import sys
from pathlib import Path


_VIOLATIONS: list[tuple[str, int, str]] = []
_SEEN: set[tuple[str, int, str]] = set()


def _walk_shallow(nodes: list[ast.stmt]) -> list[ast.AST]:
    """Walk AST nodes without descending into nested function/class scopes."""
    result = []
    for node in nodes:
        result.append(node)
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                result.extend(_walk_shallow([child]))
    return result


def _check_file(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()

    # ── Regex check: # --- section comment style ─────────────────────────────
    for i, line in enumerate(lines, 1):
        if re.match(r"^#\s*-{3,}\s*$", line.strip()):
            _VIOLATIONS.append((str(path), i, "section comment uses '# ---' style — use '# ── Name ───' unicode style"))

    # ── AST checks ────────────────────────────────────────────────────────────
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return

    for node in ast.walk(tree):
        # stdlib logging import
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "logging" or alias.name.startswith("logging."):
                    _VIOLATIONS.append((str(path), node.lineno, "stdlib 'logging' import — use loguru logger"))

        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "logging" or module.startswith("logging."):
                _VIOLATIONS.append((str(path), node.lineno, "stdlib 'logging' import — use loguru logger"))

        # asyncio.gather(return_exceptions=True)
        if isinstance(node, ast.Call):
            func = node.func
            is_gather = (
                (isinstance(func, ast.Attribute) and func.attr == "gather")
                or (isinstance(func, ast.Name) and func.id == "gather")
            )
            if is_gather:
                for kw in node.keywords:
                    if (
                        kw.arg == "return_exceptions"
                        and isinstance(kw.value, ast.Constant)
                        and kw.value.value is True
                    ):
                        _VIOLATIONS.append(
                            (str(path), node.lineno, "asyncio.gather(return_exceptions=True) used — handle each result explicitly")
                        )

        # logger.error/warning() inside except block — traceback loss
        if isinstance(node, ast.ExceptHandler):
            for child in _walk_shallow(node.body):
                if not isinstance(child, ast.Call):
                    continue
                func = child.func
                if not (
                    isinstance(func, ast.Attribute)
                    and func.attr in ("error", "warning")
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "logger"
                ):
                    continue
                if func.attr == "error":
                    msg = "logger.error() in except block loses traceback — use logger.exception()"
                else:
                    msg = "logger.warning() in except block loses traceback — use logger.opt(exception=True).warning()"
                _VIOLATIONS.append((str(path), child.lineno, msg))


def main() -> int:
    roots = [Path("src"), Path("tests")]
    files: list[Path] = []
    for root in roots:
        if root.exists():
            files.extend(root.rglob("*.py"))

    for f in sorted(files):
        _check_file(f)

    unique = sorted(set(_VIOLATIONS))
    if not unique:
        print("check_antipatterns: no violations found")
        return 0

    print("check_antipatterns: violations found")
    for path, line, msg in unique:
        print(f"  {path}:{line}: {msg}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

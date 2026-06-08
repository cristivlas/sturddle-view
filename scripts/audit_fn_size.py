#!/usr/bin/env python3
"""Report functions over the size cap, across Python (AST) and JS (heuristic).

A function is exempt if the line immediately above its declaration (or any of
the up-to-4 lines above, to allow a multi-line reason) contains the marker
`oversized-ok`. Exempt functions are listed separately, never as violations.

JS counts are approximate (brace matching; strings/comments with braces can
inflate). Python counts are exact. Excludes tests and .venv.

Usage: python scripts/audit_fn_size.py [--cap N] [--exit-nonzero-on-violation]
"""
from __future__ import annotations

import argparse
import ast
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
PY_ROOT = REPO / "server" / "sturddle_view"
JS_ROOT = REPO / "web" / "app"
EXEMPT_MARKER = "oversized-ok"
_COMMENT_PREFIXES = ("//", "#", "*", "/*")


def _exempt_above(lines: list[str], decl_idx: int) -> bool:
    """Marker is exempt if it appears in the contiguous comment block
    directly above the declaration (any length), tolerating decorators and
    blank lines between the block and the decl."""
    i = decl_idx - 1
    while i >= 0:
        s = lines[i].strip()
        if not s or s.startswith("@"):  # blank line / decorator
            i -= 1
            continue
        if s.startswith(_COMMENT_PREFIXES):
            if EXEMPT_MARKER in lines[i]:
                return True
            i -= 1
            continue
        break  # first non-comment, non-blank line ends the block
    return False


def scan_python(cap: int):
    viol, exempt = [], []
    for p in PY_ROOT.rglob("*.py"):
        if "/tests/" in p.as_posix():
            continue
        src = p.read_text(encoding="utf-8")
        lines = src.splitlines()
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not (node.lineno and node.end_lineno):
                continue
            n = node.end_lineno - node.lineno + 1
            if n < cap:
                continue
            rec = (n, p.relative_to(REPO).as_posix(), node.lineno, node.name)
            (exempt if _exempt_above(lines, node.lineno - 1) else viol).append(rec)
    return viol, exempt


_JS_DECL = re.compile(
    r"(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(\w+)\s*\(|"
    r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>\s*\{|"
    r"^\s*(?:async\s+|get\s+|set\s+|static\s+)*(\w+)\s*\([^)]*\)\s*\{"
)
_JS_SKIP = {"if", "for", "while", "switch", "catch", "function", "return"}


def scan_js(cap: int):
    viol, exempt = [], []
    for p in JS_ROOT.rglob("*.js"):
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        seen = set()
        for i, line in enumerate(lines):
            m = _JS_DECL.search(line)
            if not m:
                continue
            name = m.group(1) or m.group(2) or m.group(3)
            if not name or name in _JS_SKIP:
                continue
            depth, started, end = 0, False, i
            for j in range(i, min(len(lines), i + 3000)):
                for ch in lines[j]:
                    if ch == "{":
                        depth += 1
                        started = True
                    elif ch == "}":
                        depth -= 1
                if started and depth <= 0:
                    end = j
                    break
            n = end - i + 1
            if n < cap:
                continue
            key = (p.as_posix(), name)
            if key in seen:
                continue
            seen.add(key)
            rec = (n, p.relative_to(REPO).as_posix(), i + 1, name)
            (exempt if _exempt_above(lines, i) else viol).append(rec)
    return viol, exempt


def _dump(title, rows):
    print(f"=== {title} ({len(rows)}) ===")
    for n, f, ln, name in sorted(rows, reverse=True):
        print(f"{n:5d}  {f}:{ln}  {name}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cap", type=int, default=200)
    ap.add_argument("--exit-nonzero-on-violation", action="store_true")
    args = ap.parse_args()

    py_v, py_e = scan_python(args.cap)
    js_v, js_e = scan_js(args.cap)
    viol = py_v + js_v
    exempt = py_e + js_e

    _dump(f"Violations >= {args.cap} lines", viol)
    print()
    _dump(f"Exempt (oversized-ok) >= {args.cap} lines", exempt)

    if args.exit_nonzero_on_violation and viol:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

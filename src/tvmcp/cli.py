"""`tv` CLI: every registered MCP tool is a subcommand emitting JSON.

Derives subcommands from the same registered tool schemas and invokes the same
implementations the server uses (no per-tool wrappers). JSON goes to stdout only;
warnings/errors go to stderr.

Usage:
  tv --check                       # list registered tools (JSON)
  tv <tool> '<json-args>'          # call a tool with a JSON object of args
  tv <tool> symbol=EURUSD count=200   # or key=value pairs (values JSON-parsed)
"""

from __future__ import annotations

import asyncio
import json
import sys

from fastmcp.exceptions import ToolError

from .config import load_settings
from .server import build_server


def _parse_args(raw_args: list[str]) -> dict:
    """Parse CLI tool args: a single JSON object, or `key=value` pairs."""
    if len(raw_args) == 1:
        try:
            obj = json.loads(raw_args[0])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass  # fall through to key=value parsing
    out: dict = {}
    for a in raw_args:
        if "=" not in a:
            raise ToolError(f"arg {a!r} is not key=value (or pass one JSON object)")
        k, v = a.split("=", 1)
        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            out[k] = v
    return out


def _check() -> int:
    settings = load_settings()
    mcp = build_server(settings)
    tools = asyncio.run(mcp.list_tools())
    print(json.dumps({
        "server": mcp.name,
        "toolsets": sorted(settings.toolsets),
        "tools": sorted(t.name for t in tools),
    }, indent=2))
    return 0


def _call(name: str, args: dict) -> int:
    settings = load_settings()
    mcp = build_server(settings)
    try:
        result = asyncio.run(mcp.call_tool(name, args))
    except ToolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # unexpected tool failure
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if result.is_error:
        print(f"error: {result.content}", file=sys.stderr)
        return 1
    text = getattr(result.content[0], "text", str(result.content[0]))
    print(json.dumps(json.loads(text), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: tv --check | tv <tool> [<json-args> | key=value ...]", file=sys.stderr)
        return 2
    if argv[0] == "--check":
        return _check()
    name = argv[0]
    try:
        args = _parse_args(argv[1:])
    except ToolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return _call(name, args)


if __name__ == "__main__":
    raise SystemExit(main())
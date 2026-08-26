"""`pine` toolset: compile/typecheck Pine Script via TradingView's pine-facade.

Uses the `translate_light` endpoint (owner-authorized surface, CLAUDE.md rule 1):
POST the source, get the real Pine compiler's verdict - success or a list of
errors/warnings with line/column positions. This enables a compiler-errors-in-a-loop
authoring workflow without a browser. Read-only; no script is published or saved to
the TradingView account. The endpoint is undocumented and may change or disappear.
Not affiliated with TradingView, Inc.

Auth: works unauthenticated for compile checks; if TV_SESSIONID is set it is sent as
the cookie (some account-gated built-ins may resolve only when authenticated).
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Callable

import httpx
from fastmcp.exceptions import ToolError
from pydantic import Field

from ..config import Settings

_TRANSLATE_URL = "https://pine-facade.tradingview.com/pine-facade/translate_light/"
_HEADERS = {"Origin": "https://www.tradingview.com", "Referer": "https://www.tradingview.com/"}
_MAX_SOURCE = 100_000  # bytes; a sane cap well above real strategies
_MAX_ERRORS = 50

# "line 3: syntax error at input 'x'" positions embedded in compiler messages
_POS_RE = re.compile(r"line (\d+):? ?(?:col(?:umn)? (\d+))?", re.IGNORECASE)


def _post_translate(source: str, session_id: str | None) -> dict:
    cookies = {"sessionid": session_id} if session_id else None
    try:
        r = httpx.post(
            _TRANSLATE_URL,
            data={"source": source},
            headers=_HEADERS,
            cookies=cookies,
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise ToolError(f"Could not reach pine-facade: {exc}") from exc
    if r.status_code != 200:
        raise ToolError(
            f"pine-facade returned HTTP {r.status_code}. The undocumented "
            "translate_light endpoint may have changed; see src/tvmcp/pine/toolset.py"
        )
    try:
        return r.json()
    except ValueError as exc:
        raise ToolError("pine-facade returned non-JSON; endpoint may have changed") from exc


def _extract_issues(payload: dict) -> list[dict]:
    """Normalize the facade's issue shapes to [{message, line, column, severity}].

    Observed live shape (2026-08-26): {"success": true, "result": {"errors2":
    [{"start": {"line": N, "column": M}, "end": {...}, "message": "..."}], ...}} -
    note `success` is true even when errors2 is non-empty; success means "the
    endpoint processed the request", not "the script compiled".
    """
    out: list[dict] = []

    def _push(message: str, severity: str, line=None, column=None) -> None:
        if len(out) >= _MAX_ERRORS:
            return
        if line is None:
            m = _POS_RE.search(message)
            if m:
                line = int(m.group(1))
                column = int(m.group(2)) if m.group(2) else None
        out.append({"message": message, "line": line, "column": column, "severity": severity})

    def _walk(node, severity: str) -> None:
        if isinstance(node, str) and node.strip():
            _push(node.strip(), severity)
        elif isinstance(node, dict):
            msg = node.get("message") or node.get("text") or node.get("reason")
            if isinstance(msg, str) and msg.strip():
                start = node.get("start") if isinstance(node.get("start"), dict) else {}
                line = node.get("line", start.get("line"))
                col = node.get("column", start.get("column"))
                _push(msg.strip(), severity, line, col)
        elif isinstance(node, list):
            for v in node:
                _walk(v, severity)

    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    for key, severity in (("errors", "error"), ("errors2", "error"),
                          ("warnings", "warning"), ("warnings2", "warning")):
        _walk(payload.get(key), severity)
        _walk(result.get(key), severity)
    if not out and not payload.get("success", False):
        reason = payload.get("reason") or payload.get("error")
        if isinstance(reason, str) and reason.strip():
            _push(reason.strip(), "error")
    return out


def compile_source(source: str, session_id: str | None) -> dict:
    """Compile Pine source; return {success, errors, warnings}.

    `success` = the compiler accepted the script (no errors). The endpoint's own
    `success` field only signals that the request was processed.
    """
    payload = _post_translate(source, session_id)
    issues = _extract_issues(payload)
    errors = [i for i in issues if i["severity"] == "error"]
    warnings_ = [i for i in issues if i["severity"] == "warning"]
    return {
        "success": bool(payload.get("success")) and not errors,
        "errors": errors,
        "warnings": warnings_,
    }


def register(mcp: Any, settings: Settings, compiler: Callable | None = None) -> None:
    compile_fn = compiler or (lambda src: compile_source(src, settings.session_id))

    @mcp.tool(tags={"pine"}, annotations={"readOnlyHint": True, "openWorldHint": True})
    def tv_pine_compile(
        source: Annotated[str, Field(description="Full Pine Script source, including the //@version line")],
    ) -> dict:
        """Compile/typecheck Pine Script with TradingView's real compiler.

        Returns {success, errors[], warnings[]}; each issue has message +
        line/column when the compiler provides them. Use in a loop: write Pine,
        compile, fix reported errors, repeat. Nothing is published or saved to any
        TradingView account. The backing endpoint is undocumented and may break.
        """
        if not source.strip():
            raise ToolError("source is empty - pass the full Pine Script text")
        if len(source.encode()) > _MAX_SOURCE:
            raise ToolError(f"source exceeds {_MAX_SOURCE} bytes; split the script")
        return compile_fn(source)

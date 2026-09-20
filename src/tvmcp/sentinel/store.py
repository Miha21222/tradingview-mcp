"""Run files for the sentinel: one JSON document per run, written atomically.

The MCP server is request/response over stdio, so a run cannot be a live object in
memory - it is a file the caller polls. A run file holds
`{version, run_id, spec, state, events, cursor, created, updated}`.

The file is data this tool wrote, but it is treated defensively on load: the
`run_id` must match a strict character class (no path traversal), the JSON must be
an object with the expected keys, and the spec is re-validated through
`SentinelSpec` before anything uses it. Nothing in a run file is ever evaluated.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .spec import SentinelSpec

RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
VERSION = 1
MAX_EVENTS_KEPT = 5000


class StoreError(Exception):
    """Bad run id, unreadable/malformed run file."""


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def validate_run_id(run_id: str) -> str:
    rid = (run_id or "").strip()
    if not RUN_ID_RE.match(rid) or rid in (".", ".."):
        raise StoreError(
            f"Invalid run_id {run_id!r}: use 1-64 chars of letters, digits, dot, dash, underscore"
        )
    return rid


def run_path(directory: Path, run_id: str) -> Path:
    return Path(directory) / f"{validate_run_id(run_id)}.json"


def exists(directory: Path, run_id: str) -> bool:
    return run_path(directory, run_id).exists()


def save(directory: Path, run_id: str, doc: dict) -> Path:
    """Atomic write: temp file in the same directory, then os.replace."""
    path = run_path(directory, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = dict(doc)
    doc["updated"] = now_iso()
    if len(doc.get("events") or []) > MAX_EVENTS_KEPT:
        doc["events"] = doc["events"][-MAX_EVENTS_KEPT:]
        doc["events_trimmed"] = True
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def load(directory: Path, run_id: str) -> tuple[dict, SentinelSpec]:
    """Read a run file and re-validate its spec. Returns (doc, spec)."""
    path = run_path(directory, run_id)
    if not path.exists():
        raise StoreError(f"No sentinel run {run_id!r} in {directory}")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StoreError(f"Run file {path.name} is unreadable: {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("spec"), dict):
        raise StoreError(f"Run file {path.name} is malformed (no spec object)")
    for key, kind in (("state", dict), ("events", list), ("cursor", dict)):
        if not isinstance(doc.get(key), kind):
            raise StoreError(f"Run file {path.name} is malformed (bad {key})")
    try:
        spec = SentinelSpec.model_validate(doc["spec"])
    except Exception as exc:  # pydantic ValidationError and friends
        raise StoreError(f"Run file {path.name} holds an invalid spec: {exc}") from exc
    doc.setdefault("run_id", run_id)
    return doc, spec


def list_runs(directory: Path) -> list[dict]:
    """Summaries of every run file, newest update first. Skips unreadable files."""
    directory = Path(directory)
    if not directory.exists():
        return []
    out: list[dict] = []
    for path in sorted(directory.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(doc, dict):
                continue
        except (OSError, json.JSONDecodeError):
            continue
        spec = doc.get("spec") or {}
        state = doc.get("state") or {}
        out.append({
            "run_id": doc.get("run_id", path.stem),
            "symbol": spec.get("symbol"),
            "timeframe": spec.get("timeframe"),
            "phase": state.get("phase"),
            "replay": bool(doc.get("replay")),
            "events": len(doc.get("events") or []),
            "last_seq": (doc.get("cursor") or {}).get("last_seq", 0),
            "created": doc.get("created"),
            "updated": doc.get("updated"),
        })
    out.sort(key=lambda r: r.get("updated") or "", reverse=True)
    return out

"""Contract tests for the companion skills bundle.

Validates: expected 8 skills exist, each SKILL.md has spec-clean frontmatter
(name + description, name matches folder), stays <= 500 lines, and every `tv_*`
tool it references is a real registered tool (drives tool-ref drift to fail CI).
"""

import re
from pathlib import Path

import yaml

from tvmcp.config import ALL_TOOLSETS, Settings
from tvmcp.server import build_server

SKILLS = Path(__file__).parent.parent / "skills"
EXPECTED = {
    "smc-scanning", "chart-markup", "strategy-backtesting", "pine-authoring",
    "risk-sizing", "market-screening", "strategy-review", "journal-sync",
    "tvmcp-guide", "tradingview-tiers",
}
MAX_LINES = 500


def _settings(tmp_path) -> Settings:
    return Settings(
        toolsets=ALL_TOOLSETS,
        extra_tools=frozenset(),
        read_only=False,
        cache_dir=tmp_path,
        chart_dir=tmp_path / "charts",
        journal_dir=tmp_path / "journal",
        strategy_dir=tmp_path / "strategies",
        max_bars=5000,
        oanda_api_key=None,
        oanda_env="practice",
        session_id=None,
    )


def _registered_tools(tmp_path) -> set[str]:
    mcp = build_server(_settings(tmp_path))
    import asyncio

    return {t.name for t in asyncio.run(mcp.list_tools())}


def test_expected_skills_present():
    have = {p.name for p in SKILLS.iterdir() if (p / "SKILL.md").exists()}
    assert have == EXPECTED


def test_every_skill_has_valid_frontmatter(tmp_path):
    for name in EXPECTED:
        raw = (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
        assert raw.startswith("---\n"), f"{name}: missing frontmatter"
        parts = raw.split("---", 2)
        meta = yaml.safe_load(parts[1])
        assert meta.get("name") == name, f"{name}: frontmatter name mismatch"
        assert meta.get("description"), f"{name}: missing description"
        assert len(raw.splitlines()) <= MAX_LINES, f"{name}: > {MAX_LINES} lines"


def test_skill_tool_references_are_registered(tmp_path):
    registered = _registered_tools(tmp_path)
    for name in EXPECTED:
        raw = (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
        refs = set(re.findall(r"\btv_[a-z0-9_]+", raw))
        missing = refs - registered
        assert not missing, f"{name}: references unregistered tools {sorted(missing)}"


def test_every_reference_file_is_referenced(tmp_path):
    # no orphan references/ files
    referenced = set()
    for name in EXPECTED:
        raw = (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
        referenced |= {m for m in re.findall(r"`references/([a-z0-9.\-]+)`", raw)}
    for name in EXPECTED:
        ref_dir = SKILLS / name / "references"
        if ref_dir.exists():
            for f in ref_dir.iterdir():
                assert f.name in referenced, f"{name}: {f.name} not referenced from SKILL.md"


UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
PRIVATE_PATH_RE = re.compile(r"[Cc]:\\\\Users\\\\")


def test_skills_contain_no_private_ids_or_paths():
    # distributed skills must not leak the owner's Notion UUIDs / absolute paths
    for p in SKILLS.rglob("*.md"):
        text = p.read_text(encoding="utf-8")
        uuids = UUID_RE.findall(text)
        assert not uuids, f"{p.relative_to(SKILLS)} contains UUID-like ids: {uuids[:3]}"
        priv = PRIVATE_PATH_RE.findall(text)
        assert not priv, f"{p.relative_to(SKILLS)} contains private paths: {priv[:3]}"
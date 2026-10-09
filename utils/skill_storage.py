"""Skill index / listing utilities for the claude_agent plugin.

A "skill package" is a subdirectory under ``skills/`` that contains a
``SKILL.md`` file. The SKILL.md may start with a YAML front-matter block
delimited by ``---`` lines carrying ``name`` and ``description`` fields.
If no front-matter is present we fall back to the folder name and the
first non-empty line of the document.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any


def get_skills_dir(plugin_root: str | os.PathLike[str] | None = None) -> str:
    """Return the absolute path to the ``skills/`` directory.

    When *plugin_root* is omitted it is inferred from this file's location
    (``utils/skill_storage.py`` lives one level below the plugin root).
    The directory is created on demand.
    """
    if plugin_root is None:
        plugin_root = Path(__file__).resolve().parent.parent
    else:
        plugin_root = Path(plugin_root)
    skills_dir = plugin_root / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    return str(skills_dir)


def list_skills_sorted(skills_dir: str | os.PathLike[str]) -> list[Path]:
    """List skill package folders sorted by creation time (oldest first)."""
    root = Path(skills_dir)
    if not root.is_dir():
        return []
    folders = [p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")]
    try:
        folders.sort(key=lambda p: p.stat().st_ctime)
    except OSError:
        folders.sort(key=lambda p: p.name)
    return folders


_FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?\n)---\s*\n?", re.DOTALL)


def _parse_skill_md(skill_md_path: Path) -> dict[str, str]:
    """Extract ``name`` and ``description`` from a SKILL.md file."""
    info: dict[str, str] = {"name": "", "description": ""}
    try:
        text = skill_md_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return info

    body = text
    m = _FRONT_MATTER_RE.match(text)
    if m:
        front = m.group(1)
        body = text[m.end():]
        for key in ("name", "description"):
            mm = re.search(
                r"^" + key + r"\s*:\s*(.+?)\s*$",
                front,
                re.MULTILINE,
            )
            if mm:
                val = mm.group(1).strip().strip("\"'")
                if val:
                    info[key] = val

    if not info["name"]:
        # Fall back to first markdown heading or folder name.
        for line in body.splitlines():
            line = line.strip()
            if line.startswith("#"):
                heading = line.lstrip("#").strip()
                if heading:
                    info["name"] = heading
                    break
        if not info["name"]:
            info["name"] = skill_md_path.parent.name

    if not info["description"]:
        for line in body.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith("---"):
                info["description"] = line[:200]
                break

    return info


def load_skills_index(skills_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """Build an index of installed skill packages.

    Returns a dict shaped like::

        {"skills_count": 2, "skills": [
            {"name": "...", "folder": "...", "description": "..."},
            ...
        ]}
    """
    root = Path(skills_dir)
    if not root.is_dir():
        return {"skills_count": 0, "skills": []}

    skills: list[dict[str, str]] = []
    for folder in list_skills_sorted(root):
        skill_md = folder / "SKILL.md"
        if not skill_md.is_file():
            # Not a valid skill package — skip but still surface the folder.
            skills.append(
                {
                    "name": folder.name,
                    "folder": folder.name,
                    "description": "(missing SKILL.md)",
                }
            )
            continue
        info = _parse_skill_md(skill_md)
        skills.append(
            {
                "name": info["name"] or folder.name,
                "folder": folder.name,
                "description": info["description"],
            }
        )

    return {"skills_count": len(skills), "skills": skills}

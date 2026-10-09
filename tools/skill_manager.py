"""Skill manager tool — view / add (zip) / delete skill packages.

Three commands are recognised via the ``command`` parameter:

* ``查看技能`` — list installed skill packages by name.
* ``新增技能`` — accept one or more zip uploads, extract them with zip-slip
  protection, require a ``SKILL.md`` inside each package, and install under
  ``skills/``.
* ``删除技能N`` — delete the skill at the 1-based index *N* shown by
  ``查看技能``.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import uuid
from collections.abc import Generator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from zipfile import ZipFile

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage

from utils.skill_storage import get_skills_dir, list_skills_sorted


# ── file download helpers ───────────────────────────────────────────

def _get_file_content(url: str, timeout: int = 30) -> bytes:
    try:
        req = Request(url, headers={"User-Agent": "dify-plugin-skill-manager/1.0"})
        with urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        raise RuntimeError(f"文件下载失败: {str(e)}") from e


def _extract_url_and_name(file_item: Any) -> tuple[str | None, str | None]:
    url = None
    name = None
    if hasattr(file_item, "url"):
        url = getattr(file_item, "url", None)
    if hasattr(file_item, "filename"):
        name = getattr(file_item, "filename", None)
    if hasattr(file_item, "name") and not name:
        name = getattr(file_item, "name", None)
    if isinstance(file_item, dict):
        url = file_item.get("url", url)
        name = file_item.get("filename", name) or file_item.get("name", name)
    return url, name


def _infer_ext_from_url(url: str) -> str:
    path = urlparse(url).path
    ext = Path(path).suffix
    return ext if ext else ".zip"


def _safe_filename(preferred_name: str | None, fallback_ext: str = ".zip") -> str:
    if preferred_name:
        base = Path(preferred_name).name
        base = re.sub(r'[<>:"/\\|?*]+', "_", base).strip()
        if base:
            return base
    return f"{uuid.uuid4().hex}{fallback_ext}"


# ── zip safety ──────────────────────────────────────────────────────

def _is_within_dir(base: Path, target: Path) -> bool:
    try:
        base_resolved = base.resolve()
        target_resolved = target.resolve()
        return base_resolved == target_resolved or base_resolved in target_resolved.parents
    except Exception:
        return False


def _safe_extract_zip(zip_path: Path, dest_dir: Path) -> None:
    """Extract *zip_path* into *dest_dir* with zip-slip protection."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    with ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename
            if not name:
                continue
            if name.startswith("/") or name.startswith("\\") or ".." in Path(name).parts:
                raise RuntimeError("压缩包包含非法路径")
            target_path = (dest_dir / name).resolve()
            if not _is_within_dir(dest_dir, target_path):
                raise RuntimeError("压缩包包含越权路径")
            if info.is_dir():
                target_path.mkdir(parents=True, exist_ok=True)
                continue
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target_path, "wb") as dst:
                shutil.copyfileobj(src, dst)


def _find_skill_folders(extracted_root: Path) -> list[Path]:
    """Return the list of skill-package folders inside *extracted_root*.

    A valid skill package must contain a ``SKILL.md`` file.
    """
    candidates: list[Path] = [p for p in extracted_root.iterdir() if p.is_dir()]
    if candidates:
        with_skill_md = [p for p in candidates if (p / "SKILL.md").is_file()]
        if with_skill_md:
            return with_skill_md
        # If only one top-level folder and it contains SKILL.md at root
        if len(candidates) == 1 and (candidates[0] / "SKILL.md").is_file():
            return candidates
        # No SKILL.md found in any candidate
        return []
    if (extracted_root / "SKILL.md").is_file():
        return [extracted_root]
    return []


# ── tool class ──────────────────────────────────────────────────────

class SkillManagerTool(Tool):
    """View / add / delete skill packages installed under ``skills/``."""

    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        command = str(tool_parameters.get("command", "")).strip()
        files_param = tool_parameters.get("files")

        # ── 查看技能 ──────────────────────────────────────────────
        if command in ("查看技能", "查看 技能", "查看", "list", "ls"):
            skills_dir = get_skills_dir(os.path.dirname(os.path.dirname(__file__)))
            skills = list_skills_sorted(skills_dir)
            if not skills:
                yield self.create_text_message("❌ 当前没有已安装的技能包。\n")
                return
            lines = [f"{idx + 1}. {p.name}" for idx, p in enumerate(skills)]
            yield self.create_text_message("👓 当前技能列表：\n" + "\n".join(lines) + "\n")
            return

        # ── 新增技能 ──────────────────────────────────────────────
        if command in ("新增技能", "存入技能", "保存技能", "add"):
            file_items: list[Any] = []
            if isinstance(files_param, list):
                file_items = [x for x in files_param if x]
            elif files_param:
                file_items = [files_param]
            elif tool_parameters.get("file"):
                file_items = [tool_parameters["file"]]

            if not file_items:
                yield self.create_text_message(
                    "❌ 未检测到上传的 zip 文件，请通过 files 参数上传。\n"
                )
                return

            skills_dir = Path(
                get_skills_dir(os.path.dirname(os.path.dirname(__file__)))
            )
            installed: list[str] = []

            for file_item in file_items:
                url, preferred_name = _extract_url_and_name(file_item)
                if not url:
                    yield self.create_text_message(
                        "❌ 无法获取文件 URL，请检查入参（files[i].url）。\n"
                    )
                    return

                filename_attr = None
                try:
                    filename_attr = getattr(file_item, "filename", None)
                except Exception:
                    filename_attr = None
                if isinstance(file_item, dict):
                    filename_attr = file_item.get("filename", filename_attr)

                try:
                    content = _get_file_content(str(url))
                except Exception as e:
                    yield self.create_text_message(str(e) + "\n")
                    return

                if filename_attr:
                    filename = Path(str(filename_attr)).name
                else:
                    ext = _infer_ext_from_url(str(url))
                    filename = _safe_filename(
                        preferred_name, fallback_ext=ext if ext else ".zip"
                    )

                with tempfile.TemporaryDirectory(prefix="skill-upload-") as td:
                    tmp_dir = Path(td)
                    zip_path = tmp_dir / filename
                    try:
                        zip_path.write_bytes(content)
                    except Exception as e:
                        yield self.create_text_message(
                            f"❌ 保存临时文件失败：{e}\n"
                        )
                        return

                    extract_dir = tmp_dir / "extracted"
                    try:
                        _safe_extract_zip(zip_path, extract_dir)
                    except Exception as e:
                        yield self.create_text_message(f"❌ 解压失败：{e}\n")
                        return

                    skill_folders = _find_skill_folders(extract_dir)
                    if not skill_folders:
                        yield self.create_text_message(
                            "❌ 压缩包内未找到合法技能目录（应包含 SKILL.md）。\n"
                        )
                        return

                    for folder in skill_folders:
                        target = skills_dir / folder.name
                        if target.exists():
                            yield self.create_text_message(
                                f"❌ 技能已存在：{folder.name}（请先删除同名技能）\n"
                            )
                            return
                        try:
                            shutil.move(str(folder), str(target))
                            installed.append(target.name)
                        except Exception as e:
                            yield self.create_text_message(
                                f"❌ 安装技能失败：{e}\n"
                            )
                            return

            yield self.create_text_message(
                "✅ 技能已安装：\n" + "\n".join(installed) + "\n"
            )
            skills = list_skills_sorted(skills_dir)
            if skills:
                lines = [f"{i + 1}. {p.name}" for i, p in enumerate(skills)]
                yield self.create_text_message(
                    "👓 当前技能列表：\n" + "\n".join(lines) + "\n"
                )
            else:
                yield self.create_text_message("😑 当前技能列表为空。\n")
            return

        # ── 删除技能N ─────────────────────────────────────────────
        m_del = re.match(r"^删除技能(\d+)$", command)
        if m_del:
            idx = int(m_del.group(1))
            skills_dir = get_skills_dir(os.path.dirname(os.path.dirname(__file__)))
            skills = list_skills_sorted(skills_dir)
            if idx < 1 or idx > len(skills):
                yield self.create_text_message(
                    "❌ 技能序号无效或超出范围。请先使用“查看技能”确认序号。\n"
                )
                return
            target = skills[idx - 1]
            try:
                shutil.rmtree(target, ignore_errors=False)
            except Exception as e:
                yield self.create_text_message(f"❌ 删除失败：{e}\n")
                return
            yield self.create_text_message(
                f"✅ 已删除技能{idx}：{target.name}\n"
            )
            skills = list_skills_sorted(skills_dir)
            if not skills:
                yield self.create_text_message("😑 当前技能列表为空。\n")
            else:
                lines = [f"{i + 1}. {p.name}" for i, p in enumerate(skills)]
                yield self.create_text_message(
                    "👓 当前技能列表：\n" + "\n".join(lines) + "\n"
                )
            return

        # ── unknown command ───────────────────────────────────────
        yield self.create_text_message(
            "😑 未识别的命令。支持：查看技能、新增技能、删除技能N（例如 删除技能2）。\n"
        )

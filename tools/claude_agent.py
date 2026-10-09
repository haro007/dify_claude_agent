"""Claude Agent tool — delegates the full agent loop to claude_agent_sdk.

The SDK's ``query()`` is an async generator. Dify's tool interface expects a
synchronous ``Generator[ToolInvokeMessage]``. We bridge the two with a
Thread + Queue: the async generator runs on a dedicated daemon thread (with
its own event loop) and pushes each yielded message onto a thread-safe
``queue.Queue``; the main thread consumes the queue and yields Dify messages
in real time.

Session isolation
-----------------
Each Dify chat window gets its own work directory under
``sessions/session-{dify_id}-{uuid}/``. Conversation history is managed
natively by the claude_agent_sdk: the opaque ``session_id`` returned in
``ResultMessage`` is persisted via Dify Storage and passed back as
``ClaudeAgentOptions.resume`` on the next invocation.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import shutil
import threading
import uuid
from collections.abc import Generator
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    StreamEvent,
    TaskProgressMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage

from utils.agent_storage import (
    cleanup_old_sessions,
    clear_resume_state,
    collect_output_files,
    get_dify_session_id,
    get_or_create_session_dir,
    get_resume_state,
    get_sdk_session_id,
    set_resume_state,
    store_sdk_session_id,
)
from utils.skill_storage import (
    get_skills_dir,
    load_skills_index,
)


# ── sentinels for the queue bridge ──────────────────────────────────

class _Done:
    """Signals normal completion of the async producer."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error


# ── helper utilities ────────────────────────────────────────────────

def _download_file_content(url: str, timeout: int = 45) -> bytes:
    req = Request(url, headers={"User-Agent": "dify-plugin-claude-agent/1.0"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _safe_filename(name: str | None, fallback_ext: str = "") -> str:
    if name:
        base = os.path.basename(str(name))
        base = re.sub(r'[<>:"/\\|?*]+', "_", base).strip()
        if base:
            return base
    return f"{uuid.uuid4().hex}{fallback_ext}"


def _infer_ext_from_url(url: str) -> str:
    path = urlparse(url or "").path
    _, ext = os.path.splitext(path)
    return ext if ext else ""


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


def _guess_mime_type(filename: str) -> str:
    import mimetypes

    name = (filename or "").strip().lower()
    _, ext = os.path.splitext(name)
    ext = ext.lower()
    if ext:
        overrides = {
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".xls": "application/vnd.ms-excel",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".doc": "application/msword",
            ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ".ppt": "application/vnd.ms-powerpoint",
            ".csv": "text/csv",
            ".json": "application/json",
            ".txt": "text/plain",
            ".md": "text/markdown",
            ".html": "text/html",
            ".htm": "text/html",
            ".pdf": "application/pdf",
            ".zip": "application/zip",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".svg": "image/svg+xml",
        }
        if ext in overrides:
            return overrides[ext]
    mime_type, _ = mimetypes.guess_type(name, strict=False)
    return mime_type or "application/octet-stream"


def _build_skills_system_prompt(
    skills_dir: str | None,
    allowed_skills: list[str] | None = None,
) -> "tuple[str, list[str]]":
    """Build the skills section for the system prompt.

    Returns ``(prompt_text, matched_folders)`` where *matched_folders* is
    the list of folder names that should be made available to the agent.

    If *allowed_skills* is provided and non-empty, only skills whose
    **name** (from SKILL.md) or **folder** name matches one of the entries
    (case-insensitive) are included.  When no skill matches the filter the
    prompt still carries a warning so the user / operator can see what
    happened.
    """
    if not skills_dir or not os.path.isdir(skills_dir):
        return "", []

    skills_index = load_skills_index(skills_dir)
    skills_list = skills_index.get("skills", [])
    if not skills_list:
        return "", []

    # Resolve the "workspace skills" path — this directory will be created
    # inside the session workspace so the SDK subprocess can reach it
    # (the SDK sandboxes file access to *cwd*).
    workspace_skills_root = "./skills"

    # Filter by allowed skills if configured.
    # Match against **both** the display name and the folder name so users
    # can specify either one.
    if allowed_skills:
        allowed_lower = {s.strip().lower() for s in allowed_skills if s.strip()}
        filtered: list[dict[str, str]] = []
        for s in skills_list:
            name_lower = s.get("name", "").lower()
            folder_lower = s.get("folder", "").lower()
            if name_lower in allowed_lower or folder_lower in allowed_lower:
                filtered.append(s)
        if not filtered:
            # Nothing matched — return empty so nothing leaks into the
            # system prompt; the caller will yield a user-visible warning.
            return "", []
        skills_list = filtered

    matched_folders = [s.get("folder", "") for s in skills_list]

    lines = [
        "\n\n[技能包 / Skill Packages]",
        f"技能根目录 (skills_root): {workspace_skills_root}",
        "以下技能包已安装在工作目录的 skills/ 子目录中，",
        "你可以使用内置工具（Read、Glob、Bash 等）来查看和使用它们。",
        "每个技能包目录下包含 SKILL.md 说明文件，请先阅读 SKILL.md 了解技能用法。",
        "",
        "可用技能列表：",
    ]
    for i, skill in enumerate(skills_list, 1):
        name = skill.get("name", "")
        folder = skill.get("folder", "")
        desc = skill.get("description", "")
        lines.append(f"  {i}. {name} (目录: {folder}) — {desc}")

    lines.append("")
    return "\n".join(lines), matched_folders


def _copy_skills_to_workspace(
    skills_dir: str,
    matched_folders: list[str],
    session_dir: str,
) -> str | None:
    """Copy matched skill folders into ``{session_dir}/skills/``.

    Returns an error string on failure, or ``None`` on success.
    The destination directory is cleared first so stale skills from a
    previous invocation are never visible.
    """
    dest_root = os.path.join(session_dir, "skills")
    # Remove any previous skills directory to keep the workspace clean.
    if os.path.isdir(dest_root):
        shutil.rmtree(dest_root, ignore_errors=True)
    if not matched_folders:
        return None

    os.makedirs(dest_root, exist_ok=True)
    for folder in matched_folders:
        src = os.path.join(skills_dir, folder)
        dst = os.path.join(dest_root, folder)
        if not os.path.isdir(src):
            return f"技能目录不存在: {src}"
        try:
            shutil.copytree(src, dst)
        except OSError as exc:
            return f"复制技能 {folder} 失败: {exc}"
    return None


# ── Dedicated event-loop thread (shared across all invocations) ─────
#
# We cannot rely on ``asyncio.run()`` inside a freshly-spawned
# ``threading.Thread`` because the Dify plugin framework may reuse OS
# threads or run under an async-aware executor that leaves behind
# running-event-loop state.  Instead we maintain a single long-lived
# daemon thread whose sole job is to run an asyncio event loop.
# Every invocation submits its coroutine to this loop via
# ``run_coroutine_threadsafe`` and waits for completion on a short-lived
# waiter thread.  The bridge queue is still used to stream results back
# to the synchronous ``_invoke`` generator in real time.

_LOOP: "asyncio.AbstractEventLoop | None" = None
_LOOP_THREAD: "threading.Thread | None" = None
_LOOP_LOCK = threading.Lock()
_LOOP_IDLE: "threading.Event | None" = None


def _get_or_create_loop() -> "tuple[asyncio.AbstractEventLoop, threading.Event]":
    """Return the shared event loop and an Event that is set when it is idle.

    The function is thread-safe: only one loop + thread pair is ever created
    for the lifetime of the module.
    """
    global _LOOP, _LOOP_THREAD, _LOOP_IDLE
    with _LOOP_LOCK:
        if (
            _LOOP is None
            or _LOOP_THREAD is None
            or not _LOOP_THREAD.is_alive()
            or _LOOP.is_closed()
        ):
            _LOOP = asyncio.new_event_loop()
            _LOOP_IDLE = threading.Event()
            _LOOP_IDLE.set()  # idle until work is submitted

            def _run_loop() -> None:
                asyncio.set_event_loop(_LOOP)
                _LOOP.run_forever()

            _LOOP_THREAD = threading.Thread(
                target=_run_loop,
                name="claude-agent-event-loop",
                daemon=True,
            )
            _LOOP_THREAD.start()

        assert _LOOP_IDLE is not None
        return _LOOP, _LOOP_IDLE


# ── Thread + Queue bridge: async SDK → sync Generator ───────────────

def _run_agent_on_thread(
    prompt: str,
    options: ClaudeAgentOptions,
    q: "queue.Queue[Any]",
) -> threading.Thread:
    """Submit the async producer to the shared event loop.

    Returns the *waiter* thread (so the caller can join it), NOT the
    long-lived event-loop thread.
    """

    loop, loop_idle = _get_or_create_loop()

    async def _runner() -> None:
        await _drain_async(prompt, options, q)

    # Schedule the coroutine on the shared loop.
    future = asyncio.run_coroutine_threadsafe(_runner(), loop)
    loop_idle.clear()

    def _wait() -> None:
        try:
            future.result()  # wait for the coroutine to finish
        except BaseException as exc:  # noqa: BLE001
            q.put(_Done(error=exc))
        finally:
            loop_idle.set()

    t = threading.Thread(
        target=_wait, name="claude-agent-waiter", daemon=True,
    )
    t.start()
    return t


async def _drain_async(
    prompt: str,
    options: ClaudeAgentOptions,
    q: "queue.Queue[Any]",
) -> None:
    """Drain ``query()`` and push each message onto the queue."""
    try:
        print(
            f"[claude_agent][debug] query() starting, prompt_len={len(prompt)}",
            flush=True,
        )
        async for message in query(prompt=prompt, options=options):
            q.put(message)
        print("[claude_agent][debug] query() finished normally", flush=True)
    except BaseException as exc:  # noqa: BLE001 — surface to consumer
        print(f"[claude_agent][error] query() raised: {exc!r}", flush=True)
        q.put(_Done(error=exc))
    else:
        q.put(_Done())


# ── tool class ──────────────────────────────────────────────────────

class ClaudeAgentTool(Tool):
    """Session-aware agent tool backed by ``claude_agent_sdk.query()``.

    * **Session isolation** — each Dify chat gets its own directory
      ``sessions/session-{dify_id}-{uuid}/``.
    * **Conversation history** — handled natively by the SDK via
      ``ClaudeAgentOptions.resume``. The opaque SDK ``session_id`` from
      ``ResultMessage`` is persisted in Dify Storage and passed back on
      the next invocation so the agent remembers the full conversation.
    * **Resume** — interrupted invocations leave a pending marker; the
      next call can optionally resume.
    * **Auto-cleanup** — old session directories are pruned automatically.
    """

    def _invoke(
        self, tool_parameters: dict[str, Any],
    ) -> Generator[ToolInvokeMessage]:
        # ── storage (may raise — safe-guarded per call) ────────────
        storage = self.session.storage

        # ── parse parameters ────────────────────────────────────────
        q_text = tool_parameters.get("query")
        max_turns = int(tool_parameters.get("max_turns") or 30)
        system_prompt = str(tool_parameters.get("system_prompt") or "")
        permission_mode = str(
            tool_parameters.get("permission_mode") or "acceptEdits"
        )
        # ── parse thinking config ─────────────────────────────────
        thinking_config: dict[str, Any] | None = None
        thinking_raw = str(tool_parameters.get("thinking") or "").strip()
        if thinking_raw:
            if thinking_raw == "adaptive":
                thinking_config = {"type": "adaptive"}
            elif thinking_raw == "disabled":
                thinking_config = {"type": "disabled"}
            elif thinking_raw.startswith("enabled"):
                budget = 1024
                if ":" in thinking_raw:
                    try:
                        budget = int(thinking_raw.split(":", 1)[1].strip())
                    except ValueError:
                        budget = 1024
                thinking_config = {"type": "enabled", "budget_tokens": budget}
            else:
                # Try full JSON: {"type": "...", "budget_tokens": N}
                try:
                    parsed = json.loads(thinking_raw)
                    if isinstance(parsed, dict) and "type" in parsed:
                        thinking_config = parsed
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
        # ── parse effort ──────────────────────────────────────────
        effort: str | None = None
        effort_raw = str(tool_parameters.get("effort") or "").strip()
        if effort_raw and effort_raw in ("low", "medium", "high", "xhigh", "max"):
            effort = effort_raw
        # ── session parameters ──────────────────────────────────────
        # ``dify_session_id`` is stable across one Dify chat window.
        # We use it to: look up the SDK session_id (for resume), create
        # the session directory, and track resume state.  The tool
        # parameter ``session_id`` is an optional override.
        dify_session_id = (
            tool_parameters.get("session_id")
            or get_dify_session_id(self.session)
        )
        if not dify_session_id:
            dify_session_id = uuid.uuid4().hex[:16]
        auto_resume = tool_parameters.get("auto_resume", False)

        if not q_text or not isinstance(q_text, str):
            yield self.create_text_message("❌ 缺少 query 参数\n")
            return

        user_input = str(q_text)

        print(
            f"[claude_agent][debug] dify_session={dify_session_id} "
            f"query_len={len(user_input)}",
            flush=True,
        )

        # ── parse skills filter ─────────────────────────────────────
        skills_raw = str(tool_parameters.get("skills") or "").strip()
        allowed_skills: list[str] | None = None
        if skills_raw:
            # Comma-separated skill names, e.g. "read_pdf,execute_sql"
            allowed_skills = [s.strip() for s in skills_raw.split(",") if s.strip()]

        print(
            f"[claude_agent][debug] start "
            f"dify_session={dify_session_id} "
            f"max_turns={max_turns} "
            f"skills={skills_raw or '(all)'}",
            flush=True,
        )

        # ── parse mcp_servers ────────────────────────────────────────
        mcp_servers: dict[str, dict[str, str]] | None = None
        mcp_raw = str(tool_parameters.get("mcp_servers") or "").strip()
        if mcp_raw:
            try:
                raw_config = json.loads(mcp_raw)
                if isinstance(raw_config, dict) and raw_config:
                    mcp_servers = {}
                    for name, cfg in raw_config.items():
                        if not isinstance(cfg, dict):
                            continue
                        url = str(cfg.get("url", "") or "").strip()
                        if not url:
                            continue
                        # Map user-facing "transport" to SDK "type"
                        transport = str(cfg.get("transport", "sse") or "sse").strip()
                        mcp_servers[str(name)] = {
                            "type": transport,
                            "url": url,
                        }
                    if not mcp_servers:
                        mcp_servers = None
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                yield self.create_text_message(
                    f"⚠️ MCP 服务配置JSON解析失败: {e}\n"
                )
                mcp_servers = None

        # ── resolve directories ─────────────────────────────────────
        plugin_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..")
        )
        skills_dir = get_skills_dir(plugin_root)

        # ── check resume state ──────────────────────────────────────
        resume_state = None
        try:
            resume_state = get_resume_state(storage, dify_session_id)
        except Exception:
            pass

        if resume_state and resume_state.get("pending"):
            prev_dir = resume_state.get("session_dir", "")
            yield self.create_text_message(
                "⚠️ 检测到上次会话可能未完成。\n"
                f"   会话目录: {prev_dir}\n"
            )
            if auto_resume:
                yield self.create_text_message("🔄 自动恢复会话…\n")
            else:
                yield self.create_text_message(
                    "💡 如需恢复，请设置 auto_resume=true\n"
                )

        # ── get / create session directory ──────────────────────────
        try:
            session_dir = get_or_create_session_dir(
                storage, dify_session_id, plugin_root,
            )
        except Exception:
            sessions_root = os.path.join(plugin_root, "sessions")
            os.makedirs(sessions_root, exist_ok=True)
            dir_name = f"session-{dify_session_id[:12]}-{uuid.uuid4().hex[:8]}"
            session_dir = os.path.join(sessions_root, dir_name)
            os.makedirs(session_dir, exist_ok=True)
            os.makedirs(os.path.join(session_dir, "uploads"), exist_ok=True)

        print(
            f"[claude_agent][debug] session_dir={session_dir}",
            flush=True,
        )

        # ── handle uploaded files ───────────────────────────────────
        file_items: list[Any] = []
        files_param = tool_parameters.get("files")
        if isinstance(files_param, list):
            file_items = [x for x in files_param if x]
        elif files_param:
            file_items = [files_param]

        uploaded_files_context = ""
        if file_items:
            uploads_dir = os.path.join(session_dir, "uploads")
            os.makedirs(uploads_dir, exist_ok=True)
            uploaded: list[dict[str, Any]] = []

            for item in file_items:
                url, name = _extract_url_and_name(item)
                if not url:
                    yield self.create_text_message("❌ 无法获取上传文件 URL\n")
                    return
                try:
                    content = _download_file_content(str(url), timeout=45)
                except Exception as e:
                    yield self.create_text_message(
                        f"❌ 文件下载失败：{str(e)}\n"
                    )
                    return

                ext = _infer_ext_from_url(str(url))
                filename = _safe_filename(
                    str(name) if name else None, fallback_ext=ext,
                )
                abs_path = os.path.join(uploads_dir, filename)
                try:
                    with open(abs_path, "wb") as f:
                        f.write(content)
                except Exception as e:
                    yield self.create_text_message(
                        f"❌ 保存上传文件失败：{str(e)}\n"
                    )
                    return

                mime = ""
                if isinstance(item, dict):
                    mime = str(item.get("mime_type") or "").strip()
                if not mime:
                    try:
                        mime = _guess_mime_type(filename)
                    except Exception:
                        mime = ""
                uploaded.append(
                    {
                        "filename": filename,
                        "path": os.path.join("uploads", filename),
                        "bytes": len(content),
                        "mime_type": mime or "",
                    }
                )

            lines = [
                "\n\n[上传文件 / Uploaded Files]",
                "以下文件位于当前会话目录的 uploads/ 中，可用 Read/Glob 工具访问：",
            ]
            for f in uploaded:
                lines.append(
                    f"- uploads/{f['filename']} "
                    f"({f['bytes']} bytes, {f['mime_type'] or 'unknown'})"
                )
            uploaded_files_context = "\n".join(lines) + "\n"

        # ── SDK session_id (for native resume across invocations) ──
        # The claude_agent_sdk manages conversation history internally.
        # We persist the opaque ``session_id`` from ``ResultMessage`` on
        # each call and pass it back via ``resume`` on the next call.
        sdk_session_id: str | None = None
        try:
            sdk_session_id = get_sdk_session_id(storage, dify_session_id)
        except Exception:
            pass
        if sdk_session_id:
            print(
                f"[claude_agent][debug] resuming SDK session={sdk_session_id}",
                flush=True,
            )

        # ── build system prompt ─────────────────────────────────────
        skills_context, matched_skill_folders = _build_skills_system_prompt(
            skills_dir, allowed_skills,
        )

        # Copy the matched skills into the session workspace so the
        # SDK subprocess can actually read them (the SDK sandboxes file
        # access to *cwd*, and the real skills directory lives outside).
        copy_err = _copy_skills_to_workspace(
            skills_dir, matched_skill_folders, session_dir,
        )
        if copy_err:
            yield self.create_text_message(f"⚠️ {copy_err}\n")

        # Warn if user specified skill names but none matched
        if allowed_skills and not matched_skill_folders:
            yield self.create_text_message(
                "⚠️ 指定的技能名称未能匹配到任何已安装的技能，请检查 skills 参数。\n"
            )

        default_system_prompt = (
            "你是一个基于 claude_agent_sdk 的智能 Agent。\n"
            "你可以使用内置工具（Read、Write、Edit、Glob、Bash 等）来完成任务。\n"
            "\n[工作目录]\n"
            f"当前工作目录: {session_dir}\n"
            "所有文件操作默认在此目录下进行。\n"
            "\n[会话信息]\n"
            f"当前会话 ID: {dify_session_id}\n"
            "生成的文件请直接放在当前工作目录下（不要创建子文件夹，除非用户要求）。\n"
            "\n[技能包使用说明]\n"
            "如果系统提示中包含技能包列表，你可以：\n"
            "1. 使用 Glob 工具浏览技能包目录结构\n"
            "2. 使用 Read 工具读取技能包中的 SKILL.md 说明文件\n"
            "3. 使用 Bash 工具执行技能包中的脚本\n"
            "4. 使用 Write/Edit 工具根据技能说明生成文件\n"
            "\n[规则]\n"
            "1. 执行任务时应先了解工作目录和可用技能\n"
            "2. 先读取相关说明文件(SKILL.md)，再执行操作\n"
            "3. 生成的文件放在当前工作目录下\n"
            "4. 用中文回答用户的问题"
        )

        parts: list[str] = []
        if system_prompt.strip():
            parts.append(system_prompt.strip())
        else:
            parts.append(default_system_prompt)
        if skills_context:
            parts.append(skills_context)
        if uploaded_files_context:
            parts.append(uploaded_files_context)

        final_system_prompt = "\n".join(parts)

        # ── allowed tools & permission mode ─────────────────────────
        pm = permission_mode
        if pm not in ("default", "acceptEdits", "plan"):
            pm = "acceptEdits"

        if pm == "acceptEdits":
            allowed_tools = [
                "Read", "Edit", "Write", "Glob", "Bash", "NotebookEdit",
            ]
        elif pm == "plan":
            allowed_tools = ["Read", "Glob"]
        else:
            allowed_tools = ["Read", "Glob", "Bash"]

        # Append MCP tool patterns for each configured server
        if mcp_servers:
            for server_name in mcp_servers:
                allowed_tools.append(f"mcp__{server_name}__*")

        # ── build SDK options ───────────────────────────────────────
        options_kwargs: dict[str, Any] = {
            "max_turns": max_turns,
            "allowed_tools": allowed_tools,
            "cwd": session_dir,
            "permission_mode": pm,
            "system_prompt": final_system_prompt,
            "include_partial_messages": True,
        }
        if mcp_servers:
            options_kwargs["mcp_servers"] = mcp_servers
        if thinking_config:
            options_kwargs["thinking"] = thinking_config
        if effort:
            options_kwargs["effort"] = effort
        # Resume previous SDK session so the agent remembers history
        if sdk_session_id:
            options_kwargs["resume"] = sdk_session_id

        # ── forward SDK env vars (credentials / model config) ────
        # 1st priority: Dify provider credentials (self.runtime.credentials)
        # 2nd priority: environment variables on the plugin host
        # claude_agent_sdk spawns a subprocess that reads these env vars to
        # know which API endpoint / token / model to use.
        sdk_env: dict[str, str] = {}
        # Merge Dify credentials (prefix ANTHROPIC_)
        for key, val in self.runtime.credentials.items():
            if val and str(val).strip():
                sdk_env[str(key)] = str(val).strip()
        # Merge host env vars for keys not already set by credentials
        for key, val in os.environ.items():
            if key.startswith("ANTHROPIC_") or key in (
                "ENABLE_TOOL_SEARCH",
                "CLAUDE_CODE_ENABLE_TELEMETRY",
                "OTEL_TRACES_EXPORTER",
            ):
                if key not in sdk_env:
                    sdk_env[key] = val
        if sdk_env:
            options_kwargs["env"] = sdk_env

        if not sdk_env.get("ANTHROPIC_AUTH_TOKEN"):
            print(
                "[claude_agent][warn] ANTHROPIC_AUTH_TOKEN not found in "
                "credentials or env. The SDK subprocess will likely fail to "
                "authenticate. Configure it in the Dify provider settings.",
                flush=True,
            )

        print(
            f"[claude_agent][debug] options env keys={list(sdk_env.keys())} "
            f"max_turns={max_turns} "
            f"permission_mode={pm} cwd={session_dir}",
            flush=True,
        )

        options = ClaudeAgentOptions(**options_kwargs)

        # ── mark resume as pending (will clear on success) ──────────
        try:
            set_resume_state(
                storage, dify_session_id, pending=True, session_dir=session_dir,
            )
        except Exception:
            pass

        # ── Thread + Queue bridge ───────────────────────────────────
        # yield self.create_text_message(
        #     f"⏳ Agent 正在启动… (会话: {dify_session_id})\n"
        # )

        msg_q: "queue.Queue[Any]" = queue.Queue()
        runner = _run_agent_on_thread(user_input, options, msg_q)

        yielded_any = False
        final_text_parts: list[str] = []
        seen_tool_ids: set[str] = set()
        agent_error: str | None = None
        _captured_sdk_session: dict[str, str] = {}  # captured from ResultMessage

        # Consume the queue until a _Done sentinel arrives.
        while True:
            timeout = 120 if not yielded_any else 3600
            try:
                item = msg_q.get(timeout=timeout)
            except queue.Empty:
                yield self.create_text_message(
                    f"❌ Agent 执行超时（{timeout}s 内未产生输出，"
                    "请检查 .env 中的 ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL 是否正确）\n"
                )
                return

            if isinstance(item, _Done):
                if item.error is not None:
                    agent_error = str(item.error)
                    yield self.create_text_message(
                        f"❌ Agent 执行出错：{item.error}\n"
                    )
                break

            # ── StreamEvent: real-time streaming text deltas ──────────
            if isinstance(item, StreamEvent):
                event = item.event
                if event.get("type") == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            yield self.create_text_message(text)
                            yielded_any = True
                continue

            # SDK message → Dify stream
            if isinstance(item, AssistantMessage) or isinstance(
                item, TaskProgressMessage
            ):
                for block in getattr(item, "content", []) or []:
                    if isinstance(block, TextBlock):
                        text = str(getattr(block, "text", "") or "")
                        if text.strip():
                            final_text_parts.append(text)
                            # NOTE: text is already streamed via StreamEvent;
                            # only accumulate here for the fallback below.

                    elif isinstance(block, ToolUseBlock):
                        tool_id = getattr(block, "id", None)
                        if tool_id and tool_id in seen_tool_ids:
                            continue
                        if tool_id:
                            seen_tool_ids.add(tool_id)

                        tool_name = str(getattr(block, "name", "") or "")
                        tool_input = (
                            getattr(block, "input", None)
                            or getattr(block, "arguments", None)
                            or {}
                        )
                        input_summary = ""
                        if isinstance(tool_input, dict):
                            parts = []
                            for k, v in tool_input.items():
                                val_str = str(v)
                                if len(val_str) > 80:
                                    val_str = val_str[:80] + "..."
                                parts.append(f"{k}={val_str}")
                            input_summary = ", ".join(parts)
                        elif tool_input:
                            input_summary = str(tool_input)[:120]

                        yield self.create_text_message(
                            f"\n🔧 **调用工具: {tool_name}**\n"
                            f"   参数: {input_summary}\n"
                        )
                        yielded_any = True

            elif isinstance(item, ResultMessage):
                subtype = getattr(item, "subtype", None)
                # Capture the SDK session_id for future resume
                new_sdk_sid = getattr(item, "session_id", None)
                if new_sdk_sid and str(new_sdk_sid).strip():
                    _captured_sdk_session["id"] = str(new_sdk_sid).strip()
                if subtype:
                    yield self.create_text_message(
                        f"\n✅ **完成 ({subtype})**\n"
                    )
                    yielded_any = True

        # Make sure the runner thread is not lingering.
        runner.join(timeout=5)

        # ── fallback ────────────────────────────────────────────────
        if not yielded_any:
            combined = "".join(final_text_parts).strip()
            if combined:
                yield self.create_text_message(combined)
            else:
                yield self.create_text_message(
                    "Agent 未返回任何内容。请检查任务描述是否清晰。\n"
                )

        # ── persist SDK session_id for next invocation ──────────────
        persisted_sdk_sid = _captured_sdk_session.get("id", "")
        if persisted_sdk_sid:
            try:
                store_sdk_session_id(
                    storage, dify_session_id, persisted_sdk_sid,
                )
                print(
                    f"[claude_agent][debug] stored SDK session={persisted_sdk_sid}",
                    flush=True,
                )
            except Exception:
                pass

        # ── clear resume state on success ───────────────────────────
        if not agent_error:
            try:
                clear_resume_state(storage, dify_session_id)
            except Exception:
                pass

        # ── collect & return output files ───────────────────────────
        try:
            output_files = collect_output_files(session_dir)
        except Exception as e:
            print(
                f"[claude_agent][error] collect_output_files failed: {e!r}",
                flush=True,
            )
            output_files = []

        if output_files:
            for f in output_files:
                # Yield each file as a blob so Dify can expose it
                try:
                    with open(f["absolute_path"], "rb") as fh:
                        blob = fh.read()
                    mime = _guess_mime_type(f["filename"])
                    print(f"[claude_agent] mime_type: {mime}, filename: {f['filename']}" , flush=True)
                    yield self.create_blob_message(
                        blob,
                        meta={
                            "filename": f["filename"],
                            "path": f["relative_path"],
                            "mime_type": mime,
                        },
                    )
                except Exception as e:
                    print(
                        f"[claude_agent][error] read file failed: {e!r}",
                        flush=True,
                    )   
                    pass

        # ── cleanup old sessions ────────────────────────────────────
        try:
            cleanup_old_sessions(storage, dify_session_id, plugin_root)
        except Exception:
            pass

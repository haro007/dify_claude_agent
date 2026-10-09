"""Session storage helpers for the Claude Agent tool.

Uses Dify's ``self.session.storage`` (backed by ``StorageInvocation``) to
persist the SDK session_id, session directory paths, and resume state.
All values are stored as JSON-serialized bytes because the Dify storage
API only accepts/returns ``bytes``.

Conversation history is handled natively by the claude_agent_sdk via the
``resume`` / ``session_id`` options on ``ClaudeAgentOptions`` — we only
store the opaque ``session_id`` string that the SDK returns.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from typing import Any

# ── storage key constants ────────────────────────────────────────────

_SDK_SESSION_PREFIX = "claude_agent:sdk_session"
_SESSION_DIR_PREFIX = "claude_agent:session_dir"
_RESUME_PREFIX = "claude_agent:resume"
_SESSION_INDEX_KEY = "claude_agent:sessions"
_SDK_SESSION_KEY = "claude_agent:sdk_session_id"

# ── limits ────────────────────────────────────────────────────────────

MAX_SESSIONS = 20  # keep at most this many recent sessions on disk


# ── low-level storage operations (JSON ⟷ bytes) ──────────────────────

def _storage_get_json(session_storage: Any, key: str) -> dict[str, Any] | None:
    try:
        raw = session_storage.get(key)
    except Exception:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        return None


def _storage_set_json(session_storage: Any, key: str, value: Any) -> bool:
    try:
        session_storage.set(
            key, json.dumps(value, ensure_ascii=False).encode("utf-8"),
        )
        return True
    except Exception:
        return False


def _storage_delete(session_storage: Any, key: str) -> bool:
    try:
        session_storage.delete(key)
        return True
    except Exception:
        return False


def _safe_get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    try:
        return obj[key]
    except Exception:
        pass
    try:
        return getattr(obj, key)
    except Exception:
        return None


# ── Dify session id extraction ───────────────────────────────────────


def get_dify_session_id(session: Any) -> str:
    """Best-effort extraction of a stable id from Dify's Session object.

    Tries ``conversation_id`` first (stable across a chat), then other
    candidates. Falls back to ``"global"``.
    """
    candidates = [
        _safe_get(session, "conversation_id"),
        _safe_get(session, "chat_id"),
        _safe_get(session, "task_id"),
        _safe_get(session, "id"),
        _safe_get(session, "session_id"),
        _safe_get(session, "app_run_id"),
    ]
    for c in candidates:
        if isinstance(c, str) and c.strip():
            return c.strip()
    return "global"


# ── SDK session_id storage ───────────────────────────────────────────
#  The claude_agent_sdk returns a ``session_id`` in ``ResultMessage``.
#  We persist it here so the next invokation can pass ``resume=session_id``.


def _get_sdk_session_key(dify_session_id: str) -> str:
    return f"{_SDK_SESSION_PREFIX}:{dify_session_id}"


def get_sdk_session_id(
    session_storage: Any, dify_session_id: str,
) -> str | None:
    """Return the SDK ``session_id`` stored for *dify_session_id*, or None."""
    data = _storage_get_json(
        session_storage, _get_sdk_session_key(dify_session_id),
    )
    if isinstance(data, dict) and isinstance(data.get("sdk_session_id"), str):
        return str(data["sdk_session_id"])
    return None


def store_sdk_session_id(
    session_storage: Any,
    dify_session_id: str,
    sdk_session_id: str,
) -> bool:
    """Persist the SDK ``session_id`` keyed by *dify_session_id*."""
    return _storage_set_json(
        session_storage,
        _get_sdk_session_key(dify_session_id),
        {"sdk_session_id": sdk_session_id, "ts": time.time()},
    )


# ── session index (for cleanup) ──────────────────────────────────────

def _load_session_index(session_storage: Any) -> list[str]:
    data = _storage_get_json(session_storage, _SESSION_INDEX_KEY)
    if isinstance(data, list):
        return [str(x) for x in data if isinstance(x, str)]
    return []


def _save_session_index(
    session_storage: Any, session_ids: list[str],
) -> bool:
    return _storage_set_json(session_storage, _SESSION_INDEX_KEY, session_ids)


def _register_session(session_storage: Any, session_id: str) -> None:
    idx = _load_session_index(session_storage)
    if session_id in idx:
        idx.remove(session_id)
    idx.append(session_id)
    _save_session_index(session_storage, idx)


def _unregister_session(session_storage: Any, session_id: str) -> None:
    idx = _load_session_index(session_storage)
    if session_id in idx:
        idx.remove(session_id)
        _save_session_index(session_storage, idx)


# ── session directory management ─────────────────────────────────────

def _get_session_dir_key(dify_session_id: str) -> str:
    return f"{_SESSION_DIR_PREFIX}:{dify_session_id}"


def get_stored_session_dir(
    session_storage: Any, dify_session_id: str,
) -> str | None:
    key = _get_session_dir_key(dify_session_id)
    data = _storage_get_json(session_storage, key)
    if isinstance(data, dict) and isinstance(data.get("path"), str):
        return str(data["path"])
    return None


def store_session_dir(
    session_storage: Any, dify_session_id: str, session_dir: str,
) -> bool:
    return _storage_set_json(
        session_storage,
        _get_session_dir_key(dify_session_id),
        {"path": session_dir, "ts": time.time()},
    )


def delete_session_dir_record(
    session_storage: Any, dify_session_id: str,
) -> bool:
    return _storage_delete(session_storage, _get_session_dir_key(dify_session_id))


def get_or_create_session_dir(
    session_storage: Any,
    dify_session_id: str,
    plugin_root: str,
) -> str:
    existing = get_stored_session_dir(session_storage, dify_session_id)
    if existing and os.path.isdir(existing):
        return existing

    sessions_root = os.path.join(plugin_root, "sessions")
    os.makedirs(sessions_root, exist_ok=True)
    short_id = dify_session_id[:12] if len(dify_session_id) > 12 else dify_session_id
    dir_name = f"session-{short_id}-{uuid.uuid4().hex[:8]}"
    session_dir = os.path.join(sessions_root, dir_name)
    os.makedirs(session_dir, exist_ok=True)
    os.makedirs(os.path.join(session_dir, "uploads"), exist_ok=True)

    store_session_dir(session_storage, dify_session_id, session_dir)
    _register_session(session_storage, dify_session_id)
    return session_dir


# ── resume state ─────────────────────────────────────────────────────

def _get_resume_key(dify_session_id: str) -> str:
    return f"{_RESUME_PREFIX}:{dify_session_id}"


def get_resume_state(
    session_storage: Any, dify_session_id: str,
) -> dict[str, Any] | None:
    data = _storage_get_json(session_storage, _get_resume_key(dify_session_id))
    return data if isinstance(data, dict) else None


def set_resume_state(
    session_storage: Any,
    dify_session_id: str,
    pending: bool,
    session_dir: str = "",
    extra: dict[str, Any] | None = None,
) -> bool:
    if not pending:
        return _storage_delete(session_storage, _get_resume_key(dify_session_id))
    state: dict[str, Any] = {
        "pending": True,
        "session_dir": session_dir,
        "timestamp": time.time(),
    }
    if extra:
        state.update(extra)
    return _storage_set_json(
        session_storage, _get_resume_key(dify_session_id), state,
    )


def clear_resume_state(
    session_storage: Any, dify_session_id: str,
) -> bool:
    return set_resume_state(session_storage, dify_session_id, pending=False)


# ── session cleanup ──────────────────────────────────────────────────

def cleanup_old_sessions(
    session_storage: Any,
    dify_session_id: str,
    plugin_root: str,
    max_sessions: int = MAX_SESSIONS,
) -> None:
    idx = _load_session_index(session_storage)
    if len(idx) <= max_sessions:
        return

    to_remove = idx[: -max_sessions]
    to_remove = [sid for sid in to_remove if sid != dify_session_id]

    for sid in to_remove:
        stored_dir = get_stored_session_dir(session_storage, sid)
        if stored_dir and os.path.isdir(stored_dir):
            try:
                shutil.rmtree(stored_dir, ignore_errors=True)
            except Exception:
                pass
        delete_session_dir_record(session_storage, sid)
        _storage_delete(session_storage, _get_sdk_session_key(sid))
        _storage_delete(session_storage, _get_resume_key(sid))
        _unregister_session(session_storage, sid)


# ── output file collection ───────────────────────────────────────────

def collect_output_files(session_dir: str) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    if not os.path.isdir(session_dir):
        return files
    for root, _dirs, filenames in os.walk(session_dir):
        rel_root = os.path.relpath(root, session_dir)
        if rel_root.startswith("skills"):
            continue
        for name in filenames:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, session_dir)
            if rel.startswith("uploads" + os.sep) or rel == "uploads":
                continue
            if rel.startswith("skills" + os.sep) or rel == "skills":
                continue
            try:
                stat = os.stat(full)
                files.append({
                    "filename": name,
                    "relative_path": rel,
                    "absolute_path": full,
                    "bytes": stat.st_size,
                })
            except OSError:
                continue
    return files

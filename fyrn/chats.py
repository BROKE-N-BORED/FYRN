"""Saved chats (v0.8): plain JSON files under ~/.fyrn/chats/.

One file per chat, named by the chat's id. A chat record is a
dict:

    {"id": str, "title": str, "title_auto": bool,
     "created": float, "updated": float,
     "instructions": str, "model": str,
     "messages": [{"role": ..., "content": ..., "name": ...}]}

Messages are the DISPLAY transcript: roles "user", "assistant"
and "tool" (a tool line, with "name" set to the tool). The
model-facing history is the user/assistant subset — see
history_from_messages. Instructions are the per-chat system
prompt text ("" means none).

Qt-free and stdlib-only, like core.py, so storage is unit
testable headless. A damaged chat file is skipped by the list
and reported as missing by load — a bad file never takes the
whole list down.
"""

import json
import re
import time
import uuid
from pathlib import Path

from .core import get_home

CHATS_DIRNAME = "chats"
TITLE_MAX_CHARS = 48
_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def chats_dir(home=None):
    """The folder chat files live in (created on demand)."""
    return Path(home or get_home()) / CHATS_DIRNAME


def new_chat_id():
    return uuid.uuid4().hex[:16]


def auto_title(first_user_text):
    """A chat's title from its first user message: the first
    line, whitespace collapsed, cut to TITLE_MAX_CHARS at a word
    boundary where one fits."""
    text = " ".join(str(first_user_text or "").split())
    if not text:
        return "New chat"
    if len(text) <= TITLE_MAX_CHARS:
        return text
    cut = text[:TITLE_MAX_CHARS].rstrip()
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut


def history_from_messages(messages):
    """The model-facing history from display messages: user and
    assistant turns only, as {"role", "content"} dicts."""
    history = []
    for message in messages or []:
        if message.get("role") in ("user", "assistant"):
            history.append({
                "role": message["role"],
                "content": str(message.get("content", "")),
            })
    return history


class ChatStore:
    """Reads and writes chat files in one chats folder."""

    def __init__(self, home=None):
        self._dir = chats_dir(home)

    @property
    def folder(self):
        return self._dir

    def _path_for(self, chat_id):
        if not chat_id or not _ID_RE.match(str(chat_id)):
            return None
        return self._dir / f"{chat_id}.json"

    def new_record(self):
        now = time.time()
        return {
            "id": new_chat_id(),
            "title": "New chat",
            "title_auto": True,
            "created": now,
            "updated": now,
            "instructions": "",
            "model": "",
            "messages": [],
        }

    def list_chats(self):
        """Summaries of every readable chat, newest first:
        {"id", "title", "created", "updated", "instructions",
        "message_count"}."""
        summaries = []
        try:
            files = list(self._dir.glob("*.json"))
        except OSError:
            return summaries
        for path in files:
            record = self._read(path)
            if record is None:
                continue
            summaries.append({
                "id": record["id"],
                "title": record["title"],
                "created": record["created"],
                "updated": record["updated"],
                "instructions": record["instructions"],
                "message_count": len(record["messages"]),
            })
        summaries.sort(key=lambda item: item["updated"],
                       reverse=True)
        return summaries

    def load(self, chat_id):
        """The full record for a chat, or None."""
        path = self._path_for(chat_id)
        if path is None:
            return None
        return self._read(path)

    def save(self, record):
        """Write a record (assigning id/timestamps when new) and
        return it. Writes go to a temp file renamed over the old
        one, so a crash mid-write cannot corrupt a chat."""
        record = dict(record)
        if not record.get("id"):
            record["id"] = new_chat_id()
        now = time.time()
        if not record.get("created"):
            record["created"] = now
        record["updated"] = now
        record.setdefault("title", "New chat")
        record.setdefault("title_auto", True)
        record.setdefault("instructions", "")
        record.setdefault("model", "")
        record.setdefault("messages", [])
        path = self._path_for(record["id"])
        if path is None:
            raise ValueError("bad chat id")
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=1),
                       encoding="utf-8")
        tmp.replace(path)
        return record

    def rename(self, chat_id, title):
        """Set a chat's title by hand (it stops being auto)."""
        record = self.load(chat_id)
        if record is None:
            return None
        record["title"] = str(title).strip() or record["title"]
        record["title_auto"] = False
        return self.save(record)

    def delete(self, chat_id):
        """Remove a chat file. True when a file went away."""
        path = self._path_for(chat_id)
        if path is None:
            return False
        try:
            path.unlink()
            return True
        except OSError:
            return False

    def _read(self, path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        chat_id = data.get("id") or path.stem
        if not _ID_RE.match(str(chat_id)):
            return None
        messages = data.get("messages")
        if not isinstance(messages, list):
            messages = []
        clean = []
        for message in messages:
            if isinstance(message, dict) and \
                    message.get("role") in (
                        "user", "assistant", "tool"):
                entry = {
                    "role": message["role"],
                    "content": str(message.get("content", "")),
                }
                if message.get("name"):
                    entry["name"] = str(message["name"])
                clean.append(entry)
        return {
            "id": str(chat_id),
            "title": str(data.get("title") or "New chat"),
            "title_auto": bool(data.get("title_auto", True)),
            "created": float(data.get("created") or 0),
            "updated": float(data.get("updated") or 0),
            "instructions": str(data.get("instructions") or ""),
            "model": str(data.get("model") or ""),
            "messages": clean,
        }

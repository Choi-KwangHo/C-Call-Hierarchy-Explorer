from __future__ import annotations

import hashlib
import os
import pickle
from pathlib import Path
from typing import Any

from PySide6.QtCore import QStandardPaths


# Increment when parsed C semantics change so stale function boundaries are
# never restored after an analyzer fix.
CACHE_FORMAT = 3


def _normalized_root(root: str) -> str:
    return str(Path(root).resolve()).replace("/", "\\").casefold()


class ProjectCacheStore:
    def __init__(self, base_directory: str | Path | None = None) -> None:
        if base_directory is None:
            base = Path(QStandardPaths.writableLocation(QStandardPaths.AppLocalDataLocation))
            base_directory = base / "project-cache"
        self.base_directory = Path(base_directory)

    def path_for(self, root: str) -> Path:
        digest = hashlib.sha256(_normalized_root(root).encode("utf-8")).hexdigest()[:24]
        return self.base_directory / f"{digest}.chcache"

    def load(self, root: str) -> dict[str, Any] | None:
        path = self.path_for(root)
        if not path.is_file():
            return None
        try:
            with path.open("rb") as stream:
                payload = pickle.load(stream)
            if not isinstance(payload, dict):
                return None
            if payload.get("format") != CACHE_FORMAT or payload.get("root") != _normalized_root(root):
                return None
            if not isinstance(payload.get("session_cache"), dict) or payload.get("result") is None:
                return None
            return payload
        except (OSError, EOFError, pickle.PickleError, AttributeError, ValueError, TypeError):
            return None

    def save(self, root: str, session_cache: dict, result: object, ui_state: dict[str, Any]) -> Path:
        self.base_directory.mkdir(parents=True, exist_ok=True)
        target = self.path_for(root)
        temporary = target.with_suffix(target.suffix + ".tmp")
        payload = {
            "format": CACHE_FORMAT,
            "root": _normalized_root(root),
            "session_cache": session_cache,
            "result": result,
            "ui_state": ui_state,
        }
        try:
            with temporary.open("wb") as stream:
                pickle.dump(payload, stream, protocol=pickle.HIGHEST_PROTOCOL)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(target)
        finally:
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass
        return target

from __future__ import annotations

import hashlib
import os
import pickle
from pathlib import Path

from PySide6.QtCore import QStandardPaths

from eeprom_map import EepromMapResult, EepromSourceConfig


CACHE_FORMAT = 2


def _signature(config: EepromSourceConfig) -> tuple:
    return (
        config.id, config.source_type, config.repository_url, config.branch,
        config.capacity, config.page_size,
    )


class EepromResultCacheStore:
    def __init__(self, base_directory: str | Path | None = None) -> None:
        if base_directory is None:
            base = Path(QStandardPaths.writableLocation(QStandardPaths.AppLocalDataLocation))
            base_directory = base / "eeprom-result-cache"
        self.base_directory = Path(base_directory)

    def path_for(self, config: EepromSourceConfig) -> Path:
        digest = hashlib.sha256(config.id.encode("utf-8")).hexdigest()[:24]
        return self.base_directory / f"{digest}.eecache"

    def load(self, config: EepromSourceConfig) -> EepromMapResult | None:
        path = self.path_for(config)
        if not path.is_file():
            return None
        try:
            with path.open("rb") as stream:
                payload = pickle.load(stream)
            if not isinstance(payload, dict) or payload.get("format") != CACHE_FORMAT:
                return None
            if payload.get("signature") != _signature(config):
                return None
            result = payload.get("result")
            if isinstance(result, EepromMapResult):
                result.config = config
                return result
            return None
        except (OSError, EOFError, pickle.PickleError, AttributeError, ValueError, TypeError):
            return None

    def save(self, result: EepromMapResult) -> Path:
        self.base_directory.mkdir(parents=True, exist_ok=True)
        target = self.path_for(result.config)
        temporary = target.with_suffix(target.suffix + ".tmp")
        payload = {
            "format": CACHE_FORMAT,
            "signature": _signature(result.config),
            "result": result,
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

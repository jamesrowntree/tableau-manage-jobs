"""Local JSON persistence for per-job detail (see tableau.get_job_detail).

Detail (target name, failure notes, etc.) is only available via a per-job
`get_by_id` call - too expensive to make on every jobs-list poll. This module
caches that detail on disk, keyed by job id, so it's fetched once and reused
across app restarts.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

_PATH = Path(__file__).parent / "job_details.json"
_lock = threading.Lock()
_data: dict[str, dict] | None = None


def _load() -> dict[str, dict]:
    global _data
    if _data is None:
        if _PATH.exists():
            _data = json.loads(_PATH.read_text())
        else:
            _data = {}
    return _data


def all() -> dict[str, dict]:
    with _lock:
        return dict(_load())


def get(job_id: str) -> dict | None:
    with _lock:
        return _load().get(job_id)


def known_ids() -> set[str]:
    with _lock:
        return set(_load().keys())


def upsert(job_id: str, detail: dict, *, flush: bool = True) -> None:
    with _lock:
        _load()[job_id] = detail
        if flush:
            _save_locked()


def save() -> None:
    with _lock:
        _save_locked()


def _save_locked() -> None:
    """Atomic write of the whole store; caller must hold `_lock`."""
    tmp_path = _PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(_load(), indent=2))
    os.replace(tmp_path, _PATH)

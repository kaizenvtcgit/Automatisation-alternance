from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
EXPORT_ROOT = BASE_DIR / "export"
MESSAGES_DIR = EXPORT_ROOT / "messages"
UPLOADS_DIR = EXPORT_ROOT / "uploads"

CSV_PATH = EXPORT_ROOT / "offres_filtrees.csv"
LETTRES_PATH = EXPORT_ROOT / "lettres.json"
SCORES_PATH = EXPORT_ROOT / "scores.json"
SCAN_STATE_PATH = EXPORT_ROOT / "scan_state.json"
PROFILE_PATH = EXPORT_ROOT / "profil_recherche.json"
REFUS_PATH = EXPORT_ROOT / "offres_refusees.json"
SUPABASE_SYNC_STATE_PATH = EXPORT_ROOT / "supabase_sync_state.json"
HISTORIQUE_PATH = BASE_DIR / "historique_postulations.json"
PID_PATH = BASE_DIR / "app_server.pid"


def storage_backend() -> str:
    return (os.environ.get("STORAGE_BACKEND") or "local").strip().lower() or "local"


def is_local_storage() -> bool:
    return storage_backend() == "local"


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json_atomic(path: Path, payload: Any, retries: int = 8, retry_delay: float = 0.12) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent, suffix=".tmp") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        temp_path = Path(handle.name)
    last_error = None
    for _ in range(retries):
        try:
            temp_path.replace(path)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(retry_delay)
    if temp_path.exists():
        try:
            temp_path.unlink()
        except OSError:
            pass
    if last_error:
        raise last_error

"""
Interface web locale - Alternance Auto.
Lancement : python app.py puis http://localhost:5001
"""

import csv
import importlib.util
import json
import os
import atexit
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import webbrowser
from datetime import datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from threading import Lock, Thread
from urllib.request import urlopen

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request, session
from settings_service import (
    build_shareable_settings_example,
    get_auth_user_id,
    get_settings,
    get_setup_status,
    get_workspace_slug,
    save_settings,
    set_workspace_slug,
)
from storage_service import (
    BASE_DIR,
    CSV_PATH,
    HISTORIQUE_PATH as HISTO_PATH,
    LETTRES_PATH,
    PID_PATH,
    REFUS_PATH,
    SCORES_PATH,
    SUPABASE_SYNC_STATE_PATH,
    UPLOADS_DIR,
    read_json as storage_read_json,
    write_json_atomic,
)
from werkzeug.exceptions import HTTPException

load_dotenv(BASE_DIR / ".env")

app = Flask(__name__)
app.secret_key = (os.environ.get("APP_SESSION_SECRET") or os.environ.get("APP_SECRET") or "alternance-auto-dev-secret").strip()
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=14)

CSV_SEP = ";"

_proc_actif: subprocess.Popen | None = None
_scores_lock = Lock()
_scan_state_lock = Lock()
_supabase_runtime_cache: dict[str, object] = {"ts": 0.0, "payload": None}
_cloud_task_lock = Lock()
CLOUD_TASK_STATE_PATH = BASE_DIR / "export" / "cloud_task_state.json"
CLOUD_TASK_LOG_PATH = BASE_DIR / "export" / "cloud_task.log"


def _invalidate_supabase_runtime_cache() -> None:
    _supabase_runtime_cache["ts"] = 0.0
    _supabase_runtime_cache["payload"] = None


def _supabase_publishable_key() -> str:
    return (os.environ.get("SUPABASE_PUBLISHABLE_KEY") or os.environ.get("SUPABASE_ANON_KEY") or "").strip()


def _supabase_auth_enabled() -> bool:
    return (
        (os.environ.get("SUPABASE_AUTH_ENABLED") or "0").strip().lower() in {"1", "true", "yes", "on", "oui"}
        and _cloud_mode_enabled()
        and bool((os.environ.get("SUPABASE_URL") or "").strip())
        and bool(_supabase_publishable_key())
    )


def _auth_workspace_slug(user_id: str = "", email: str = "") -> str:
    raw = str(user_id or "").strip()
    if raw:
        return f"user-{raw.split('-')[0][:12]}"
    email_prefix = str(email or "").split("@", 1)[0].strip()
    if email_prefix:
        return f"user-{email_prefix}"
    return "principal"


def _auth_user_payload() -> dict | None:
    user_id = str(session.get("auth_user_id") or "").strip()
    email = str(session.get("auth_user_email") or "").strip()
    if not user_id and not email:
        return None
    return {
        "id": user_id,
        "email": email,
        "workspace": str(session.get("workspace_slug") or _auth_workspace_slug(user_id, email)).strip() or "principal",
    }


def _auth_logged_in() -> bool:
    return _auth_user_payload() is not None


def _set_auth_session(user_payload: dict, access_token: str = "", refresh_token: str = "") -> dict:
    user_id = str((user_payload or {}).get("id") or "").strip()
    email = str((user_payload or {}).get("email") or "").strip()
    workspace = set_workspace_slug(_auth_workspace_slug(user_id, email))
    session["auth_user_id"] = user_id
    session["auth_user_email"] = email
    session["auth_access_token"] = str(access_token or "").strip()
    session["auth_refresh_token"] = str(refresh_token or "").strip()
    return {"id": user_id, "email": email, "workspace": workspace}


def _clear_auth_session() -> None:
    for key in ("auth_user_id", "auth_user_email", "auth_access_token", "auth_refresh_token"):
        session.pop(key, None)


def _supabase_auth_headers(access_token: str = "") -> dict[str, str]:
    headers = {
        "apikey": _supabase_publishable_key(),
        "Content-Type": "application/json",
    }
    token = str(access_token or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _supabase_auth_request(path: str, payload: dict | None = None, *, method: str = "POST", access_token: str = "") -> dict:
    base = str(os.environ.get("SUPABASE_URL") or "").rstrip("/")
    response = requests.request(
        method.upper(),
        f"{base}{path}",
        headers=_supabase_auth_headers(access_token),
        data=json.dumps(payload or {}, ensure_ascii=False) if payload is not None else None,
        timeout=15,
    )
    data = response.json() if response.content else {}
    if not response.ok:
        message = data.get("msg") or data.get("error_description") or data.get("error") or f"Erreur Supabase Auth ({response.status_code})"
        raise RuntimeError(str(message))
    return data if isinstance(data, dict) else {}


def _access_secret() -> str:
    return (os.environ.get("APP_SECRET") or "").strip()


def _access_protection_enabled() -> bool:
    return bool(_access_secret())


def _access_unlocked() -> bool:
    return not _access_protection_enabled() or session.get("app_unlocked") is True


def _current_workspace() -> str:
    return get_workspace_slug()


def _cloud_workspace_isolated() -> bool:
    return _supabase_runtime_enabled() and _current_workspace() != "principal"


def _workspace_blob_key(name: str) -> str:
    return f"{name}::{_current_workspace()}"


def _current_owner_user_id() -> str:
    return get_auth_user_id()


def _user_scoped_table_mode() -> bool:
    return _supabase_runtime_enabled() and bool(_current_owner_user_id())


def _supabase_auto_sync_enabled() -> bool:
    return (os.environ.get("SUPABASE_SYNC_AUTO") or "1").strip().lower() in {"1", "true", "yes", "on", "oui"}


def _cloud_mode_enabled() -> bool:
    return (os.environ.get("ALTERNANCE_CLOUD_MODE") or "0").strip() == "1"


def _supabase_runtime_enabled() -> bool:
    return bool(_cloud_mode_enabled() and (os.environ.get("SUPABASE_URL") or "").strip() and (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip())


def _offer_title(row: dict) -> str:
    for key in ("Intitulé du poste", "IntitulÃ© du poste", "IntitulÃƒÂ© du poste"):
        value = row.get(key)
        if value:
            return str(value)
    return ""


def _current_search_env_overrides() -> dict[str, str]:
    search = (get_settings().get("search") or {})
    if not isinstance(search, dict):
        search = {}

    def _pack(values) -> str:
        return "|||".join(
            str(item).strip()
            for item in (values or [])
            if str(item).strip()
        )

    return {
        "ALTERNANCE_TARGET_ROLES": _pack(search.get("postes_cibles")),
        "ALTERNANCE_POSITIVE_KEYWORDS": _pack(search.get("mots_cles_positifs")),
        "ALTERNANCE_NEGATIVE_KEYWORDS": _pack(search.get("mots_cles_negatifs")),
        "ALTERNANCE_CONTRACT_TYPES": _pack(search.get("types_contrat")),
        "ALTERNANCE_ZONE_MODE": str(search.get("zone_mode") or "").strip(),
        "ALTERNANCE_ZONE_GEO": str(search.get("zone_geo") or "").strip(),
        "ALTERNANCE_RADIUS_KM": str(int(search.get("rayon_km") or 30)),
        "INCLURE_OFFRES_REMOTE": "1" if bool(search.get("inclure_remote", True)) else "0",
    }


def _repair_text(value: str) -> str:
    text = str(value or "")
    if any(marker in text for marker in ("Ã", "Â", "â€", "â€™", "â€œ", "â€\x9d", "â€¦")):
        for source_encoding in ("cp1252", "latin-1"):
            try:
                text = text.encode(source_encoding).decode("utf-8")
                break
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
    return text.replace("\u00a0", " ").replace("\u202f", " ")


def _normalized_text(value: str) -> str:
    text = unicodedata.normalize("NFD", str(value or ""))
    return "".join(ch for ch in text if unicodedata.category(ch) != "Mn").lower()


def _text_contains_keyword(text: str, keyword: str) -> bool:
    source = _normalized_text(text)
    target = _normalized_text(keyword)
    if not target:
        return False
    if len(target) <= 3:
        return re.search(rf"\b{re.escape(target)}\b", source) is not None
    return target in source


def _repair_payload_strings(value):
    if isinstance(value, str):
        return _repair_text(value)
    if isinstance(value, list):
        return [_repair_payload_strings(item) for item in value]
    if isinstance(value, dict):
        return {key: _repair_payload_strings(item) for key, item in value.items()}
    return value


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name or "").strip())
    return cleaned.strip("._") or "cv"


def _module_available(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError):
        return False


def _path_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".healthcheck_{int(time.time() * 1000)}.tmp"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _health_check_item(label: str, status: str, detail: str, action: str | None = None) -> dict:
    return {
        "label": label,
        "status": status,
        "detail": detail,
        "action": action or "",
    }


def build_health_status() -> dict:
    settings = get_settings()
    profile = settings.get("profile", {})
    api_keys = settings.get("api_keys", {})
    sync_state = _lire_sync_supabase_state()

    checks: list[dict] = []
    env_path = BASE_DIR / ".env"
    venv_python = BASE_DIR / ".venv" / "Scripts" / "python.exe"
    cv_path_raw = str(profile.get("cv_path") or "").strip()
    cv_path = Path(cv_path_raw) if cv_path_raw else None

    checks.append(
        _health_check_item(
            "Protection d'accès",
            "ok" if _access_protection_enabled() else "warn",
            "Un token d'accès APP_SECRET protège l'interface." if _access_protection_enabled() else "Aucune protection d'accès n'est active pour l'interface.",
            "api",
        )
    )
    checks.append(
        _health_check_item(
            "Auto-sync Supabase",
            "ok" if _supabase_auto_sync_enabled() else "warn",
            "La synchronisation distante se déclenche automatiquement après les actions clés." if _supabase_auto_sync_enabled() else "L'auto-sync Supabase est désactivé ; il faudra lancer la sync manuellement.",
            "sync",
        )
    )

    checks.append(
        _health_check_item(
            "Configuration .env",
            "ok" if env_path.exists() else "error",
            "Fichier .env present." if env_path.exists() else "Le fichier .env est manquant.",
            "settings",
        )
    )
    checks.append(
        _health_check_item(
            "Runtime Python",
            "ok",
            f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro} via {Path(sys.executable).name}",
        )
    )
    checks.append(
        _health_check_item(
            "Environnement virtuel",
            "ok" if ".venv" in sys.executable.lower() or venv_python.exists() else "warn",
            "Environnement virtuel detecte." if ".venv" in sys.executable.lower() or venv_python.exists() else "Aucun .venv detecte sur ce projet.",
            "installer",
        )
    )

    for label, path_obj in (
        ("Dossier export", BASE_DIR / "export"),
        ("Dossier uploads", UPLOADS_DIR),
        ("Dossier messages", BASE_DIR / "export" / "messages"),
    ):
        writable = _path_writable(path_obj)
        checks.append(
            _health_check_item(
                label,
                "ok" if writable else "error",
                f"{path_obj} est accessible en écriture." if writable else f"{path_obj} n'est pas accessible en écriture.",
            )
        )

    checks.append(
        _health_check_item(
            "CV candidat",
            "ok" if cv_path and cv_path.exists() else ("warn" if cv_path_raw else "warn"),
            f"CV trouvé : {cv_path}" if cv_path and cv_path.exists() else ("Le chemin du CV ne pointe vers aucun fichier existant." if cv_path_raw else "Aucun CV renseigné pour le moment."),
            "profile",
        )
    )

    source_ready = any(api_keys.get(key) for key in ("ft_client_id", "adzuna_app_id", "lba_api_key"))
    checks.append(
        _health_check_item(
            "Sources d'offres",
            "ok" if source_ready else "warn",
            "Au moins une source d'offres est configuree." if source_ready else "Aucune source d'offres n'est encore configuree.",
            "api",
        )
    )

    checks.append(
        _health_check_item(
            "Groq",
            "ok" if api_keys.get("groq_api_key") else "warn",
            "Cle Groq configuree pour les lettres et le coach." if api_keys.get("groq_api_key") else "GROQ_API_KEY manque encore.",
            "api",
        )
    )
    checks.append(
        _health_check_item(
            "Gemini",
            "ok" if api_keys.get("gemini_api_key") else "warn",
            "Cle Gemini configuree pour l'agent." if api_keys.get("gemini_api_key") else "GEMINI_API_KEY manque encore.",
            "api",
        )
    )

    module_checks = [
        ("Flask", "flask"),
        ("Playwright", "playwright"),
        ("Groq package", "groq"),
        ("Dotenv", "dotenv"),
    ]
    for label, module_name in module_checks:
        available = _module_available(module_name)
        checks.append(
            _health_check_item(
                label,
                "ok" if available else "error",
                f"Module {module_name} disponible." if available else f"Module {module_name} introuvable dans l'environnement courant.",
                "installer" if not available else "",
            )
        )

    supabase_url = os.environ.get("SUPABASE_URL", "").strip()
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if supabase_url and supabase_key:
        sync_status = str(sync_state.get("status") or "idle").lower()
        status = "ok" if sync_status == "completed" else "warn" if sync_status in {"idle", "running"} else "error"
        detail = (
            f"Derniere sync reussie : {sync_state.get('last_success_at') or 'jamais'}."
            if sync_status == "completed"
            else f"Etat actuel : {sync_status}."
        )
        if sync_status == "failed" and sync_state.get("error"):
            detail = f"Erreur de sync Supabase : {sync_state.get('error')}"
        checks.append(_health_check_item("Supabase", status, detail, "sync"))
    else:
        checks.append(
            _health_check_item(
                "Supabase",
                "warn",
                "Supabase n'est pas configure. Le mode local reste utilisable.",
                "api",
            )
        )

    counts = {
        "ok": sum(1 for item in checks if item["status"] == "ok"),
        "warn": sum(1 for item in checks if item["status"] == "warn"),
        "error": sum(1 for item in checks if item["status"] == "error"),
    }
    overall = "error" if counts["error"] else "warn" if counts["warn"] else "ok"
    summary = (
        "Le projet est pret a etre utilise."
        if overall == "ok"
        else "Le projet est utilisable, mais quelques points meritent une verification."
        if overall == "warn"
        else "Le projet a des points bloquants a corriger avant un usage serein."
    )
    return {
        "status": overall,
        "summary": summary,
        "counts": counts,
        "checks": checks,
    }


def build_shareable_diagnostic() -> dict:
    settings = get_settings()
    profile = settings.get("profile", {})
    search = settings.get("search", {})
    api_keys = settings.get("api_keys", {})
    health = build_health_status()
    setup = get_setup_status()
    offers = _filtered_offers()
    histo = _lire_historique()
    sync = _lire_sync_supabase_state()
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "app": {
            "name": "Alternance Auto",
            "mode": "local",
            "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "storage_backend": os.environ.get("STORAGE_BACKEND", "local"),
            "access_protection_enabled": _access_protection_enabled(),
        },
        "profile_snapshot": {
            "has_name": bool(profile.get("prenom") and profile.get("nom")),
            "has_email": bool(profile.get("email")),
            "has_cv": bool(profile.get("cv_path") and Path(profile.get("cv_path")).exists()),
            "has_professional_link": any(profile.get(key) for key in ("portfolio", "linkedin", "github")),
        },
        "search_snapshot": {
            "postes_cibles_count": len(search.get("postes_cibles") or []),
            "types_contrat": list(search.get("types_contrat") or []),
            "zone_mode": search.get("zone_mode") or "",
            "zone_geo": search.get("zone_geo") or "",
            "score_min": search.get("score_min") or 0,
        },
        "api_snapshot": {
            "sources_configured": {
                "france_travail": bool(api_keys.get("ft_client_id")),
                "adzuna": bool(api_keys.get("adzuna_app_id")),
                "la_bonne_alternance": bool(api_keys.get("lba_api_key")),
            },
            "ai_configured": {
                "groq": bool(api_keys.get("groq_api_key")),
                "gemini": bool(api_keys.get("gemini_api_key")),
            },
            "supabase": bool(os.environ.get("SUPABASE_URL", "").strip() and os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()),
        },
        "data_snapshot": {
            "offers_count": len(offers),
            "applications_count": len(histo),
            "letters_count": sum(1 for offer in offers if offer.get("letter_generated")),
            "sync_status": sync.get("status"),
            "last_sync_success_at": sync.get("last_success_at"),
        },
        "setup": setup,
        "health": health,
    }


def _extract_cv_text(cv_path: str, limit: int = 4000) -> str:
    if not cv_path:
        return ""
    path = Path(cv_path)
    if not path.exists() or not path.is_file():
        return ""
    try:
        suffix = path.suffix.lower()
        if suffix in {".txt", ".md"}:
            return _repair_text(path.read_text(encoding="utf-8", errors="replace"))[:limit]
        raw = path.read_bytes()
    except Exception:
        return ""

    # Best effort sans dépendance PDF dédiée : on extrait les séquences lisibles.
    decoded = raw.decode("latin-1", errors="ignore")
    chunks = re.findall(r"[A-Za-zÀ-ÿ0-9@/+'’().,:; -]{4,}", decoded)
    compact = " ".join(chunk.strip() for chunk in chunks if any(ch.isalpha() for ch in chunk))
    compact = re.sub(r"\s+", " ", compact)
    return _repair_text(compact)[:limit]


def _build_search_coach_context() -> dict:
    settings = get_settings()
    current_search = settings.get("search", {})
    current_profile = settings.get("profile", {})
    if not isinstance(current_search, dict):
        current_search = {}
    if not isinstance(current_profile, dict):
        current_profile = {}
    history = _lire_historique()
    scores = _lire_scores()
    offers = _lire_csv()
    if not isinstance(history, list):
        history = []
    if not isinstance(scores, dict):
        scores = {}
    if not isinstance(offers, list):
        offers = []

    top_offers = []
    for row in offers:
        key = _row_key(row)
        score_info = scores.get(key, {})
        score_value = score_info.get("score")
        if score_value is None:
            continue
        top_offers.append(
            {
                "titre": row.get("Intitulé du poste", row.get("IntitulÃ© du poste", "")),
                "entreprise": row.get("Entreprise", ""),
                "score": int(score_value),
                "raisons": score_info.get("positiveReasons", [])[:2],
            }
        )
    top_offers.sort(key=lambda item: item.get("score", 0), reverse=True)

    recent_history = [
        {
            "titre": row.get("titre", ""),
            "entreprise": row.get("entreprise", ""),
            "statut": row.get("statut", ""),
            "date": row.get("date_postulation", ""),
        }
        for row in history[-4:]
    ]

    cv_excerpt = _extract_cv_text(current_profile.get("cv_path", ""), limit=900)

    return {
        "profile": {
            "presentation": current_profile.get("presentation", ""),
            "portfolio": current_profile.get("portfolio", ""),
            "linkedin": current_profile.get("linkedin", ""),
            "github": current_profile.get("github", ""),
            "cv_excerpt": cv_excerpt,
        },
        "current_search": current_search,
        "recent_history": recent_history,
        "top_scored_offers": top_offers[:4],
        "letters_count": len(_lire_lettres()),
        "scores_count": len(scores),
    }


def _coach_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    return []


def _coach_fallback_payload(current_search: dict | None = None, has_context: bool = False, error_message: str = "") -> dict:
    current_search = current_search if isinstance(current_search, dict) else {}
    return {
        "ok": True,
        "reply": (
            "Je n'ai pas pu contacter le modèle IA cette fois. "
            "Tu peux quand même me répondre en précisant les postes visés, les mots-clés importants et ce que tu veux exclure."
        ),
        "ready": False,
        "suggestions": {
            "postes_cibles": _coach_list(current_search.get("postes_cibles")),
            "mots_cles_positifs": _coach_list(current_search.get("mots_cles_positifs")),
            "mots_cles_negatifs": _coach_list(current_search.get("mots_cles_negatifs")),
            "types_contrat": _coach_list(current_search.get("types_contrat")),
            "zone_mode": str(current_search.get("zone_mode", "idf") or "idf"),
            "zone_geo": str(current_search.get("zone_geo", "") or ""),
        },
        "fallback": True,
        "context_used": bool(has_context),
        "error": error_message or "Assistant indisponible",
    }


def _ensure_stdio() -> None:
    """Provide usable streams when launched with pythonw.exe on Windows."""
    stdout_log = BASE_DIR / "server_stdout.log"
    stderr_log = BASE_DIR / "server_stderr.log"

    if sys.stdout is None:
        sys.stdout = open(stdout_log, "a", encoding="utf-8", buffering=1)
    if sys.stderr is None:
        sys.stderr = open(stderr_log, "a", encoding="utf-8", buffering=1)


def _write_pid_file() -> None:
    PID_PATH.write_text(str(os.getpid()), encoding="utf-8")


def _cleanup_pid_file() -> None:
    try:
        if PID_PATH.exists() and PID_PATH.read_text(encoding="utf-8").strip() == str(os.getpid()):
            PID_PATH.unlink()
    except Exception:
        pass


def _read_json(path: Path, default):
    data = storage_read_json(path, default)
    return _repair_payload_strings(data)


def _write_json(path: Path, payload) -> None:
    write_json_atomic(path, payload)


def _cloud_task_state_default() -> dict:
    return {
        "status": "idle",
        "task_type": "",
        "label": "",
        "pid": None,
        "started_at": "",
        "finished_at": "",
        "exit_code": None,
        "message": "",
        "log_path": str(CLOUD_TASK_LOG_PATH),
    }


def _process_alive(pid: int | None) -> bool:
    try:
        target = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if target <= 0:
        return False
    try:
        os.kill(target, 0)
    except OSError:
        return False
    return True


def _lire_cloud_task_state() -> dict:
    data = _read_json(CLOUD_TASK_STATE_PATH, _cloud_task_state_default())
    state = data if isinstance(data, dict) else _cloud_task_state_default()
    if state.get("status") == "running":
        pid = state.get("pid")
        started_at = str(state.get("started_at") or "").strip()
        is_stale = False
        if started_at:
            try:
                started_dt = datetime.fromisoformat(started_at)
                is_stale = (datetime.now() - started_dt).total_seconds() > 420
            except ValueError:
                is_stale = False
        if not _process_alive(pid) or is_stale:
            finished_at = datetime.now().isoformat(timespec="seconds")
            state = _ecrire_cloud_task_state(
                {
                    **state,
                    "status": "failed",
                    "finished_at": finished_at,
                    "exit_code": -1,
                    "message": "La tache cloud a ete interrompue ou a depasse le delai maximal.",
                }
            )
    return state


def _ecrire_cloud_task_state(payload: dict) -> dict:
    merged = {**_cloud_task_state_default(), **(payload if isinstance(payload, dict) else {})}
    _write_json(CLOUD_TASK_STATE_PATH, merged)
    return merged


def _tail_cloud_task_log(limit: int = 120) -> list[str]:
    if not CLOUD_TASK_LOG_PATH.exists():
        return []
    try:
        lines = CLOUD_TASK_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    return lines[-limit:]


def _launch_cloud_background_task(task_type: str, label: str, cmd: list[str], extra_env: dict | None = None) -> tuple[bool, str, dict]:
    global _proc_actif
    with _cloud_task_lock:
        if _proc_actif is not None and _proc_actif.poll() is None:
            state = _lire_cloud_task_state()
            return False, "already_running", state

        CLOUD_TASK_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CLOUD_TASK_LOG_PATH.write_text("", encoding="utf-8")
        started_at = datetime.now().isoformat(timespec="seconds")
        state = _ecrire_cloud_task_state(
            {
                "status": "running",
                "task_type": task_type,
                "label": label,
                "pid": None,
                "started_at": started_at,
                "finished_at": "",
                "exit_code": None,
                "message": f"{label} lance en arriere-plan.",
            }
        )
        env = {
            **os.environ,
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
            **(extra_env or {}),
        }
        log_handle = CLOUD_TASK_LOG_PATH.open("a", encoding="utf-8", buffering=1)
        proc = subprocess.Popen(
            cmd,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=str(BASE_DIR),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        _proc_actif = proc
        state = _ecrire_cloud_task_state({**state, "pid": proc.pid})

        def _watch() -> None:
            global _proc_actif
            try:
                code = proc.wait()
                finished_at = datetime.now().isoformat(timespec="seconds")
                _ecrire_cloud_task_state(
                    {
                        "status": "completed" if code == 0 else "failed",
                        "task_type": task_type,
                        "label": label,
                        "pid": proc.pid,
                        "started_at": started_at,
                        "finished_at": finished_at,
                        "exit_code": code,
                        "message": f"{label} termine." if code == 0 else f"{label} termine avec erreurs.",
                    }
                )
            finally:
                try:
                    log_handle.close()
                except Exception:
                    pass
                _proc_actif = None

        Thread(target=_watch, daemon=True).start()
        return True, "started", state


def _supabase_headers() -> dict[str, str]:
    service_role = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    return {
        "apikey": service_role,
        "Authorization": f"Bearer {service_role}",
    }


def _supabase_fetch(table: str, *, select: str = "*", order: str | None = None, limit: int | None = None, filters: dict[str, str] | None = None) -> list[dict]:
    base = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
    params: dict[str, str] = {"select": select}
    if order:
        params["order"] = order
    if limit is not None:
        params["limit"] = str(limit)
    if filters:
        params.update(filters)
    response = requests.get(
        f"{base}/rest/v1/{table}",
        params=params,
        headers=_supabase_headers(),
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, list) else []


def _format_dt(value: str | None, with_time: bool = True) -> str:
    if not value:
        return ""
    text = str(value).replace("T", " ").replace("Z", "")
    return text[:16] if with_time else text[:10]


def _to_iso_datetime(value: str | None, *, end_of_day: bool = False) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            if fmt == "%Y-%m-%d" and end_of_day:
                parsed = parsed.replace(hour=23, minute=59, second=0)
            return parsed.isoformat()
        except ValueError:
            continue
    return text


def _stable_uuid(*parts) -> str:
    from uuid import NAMESPACE_URL, uuid5

    raw = "||".join(str(part or "").strip() for part in parts)
    return str(uuid5(NAMESPACE_URL, raw))


def _upsert_supabase_rows(table: str, rows: list[dict], on_conflict: str) -> None:
    if not rows:
        return
    response = requests.post(
        f"{str(os.environ.get('SUPABASE_URL') or '').rstrip('/')}/rest/v1/{table}?on_conflict={on_conflict}",
        headers={
            **_supabase_headers(),
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        data=json.dumps(rows, ensure_ascii=False),
        timeout=20,
    )
    response.raise_for_status()
    _invalidate_supabase_runtime_cache()


def _sync_user_scoped_scores_table(scores: dict) -> None:
    if not _user_scoped_table_mode():
        return
    owner_user_id = _current_owner_user_id()
    rows = []
    for signature, payload in (scores or {}).items():
        if not signature or not isinstance(payload, dict):
            continue
        rows.append(
            {
                "offer_signature": signature,
                "owner_user_id": owner_user_id,
                "score": int(payload.get("score", 0) or 0),
                "level": str(payload.get("level") or payload.get("score_level") or "faible"),
                "score_payload": payload,
                "scored_at": _to_iso_datetime(payload.get("date")),
                "updated_at": datetime.now().isoformat(),
            }
        )
    _upsert_supabase_rows("offer_scores", rows, "offer_signature,owner_user_id")


def _sync_user_scoped_letters_table(lettres: dict) -> None:
    if not _user_scoped_table_mode():
        return
    owner_user_id = _current_owner_user_id()
    rows = []
    for signature, payload in (lettres or {}).items():
        if not signature or not isinstance(payload, dict):
            continue
        rows.append(
            {
                "offer_signature": signature,
                "owner_user_id": owner_user_id,
                "title": str(payload.get("titre") or ""),
                "company": str(payload.get("entreprise") or ""),
                "letter_text": str(payload.get("lettre") or ""),
                "letter_payload": payload,
                "generated_at": _to_iso_datetime(payload.get("date_gen")),
                "updated_at": datetime.now().isoformat(),
            }
        )
    _upsert_supabase_rows("offer_letters", rows, "offer_signature,owner_user_id")


def _sync_user_scoped_history_table(histo: list[dict]) -> None:
    if not _user_scoped_table_mode():
        return
    owner_user_id = _current_owner_user_id()
    rows = []
    for row in (histo or []):
        if not isinstance(row, dict):
            continue
        signature = str(row.get("offer_signature") or "").strip()
        row_id = _stable_uuid(owner_user_id, signature or row.get("url") or row.get("titre") or "", row.get("date_postulation") or row.get("statut") or "")
        rows.append(
            {
                "id": row_id,
                "owner_user_id": owner_user_id,
                "offer_signature": signature or None,
                "source_offer_id": str(row.get("id_adzuna") or ""),
                "offer_url": str(row.get("url") or ""),
                "title": str(row.get("titre") or "Candidature"),
                "status": str(row.get("statut") or "a_analyser"),
                "notes": str(row.get("notes") or ""),
                "applied_at": _to_iso_datetime(row.get("date_postulation")),
                "followup_due_at": _to_iso_datetime(row.get("date_relance_prevue"), end_of_day=True),
                "updated_at": datetime.now().isoformat(),
            }
        )
    _upsert_supabase_rows("applications_history", rows, "id")


def _sync_user_scoped_refused_table(refus: list[str]) -> None:
    if not _user_scoped_table_mode():
        return
    owner_user_id = _current_owner_user_id()
    rows = [
        {
            "offer_signature": str(signature or "").strip(),
            "owner_user_id": owner_user_id,
            "refused_at": datetime.now().isoformat(),
        }
        for signature in (refus or [])
        if str(signature or "").strip()
    ]
    _upsert_supabase_rows("refused_offers", rows, "offer_signature,owner_user_id")


def _supabase_runtime_snapshot(ttl_seconds: int = 12) -> dict | None:
    if not _supabase_runtime_enabled():
        return None
    owner_user_id = _current_owner_user_id()
    cached = _supabase_runtime_cache.get("payload")
    ts = float(_supabase_runtime_cache.get("ts") or 0.0)
    if cached and isinstance(cached, dict) and cached.get("owner_user_id") == owner_user_id and (time.time() - ts) < ttl_seconds:
        return cached if isinstance(cached, dict) else None
    try:
        offers = _supabase_fetch("offers", order="updated_at.desc")
        user_scope_filters = {"owner_user_id": f"eq.{owner_user_id}"} if owner_user_id else {"owner_user_id": "is.null"}
        scores = _supabase_fetch("offer_scores", filters=user_scope_filters)
        letters = _supabase_fetch("offer_letters", filters=user_scope_filters)
        history = _supabase_fetch("applications_history", order="created_at.desc", filters=user_scope_filters)
        refused = _supabase_fetch("refused_offers", filters=user_scope_filters)
        scan_runs = _supabase_fetch("scan_runs", order="started_at.desc", limit=6)
        app_settings_rows = _supabase_fetch(
            "app_settings",
            select="key,value,owner_user_id",
            filters=user_scope_filters,
        )
        latest_scan_id = scan_runs[0]["id"] if scan_runs and isinstance(scan_runs[0], dict) else None
        scan_sources = _supabase_fetch("scan_run_sources", order="created_at.desc", filters={"scan_run_id": f"eq.{latest_scan_id}"}) if latest_scan_id else []

        score_map = {
            row.get("offer_signature"): {
                **(row.get("score_payload") if isinstance(row.get("score_payload"), dict) else {}),
                "score": row.get("score"),
                "level": row.get("level"),
                "date": _format_dt(row.get("scored_at")),
            }
            for row in scores
            if isinstance(row, dict) and row.get("offer_signature")
        }
        letter_map = {
            row.get("offer_signature"): row
            for row in letters
            if isinstance(row, dict) and row.get("offer_signature")
        }
        history_rows = []
        history_map: dict[str, dict] = {}
        for row in history:
            if not isinstance(row, dict):
                continue
            signature = str(row.get("offer_signature") or "").strip()
            local_row = {
                "offer_signature": signature,
                "id_adzuna": row.get("source_offer_id") or "",
                "url": row.get("offer_url") or "",
                "titre": row.get("title") or "",
                "statut": row.get("status") or "a_analyser",
                "notes": row.get("notes") or "",
                "date_postulation": _format_dt(row.get("applied_at")),
                "date_relance_prevue": _format_dt(row.get("followup_due_at"), with_time=False),
            }
            history_rows.append(local_row)
            if signature and signature not in history_map:
                history_map[signature] = local_row

        refused_ids = {str(row.get("offer_signature") or "").strip() for row in refused if isinstance(row, dict)}
        offer_rows: list[dict] = []
        offer_records: dict[str, dict] = {}
        for row in offers:
            if not isinstance(row, dict):
                continue
            signature = str(row.get("signature") or "").strip()
            if not signature:
                continue
            score_info = score_map.get(signature, {})
            letter_info = letter_map.get(signature, {})
            histo_row = history_map.get(signature)
            local_row = {
                "ID annonce": row.get("source_offer_id") or signature,
                "Intitulé du poste": row.get("title") or signature,
                "Entreprise": row.get("company") or "",
                "Ville ou zone": row.get("location") or "",
                "Lien vers l'annonce": row.get("offer_url") or "",
                "Date de publication": _format_dt(row.get("published_at"), with_time=False),
                "Type de contrat": row.get("contract_type") or "",
                "Source": row.get("source") or "",
                "Description (texte complet)": row.get("description") or "",
                "pipeline_id": signature,
                "pipeline_status": row.get("pipeline_status") or "a_analyser",
                "is_new": False,
                "is_refused": signature in refused_ids or bool(row.get("is_refused")),
                "is_applied": bool(histo_row and histo_row.get("statut") == "postule"),
                "analysis_done": bool(score_info),
                "letter_generated": bool(letter_info.get("letter_text")),
                "score_value": score_info.get("score"),
                "score_level": score_info.get("level"),
                "positive_reasons": score_info.get("positiveReasons", []),
                "negative_reasons": score_info.get("negativeReasons", []),
                "detected_keywords": score_info.get("detectedKeywords", []),
                "warnings": score_info.get("warnings", []),
                "score_reasons": score_info.get("positiveReasons", score_info.get("raisons", [])),
                "first_seen_at": _format_dt(row.get("first_seen_at")),
                "last_seen_at": _format_dt(row.get("last_seen_at")),
            }
            offer_rows.append(local_row)
            offer_records[signature] = {
                "manual_status": row.get("pipeline_status") or "",
                "score": score_info.get("score"),
                "score_level": score_info.get("level"),
                "letter_generated": bool(letter_info.get("letter_text")),
                "analyzed": bool(score_info),
                "first_seen_at": _format_dt(row.get("first_seen_at")),
                "last_seen_at": _format_dt(row.get("last_seen_at")),
            }

        letters_payload = {
            signature: {
                "signature": signature,
                "titre": row.get("title") or "",
                "entreprise": row.get("company") or "",
                "lettre": row.get("letter_text") or "",
                "date_gen": _format_dt(row.get("generated_at")),
                "date_modif": _format_dt(row.get("updated_at")),
            }
            for signature, row in letter_map.items()
        }
        scores_payload = {signature: value for signature, value in score_map.items()}

        last_scan = {}
        if scan_runs and isinstance(scan_runs[0], dict):
            current = scan_runs[0]
            last_scan = {
                "status": current.get("status") or "unknown",
                "started_at": _format_dt(current.get("started_at")),
                "finished_at": _format_dt(current.get("finished_at")),
                "offers_found": current.get("offers_found", 0) or 0,
                "new_offers": current.get("new_offers", 0) or 0,
                "duplicates_ignored": current.get("duplicates_ignored", 0) or 0,
                "exported_offers": current.get("exported_offers", 0) or 0,
                "errors": current.get("errors", []) or [],
                "new_offer_keys": current.get("new_offer_keys", []) or [],
                "sources_scanned": {
                    str(src.get("source") or ""): {
                        "status": src.get("status") or "unknown",
                        "offers_found": src.get("offers_found", 0) or 0,
                        "new_offers": src.get("new_offers", 0) or 0,
                        "duplicates": src.get("duplicates", 0) or 0,
                        "error_message": src.get("error_message") or "",
                        "timestamp": _format_dt(src.get("source_timestamp")),
                    }
                    for src in scan_sources
                    if isinstance(src, dict) and src.get("source")
                },
            }

        payload = {
            "owner_user_id": owner_user_id,
            "offer_rows": offer_rows,
            "history_rows": history_rows,
            "letters": letters_payload,
            "scores": scores_payload,
            "refused_ids": refused_ids,
            "app_settings": {row.get("key"): row.get("value") for row in app_settings_rows if isinstance(row, dict)},
            "scan_state": {
                "offers": offer_records,
                "last_scan": last_scan,
                "history": [],
            },
        }
        _supabase_runtime_cache["ts"] = time.time()
        _supabase_runtime_cache["payload"] = payload
        return payload
    except Exception:
        return None


def _workspace_blob_read(name: str, default):
    snapshot = _supabase_runtime_snapshot()
    if not snapshot:
        return default
    app_settings = snapshot.get("app_settings", {})
    if not isinstance(app_settings, dict):
        return default
    payload = app_settings.get(_workspace_blob_key(name))
    if payload is None:
        return default
    if isinstance(default, list) and not isinstance(payload, list):
        return default
    if isinstance(default, dict) and not isinstance(payload, dict):
        return default
    return payload


def _workspace_blob_write(name: str, payload) -> None:
    if not _supabase_runtime_enabled():
        return
    owner_user_id = _current_owner_user_id()
    response = requests.post(
        f"{str(os.environ.get('SUPABASE_URL') or '').rstrip('/')}/rest/v1/app_settings?on_conflict=key",
        headers={
            **_supabase_headers(),
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        data=json.dumps(
            [
                {
                    "key": _workspace_blob_key(name),
                    "value": payload,
                    "owner_user_id": owner_user_id or None,
                }
            ],
            ensure_ascii=False,
        ),
        timeout=15,
    )
    response.raise_for_status()
    _invalidate_supabase_runtime_cache()


def _workspace_pipeline_data() -> dict:
    payload = _workspace_blob_read("workspace_pipeline", {})
    return payload if isinstance(payload, dict) else {}


def _workspace_pipeline_save(payload: dict) -> None:
    _workspace_blob_write("workspace_pipeline", payload)


def _workspace_pipeline_update_offer(offre_id: str, updates: dict) -> None:
    payload = _workspace_pipeline_data()
    offers = payload.get("offers", {})
    offers = offers if isinstance(offers, dict) else {}
    current = offers.get(offre_id, {})
    current = current if isinstance(current, dict) else {}
    offers[offre_id] = {**current, **updates}
    payload["offers"] = offers
    _workspace_pipeline_save(payload)


def _lire_csv() -> list[dict]:
    from main import is_offer_within_max_age
    from sources._common import is_relevant_offer

    if _supabase_runtime_enabled() and not CSV_PATH.exists():
        snapshot = _supabase_runtime_snapshot()
        if snapshot:
            rows = snapshot.get("offer_rows", [])
            return rows if isinstance(rows, list) else []

    if not CSV_PATH.exists():
        return []
    try:
        with CSV_PATH.open(encoding="utf-8-sig") as handle:
            rows = [_repair_payload_strings(row) for row in csv.DictReader(handle, delimiter=CSV_SEP)]
            return [
                row for row in rows
                if is_offer_within_max_age(row.get("Date de publication", ""))
                and is_relevant_offer(
                    row.get("Intitulé du poste", row.get("IntitulÃ© du poste", "")),
                    row.get("Description (texte complet)", ""),
                    row.get("Type de contrat", ""),
                )
            ]
    except Exception:
        return []


def _lire_historique() -> list[dict]:
    if _user_scoped_table_mode():
        snapshot = _supabase_runtime_snapshot()
        if snapshot:
            rows = snapshot.get("history_rows", [])
            return rows if isinstance(rows, list) else []
        return []
    if _supabase_runtime_enabled():
        payload = _workspace_blob_read("workspace_history", None)
        if isinstance(payload, list):
            return payload
        if _cloud_workspace_isolated():
            return []
    data = _read_json(HISTO_PATH, [])
    if isinstance(data, list) and data:
        return data
    snapshot = _supabase_runtime_snapshot()
    if snapshot:
        rows = snapshot.get("history_rows", [])
        return rows if isinstance(rows, list) else []
    return data if isinstance(data, list) else []


def _lire_lettres() -> dict:
    if _user_scoped_table_mode():
        snapshot = _supabase_runtime_snapshot()
        if snapshot:
            payload = snapshot.get("letters", {})
            return payload if isinstance(payload, dict) else {}
        return {}
    if _supabase_runtime_enabled():
        payload = _workspace_blob_read("workspace_letters", None)
        if isinstance(payload, dict):
            return payload
        if _cloud_workspace_isolated():
            return {}
    data = _read_json(LETTRES_PATH, {})
    if isinstance(data, dict) and data:
        return data
    snapshot = _supabase_runtime_snapshot()
    if snapshot:
        payload = snapshot.get("letters", {})
        return payload if isinstance(payload, dict) else {}
    return data if isinstance(data, dict) else {}


def _lire_scores() -> dict:
    if _user_scoped_table_mode():
        snapshot = _supabase_runtime_snapshot()
        if snapshot:
            payload = snapshot.get("scores", {})
            return payload if isinstance(payload, dict) else {}
        return {}
    if _supabase_runtime_enabled():
        payload = _workspace_blob_read("workspace_scores", None)
        if isinstance(payload, dict):
            return payload
        if _cloud_workspace_isolated():
            return {}
    data = _read_json(SCORES_PATH, {})
    if isinstance(data, dict) and data:
        return data
    snapshot = _supabase_runtime_snapshot()
    if snapshot:
        payload = snapshot.get("scores", {})
        return payload if isinstance(payload, dict) else {}
    return data if isinstance(data, dict) else {}


def _save_scores_merged(updates: dict[str, dict]) -> dict:
    if _user_scoped_table_mode():
        scores = _lire_scores()
        scores.update(updates)
        _sync_user_scoped_scores_table(scores)
        return scores
    if _supabase_runtime_enabled():
        scores = _lire_scores()
        scores.update(updates)
        _workspace_blob_write("workspace_scores", scores)
        _sync_user_scoped_scores_table(scores)
        return scores
    with _scores_lock:
        scores = _lire_scores()
        scores.update(updates)
        _write_json(SCORES_PATH, scores)
        return scores


def _lire_refus() -> list[str]:
    if _user_scoped_table_mode():
        snapshot = _supabase_runtime_snapshot()
        if snapshot:
            payload = snapshot.get("refused_ids", [])
            return payload if isinstance(payload, list) else []
        return []
    if _supabase_runtime_enabled():
        payload = _workspace_blob_read("workspace_refused", None)
        if isinstance(payload, list):
            return payload
        if _cloud_workspace_isolated():
            return []
    data = _read_json(REFUS_PATH, [])
    if isinstance(data, list) and data:
        return data
    snapshot = _supabase_runtime_snapshot()
    if snapshot:
        payload = snapshot.get("refused_ids", [])
        return payload if isinstance(payload, list) else []
    return data if isinstance(data, list) else []


def _lire_sync_supabase_state() -> dict:
    data = _read_json(SUPABASE_SYNC_STATE_PATH, {})
    return data if isinstance(data, dict) else {}


def _ecrire_lettres(lettres: dict) -> None:
    if _user_scoped_table_mode():
        _sync_user_scoped_letters_table(lettres)
        return
    if _supabase_runtime_enabled():
        _workspace_blob_write("workspace_letters", lettres)
        _sync_user_scoped_letters_table(lettres)
        return
    _write_json(LETTRES_PATH, lettres)


def _ecrire_historique(histo: list[dict]) -> None:
    if _user_scoped_table_mode():
        _sync_user_scoped_history_table(histo)
        return
    if _supabase_runtime_enabled():
        _workspace_blob_write("workspace_history", histo)
        _sync_user_scoped_history_table(histo)
        return
    _write_json(HISTO_PATH, histo)


def _ecrire_refus(refus: list[str]) -> None:
    if _user_scoped_table_mode():
        _sync_user_scoped_refused_table(refus)
        return
    if _supabase_runtime_enabled():
        _workspace_blob_write("workspace_refused", refus)
        _sync_user_scoped_refused_table(refus)
        return
    _write_json(REFUS_PATH, refus)


def _supabase_sync_configured() -> bool:
    return bool((os.environ.get("SUPABASE_URL") or "").strip() and (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip())


def _launch_supabase_sync_background(reason: str = "") -> tuple[bool, str]:
    sync_state = _lire_sync_supabase_state()
    if not _supabase_sync_configured():
        return False, "not_configured"
    if sync_state.get("status") == "running":
        return False, "already_running"
    if _proc_actif is not None and _proc_actif.poll() is None:
        return False, "process_busy"

    env = {
        **os.environ,
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    if reason:
        env["SUPABASE_SYNC_REASON"] = reason
    subprocess.Popen(
        [sys.executable, "scripts/sync_to_supabase.py", "--execute"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        cwd=str(BASE_DIR),
        env=env,
    )
    return True, "started"


def _maybe_launch_supabase_sync(reason: str = "") -> tuple[bool, str]:
    if not _supabase_auto_sync_enabled():
        return False, "disabled"
    return _launch_supabase_sync_background(reason)


def _scan_state() -> dict:
    from main import refresh_scan_state_from_exports

    if _supabase_runtime_enabled() and not CSV_PATH.exists():
        snapshot = _supabase_runtime_snapshot()
        if snapshot:
            payload = snapshot.get("scan_state", {})
            if isinstance(payload, dict):
                workspace_payload = _workspace_pipeline_data()
                workspace_offers = workspace_payload.get("offers", {})
                if isinstance(workspace_offers, dict) and workspace_offers:
                    merged_offers = dict(payload.get("offers") or {})
                    for key, value in workspace_offers.items():
                        base = merged_offers.get(key, {})
                        base = base if isinstance(base, dict) else {}
                        merged_offers[key] = {**base, **(value if isinstance(value, dict) else {})}
                    return {
                        **payload,
                        "offers": merged_offers,
                    }
                return payload

    with _scan_state_lock:
        return refresh_scan_state_from_exports()


def _sync_pipeline_status(
    offre_id: str,
    statut: str,
    url: str = "",
    titre: str = "",
    entreprise: str = "",
    source: str = "",
    lieu: str = "",
) -> None:
    from main import _offer_key_from_parts, sync_pipeline_status

    key = _offer_key_from_parts(offre_id, url, titre, entreprise, source, lieu)
    if _supabase_runtime_enabled():
        _workspace_pipeline_update_offer(key, {"manual_status": statut})
        return
    sync_pipeline_status(key, statut)


def _mark_offer_analyzed(offre_id: str, evaluation: dict) -> None:
    from main import mark_offer_analyzed

    if _supabase_runtime_enabled():
        _workspace_pipeline_update_offer(
            offre_id,
            {
                "analyzed": True,
                "score": evaluation.get("score"),
                "score_level": evaluation.get("level"),
                "positive_reasons": evaluation.get("positiveReasons", evaluation.get("raisons", [])),
                "negative_reasons": evaluation.get("negativeReasons", []),
                "detected_keywords": evaluation.get("detectedKeywords", []),
                "warnings": evaluation.get("warnings", []),
                "last_seen_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        return
    mark_offer_analyzed(offre_id, evaluation)


def _row_key(row: dict) -> str:
    from main import offer_key_from_export_row

    return offer_key_from_export_row(row)


def _pipeline_context() -> tuple[dict, dict, dict, list[dict], set[str]]:
    return _scan_state(), _lire_lettres(), _lire_scores(), _lire_historique(), set(_lire_refus())


def _historique_key_map(histo: list[dict]) -> dict[str, dict]:
    from main import _offer_key_from_parts, normalize_status, offer_aliases

    mapped: dict[str, dict] = {}
    for row in histo:
        key = str(row.get("offer_signature") or "").strip() or _offer_key_from_parts(
            row.get("id_adzuna", ""),
            row.get("url", ""),
            row.get("titre", ""),
            row.get("entreprise", ""),
            row.get("source", ""),
            row.get("lieu", ""),
        )
        if key:
            normalized_row = {**row, "statut": normalize_status(row.get("statut"))}
            for alias in offer_aliases(row, key):
                mapped[alias] = normalized_row
    return mapped


def _find_history_row_index(histo: list[dict], offre_id: str = "", url_cible: str = "") -> int:
    for index, row in enumerate(histo):
        if _history_row_matches(row, offre_id, url_cible):
            return index
    return -1


def _history_row_matches(row: dict, offre_id: str = "", url_cible: str = "") -> bool:
    if offre_id and str(row.get("offer_signature") or "").strip() == offre_id:
        return True
    if offre_id and row.get("id_adzuna") == offre_id:
        return True
    if url_cible and row.get("url") == url_cible:
        return True
    return False


def _compute_pipeline_status(record: dict, key: str, histo_row: dict | None, refus_ids: set[str], last_scan_new_keys: set[str]) -> str:
    from main import normalize_status

    manual = record.get("manual_status") or ""
    if manual:
        return normalize_status(manual)
    if key in refus_ids:
        return "refusee"
    if histo_row and normalize_status(histo_row.get("statut")) == "postule":
        return "postule"
    if record.get("letter_generated"):
        return "lettre_generee"
    if record.get("analyzed") and (record.get("score") or 0) >= 7:
        return "interessante"
    if key in last_scan_new_keys:
        return "nouvelle"
    return "a_analyser"


def _compute_pipeline_status_v2(record: dict, key: str, histo_row: dict | None, refus_ids: set[str], last_scan_new_keys: set[str]) -> str:
    from main import normalize_status

    manual = record.get("manual_status") or ""
    if manual:
        return normalize_status(manual)
    if key in refus_ids:
        return "refusee"
    if histo_row and normalize_status(histo_row.get("statut")) == "postule":
        return "postule"
    if record.get("letter_generated"):
        return "lettre_generee"
    if record.get("analyzed") and (record.get("score") or 0) >= 75:
        return "interessante"
    if key in last_scan_new_keys:
        return "nouvelle"
    return "a_analyser"


def _build_offer_payload(row: dict, scan_state: dict, lettres: dict, scores: dict, histo_map: dict[str, dict], refus_ids: set[str], last_scan_new_keys: set[str]) -> dict:
    key = _row_key(row)
    record = (scan_state.get("offers") or {}).get(key, {})
    lettre_info = lettres.get(key, {})
    score_info = scores.get(key, {})
    histo_row = histo_map.get(key)
    payload = dict(row)
    payload.update(
        {
            "pipeline_id": key,
            "pipeline_status": _compute_pipeline_status_v2(record, key, histo_row, refus_ids, last_scan_new_keys),
            "is_new": key in last_scan_new_keys,
            "is_refused": key in refus_ids,
            "is_applied": bool(histo_row and histo_row.get("statut") == "postule"),
            "analysis_done": bool(record.get("analyzed") or score_info),
            "letter_generated": bool(lettre_info.get("lettre") or record.get("letter_generated")),
            "score_value": score_info.get("score", record.get("score")),
            "score_level": score_info.get("level", record.get("score_level")),
            "positive_reasons": score_info.get("positiveReasons", record.get("positive_reasons", [])),
            "negative_reasons": score_info.get("negativeReasons", record.get("negative_reasons", [])),
            "detected_keywords": score_info.get("detectedKeywords", record.get("detected_keywords", [])),
            "warnings": score_info.get("warnings", record.get("warnings", [])),
            "score_reasons": score_info.get("positiveReasons", score_info.get("raisons", record.get("positive_reasons", []))),
            "first_seen_at": record.get("first_seen_at"),
            "last_seen_at": record.get("last_seen_at"),
        }
    )
    return payload


def _offer_matches_workspace_search(offer: dict, search: dict | None = None) -> bool:
    if not _supabase_runtime_enabled():
        return True

    from sources._common import offer_matches_search_settings

    search = search if isinstance(search, dict) else (get_settings().get("search") or {})
    if not isinstance(search, dict):
        return True

    title = _offer_title(offer)
    company = offer.get("Entreprise", "")
    location = offer.get("Ville ou zone", "")
    description = offer.get("Description (texte complet)", "")
    contract = offer.get("Type de contrat", "")
    return offer_matches_search_settings(title, company, location, description, contract, search)


def _filtered_offers() -> list[dict]:
    rows = _lire_csv()
    scan_state, lettres, scores, histo, refus_ids = _pipeline_context()
    histo_map = _historique_key_map(histo)
    last_scan_new_keys = set((scan_state.get("last_scan") or {}).get("new_offer_keys", []) or [])
    search = (get_settings().get("search") or {}) if _supabase_runtime_enabled() else {}
    offers = [
        _build_offer_payload(row, scan_state, lettres, scores, histo_map, refus_ids, last_scan_new_keys)
        for row in rows
    ]

    q = (request.args.get("q") or "").strip().lower()
    source = (request.args.get("source") or "").strip()
    statut = (request.args.get("statut") or "").strip()
    pertinence = (request.args.get("pertinence") or "").strip().lower()
    score_min = request.args.get("score_min", type=int)
    if score_min is None and _supabase_runtime_enabled():
        score_min = int((search if isinstance(search, dict) else {}).get("score_min") or 0)

    filtered = []
    for row in offers:
        if not _offer_matches_workspace_search(row, search):
            continue
        haystack = " ".join(
            [
                row.get("Intitulé du poste", ""),
                row.get("Entreprise", ""),
                row.get("Ville ou zone", ""),
                row.get("Description (texte complet)", ""),
            ]
        ).lower()
        score_value = row.get("score_value")
        if q and q not in haystack:
            continue
        if source and row.get("Source") != source:
            continue
        if statut and row.get("pipeline_status") != statut:
            continue
        if score_min is not None and score_min > 0 and (score_value is None or int(score_value) < score_min):
            continue
        if pertinence == "forte" and (score_value is None or int(score_value) < 75):
            continue
        if pertinence == "moyenne" and (score_value is None or int(score_value) < 50 or int(score_value) >= 75):
            continue
        if pertinence == "faible" and (score_value is None or int(score_value) >= 50):
            continue
        filtered.append(row)
    return filtered


@app.errorhandler(HTTPException)
def handle_http_error(err: HTTPException):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": err.description, "status": err.code}), err.code
    return err


@app.errorhandler(Exception)
def handle_unexpected_error(err: Exception):
    if request.path.startswith("/api/"):
        return jsonify(
            {
                "ok": False,
                "error": str(err),
                "status": HTTPStatus.INTERNAL_SERVER_ERROR,
            }
        ), HTTPStatus.INTERNAL_SERVER_ERROR
    raise err


@app.route("/")
def index():
    return render_template("index.html", cloud_mode=_cloud_mode_enabled())


@app.before_request
def require_access_token():
    if not _access_protection_enabled():
        return None
    path = request.path or "/"
    if path == "/" or path.startswith("/static/") or path in {"/api/auth/status", "/api/auth/unlock", "/api/auth/logout"}:
        return None
    if _access_unlocked():
        return None
    if path.startswith("/api/"):
        return jsonify({"ok": False, "locked": True, "error": "Accès protégé par token"}), HTTPStatus.UNAUTHORIZED
    return render_template("index.html", cloud_mode=_cloud_mode_enabled()), HTTPStatus.UNAUTHORIZED


@app.route("/api/auth/status")
def api_auth_status():
    return jsonify(
        {
            "ok": True,
            "enabled": _access_protection_enabled(),
            "auth_enabled": _supabase_auth_enabled(),
            "unlocked": _access_unlocked(),
            "cloud_mode": _cloud_mode_enabled(),
            "workspace": _current_workspace(),
        }
    )


@app.route("/api/user/status")
def api_user_status():
    user = _auth_user_payload()
    return jsonify(
        {
            "ok": True,
            "enabled": _supabase_auth_enabled(),
            "authenticated": user is not None,
            "user": user,
        }
    )


@app.route("/api/user/signup", methods=["POST"])
def api_user_signup():
    if not _supabase_auth_enabled():
        return jsonify({"ok": False, "error": "Authentification Supabase non active"}), HTTPStatus.BAD_REQUEST
    data = request.get_json(silent=True) or {}
    email = str(data.get("email") or "").strip().lower()
    password = str(data.get("password") or "")
    if not email or not password:
        return jsonify({"ok": False, "error": "Email et mot de passe requis"}), HTTPStatus.BAD_REQUEST
    try:
        payload = _supabase_auth_request(
            "/auth/v1/signup",
            {
                "email": email,
                "password": password,
                "data": {
                    "workspace_hint": _auth_workspace_slug("", email),
                },
            },
        )
    except Exception as err:
        return jsonify({"ok": False, "error": str(err)}), HTTPStatus.BAD_REQUEST
    session_payload = payload.get("session") if isinstance(payload.get("session"), dict) else None
    user_payload = payload.get("user") if isinstance(payload.get("user"), dict) else None
    authenticated = False
    user = None
    if session_payload and user_payload:
        user = _set_auth_session(
            user_payload,
            access_token=str(session_payload.get("access_token") or ""),
            refresh_token=str(session_payload.get("refresh_token") or ""),
        )
        authenticated = True
    return jsonify(
        {
            "ok": True,
            "authenticated": authenticated,
            "user": user,
            "requires_email_confirmation": not authenticated,
            "message": (
                "Compte cree. Verifie l'email de confirmation avant de te connecter."
                if not authenticated
                else "Compte cree et connecte."
            ),
        }
    )


@app.route("/api/user/login", methods=["POST"])
def api_user_login():
    if not _supabase_auth_enabled():
        return jsonify({"ok": False, "error": "Authentification Supabase non active"}), HTTPStatus.BAD_REQUEST
    data = request.get_json(silent=True) or {}
    email = str(data.get("email") or "").strip().lower()
    password = str(data.get("password") or "")
    if not email or not password:
        return jsonify({"ok": False, "error": "Email et mot de passe requis"}), HTTPStatus.BAD_REQUEST
    try:
        payload = _supabase_auth_request(
            "/auth/v1/token?grant_type=password",
            {"email": email, "password": password},
        )
    except Exception as err:
        return jsonify({"ok": False, "error": str(err)}), HTTPStatus.UNAUTHORIZED
    user_payload = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    user = _set_auth_session(
        user_payload,
        access_token=str(payload.get("access_token") or ""),
        refresh_token=str(payload.get("refresh_token") or ""),
    )
    session.permanent = True
    return jsonify({"ok": True, "authenticated": True, "user": user})


@app.route("/api/user/logout", methods=["POST"])
def api_user_logout():
    if _supabase_auth_enabled():
        token = str(session.get("auth_access_token") or "").strip()
        if token:
            try:
                _supabase_auth_request("/auth/v1/logout?scope=local", {}, access_token=token)
            except Exception:
                pass
    _clear_auth_session()
    return jsonify({"ok": True, "enabled": _supabase_auth_enabled(), "authenticated": False})


@app.route("/api/auth/unlock", methods=["POST"])
def api_auth_unlock():
    data = request.get_json(silent=True) or {}
    provided = str(data.get("token") or "").strip()
    requested_workspace = data.get("workspace") or request.args.get("workspace") or "principal"
    if _supabase_auth_enabled():
        if _auth_logged_in():
            auth_user = _auth_user_payload() or {}
            workspace = set_workspace_slug(auth_user.get("workspace") or "principal")
        else:
            workspace = set_workspace_slug("principal")
    else:
        workspace = set_workspace_slug(requested_workspace)
    if not _access_protection_enabled():
        return jsonify({"ok": True, "enabled": False, "unlocked": True, "workspace": workspace})
    if not provided or provided != _access_secret():
        return jsonify({"ok": False, "error": "Token invalide", "enabled": True, "unlocked": False}), HTTPStatus.UNAUTHORIZED
    session.permanent = True
    session["app_unlocked"] = True
    return jsonify({"ok": True, "enabled": True, "unlocked": True, "workspace": workspace})


@app.route("/api/auth/logout", methods=["POST"])
def api_auth_logout():
    session.pop("app_unlocked", None)
    _clear_auth_session()
    return jsonify({"ok": True, "enabled": _access_protection_enabled(), "unlocked": False, "workspace": _current_workspace()})


@app.route("/api/settings")
def api_settings():
    return jsonify({"ok": True, "settings": get_settings()})


@app.route("/api/settings", methods=["POST"])
def api_settings_save():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "Payload JSON invalide"}), HTTPStatus.BAD_REQUEST
    settings = save_settings(data)
    _maybe_launch_supabase_sync("settings_save")
    return jsonify({"ok": True, "settings": settings})


@app.route("/api/settings/export-example")
def api_settings_export_example():
    payload = build_shareable_settings_example()
    filename = f"alternance_auto_settings_example_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    return Response(
        content,
        mimetype="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/api/settings/cv-path", methods=["POST"])
def api_settings_cv_path():
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askopenfilename(
            title="Choisir un CV",
            filetypes=[
                ("Documents PDF", "*.pdf"),
                ("Documents Word", "*.docx;*.doc"),
                ("Tous les fichiers", "*.*"),
            ],
        )
        root.destroy()
        return jsonify({"ok": True, "path": selected or ""})
    except Exception as err:
        return jsonify({"ok": False, "error": str(err)}), HTTPStatus.INTERNAL_SERVER_ERROR


@app.route("/api/settings/cv-upload", methods=["POST"])
def api_settings_cv_upload():
    uploaded = request.files.get("cv")
    if uploaded is None or not uploaded.filename:
        return jsonify({"ok": False, "error": "Aucun fichier CV reçu"}), HTTPStatus.BAD_REQUEST

    suffix = Path(uploaded.filename).suffix.lower()
    if suffix not in {".pdf", ".doc", ".docx", ".txt", ".md"}:
        return jsonify({"ok": False, "error": "Format de CV non pris en charge"}), HTTPStatus.BAD_REQUEST

    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    filename = _safe_filename(uploaded.filename)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = UPLOADS_DIR / f"{timestamp}_{filename}"
    uploaded.save(destination)

    current = get_settings()
    updated = save_settings(
        {
            "profile": {
                **current.get("profile", {}),
                "cv_path": str(destination),
            }
        }
    )
    _maybe_launch_supabase_sync("cv_upload")
    return jsonify(
        {
            "ok": True,
            "path": str(destination),
            "filename": destination.name,
            "cv_excerpt": _extract_cv_text(str(destination), limit=1200),
            "settings": updated,
        }
    )


@app.route("/api/settings/search-coach", methods=["POST"])
def api_settings_search_coach():
    try:
        data = request.get_json(silent=True) or {}
        messages = data.get("messages", [])
        if not isinstance(messages, list):
            return jsonify({"ok": False, "error": "messages doit etre une liste"}), HTTPStatus.BAD_REQUEST
        coach_context = _build_search_coach_context()
        current_search = coach_context.get("current_search", {})
        current_profile = coach_context.get("profile", {})
        if not isinstance(current_search, dict):
            current_search = {}
        if not isinstance(current_profile, dict):
            current_profile = {}

        has_context = bool(
            _coach_list(coach_context.get("recent_history"))
            or _coach_list(coach_context.get("top_scored_offers"))
            or str((coach_context.get("profile") or {}).get("cv_excerpt") or "").strip()
        )

        from groq import Groq
        from main import _MODELES_GROQ

        api_key = os.environ.get("GROQ_API_KEY", "")
        if not api_key:
            return jsonify(
                {
                    "ok": True,
                    "reply": (
                        "Je n'ai pas accès au modèle IA pour l'instant. "
                        "Décris-moi le poste idéal, les outils clés, la zone et ce que tu veux éviter, "
                        "puis je pourrai te proposer une base de paramètres dès que l'API sera configurée."
                    ),
                    "ready": False,
                    "suggestions": {
                        "postes_cibles": _coach_list(current_search.get("postes_cibles")),
                        "mots_cles_positifs": _coach_list(current_search.get("mots_cles_positifs")),
                        "mots_cles_negatifs": _coach_list(current_search.get("mots_cles_negatifs")),
                        "types_contrat": _coach_list(current_search.get("types_contrat")),
                        "zone_mode": str(current_search.get("zone_mode", "idf") or "idf"),
                        "zone_geo": str(current_search.get("zone_geo", "") or ""),
                    },
                    "fallback": True,
                    "context_used": has_context,
                }
            )

        compact_context = json.dumps(coach_context, ensure_ascii=False, separators=(",", ":"))
        system_prompt = f"""
Tu es un coach de recherche d'emploi pour une application locale d'alternance.
Tu aides la personne a clarifier sa recherche ciblee, surtout pour les postes, mots-cles, exclusions, zone geographique et type de contrat.

Contexte réel déjà présent dans l'application:
{compact_context}

Objectif:
- poser des questions utiles, une ou deux a la fois
- rester concret, simple et chaleureux
- aider a formuler une recherche exploitable par un moteur de filtrage
- quand tu as assez d'informations, proposer une version exploitable des champs
- si le contexte existant est déjà riche, pars de lui pour proposer des pistes très adaptées
- si le contexte est pauvre ou ambigu, pose des questions d'accompagnement

Tu dois repondre STRICTEMENT en JSON valide avec cette forme:
{{
  "reply": "message pour l'utilisateur",
  "ready": true ou false,
  "suggestions": {{
    "postes_cibles": ["..."],
    "mots_cles_positifs": ["..."],
    "mots_cles_negatifs": ["..."],
    "types_contrat": ["alternance"],
    "zone_mode": "idf|lyon|bordeaux|remote|france|custom",
    "zone_geo": "texte libre"
  }}
}}

Regles:
- "reply" doit etre en francais
- si tu n'as pas encore assez d'infos, "ready" = false et tu poses la prochaine question
- si tu proposes des champs, garde seulement les plus utiles
- n'invente pas des preferences que l'utilisateur n'a pas exprimees
- les listes doivent etre courtes, propres et sans doublons
""".strip()

        chat_messages = [{"role": "system", "content": system_prompt}]
        if not messages:
            chat_messages.append(
                {
                    "role": "user",
                    "content": (
                        "Commence l'accompagnement. Analyse d'abord ce qui existe déjà dans l'application "
                        "pour proposer des pistes de postes adaptés. "
                        "S'il manque des informations, pose-moi des questions."
                    ),
                }
            )
        else:
            for item in messages[-8:]:
                if not isinstance(item, dict):
                    continue
                role = str(item.get("role", "user"))
                if role not in {"user", "assistant"}:
                    continue
                content = str(item.get("content", "")).strip()
                if not content:
                    continue
                chat_messages.append({"role": role, "content": content})

        client = Groq(api_key=api_key)
        last_error = None
        coach_models = sorted(_MODELES_GROQ, key=lambda item: 0 if "8b" in item.lower() or "instant" in item.lower() else 1)
        for modele in coach_models:
            try:
                resp = client.chat.completions.create(
                    model=modele,
                    messages=chat_messages,
                    temperature=0.2,
                    max_tokens=420,
                    response_format={"type": "json_object"},
                )
                raw = (resp.choices[0].message.content or "").strip()
                payload = json.loads(raw)
                suggestions = payload.get("suggestions", {}) if isinstance(payload.get("suggestions"), dict) else {}
                cleaned = {
                    "postes_cibles": [str(x).strip() for x in _coach_list(suggestions.get("postes_cibles")) if str(x).strip()][:10],
                    "mots_cles_positifs": [str(x).strip() for x in _coach_list(suggestions.get("mots_cles_positifs")) if str(x).strip()][:12],
                    "mots_cles_negatifs": [str(x).strip() for x in _coach_list(suggestions.get("mots_cles_negatifs")) if str(x).strip()][:12],
                    "types_contrat": [str(x).strip().lower() for x in _coach_list(suggestions.get("types_contrat")) if str(x).strip()][:4],
                    "zone_mode": str(suggestions.get("zone_mode", current_search.get("zone_mode", "idf")) or "idf").strip().lower(),
                    "zone_geo": str(suggestions.get("zone_geo", current_search.get("zone_geo", "")) or "").strip(),
                }
                return jsonify(
                    {
                        "ok": True,
                        "reply": _repair_text(str(payload.get("reply", "")).strip()),
                        "ready": bool(payload.get("ready", False)),
                        "suggestions": cleaned,
                        "modele": modele,
                        "context_used": has_context,
                    }
                )
            except Exception as exc:
                last_error = str(exc)
                if "429" in last_error or "rate_limit" in last_error.lower():
                    continue
                break
        return jsonify(_coach_fallback_payload(current_search, has_context, last_error or "Assistant indisponible"))
    except Exception as exc:
        return jsonify(_coach_fallback_payload({}, False, str(exc)))


def _stats_payload(offers: list[dict] | None = None, histo: list[dict] | None = None, last_scan: dict | None = None, include_health: bool = False) -> dict:
    from main import normalize_status

    offers = offers if isinstance(offers, list) else _filtered_offers()
    histo = histo if isinstance(histo, list) else _lire_historique()
    last_scan = last_scan if isinstance(last_scan, dict) else (_scan_state().get("last_scan") or {})
    today = datetime.now().strftime("%Y-%m-%d")
    relances = [
        row
        for row in histo
        if normalize_status(row.get("statut")) == "postule" and (row.get("date_relance_prevue") or "9999") <= today
    ]
    pipeline_counts: dict[str, int] = {}
    for offer in offers:
        status = offer.get("pipeline_status", "a_analyser")
        pipeline_counts[status] = pipeline_counts.get(status, 0) + 1
    nb_nouvelles_visible = sum(1 for offer in offers if offer.get("is_new"))
    nb_interessantes_visible = pipeline_counts.get("interessante", 0)
    nb_a_analyser_visible = pipeline_counts.get("a_analyser", 0)
    nb_postulations_envoyees = sum(1 for row in histo if normalize_status(row.get("statut")) == "postule")
    return {
        "nb_offres": len(offers),
        "nb_postulations": len(histo),
        "nb_envoyees": nb_postulations_envoyees,
        "nb_relances": len(relances),
        "nb_lettres": sum(1 for offer in offers if offer.get("letter_generated")),
        "nb_nouvelles_visible": nb_nouvelles_visible,
        "nb_interessantes_visible": nb_interessantes_visible,
        "nb_a_analyser_visible": nb_a_analyser_visible,
        "nb_nouvelles_scan_global": int(last_scan.get("new_offers", 0) or 0),
        "process_actif": _proc_actif is not None and _proc_actif.poll() is None,
        "pipeline": pipeline_counts,
        "scan_state": last_scan,
        "supabase_sync": _lire_sync_supabase_state(),
        "setup": get_setup_status(),
        "health": build_health_status() if include_health else {},
        "cloud_mode": _cloud_mode_enabled(),
        "shared_scan": _cloud_mode_enabled(),
        "workspace": _current_workspace(),
    }


@app.route("/api/stats")
def api_stats():
    include_health = request.args.get("include_health") == "1"
    return jsonify(_stats_payload(include_health=include_health))


@app.route("/api/dashboard")
def api_dashboard():
    return api_stats()


@app.route("/api/bootstrap")
def api_bootstrap():
    offers = _filtered_offers()
    histo = _lire_historique()
    last_scan = (_scan_state().get("last_scan") or {})
    return jsonify(
        {
            "ok": True,
            "stats": _stats_payload(offers=offers, histo=histo, last_scan=last_scan, include_health=False),
            "offres": offers,
            "historique": histo,
            "lettres": _lire_lettres(),
            "settings": get_settings(),
        }
    )


@app.route("/api/health")
def api_health():
    return jsonify({"ok": True, "health": build_health_status()})


@app.route("/api/health/export")
def api_health_export():
    payload = build_shareable_diagnostic()
    filename = f"alternance_auto_diagnostic_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    return Response(
        content,
        mimetype="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/api/offres")
def api_offres():
    return jsonify(_filtered_offers())


@app.route("/api/offres/<path:offre_id>/analyse")
def api_offre_analyse(offre_id: str):
    offer = next((row for row in _filtered_offers() if row.get("pipeline_id") == offre_id), None)
    if not offer:
        return jsonify({"ok": False, "error": "Offre introuvable"}), 404
    summary = []
    if offer.get("score_value") is not None:
        level = offer.get("score_level") or "non classe"
        summary.append(f"Score actuel : {offer['score_value']}/100 ({level})")
    for reason in offer.get("positive_reasons", [])[:3]:
        summary.append(reason)
    for warning in offer.get("warnings", [])[:2]:
        summary.append(f"Alerte : {warning}")
    if offer.get("letter_generated"):
        summary.append("Lettre disponible")
    if offer.get("is_applied"):
        summary.append("Postulation deja enregistree")
    if not summary:
        summary.append("A analyser en priorite")
    return jsonify(
        {
            "ok": True,
            "id": offre_id,
            "status": offer.get("pipeline_status"),
            "title": offer.get("Intitulé du poste", ""),
            "company": offer.get("Entreprise", ""),
            "source": offer.get("Source", ""),
            "date": offer.get("Date de publication", ""),
            "score": offer.get("score_value"),
            "level": offer.get("score_level"),
            "reasons": offer.get("positive_reasons", []),
            "negativeReasons": offer.get("negative_reasons", []),
            "detectedKeywords": offer.get("detected_keywords", []),
            "warnings": offer.get("warnings", []),
            "summary": summary,
            "description": offer.get("Description (texte complet)", ""),
            "letter_generated": offer.get("letter_generated", False),
            "is_applied": offer.get("is_applied", False),
            "first_seen_at": offer.get("first_seen_at"),
            "last_seen_at": offer.get("last_seen_at"),
        }
    )


@app.route("/api/offres/<path:offre_id>/statut", methods=["PUT"])
def api_offre_statut(offre_id: str):
    data = request.get_json(silent=True) or {}
    statut = (data.get("statut") or "").strip()
    if not statut:
        return jsonify({"ok": False, "error": "statut manquant"}), 400
    _sync_pipeline_status(
        offre_id,
        statut,
        data.get("url", ""),
        data.get("titre", ""),
        data.get("entreprise", ""),
        data.get("source", ""),
        data.get("lieu", ""),
    )
    _maybe_launch_supabase_sync("offre_statut")
    return jsonify({"ok": True, "id": offre_id, "statut": statut})


@app.route("/api/pipeline")
def api_pipeline():
    scan_state = _scan_state()
    offers = _filtered_offers()
    kanban: dict[str, list[dict]] = {}
    for offer in offers:
        kanban.setdefault(offer.get("pipeline_status", "a_analyser"), []).append(offer)
    return jsonify({"scan_state": scan_state, "offers": offers, "kanban": kanban})


@app.route("/api/scan/state")
def api_scan_state():
    return jsonify(_scan_state())


@app.route("/api/historique")
def api_historique():
    _scan_state()
    return jsonify(_lire_historique())


@app.route("/api/lettres")
def api_lettres():
    _scan_state()
    return jsonify(_lire_lettres())


@app.route("/api/lettre/<path:offre_id>", methods=["PUT"])
def api_lettre_put(offre_id: str):
    from main import mark_offer_letter_generated

    data = request.get_json(silent=True) or {}
    lettre = data.get("lettre", "")
    lettres = _lire_lettres()
    existing = lettres.get(offre_id, {})
    lettres[offre_id] = {
        **existing,
        "signature": offre_id,
        "lettre": lettre,
        "titre": data.get("titre", existing.get("titre", "")),
        "entreprise": data.get("entreprise", existing.get("entreprise", "")),
        "url": data.get("url", existing.get("url", "")),
        "source": data.get("source", existing.get("source", "")),
        "date_gen": existing.get("date_gen", datetime.now().strftime("%Y-%m-%d %H:%M")),
        "date_modif": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    _ecrire_lettres(lettres)
    if _supabase_runtime_enabled():
        _workspace_pipeline_update_offer(
            offre_id,
            {
                "letter_generated": True,
                "last_seen_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
    else:
        mark_offer_letter_generated(
            offre_id,
            titre=lettres[offre_id].get("titre", ""),
            entreprise=lettres[offre_id].get("entreprise", ""),
        )
    _maybe_launch_supabase_sync("lettre_save")
    return jsonify({"ok": True})


@app.route("/api/lettre/regen", methods=["POST"])
def api_lettre_regen():
    data = request.get_json(silent=True) or {}
    offre_id = data.get("id", "")
    titre = data.get("titre", "")
    entreprise = data.get("entreprise", "")
    description = data.get("description", "")
    if not offre_id:
        return jsonify({"ok": False, "error": "id manquant"}), 400

    from groq import Groq
    from main import _MODELES_GROQ, _construire_prompt_lettre, _sauvegarder_lettre

    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        return jsonify({"ok": False, "error": "GROQ_API_KEY manquante dans .env"}), 500

    client = Groq(api_key=api_key)
    prompt = _construire_prompt_lettre(titre, entreprise, description)
    for modele in _MODELES_GROQ:
        try:
            resp = client.chat.completions.create(
                model=modele,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=800,
            )
            lettre = resp.choices[0].message.content.strip()
            if _supabase_runtime_enabled():
                lettres = _lire_lettres()
                existing = lettres.get(offre_id, {})
                lettres[offre_id] = {
                    **existing,
                    "signature": offre_id,
                    "lettre": lettre,
                    "titre": titre,
                    "entreprise": entreprise,
                    "date_gen": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "date_modif": datetime.now().strftime("%Y-%m-%d %H:%M"),
                }
                _ecrire_lettres(lettres)
                _workspace_pipeline_update_offer(
                    offre_id,
                    {
                        "letter_generated": True,
                        "last_seen_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    },
                )
            else:
                _sauvegarder_lettre(offre_id, lettre, titre, entreprise)
            _maybe_launch_supabase_sync("lettre_regen")
            return jsonify({"ok": True, "lettre": lettre, "modele": modele})
        except Exception as exc:
            message = str(exc)
            if "429" in message or "rate_limit" in message.lower():
                continue
            return jsonify({"ok": False, "error": message}), 500
    return jsonify({"ok": False, "quota": True, "error": "Limite Groq atteinte"}), 429


def _stream(cmd: list[str], extra_env: dict | None = None) -> Response:
    global _proc_actif

    if _proc_actif is not None and _proc_actif.poll() is None:
        def busy():
            yield f"data: {json.dumps({'error': 'Un processus est deja en cours.'})}\n\n"

        return Response(busy(), mimetype="text/event-stream")

    env = {
        **os.environ,
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        **(extra_env or {}),
    }
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(BASE_DIR),
        env=env,
    )
    _proc_actif = proc

    def generate():
        global _proc_actif
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                yield f"data: {json.dumps({'line': line.rstrip()})}\n\n"
            proc.wait()
            yield f"data: {json.dumps({'done': True, 'code': proc.returncode})}\n\n"
        finally:
            _proc_actif = None

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/stream/recuperer")
def stream_recuperer():
    return _stream([sys.executable, "main.py"], extra_env=_current_search_env_overrides())


@app.route("/api/scan/start", methods=["POST"])
def api_scan_start():
    if _cloud_mode_enabled():
        ok, status, state = _launch_cloud_background_task(
            "recuperer",
            "Scan des sources",
            [sys.executable, "main.py"],
            _current_search_env_overrides(),
        )
        return jsonify({"ok": ok, "status": status, "task": state})
    return jsonify({"ok": True, "stream_url": "/api/stream/recuperer"})


@app.route("/api/cloud-task/start", methods=["POST"])
def api_cloud_task_start():
    if not _cloud_mode_enabled():
        return jsonify({"ok": False, "error": "Route reservee au mode cloud"}), HTTPStatus.BAD_REQUEST
    data = request.get_json(silent=True) or {}
    task_type = str(data.get("type") or "").strip().lower()
    task_map = {
        "recuperer": ("Scan des sources", [sys.executable, "main.py"], None),
        "generer-lettres": ("Generation des lettres", [sys.executable, "main.py", "lettres"], None),
        "postuler": ("Lancement de l agent", [sys.executable, "main.py", "postuler", "--auto"], {"ALTERNANCE_WEB_MODE": "1"}),
        "sync-supabase": ("Synchronisation Supabase", [sys.executable, "scripts/sync_to_supabase.py", "--execute"], None),
    }
    config = task_map.get(task_type)
    if not config:
        return jsonify({"ok": False, "error": "Type de tache inconnu"}), HTTPStatus.BAD_REQUEST
    label, cmd, extra_env = config
    ok, status, state = _launch_cloud_background_task(task_type, label, cmd, extra_env)
    return jsonify({"ok": ok, "status": status, "task": state})


@app.route("/api/cloud-task/state")
def api_cloud_task_state():
    if not _cloud_mode_enabled():
        return jsonify({"ok": False, "error": "Route reservee au mode cloud"}), HTTPStatus.BAD_REQUEST
    state = _lire_cloud_task_state()
    return jsonify({"ok": True, "task": state, "log_lines": _tail_cloud_task_log()})


@app.route("/api/stream/postuler")
def stream_postuler():
    return _stream(
        [sys.executable, "main.py", "postuler", "--auto"],
        extra_env={"ALTERNANCE_WEB_MODE": "1"},
    )


@app.route("/api/stream/generer-lettres")
def stream_generer_lettres():
    return _stream([sys.executable, "main.py", "lettres"])


@app.route("/api/stream/sync-supabase")
def stream_sync_supabase():
    return _stream([sys.executable, "scripts/sync_to_supabase.py", "--execute"])


@app.route("/api/supabase/sync", methods=["POST"])
def api_supabase_sync():
    data = request.get_json(silent=True) or {}
    ok, status = _launch_supabase_sync_background(str(data.get("reason", "") or "").strip())
    return jsonify({"ok": ok, "status": status, "sync_state": _lire_sync_supabase_state()})


@app.route("/api/historique/statut", methods=["PUT"])
def api_historique_statut():
    from main import normalize_status

    data = request.get_json(silent=True) or {}
    id_adzuna = data.get("id_adzuna", "")
    url_cible = data.get("url", "")
    offre_id = data.get("offer_signature", "") or id_adzuna
    nouveau_normalise = normalize_status(data.get("statut", "postule"))

    histo = _lire_historique()
    for index, row in enumerate(histo):
        if not _history_row_matches(row, offre_id, url_cible):
            continue
        histo[index]["offer_signature"] = str(row.get("offer_signature") or offre_id).strip() or offre_id
        histo[index]["statut"] = nouveau_normalise
        histo[index]["notes"] = data.get("notes", row.get("notes", ""))
        if nouveau_normalise == "postule":
            histo[index]["date_postulation"] = histo[index].get("date_postulation") or datetime.now().strftime("%Y-%m-%d %H:%M")
            histo[index]["date_relance_prevue"] = histo[index].get("date_relance_prevue") or (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
        _sync_pipeline_status(
            histo[index]["offer_signature"] or row.get("id_adzuna", ""),
            nouveau_normalise,
            row.get("url", ""),
            row.get("titre", ""),
            row.get("entreprise", ""),
            row.get("source", ""),
            row.get("lieu", ""),
        )
        _ecrire_historique(histo)
        _maybe_launch_supabase_sync("historique_statut")
        return jsonify({"ok": True, "statut": nouveau_normalise})
    for index, row in enumerate(histo):
        match = (id_adzuna and row.get("id_adzuna") == id_adzuna) or (url_cible and row.get("url") == url_cible)
        if not match:
            continue
        histo[index]["statut"] = nouveau_normalise
        histo[index]["notes"] = data.get("notes", row.get("notes", ""))
        if nouveau_normalise == "postule":
            histo[index]["date_postulation"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            histo[index]["date_relance_prevue"] = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
        _sync_pipeline_status(
            row.get("id_adzuna", ""),
            nouveau_normalise,
            row.get("url", ""),
            row.get("titre", ""),
            row.get("entreprise", ""),
        )
        break
    _ecrire_historique(histo)
    _maybe_launch_supabase_sync("historique_statut")
    return jsonify({"ok": True})


@app.route("/api/historique/notes", methods=["PUT"])
def api_historique_notes():
    data = request.get_json(silent=True) or {}
    id_adzuna = data.get("offer_signature", "") or data.get("id_adzuna", "")
    url_cible = data.get("url", "")
    histo = _lire_historique()
    for index, row in enumerate(histo):
        match = _history_row_matches(row, id_adzuna, url_cible)
        if match:
            histo[index]["notes"] = data.get("notes", "")
            break
    _ecrire_historique(histo)
    _maybe_launch_supabase_sync("historique_notes")
    return jsonify({"ok": True})


@app.route("/api/scores")
def api_scores():
    return jsonify(_lire_scores())


@app.route("/api/diagnostic")
def api_diagnostic():
    from main import build_pipeline_diagnostic

    return jsonify({"ok": True, "diagnostic": build_pipeline_diagnostic()})


@app.route("/api/offres/scorer", methods=["POST"])
def api_offres_scorer():
    data = request.get_json(silent=True) or {}
    offre_id = data.get("id", "")
    titre = data.get("titre", "")
    entreprise = data.get("entreprise", "")
    description = data.get("description", "")
    if not offre_id:
        return jsonify({"ok": False, "error": "id manquant"}), 400

    from main import score_offer_fit

    evaluation = score_offer_fit(
        {
            "pipeline_id": offre_id,
            "id": offre_id,
            "source": data.get("source", ""),
            "url": data.get("url", ""),
            "titre": titre,
            "entreprise": entreprise,
            "lieu": data.get("lieu", ""),
            "description": description,
            "contrat": data.get("contrat", ""),
        }
    )
    evaluation["signature"] = offre_id
    _save_scores_merged({offre_id: evaluation})
    _mark_offer_analyzed(offre_id, evaluation)
    return jsonify({"ok": True, **evaluation})


@app.route("/api/offres/scorer-batch", methods=["POST"])
def api_offres_scorer_batch():
    from main import score_offer_fit

    data = request.get_json(silent=True) or {}
    offers = data.get("offers", [])
    if not isinstance(offers, list):
        return jsonify({"ok": False, "error": "offers invalide"}), 400

    score_updates: dict[str, dict] = {}
    updated = 0
    processed_ids: list[str] = []

    for offer in offers:
        if not isinstance(offer, dict):
            continue
        offre_id = str(offer.get("id", "")).strip()
        if not offre_id:
            continue
        evaluation = score_offer_fit(
            {
                "pipeline_id": offre_id,
                "id": offre_id,
                "source": offer.get("source", ""),
                "url": offer.get("url", ""),
                "titre": offer.get("titre", ""),
                "entreprise": offer.get("entreprise", ""),
                "lieu": offer.get("lieu", ""),
                "description": offer.get("description", ""),
                "contrat": offer.get("contrat", ""),
            }
        )
        evaluation["signature"] = offre_id
        score_updates[offre_id] = evaluation
        _mark_offer_analyzed(offre_id, evaluation)
        updated += 1
        processed_ids.append(offre_id)

    if score_updates:
        _save_scores_merged(score_updates)
    return jsonify({"ok": True, "updated": updated, "processed_ids": processed_ids})


@app.route("/api/offres/refusees")
def api_offres_refusees():
    return jsonify(_lire_refus())


@app.route("/api/offres/refuser", methods=["POST"])
def api_offres_refuser():
    data = request.get_json(silent=True) or {}
    offre_id = data.get("id", "")
    if not offre_id:
        return jsonify({"ok": False, "error": "id manquant"}), 400
    refus = _lire_refus()
    if offre_id not in refus:
        refus.append(offre_id)
    _ecrire_refus(refus)
    _sync_pipeline_status(offre_id, "refusee")
    _maybe_launch_supabase_sync("offre_refusee")
    return jsonify({"ok": True})


@app.route("/api/offres/refuser", methods=["DELETE"])
def api_offres_refuser_annuler():
    data = request.get_json(silent=True) or {}
    offre_id = data.get("id", "")
    refus = [row for row in _lire_refus() if row != offre_id]
    _ecrire_refus(refus)
    _sync_pipeline_status(offre_id, "")
    _maybe_launch_supabase_sync("offre_refus_annule")
    return jsonify({"ok": True})


@app.route("/api/historique/ajouter", methods=["POST"])
def api_historique_ajouter():
    from main import build_offer_signature

    data = request.get_json(silent=True) or {}
    id_adzuna = data.get("id_adzuna", "")
    url_cible = data.get("url", "")
    offer_signature = data.get("offer_signature", "")
    offre_id = offer_signature or build_offer_signature(
        {
            "source": data.get("source", ""),
            "sourceId": id_adzuna,
            "id": id_adzuna,
            "url": url_cible,
            "titre": data.get("titre", ""),
            "entreprise": data.get("entreprise", ""),
            "lieu": data.get("lieu", ""),
        }
    )
    histo = _lire_historique()
    existing_index = _find_history_row_index(histo, offre_id, url_cible)
    if existing_index >= 0:
        existing = histo[existing_index]
        existing["offer_signature"] = offre_id
        if id_adzuna:
            existing["id_adzuna"] = id_adzuna
        if url_cible:
            existing["url"] = url_cible
        if data.get("titre"):
            existing["titre"] = data.get("titre", "")
        if data.get("entreprise"):
            existing["entreprise"] = data.get("entreprise", "")
        if data.get("source"):
            existing["source"] = data.get("source", "")
        if data.get("lieu"):
            existing["lieu"] = data.get("lieu", "")
        existing["statut"] = "postule"
        if not existing.get("date_relance_prevue"):
            existing["date_relance_prevue"] = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
        _ecrire_historique(histo)
        _sync_pipeline_status(
            offre_id,
            "postule",
            url_cible,
            data.get("titre", ""),
            data.get("entreprise", ""),
            data.get("source", ""),
            data.get("lieu", ""),
        )
        _maybe_launch_supabase_sync("historique_ajout")
        return jsonify({"ok": True, "already_present": True})
    for row in histo:
        if _history_row_matches(row, offre_id, url_cible):
            return jsonify({"ok": False, "error": "Offre deja dans l'historique"}), 409
    for row in histo:
        if (id_adzuna and row.get("id_adzuna") == id_adzuna) or (url_cible and row.get("url") == url_cible):
            return jsonify({"ok": False, "error": "Déjà dans l'historique"}), 409
    now = datetime.now()
    histo.append(
        {
            "id_adzuna": id_adzuna,
            "offer_signature": offre_id,
            "url": url_cible,
            "titre": data.get("titre", ""),
            "entreprise": data.get("entreprise", ""),
            "source": data.get("source", ""),
            "lieu": data.get("lieu", ""),
            "statut": "postule",
            "date_postulation": now.strftime("%Y-%m-%d %H:%M"),
            "date_relance_prevue": (now + timedelta(days=7)).strftime("%Y-%m-%d"),
            "notes": "",
        }
    )
    _ecrire_historique(histo)
    _sync_pipeline_status(
        offre_id,
        "postule",
        url_cible,
        data.get("titre", ""),
        data.get("entreprise", ""),
        data.get("source", ""),
        data.get("lieu", ""),
    )
    _maybe_launch_supabase_sync("historique_ajout")
    return jsonify({"ok": True})


@app.route("/api/historique/supprimer", methods=["DELETE"])
def api_historique_supprimer():
    data = request.get_json(silent=True) or {}
    id_adzuna = data.get("offer_signature", "") or data.get("id_adzuna", "")
    url_cible = data.get("url", "")
    histo = _lire_historique()
    before = len(histo)
    histo = [
        row
        for row in histo
        if not _history_row_matches(row, id_adzuna, url_cible)
    ]
    _ecrire_historique(histo)
    _sync_pipeline_status(id_adzuna, "", url_cible)
    _maybe_launch_supabase_sync("historique_suppression")
    return jsonify({"ok": True, "supprime": before - len(histo)})


@app.route("/api/arreter", methods=["POST"])
def api_arreter():
    global _proc_actif
    if _proc_actif and _proc_actif.poll() is None:
        _proc_actif.terminate()
        if _cloud_mode_enabled():
            _ecrire_cloud_task_state(
                {
                    **_lire_cloud_task_state(),
                    "status": "failed",
                    "finished_at": datetime.now().isoformat(timespec="seconds"),
                    "message": "Traitement arrete manuellement.",
                }
            )
        return jsonify({"ok": True})
    return jsonify({"ok": False})


@app.route("/api/serveur/fermer", methods=["POST"])
def api_serveur_fermer():
    global _proc_actif

    if _cloud_mode_enabled():
        return jsonify({"ok": False, "error": "Arrêt distant désactivé en mode cloud"}), HTTPStatus.FORBIDDEN

    if _proc_actif and _proc_actif.poll() is None:
        _proc_actif.terminate()

    def _shutdown_app():
        time.sleep(0.35)
        os._exit(0)

    Thread(target=_shutdown_app, daemon=True).start()
    return jsonify({"ok": True, "message": "Serveur en cours d'arret"})


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "cloud_mode": _cloud_mode_enabled()}), HTTPStatus.OK


if __name__ == "__main__":
    _ensure_stdio()
    _write_pid_file()
    atexit.register(_cleanup_pid_file)
    cloud_mode = os.environ.get("ALTERNANCE_CLOUD_MODE") == "1"
    host = os.environ.get("APP_HOST", "0.0.0.0" if cloud_mode else "127.0.0.1")
    try:
        port = int(os.environ.get("PORT") or os.environ.get("APP_PORT") or "5001")
    except Exception:
        port = 5001
    base_url = f"http://{host}:{port}"

    def _ouvrir_navigateur():
        if cloud_mode:
            return
        # Wait until Flask is actually accepting connections before opening the page.
        for _ in range(30):
            try:
                with urlopen(base_url, timeout=0.5):
                    webbrowser.open(base_url)
                    return
            except Exception:
                time.sleep(0.5)

    Thread(target=_ouvrir_navigateur, daemon=True).start()
    print(f"Alternance Auto - {base_url}")
    app.run(host=host, port=port, debug=False, threaded=True)

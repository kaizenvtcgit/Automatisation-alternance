import importlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from storage_service import BASE_DIR, PROFILE_PATH, read_json, write_json_atomic


ENV_PATH = BASE_DIR / ".env"


def _parse_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "oui", "yes", "on"}


def _parse_int(value, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _normalize_tags(value) -> list[str]:
    if isinstance(value, list):
        items = value
    elif isinstance(value, str):
        items = value.split(",")
    else:
        items = []

    seen: set[str] = set()
    normalized: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(text)
    return normalized


def _read_env_lines() -> list[str]:
    if not ENV_PATH.exists():
        return []
    return ENV_PATH.read_text(encoding="utf-8").splitlines()


def _read_env_map() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in _read_env_lines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _write_env_updates(updates: dict[str, str]) -> None:
    lines = _read_env_lines()
    if not lines:
        lines = []

    remaining = dict(updates)
    output: list[str] = []
    for line in lines:
        if "=" not in line or line.lstrip().startswith("#"):
            output.append(line)
            continue
        key, _value = line.split("=", 1)
        env_key = key.strip()
        if env_key in remaining:
            output.append(f"{env_key}={remaining.pop(env_key)}")
        else:
            output.append(line)

    if output and output[-1].strip():
        output.append("")

    for key, value in remaining.items():
        output.append(f"{key}={value}")

    ENV_PATH.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def _default_profile_settings() -> dict:
    env = _read_env_map()
    return {
        "postes_cibles": ["motion designer", "ux designer", "ui designer", "product designer", "web designer"],
        "mots_cles_positifs": [],
        "mots_cles_negatifs": ["senior", "commercial", "print"],
        "types_contrat": ["alternance"],
        "zone_geo": "Ile-de-France",
        "zone_mode": "idf",
        "rayon_km": 30,
        "score_min": 0,
        "inclure_remote": _parse_bool(env.get("INCLURE_OFFRES_REMOTE", "1"), True),
    }


def _read_profile_settings() -> dict:
    defaults = _default_profile_settings()
    loaded = read_json(PROFILE_PATH, defaults)
    if not isinstance(loaded, dict):
        return defaults
    merged = {**defaults, **loaded}
    merged["postes_cibles"] = _normalize_tags(merged.get("postes_cibles"))
    merged["mots_cles_positifs"] = _normalize_tags(merged.get("mots_cles_positifs"))
    merged["mots_cles_negatifs"] = _normalize_tags(merged.get("mots_cles_negatifs"))
    merged["types_contrat"] = _normalize_tags(merged.get("types_contrat"))
    merged["rayon_km"] = _parse_int(merged.get("rayon_km"), 30, 10, 100)
    merged["score_min"] = _parse_int(merged.get("score_min"), 0, 0, 100)
    merged["inclure_remote"] = _parse_bool(merged.get("inclure_remote"), True)
    merged["zone_geo"] = str(merged.get("zone_geo") or "Ile-de-France").strip()
    merged["zone_mode"] = str(merged.get("zone_mode") or "idf").strip().lower()
    return merged


def _profile_payload_from_input(data: dict) -> dict:
    defaults = _default_profile_settings()
    payload = {
        "postes_cibles": _normalize_tags(data.get("postes_cibles", defaults["postes_cibles"])),
        "mots_cles_positifs": _normalize_tags(data.get("mots_cles_positifs", defaults["mots_cles_positifs"])),
        "mots_cles_negatifs": _normalize_tags(data.get("mots_cles_negatifs", defaults["mots_cles_negatifs"])),
        "types_contrat": _normalize_tags(data.get("types_contrat", defaults["types_contrat"])),
        "zone_geo": str(data.get("zone_geo", defaults["zone_geo"]) or "").strip(),
        "zone_mode": str(data.get("zone_mode", defaults["zone_mode"]) or "idf").strip().lower(),
        "rayon_km": _parse_int(data.get("rayon_km"), defaults["rayon_km"], 10, 100),
        "score_min": _parse_int(data.get("score_min"), defaults["score_min"], 0, 100),
        "inclure_remote": _parse_bool(data.get("inclure_remote"), defaults["inclure_remote"]),
    }
    return payload


def get_settings() -> dict:
    env = _read_env_map()
    profile = _read_profile_settings()
    return {
        "profile": {
            "prenom": env.get("CANDIDAT_PRENOM", ""),
            "nom": env.get("CANDIDAT_NOM", ""),
            "email": env.get("CANDIDAT_EMAIL", ""),
            "tel": env.get("CANDIDAT_TEL", ""),
            "portfolio": env.get("CANDIDAT_PORTFOLIO", env.get("CANDIDAT_LIEN", "")),
            "linkedin": env.get("CANDIDAT_LINKEDIN", ""),
            "github": env.get("CANDIDAT_GITHUB", ""),
            "cv_path": env.get("CV_PATH", ""),
            "presentation": env.get("CANDIDAT_PRESENTATION", ""),
        },
        "search": profile,
        "api_keys": {
            "adzuna_app_id": env.get("ADZUNA_APP_ID", ""),
            "adzuna_app_key": env.get("ADZUNA_APP_KEY", ""),
            "ft_client_id": env.get("FT_CLIENT_ID", ""),
            "ft_client_secret": env.get("FT_CLIENT_SECRET", ""),
            "lba_api_key": env.get("LBA_API_KEY", ""),
            "groq_api_key": env.get("GROQ_API_KEY", ""),
            "gemini_api_key": env.get("GEMINI_API_KEY", ""),
        },
        "agent": {
            "confirmation_required": _parse_bool(env.get("AGENT_CONFIRMATION_REQUIRED", "1"), True),
            "delay_seconds": _parse_int(env.get("AGENT_DELAY_SECONDS", "5"), 5, 1, 30),
            "max_candidatures_session": _parse_int(env.get("AGENT_MAX_CANDIDATURES_SESSION", "20"), 20, 1, 500),
        },
    }


def get_setup_status() -> dict:
    settings = get_settings()
    profile = settings["profile"]
    api_keys = settings["api_keys"]

    missing_profile: list[str] = []
    if not profile.get("prenom"):
        missing_profile.append("Prenom")
    if not profile.get("nom"):
        missing_profile.append("Nom")
    if not profile.get("email"):
        missing_profile.append("Email")
    if not profile.get("cv_path"):
        missing_profile.append("CV")
    if not any(profile.get(key) for key in ("portfolio", "linkedin", "github")):
        missing_profile.append("Lien pro")

    missing_sources: list[str] = []
    if not any(api_keys.get(key) for key in ("ft_client_id", "adzuna_app_id", "lba_api_key")):
        missing_sources.append("Au moins une source d'offres")

    missing_ai: list[str] = []
    if not api_keys.get("groq_api_key"):
        missing_ai.append("Groq")
    if not api_keys.get("gemini_api_key"):
        missing_ai.append("Gemini")

    ready_for_scan = not missing_sources
    ready_for_letters = not missing_ai[:1]
    ready_for_apply = not missing_profile and not missing_ai

    return {
        "profile_complete": not missing_profile,
        "scan_ready": ready_for_scan,
        "letters_ready": ready_for_letters,
        "apply_ready": ready_for_apply,
        "missing_profile": missing_profile,
        "missing_sources": missing_sources,
        "missing_ai": missing_ai,
    }


def _refresh_runtime_modules() -> None:
    modules_to_reload = [
        "main",
        "agent_candidature",
        "sources._common",
        "sources.adzuna",
        "sources.france_travail",
        "sources.la_bonne_alternance",
        "sources.remotive",
    ]
    for module_name in modules_to_reload:
        module = sys.modules.get(module_name)
        if module is not None:
            importlib.reload(module)


def save_settings(payload: dict) -> dict:
    current = get_settings()
    profile_input = payload.get("profile", {}) if isinstance(payload.get("profile"), dict) else {}
    search_input = payload.get("search", {}) if isinstance(payload.get("search"), dict) else {}
    api_input = payload.get("api_keys", {}) if isinstance(payload.get("api_keys"), dict) else {}
    agent_input = payload.get("agent", {}) if isinstance(payload.get("agent"), dict) else {}

    env_updates = {
        "CANDIDAT_PRENOM": str(profile_input.get("prenom", current["profile"]["prenom"]) or "").strip(),
        "CANDIDAT_NOM": str(profile_input.get("nom", current["profile"]["nom"]) or "").strip(),
        "CANDIDAT_EMAIL": str(profile_input.get("email", current["profile"]["email"]) or "").strip(),
        "CANDIDAT_TEL": str(profile_input.get("tel", current["profile"]["tel"]) or "").strip(),
        "CANDIDAT_PORTFOLIO": str(profile_input.get("portfolio", current["profile"]["portfolio"]) or "").strip(),
        "CANDIDAT_LINKEDIN": str(profile_input.get("linkedin", current["profile"].get("linkedin", "")) or "").strip(),
        "CANDIDAT_GITHUB": str(profile_input.get("github", current["profile"].get("github", "")) or "").strip(),
        "CV_PATH": str(profile_input.get("cv_path", current["profile"]["cv_path"]) or "").strip(),
        "CANDIDAT_PRESENTATION": str(profile_input.get("presentation", current["profile"]["presentation"]) or "").strip(),
        "ADZUNA_APP_ID": str(api_input.get("adzuna_app_id", current["api_keys"]["adzuna_app_id"]) or "").strip(),
        "ADZUNA_APP_KEY": str(api_input.get("adzuna_app_key", current["api_keys"]["adzuna_app_key"]) or "").strip(),
        "FT_CLIENT_ID": str(api_input.get("ft_client_id", current["api_keys"]["ft_client_id"]) or "").strip(),
        "FT_CLIENT_SECRET": str(api_input.get("ft_client_secret", current["api_keys"]["ft_client_secret"]) or "").strip(),
        "LBA_API_KEY": str(api_input.get("lba_api_key", current["api_keys"]["lba_api_key"]) or "").strip(),
        "GROQ_API_KEY": str(api_input.get("groq_api_key", current["api_keys"]["groq_api_key"]) or "").strip(),
        "GEMINI_API_KEY": str(api_input.get("gemini_api_key", current["api_keys"]["gemini_api_key"]) or "").strip(),
        "AGENT_CONFIRMATION_REQUIRED": "1" if _parse_bool(agent_input.get("confirmation_required", current["agent"]["confirmation_required"]), True) else "0",
        "AGENT_DELAY_SECONDS": str(_parse_int(agent_input.get("delay_seconds"), current["agent"]["delay_seconds"], 1, 30)),
        "AGENT_MAX_CANDIDATURES_SESSION": str(_parse_int(agent_input.get("max_candidatures_session"), current["agent"]["max_candidatures_session"], 1, 500)),
    }

    profile_payload = _profile_payload_from_input({**current["search"], **search_input})
    env_updates["INCLURE_OFFRES_REMOTE"] = "1" if profile_payload["inclure_remote"] else "0"

    _write_env_updates(env_updates)
    write_json_atomic(PROFILE_PATH, profile_payload)

    for key, value in env_updates.items():
        os.environ[key] = value

    _refresh_runtime_modules()
    return get_settings()


def build_shareable_settings_example() -> dict:
    current = get_settings()
    search = current.get("search", {})
    agent = current.get("agent", {})
    return {
        "meta": {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "description": "Exemple de configuration partageable sans donnees personnelles ni cles secretes.",
        },
        "profile": {
            "prenom": "",
            "nom": "",
            "email": "",
            "tel": "",
            "portfolio": "",
            "linkedin": "",
            "github": "",
            "cv_path": "",
            "presentation": "",
        },
        "search": {
            "postes_cibles": list(search.get("postes_cibles") or []),
            "mots_cles_positifs": list(search.get("mots_cles_positifs") or []),
            "mots_cles_negatifs": list(search.get("mots_cles_negatifs") or []),
            "types_contrat": list(search.get("types_contrat") or ["alternance"]),
            "zone_geo": str(search.get("zone_geo") or "Ile-de-France"),
            "zone_mode": str(search.get("zone_mode") or "idf"),
            "rayon_km": int(search.get("rayon_km") or 30),
            "score_min": int(search.get("score_min") or 0),
            "inclure_remote": bool(search.get("inclure_remote", True)),
        },
        "api_keys": {
            "adzuna_app_id": "",
            "adzuna_app_key": "",
            "ft_client_id": "",
            "ft_client_secret": "",
            "lba_api_key": "",
            "groq_api_key": "",
            "gemini_api_key": "",
        },
        "agent": {
            "confirmation_required": bool(agent.get("confirmation_required", True)),
            "delay_seconds": int(agent.get("delay_seconds") or 5),
            "max_candidatures_session": int(agent.get("max_candidatures_session") or 20),
        },
    }

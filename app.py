"""
Interface web locale - Alternance Auto.
Lancement : python app.py puis http://localhost:5001
"""

import csv
import json
import os
import atexit
import re
import shutil
import subprocess
import sys
import time
import webbrowser
from datetime import datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from threading import Lock, Thread
from urllib.request import urlopen

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request
from settings_service import get_settings, save_settings
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

CSV_SEP = ";"

_proc_actif: subprocess.Popen | None = None
_scores_lock = Lock()
_scan_state_lock = Lock()


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
    history = _lire_historique()
    scores = _lire_scores()
    offers = _lire_csv()

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
                "raisons": score_info.get("positiveReasons", [])[:3],
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
        for row in history[-12:]
    ]

    return {
        "profile": {
            "presentation": current_profile.get("presentation", ""),
            "portfolio": current_profile.get("portfolio", ""),
            "linkedin": current_profile.get("linkedin", ""),
            "github": current_profile.get("github", ""),
            "cv_path": current_profile.get("cv_path", ""),
            "cv_excerpt": _extract_cv_text(current_profile.get("cv_path", "")),
        },
        "current_search": current_search,
        "recent_history": recent_history,
        "top_scored_offers": top_offers[:8],
        "letters_count": len(_lire_lettres()),
        "scores_count": len(scores),
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


def _lire_csv() -> list[dict]:
    from main import is_offer_within_max_age
    from sources._common import is_relevant_offer

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
    data = _read_json(HISTO_PATH, [])
    return data if isinstance(data, list) else []


def _lire_lettres() -> dict:
    data = _read_json(LETTRES_PATH, {})
    return data if isinstance(data, dict) else {}


def _lire_scores() -> dict:
    data = _read_json(SCORES_PATH, {})
    return data if isinstance(data, dict) else {}


def _save_scores_merged(updates: dict[str, dict]) -> dict:
    with _scores_lock:
        scores = _lire_scores()
        scores.update(updates)
        _write_json(SCORES_PATH, scores)
        return scores


def _lire_refus() -> list[str]:
    data = _read_json(REFUS_PATH, [])
    return data if isinstance(data, list) else []


def _lire_sync_supabase_state() -> dict:
    data = _read_json(SUPABASE_SYNC_STATE_PATH, {})
    return data if isinstance(data, dict) else {}


def _ecrire_lettres(lettres: dict) -> None:
    _write_json(LETTRES_PATH, lettres)


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


def _scan_state() -> dict:
    from main import refresh_scan_state_from_exports

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

    sync_pipeline_status(_offer_key_from_parts(offre_id, url, titre, entreprise, source, lieu), statut)


def _mark_offer_analyzed(offre_id: str, evaluation: dict) -> None:
    from main import mark_offer_analyzed

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


def _filtered_offers() -> list[dict]:
    rows = _lire_csv()
    scan_state, lettres, scores, histo, refus_ids = _pipeline_context()
    histo_map = _historique_key_map(histo)
    last_scan_new_keys = set((scan_state.get("last_scan") or {}).get("new_offer_keys", []) or [])
    offers = [
        _build_offer_payload(row, scan_state, lettres, scores, histo_map, refus_ids, last_scan_new_keys)
        for row in rows
    ]

    q = (request.args.get("q") or "").strip().lower()
    source = (request.args.get("source") or "").strip()
    statut = (request.args.get("statut") or "").strip()
    pertinence = (request.args.get("pertinence") or "").strip().lower()
    score_min = request.args.get("score_min", type=int)

    filtered = []
    for row in offers:
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
        if score_min is not None and (score_value is None or int(score_value) < score_min):
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
    return render_template("index.html")


@app.route("/api/settings")
def api_settings():
    return jsonify({"ok": True, "settings": get_settings()})


@app.route("/api/settings", methods=["POST"])
def api_settings_save():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "Payload JSON invalide"}), HTTPStatus.BAD_REQUEST
    settings = save_settings(data)
    return jsonify({"ok": True, "settings": settings})


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
    data = request.get_json(silent=True) or {}
    messages = data.get("messages", [])
    if not isinstance(messages, list):
        return jsonify({"ok": False, "error": "messages doit etre une liste"}), HTTPStatus.BAD_REQUEST
    coach_context = _build_search_coach_context()
    current_search = coach_context.get("current_search", {})
    current_profile = coach_context.get("profile", {})

    has_context = bool(
        coach_context.get("recent_history")
        or coach_context.get("top_scored_offers")
        or coach_context.get("profile", {}).get("cv_excerpt")
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
                    "postes_cibles": current_search.get("postes_cibles", []),
                    "mots_cles_positifs": current_search.get("mots_cles_positifs", []),
                    "mots_cles_negatifs": current_search.get("mots_cles_negatifs", []),
                    "types_contrat": current_search.get("types_contrat", []),
                    "zone_mode": current_search.get("zone_mode", "idf"),
                    "zone_geo": current_search.get("zone_geo", ""),
                },
                "fallback": True,
                "context_used": has_context,
            }
        )

    system_prompt = f"""
Tu es un coach de recherche d'emploi pour une application locale d'alternance.
Tu aides la personne a clarifier sa recherche ciblee, surtout pour les postes, mots-cles, exclusions, zone geographique et type de contrat.

Contexte réel déjà présent dans l'application:
{json.dumps(coach_context, ensure_ascii=False, indent=2)}

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
        for item in messages[-12:]:
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
    for modele in _MODELES_GROQ:
        try:
            resp = client.chat.completions.create(
                model=modele,
                messages=chat_messages,
                temperature=0.3,
                max_tokens=900,
                response_format={"type": "json_object"},
            )
            raw = (resp.choices[0].message.content or "").strip()
            payload = json.loads(raw)
            suggestions = payload.get("suggestions", {}) if isinstance(payload.get("suggestions"), dict) else {}
            cleaned = {
                "postes_cibles": [str(x).strip() for x in suggestions.get("postes_cibles", []) if str(x).strip()][:10],
                "mots_cles_positifs": [str(x).strip() for x in suggestions.get("mots_cles_positifs", []) if str(x).strip()][:12],
                "mots_cles_negatifs": [str(x).strip() for x in suggestions.get("mots_cles_negatifs", []) if str(x).strip()][:12],
                "types_contrat": [str(x).strip().lower() for x in suggestions.get("types_contrat", []) if str(x).strip()][:4],
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
    return jsonify(
        {
            "ok": True,
            "reply": (
                "Je n'ai pas pu contacter le modèle IA cette fois. "
                "Tu peux quand même me répondre en précisant les postes visés, les mots-clés importants et ce que tu veux exclure."
            ),
            "ready": False,
            "suggestions": {
                "postes_cibles": current_search.get("postes_cibles", []),
                "mots_cles_positifs": current_search.get("mots_cles_positifs", []),
                "mots_cles_negatifs": current_search.get("mots_cles_negatifs", []),
                "types_contrat": current_search.get("types_contrat", []),
                "zone_mode": current_search.get("zone_mode", "idf"),
                "zone_geo": current_search.get("zone_geo", ""),
            },
            "fallback": True,
            "context_used": has_context,
            "error": last_error or "Assistant indisponible",
        }
    )


@app.route("/api/stats")
def api_stats():
    from main import normalize_status

    offers = _filtered_offers()
    histo = _lire_historique()
    last_scan = (_scan_state().get("last_scan") or {})
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
    return jsonify(
        {
            "nb_offres": len(offers),
            "nb_postulations": len(histo),
            "nb_envoyees": sum(1 for row in histo if normalize_status(row.get("statut")) == "postule"),
            "nb_relances": len(relances),
            "nb_lettres": sum(1 for offer in offers if offer.get("letter_generated")),
            "process_actif": _proc_actif is not None and _proc_actif.poll() is None,
            "pipeline": pipeline_counts,
            "scan_state": last_scan,
            "supabase_sync": _lire_sync_supabase_state(),
        }
    )


@app.route("/api/dashboard")
def api_dashboard():
    return api_stats()


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
    mark_offer_letter_generated(
        offre_id,
        titre=lettres[offre_id].get("titre", ""),
        entreprise=lettres[offre_id].get("entreprise", ""),
    )
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
            _sauvegarder_lettre(offre_id, lettre, titre, entreprise)
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
    return _stream([sys.executable, "main.py"])


@app.route("/api/scan/start", methods=["POST"])
def api_scan_start():
    return jsonify({"ok": True, "stream_url": "/api/stream/recuperer"})


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
        _write_json(HISTO_PATH, histo)
        _launch_supabase_sync_background("historique_statut")
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
    _write_json(HISTO_PATH, histo)
    _launch_supabase_sync_background("historique_statut")
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
    _write_json(HISTO_PATH, histo)
    _launch_supabase_sync_background("historique_notes")
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
    _write_json(REFUS_PATH, refus)
    _sync_pipeline_status(offre_id, "refusee")
    _launch_supabase_sync_background("offre_refusee")
    return jsonify({"ok": True})


@app.route("/api/offres/refuser", methods=["DELETE"])
def api_offres_refuser_annuler():
    data = request.get_json(silent=True) or {}
    offre_id = data.get("id", "")
    refus = [row for row in _lire_refus() if row != offre_id]
    _write_json(REFUS_PATH, refus)
    _sync_pipeline_status(offre_id, "")
    _launch_supabase_sync_background("offre_refus_annule")
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
        _write_json(HISTO_PATH, histo)
        _sync_pipeline_status(
            offre_id,
            "postule",
            url_cible,
            data.get("titre", ""),
            data.get("entreprise", ""),
            data.get("source", ""),
            data.get("lieu", ""),
        )
        _launch_supabase_sync_background("historique_ajout")
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
    _write_json(HISTO_PATH, histo)
    _sync_pipeline_status(
        offre_id,
        "postule",
        url_cible,
        data.get("titre", ""),
        data.get("entreprise", ""),
        data.get("source", ""),
        data.get("lieu", ""),
    )
    _launch_supabase_sync_background("historique_ajout")
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
    _write_json(HISTO_PATH, histo)
    _sync_pipeline_status(id_adzuna, "", url_cible)
    _launch_supabase_sync_background("historique_suppression")
    return jsonify({"ok": True, "supprime": before - len(histo)})


@app.route("/api/arreter", methods=["POST"])
def api_arreter():
    global _proc_actif
    if _proc_actif and _proc_actif.poll() is None:
        _proc_actif.terminate()
        return jsonify({"ok": True})
    return jsonify({"ok": False})


@app.route("/api/serveur/fermer", methods=["POST"])
def api_serveur_fermer():
    global _proc_actif

    if _proc_actif and _proc_actif.poll() is None:
        _proc_actif.terminate()

    def _shutdown_app():
        time.sleep(0.35)
        os._exit(0)

    Thread(target=_shutdown_app, daemon=True).start()
    return jsonify({"ok": True, "message": "Serveur en cours d'arret"})


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

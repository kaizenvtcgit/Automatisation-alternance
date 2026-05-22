from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from main import normalize_status, offer_key_from_export_row, parse_offer_publication_date, score_offer_fit
from settings_service import get_settings
from storage_service import (
    CSV_PATH,
    HISTORIQUE_PATH,
    LETTRES_PATH,
    PROFILE_PATH,
    REFUS_PATH,
    SCAN_STATE_PATH,
    SCORES_PATH,
    read_json,
    write_json_atomic,
    SUPABASE_SYNC_STATE_PATH,
)


load_dotenv(BASE_DIR / ".env")

CSV_SEP = ";"


def _to_iso(value: str | None) -> str | None:
    parsed = parse_offer_publication_date(value or "")
    if parsed is None:
        return None
    return parsed.isoformat()


def _to_iso_datetime(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(text, fmt).isoformat()
        except ValueError:
            continue
    return text


def _stable_uuid(*parts: Any) -> str:
    raw = "||".join(str(part or "").strip() for part in parts)
    return str(uuid5(NAMESPACE_URL, raw))


def write_sync_state(status: str, table_counts: dict[str, int] | None = None, error: str | None = None) -> None:
    existing = read_json(SUPABASE_SYNC_STATE_PATH, {})
    existing = existing if isinstance(existing, dict) else {}
    history = list(existing.get("history") or [])
    payload = {
        "status": status,
        "last_attempt_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "table_counts": table_counts or {},
        "error": error or "",
        "history": history,
    }
    if status == "completed":
        payload["last_success_at"] = payload["last_attempt_at"]
        history.append(
            {
                "status": status,
                "at": payload["last_attempt_at"],
                "table_counts": table_counts or {},
                "error": "",
            }
        )
    else:
        if existing.get("last_success_at"):
            payload["last_success_at"] = existing.get("last_success_at")
        if status == "failed":
            history.append(
                {
                    "status": status,
                    "at": payload["last_attempt_at"],
                    "table_counts": table_counts or {},
                    "error": error or "",
                }
            )
    payload["history"] = history[-12:]
    write_json_atomic(SUPABASE_SYNC_STATE_PATH, payload)


def _read_csv_rows() -> list[dict]:
    if not CSV_PATH.exists():
        return []
    with CSV_PATH.open(encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle, delimiter=CSV_SEP))


def _load_scores() -> dict:
    loaded = read_json(SCORES_PATH, {})
    return loaded if isinstance(loaded, dict) else {}


def _load_letters() -> dict:
    loaded = read_json(LETTRES_PATH, {})
    return loaded if isinstance(loaded, dict) else {}


def _load_history() -> list[dict]:
    loaded = read_json(HISTORIQUE_PATH, [])
    return loaded if isinstance(loaded, list) else []


def _load_refused() -> list[str]:
    loaded = read_json(REFUS_PATH, [])
    return loaded if isinstance(loaded, list) else []


def _load_scan_state() -> dict:
    loaded = read_json(SCAN_STATE_PATH, {})
    return loaded if isinstance(loaded, dict) else {}


def _load_profile() -> dict:
    loaded = read_json(PROFILE_PATH, {})
    return loaded if isinstance(loaded, dict) else {}


def _csv_title(row: dict) -> str:
    return row.get("Intitulé du poste", row.get("IntitulÃ© du poste", row.get("IntitulÃƒÂ© du poste", "")))


def _csv_category(row: dict) -> str:
    return row.get("Catégorie", row.get("CatÃ©gorie", ""))


def _csv_family(row: dict) -> str:
    return row.get("Famille détectée (motion / UX…)", row.get("Famille dÃ©tectÃ©e (motion / UXâ€¦)", ""))


def _csv_query(row: dict) -> str:
    return row.get("Requête qui a trouvé l'annonce", row.get("RequÃªte qui a trouvÃ© l'annonce", ""))


def _csv_family(row: dict) -> str:
    return row.get(
        "Famille detectee",
        row.get("Famille dÃ©tectÃ©e (motion / UXâ€¦)", row.get("Famille dÃƒÂ©tectÃƒÂ©e (motion / UXÃ¢â‚¬Â¦)", "")),
    )


def _csv_query(row: dict) -> str:
    return row.get(
        "Requete source",
        row.get("RequÃªte qui a trouvÃ© l'annonce", row.get("RequÃƒÂªte qui a trouvÃƒÂ© l'annonce", "")),
    )


def _build_offer_lookup(
    csv_rows: list[dict],
    scan_state: dict,
    letters: dict,
    history_rows: list[dict],
    refused_signatures: set[str],
    scores: dict,
) -> dict[str, dict]:
    base_offer = {
        "signature": "",
        "source_offer_id": "",
        "source": "",
        "title": "",
        "company": "",
        "location": "",
        "offer_url": "",
        "published_at": None,
        "contract_type": "",
        "category": "",
        "detected_family": "",
        "found_query": "",
        "description": "",
        "pipeline_status": "a_analyser",
        "is_refused": False,
        "first_seen_at": None,
        "last_seen_at": None,
    }
    offers: dict[str, dict] = {}
    for row in csv_rows:
        signature = offer_key_from_export_row(row)
        evaluation = scores.get(signature) or score_offer_fit(
            {
                "id": signature,
                "source": row.get("Source", ""),
                "titre": _csv_title(row),
                "entreprise": row.get("Entreprise", ""),
                "lieu": row.get("Ville ou zone", ""),
                "description": row.get("Description (texte complet)", ""),
                "url": row.get("Lien vers l'annonce", ""),
                "contrat": row.get("Type de contrat", ""),
            }
        )
        offers[signature] = {
            **base_offer,
            "signature": signature,
            "source_offer_id": row.get("ID annonce", ""),
            "source": row.get("Source", ""),
            "title": _csv_title(row),
            "company": row.get("Entreprise", ""),
            "location": row.get("Ville ou zone", ""),
            "offer_url": row.get("Lien vers l'annonce", ""),
            "published_at": _to_iso(row.get("Date de publication")),
            "contract_type": row.get("Type de contrat", ""),
            "category": _csv_category(row),
            "detected_family": _csv_family(row),
            "found_query": _csv_query(row),
            "description": row.get("Description (texte complet)", ""),
            "pipeline_status": "interessante" if int(evaluation.get("score", 0)) >= 75 else "a_analyser",
            "is_refused": signature in refused_signatures,
        }

    scan_offers = (scan_state.get("offers") or {})
    for signature, record in scan_offers.items():
        if not isinstance(record, dict):
            continue
        existing = offers.get(signature, {})
        offers[signature] = {
            **base_offer,
            **existing,
            "signature": signature,
            "source_offer_id": existing.get("source_offer_id") or record.get("id", ""),
            "source": existing.get("source") or record.get("source", ""),
            "title": existing.get("title") or record.get("title", "") or signature,
            "company": existing.get("company") or record.get("company", ""),
            "location": existing.get("location") or record.get("location", ""),
            "offer_url": existing.get("offer_url") or record.get("url", ""),
            "published_at": existing.get("published_at") or _to_iso(record.get("date_pub")),
            "contract_type": existing.get("contract_type") or record.get("contract_type", ""),
            "category": existing.get("category") or record.get("category", ""),
            "detected_family": existing.get("detected_family") or record.get("family", ""),
            "found_query": existing.get("found_query") or record.get("query", ""),
            "description": existing.get("description") or record.get("description_preview", ""),
            "pipeline_status": normalize_status(record.get("manual_status") or record.get("status"), "a_analyser"),
            "is_refused": signature in refused_signatures,
            "first_seen_at": _to_iso_datetime(record.get("first_seen_at")),
            "last_seen_at": _to_iso_datetime(record.get("last_seen_at")),
        }

    for signature, letter_payload in letters.items():
        if signature in offers:
            continue
        if not isinstance(letter_payload, dict):
            continue
        offers[signature] = {
            **base_offer,
            "signature": signature,
            "source_offer_id": "",
            "source": "",
            "title": letter_payload.get("titre", "") or signature,
            "company": letter_payload.get("entreprise", ""),
            "location": "",
            "offer_url": "",
            "published_at": None,
            "contract_type": "",
            "category": "",
            "detected_family": "",
            "found_query": "",
            "description": "",
            "pipeline_status": "lettre_generee",
            "is_refused": signature in refused_signatures,
        }

    for row in history_rows:
        signature = str(row.get("offer_signature") or "").strip()
        if not signature or signature in offers:
            continue
        offers[signature] = {
            **base_offer,
            "signature": signature,
            "source_offer_id": row.get("id_adzuna", ""),
            "source": "",
            "title": row.get("titre", "") or signature,
            "company": "",
            "location": "",
            "offer_url": row.get("url", ""),
            "published_at": None,
            "contract_type": "",
            "category": "",
            "detected_family": "",
            "found_query": "",
            "description": "",
            "pipeline_status": normalize_status(row.get("statut")),
            "is_refused": signature in refused_signatures or normalize_status(row.get("statut")) == "refusee",
        }

    return offers


def build_offers_payload(csv_rows: list[dict], scan_state: dict, letters: dict, history_rows: list[dict], refused_signatures: set[str], scores: dict) -> list[dict]:
    return list(_build_offer_lookup(csv_rows, scan_state, letters, history_rows, refused_signatures, scores).values())


def build_offer_scores_payload(scores: dict) -> list[dict]:
    payload: list[dict] = []
    for signature, value in scores.items():
        if not isinstance(value, dict):
            continue
        payload.append(
            {
                "offer_signature": signature,
                "score": int(value.get("score", 0) or 0),
                "level": str(value.get("level", "faible") or "faible"),
                "score_payload": value,
                "scored_at": _to_iso_datetime(value.get("date")),
            }
        )
    return payload


def build_offer_letters_payload(letters: dict) -> list[dict]:
    payload: list[dict] = []
    for signature, value in letters.items():
        if not isinstance(value, dict):
            continue
        payload.append(
            {
                "offer_signature": signature,
                "title": value.get("titre", ""),
                "company": value.get("entreprise", ""),
                "letter_text": value.get("lettre", ""),
                "letter_payload": value,
                "generated_at": _to_iso_datetime(value.get("date_gen")),
            }
        )
    return payload


def build_applications_history_payload(history_rows: list[dict]) -> list[dict]:
    payload: list[dict] = []
    for row in history_rows:
        offer_signature = row.get("offer_signature") or None
        applied_at = _to_iso_datetime(row.get("date_postulation"))
        payload.append(
            {
                "id": _stable_uuid(offer_signature, row.get("titre"), applied_at, row.get("url")),
                "offer_signature": offer_signature,
                "source_offer_id": row.get("id_adzuna") or None,
                "offer_url": row.get("url") or None,
                "title": row.get("titre", "") or "(sans titre)",
                "status": normalize_status(row.get("statut")),
                "notes": row.get("notes") or None,
                "applied_at": applied_at,
                "followup_due_at": _to_iso_datetime(row.get("date_relance_prevue")),
            }
        )
    return payload


def build_refused_offers_payload(refused_signatures: set[str], offer_lookup: dict[str, dict]) -> list[dict]:
    source_id_to_signature: dict[str, str] = {}
    url_to_signature: dict[str, str] = {}
    for signature, offer in offer_lookup.items():
        source_offer_id = str(offer.get("source_offer_id") or "").strip()
        offer_url = str(offer.get("offer_url") or "").strip()
        if source_offer_id:
            source_id_to_signature[source_offer_id] = signature
        if offer_url:
            url_to_signature[offer_url] = signature

    resolved: list[dict] = []
    seen: set[str] = set()
    for raw_value in sorted(refused_signatures):
        signature = raw_value
        if signature not in offer_lookup:
            signature = source_id_to_signature.get(raw_value, raw_value)
        if signature not in offer_lookup:
            signature = url_to_signature.get(raw_value, raw_value)
        if signature not in offer_lookup or signature in seen:
            continue
        seen.add(signature)
        resolved.append({"offer_signature": signature})
    return resolved


def build_scan_payloads(scan_state: dict) -> tuple[list[dict], list[dict]]:
    last_scan = dict((scan_state.get("last_scan") or {}))
    history_items = list(scan_state.get("history") or [])
    scan_runs: list[dict] = []
    source_runs: list[dict] = []

    for item in history_items:
        started_at = _to_iso_datetime(item.get("started_at"))
        run_id = _stable_uuid("scan_run", started_at, item.get("finished_at"), item.get("status"))
        scan_runs.append(
            {
                "id": run_id,
                "status": item.get("status", "unknown"),
                "started_at": started_at,
                "finished_at": _to_iso_datetime(item.get("finished_at")),
                "offers_found": int(item.get("offers_found", 0) or 0),
                "new_offers": int(item.get("new_offers", 0) or 0),
                "duplicates_ignored": int(item.get("duplicates_ignored", 0) or 0),
                "exported_offers": int(item.get("exported_offers", 0) or 0),
                "errors": item.get("errors", []),
                "new_offer_keys": item.get("new_offer_keys", []),
                "raw_payload": item,
            }
        )
        for source_name, source_payload in (item.get("sources_scanned") or {}).items():
            source_runs.append(
                {
                    "id": _stable_uuid("scan_run_source", run_id, source_name),
                    "scan_run_id": run_id,
                    "source": source_name,
                    "status": source_payload.get("status", "unknown"),
                    "offers_found": int(source_payload.get("offers_found", 0) or 0),
                    "new_offers": int(source_payload.get("new_offers", 0) or 0),
                    "duplicates": int(source_payload.get("duplicates", 0) or 0),
                    "error_message": source_payload.get("error_message") or None,
                    "source_timestamp": _to_iso_datetime(source_payload.get("timestamp")),
                    "raw_payload": source_payload,
                }
            )

    if last_scan:
        started_at = _to_iso_datetime(last_scan.get("started_at"))
        run_id = _stable_uuid("scan_run_current", started_at, last_scan.get("finished_at"), last_scan.get("status"))
        scan_runs.append(
            {
                "id": run_id,
                "status": last_scan.get("status", "unknown"),
                "started_at": started_at,
                "finished_at": _to_iso_datetime(last_scan.get("finished_at")),
                "offers_found": int(last_scan.get("offers_found", 0) or 0),
                "new_offers": int(last_scan.get("new_offers", 0) or 0),
                "duplicates_ignored": int(last_scan.get("duplicates_ignored", 0) or 0),
                "exported_offers": int(last_scan.get("exported_offers", 0) or 0),
                "errors": last_scan.get("errors", []),
                "new_offer_keys": last_scan.get("new_offer_keys", []),
                "raw_payload": last_scan,
            }
        )
        for source_name, source_payload in (last_scan.get("sources_scanned") or {}).items():
            source_runs.append(
                {
                    "id": _stable_uuid("scan_run_source", run_id, source_name),
                    "scan_run_id": run_id,
                    "source": source_name,
                    "status": source_payload.get("status", "unknown"),
                    "offers_found": int(source_payload.get("offers_found", 0) or 0),
                    "new_offers": int(source_payload.get("new_offers", 0) or 0),
                    "duplicates": int(source_payload.get("duplicates", 0) or 0),
                    "error_message": source_payload.get("error_message") or None,
                    "source_timestamp": _to_iso_datetime(source_payload.get("timestamp")),
                    "raw_payload": source_payload,
                }
            )
    return scan_runs, source_runs


def build_search_profiles_payload(profile_data: dict) -> list[dict]:
    return [
        {
            "slug": "profil-principal",
            "name": "Profil principal",
            "is_active": True,
            "profile_data": profile_data,
        }
    ]


def build_app_settings_payload() -> list[dict]:
    settings = get_settings()
    return [
        {
            "key": "candidate_profile",
            "value": settings.get("profile", {}),
        },
        {
            "key": "agent_behavior",
            "value": settings.get("agent", {}),
        },
    ]


def build_dataset() -> dict[str, list[dict]]:
    csv_rows = _read_csv_rows()
    scores = _load_scores()
    letters = _load_letters()
    history_rows = _load_history()
    refused_signatures = set(_load_refused())
    scan_state = _load_scan_state()
    profile_data = _load_profile()
    scan_runs, scan_run_sources = build_scan_payloads(scan_state)
    offer_lookup = _build_offer_lookup(csv_rows, scan_state, letters, history_rows, refused_signatures, scores)

    return {
        "offers": list(offer_lookup.values()),
        "offer_scores": build_offer_scores_payload(scores),
        "offer_letters": build_offer_letters_payload(letters),
        "applications_history": build_applications_history_payload(history_rows),
        "refused_offers": build_refused_offers_payload(refused_signatures, offer_lookup),
        "scan_runs": scan_runs,
        "scan_run_sources": scan_run_sources,
        "search_profiles": build_search_profiles_payload(profile_data),
        "app_settings": build_app_settings_payload(),
    }


def _headers() -> dict[str, str]:
    service_role = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not service_role:
        raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY manquant dans l'environnement.")
    return {
        "apikey": service_role,
        "Authorization": f"Bearer {service_role}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }


def _table_upsert_url(base_url: str, table_name: str, on_conflict: str) -> str:
    return f"{base_url.rstrip('/')}/rest/v1/{table_name}?on_conflict={on_conflict}"


def push_dataset(dataset: dict[str, list[dict]]) -> None:
    base_url = (os.environ.get("SUPABASE_URL") or "").strip()
    if not base_url:
        raise RuntimeError("SUPABASE_URL manquant dans l'environnement.")
    headers = _headers()
    table_conflicts = {
        "offers": "signature",
        "offer_scores": "offer_signature",
        "offer_letters": "offer_signature",
        "applications_history": "id",
        "refused_offers": "offer_signature",
        "scan_runs": "id",
        "scan_run_sources": "id",
        "search_profiles": "slug",
        "app_settings": "key",
    }

    synced_counts: dict[str, int] = {}
    for table_name, rows in dataset.items():
        if not rows:
            print(f"[skip] {table_name}: 0 ligne")
            synced_counts[table_name] = 0
            continue
        response = requests.post(
            _table_upsert_url(base_url, table_name, table_conflicts[table_name]),
            headers=headers,
            data=json.dumps(rows, ensure_ascii=False),
            timeout=30,
        )
        response.raise_for_status()
        print(f"[ok] {table_name}: {len(rows)} ligne(s)")
        synced_counts[table_name] = len(rows)
    write_sync_state("completed", synced_counts)


def print_preview(dataset: dict[str, list[dict]]) -> None:
    print("Aperçu de synchronisation Supabase")
    print("---------------------------------")
    for table_name, rows in dataset.items():
        print(f"- {table_name}: {len(rows)} ligne(s)")
    offers = dataset.get("offers", [])
    if offers:
        sample = offers[0]
        print("")
        print("Exemple offre :")
        print(json.dumps(sample, ensure_ascii=False, indent=2)[:1200])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prépare ou exécute une synchronisation locale -> Supabase.")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Exécute réellement l'envoi vers Supabase. Sans cette option, le script reste en aperçu.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    dataset = build_dataset()
    if not args.execute:
        print_preview(dataset)
        print("")
        print("Aucun envoi effectué. Lance avec --execute uniquement quand Supabase sera prêt.")
        return 0

    table_counts = {table_name: len(rows) for table_name, rows in dataset.items()}
    write_sync_state("running", table_counts)
    try:
        push_dataset(dataset)
    except Exception as exc:
        write_sync_state("failed", table_counts, str(exc))
        raise
    print("")
    print("Synchronisation terminée.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

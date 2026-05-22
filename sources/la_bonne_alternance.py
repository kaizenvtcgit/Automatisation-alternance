"""
Source La Bonne Alternance - API v3 (api.apprentissage.beta.gouv.fr).

Token gratuit : https://api.apprentissage.beta.gouv.fr/fr/compte/profil
Variable d'environnement requise : LBA_API_KEY
"""

import os
import sys

import requests

from ._common import dynamic_search_scope, dynamic_search_terms, search_scope_coordinates, search_scope_label

API_KEY = os.environ.get("LBA_API_KEY", "")
CLOUD_MODE = os.environ.get("ALTERNANCE_CLOUD_MODE", "0").strip() == "1"
BASE_URL = "https://api.apprentissage.beta.gouv.fr/api"

ROME_CODES = "E1205,E1207,E1210,E1211"
REQUEST_TIMEOUT = 12 if CLOUD_MODE else 30

LBA_ROLE_MARKERS = (
    "motion",
    "ux",
    "ui",
    "designer",
    "design",
    "graphiste",
    "web designer",
    "product designer",
    "digital",
)


def _search_compatible_with_lba() -> bool:
    roles = dynamic_search_terms().get("postes_cibles", [])
    if not roles:
        return True
    blob = " | ".join(str(role or "").lower() for role in roles)
    return any(marker in blob for marker in LBA_ROLE_MARKERS)


def _location_config() -> dict[str, float | int] | None:
    scope = dynamic_search_scope()
    coords = search_scope_coordinates(scope)
    if coords:
        return coords

    zone_mode = str(scope.get("zone_mode") or "").strip().lower()
    if zone_mode in {"france", "remote"}:
        print(
            "[La Bonne Alternance] Source ignoree pour cette recherche : l'API demande une zone locale et ne couvre pas proprement France entiere / remote pur.",
            file=sys.stderr,
        )
        return None

    print(
        f"[La Bonne Alternance] Zone '{search_scope_label(scope)}' non geocodee automatiquement - source ignoree.",
        file=sys.stderr,
    )
    return None


def _extraire_lieu(job: dict) -> tuple[str, list[str]]:
    workplace = job.get("workplace") or {}
    location = workplace.get("location") or {}
    address = location.get("address") or ""
    parts = [p.strip() for p in address.split(",") if p.strip()]
    lieu = parts[-1] if parts else address
    return lieu or "France", [lieu] if lieu else []


def _extraire_entreprise(job: dict) -> str:
    workplace = job.get("workplace") or {}
    return (
        workplace.get("name")
        or workplace.get("brand")
        or workplace.get("legal_name")
        or ""
    )


def _extraire_url(job: dict) -> str:
    apply_data = job.get("apply") or {}
    url = apply_data.get("url") or ""
    if url:
        return url
    job_id = ((job.get("identifier") or {}).get("id") or "").strip()
    if job_id:
        return f"https://labonnealternance.apprentissage.beta.gouv.fr/recherche-emploi?type=matcha&itemId={job_id}"
    return "https://labonnealternance.apprentissage.beta.gouv.fr"


def _extraire_contrat(job: dict) -> str:
    contract = job.get("contract") or {}
    types = contract.get("type") or []
    if isinstance(types, list) and types:
        return ", ".join(types)
    if isinstance(types, str):
        return types
    return "Apprentissage"


def _extraire_remote(job: dict) -> bool:
    contract = job.get("contract") or {}
    return str(contract.get("remote") or "").lower() in {"remote", "hybrid"}


def _vers_offre(job: dict) -> dict:
    identifier = job.get("identifier") or {}
    offer = job.get("offer") or {}
    publication = offer.get("publication") or {}
    lieu, zones = _extraire_lieu(job)
    job_id = (identifier.get("id") or "").strip()

    return {
        "id": f"lba_{job_id}" if job_id else "",
        "source": "La Bonne Alternance",
        "titre": offer.get("title") or "(sans titre)",
        "entreprise": _extraire_entreprise(job),
        "lieu": lieu,
        "zones_geo": zones,
        "url": _extraire_url(job),
        "description": offer.get("description") or "",
        "date_pub": (publication.get("creation") or "")[:10],
        "categorie": ", ".join(offer.get("rome_codes") or []),
        "requete_source": ROME_CODES,
        "contrat": _extraire_contrat(job),
        "remote": _extraire_remote(job),
    }


def recuperer() -> list[dict]:
    """Recupere les offres La Bonne Alternance via l'API v3."""
    if not _search_compatible_with_lba():
        print(
            "[La Bonne Alternance] Recherche active hors perimetre design/motion - source ignoree pour eviter des resultats hors cible.",
            file=sys.stderr,
        )
        return []

    location = _location_config()
    if not location:
        return []

    if not API_KEY:
        print(
            "[La Bonne Alternance] LBA_API_KEY non configuree - source ignoree.\n"
            "  -> Inscription gratuite : https://api.apprentissage.beta.gouv.fr/fr/compte/profil\n"
            "  -> Ajoutez LBA_API_KEY=votre_cle dans le fichier .env",
            file=sys.stderr,
        )
        return []

    url = f"{BASE_URL}/job/v1/search"
    params = {
        "latitude": location["latitude"],
        "longitude": location["longitude"],
        "radius": location["radius_km"],
        "romes": ROME_CODES,
    }
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json",
    }

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.Timeout:
        print("[La Bonne Alternance] TIMEOUT - serveur trop lent.", file=sys.stderr)
        return []
    except requests.RequestException as e:
        print(f"[La Bonne Alternance] ERREUR RESEAU : {e}", file=sys.stderr)
        return []

    if resp.status_code == 401:
        print(
            "[La Bonne Alternance] HTTP 401 - LBA_API_KEY invalide ou expiree.\n"
            "  -> Verifiez votre token sur https://api.apprentissage.beta.gouv.fr/fr/compte/profil",
            file=sys.stderr,
        )
        return []
    if resp.status_code == 403:
        print("[La Bonne Alternance] HTTP 403 - acces refuse (quota ou droits insuffisants).", file=sys.stderr)
        return []
    if resp.status_code == 429:
        print("[La Bonne Alternance] HTTP 429 - quota API atteint.", file=sys.stderr)
        return []
    if not resp.ok:
        print(
            f"[La Bonne Alternance] HTTP {resp.status_code} - reponse inattendue : {resp.text[:200]}",
            file=sys.stderr,
        )
        return []

    try:
        data = resp.json()
    except Exception:
        print(
            f"[La Bonne Alternance] PARSE_ERROR - reponse non-JSON. Debut : {resp.text[:200]}",
            file=sys.stderr,
        )
        return []

    jobs_bruts = data.get("jobs") or []
    warnings = data.get("warnings") or []
    if warnings:
        for warning in warnings:
            print(f"[La Bonne Alternance] Warning API : {warning.get('message', warning)}", file=sys.stderr)

    if not isinstance(jobs_bruts, list):
        print(
            f"[La Bonne Alternance] PARSE_ERROR - 'jobs' n'est pas une liste. Structure recue : {list(data.keys())}",
            file=sys.stderr,
        )
        return []

    offres: list[dict] = []
    for job in jobs_bruts:
        try:
            offres.append(_vers_offre(job))
        except Exception as e:
            print(f"[La Bonne Alternance] Erreur normalisation offre : {e}", file=sys.stderr)
            continue

    print(f"[La Bonne Alternance] {len(offres)} offre(s) recuperee(s).")
    return offres

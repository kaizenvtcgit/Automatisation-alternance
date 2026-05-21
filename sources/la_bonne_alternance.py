"""
Source La Bonne Alternance — API v3 (api.apprentissage.beta.gouv.fr).

Token gratuit : https://api.apprentissage.beta.gouv.fr/fr/compte/profil
Variable d'environnement requise : LBA_API_KEY
"""

import os
import sys

import requests

# ─── Configuration ────────────────────────────────────────────────────────────

API_KEY  = os.environ.get("LBA_API_KEY", "")
CLOUD_MODE = (os.environ.get("ALTERNANCE_CLOUD_MODE", "0").strip() == "1")
# API correcte confirmée via swagger.json : api.apprentissage.beta.gouv.fr/api/swagger.json
BASE_URL = "https://api.apprentissage.beta.gouv.fr/api"

# Coordonnées Paris + rayon 60 km (couvre l'IDF)
LATITUDE  = 48.8566
LONGITUDE = 2.3522
RADIUS_KM = 60

# Codes ROME pertinents pour design / motion / UX-UI
# Vérifiés via GET /api/v1/metiers (endpoint public)
# E1205 : Graphisme  E1207 : Production AV / multimédia
# E1210 : Développement et intégration multimédia
# E1211 : Scénarisation multimédia
ROME_CODES = "E1205,E1207,E1210,E1211"
REQUEST_TIMEOUT = 12 if CLOUD_MODE else 30


# ─── Normalisation ────────────────────────────────────────────────────────────

def _extraire_lieu(job: dict) -> tuple[str, list[str]]:
    """Extrait ville + zones_geo depuis l'objet workplace.location."""
    workplace = job.get("workplace") or {}
    location  = workplace.get("location") or {}
    address   = location.get("address") or ""
    # Tenter d'extraire ville depuis l'adresse
    # L'adresse est souvent "75001 PARIS" ou "PARIS 75001"
    parts = [p.strip() for p in address.split(",") if p.strip()]
    lieu  = parts[-1] if parts else address
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
    apply = job.get("apply") or {}
    url   = apply.get("url") or ""
    if url:
        return url
    # Fallback : lien direct LBA
    job_id = ((job.get("identifier") or {}).get("id") or "").strip()
    if job_id:
        return f"https://labonnealternance.apprentissage.beta.gouv.fr/recherche-emploi?type=matcha&itemId={job_id}"
    return "https://labonnealternance.apprentissage.beta.gouv.fr"


def _extraire_contrat(job: dict) -> str:
    contract = job.get("contract") or {}
    types    = contract.get("type") or []
    if isinstance(types, list) and types:
        return ", ".join(types)
    if isinstance(types, str):
        return types
    return "Apprentissage"


def _extraire_remote(job: dict) -> bool:
    contract = job.get("contract") or {}
    return (contract.get("remote") or "").lower() in ("remote", "hybrid")


def _vers_offre(job: dict) -> dict:
    identifier = job.get("identifier") or {}
    offer      = job.get("offer") or {}
    pub        = offer.get("publication") or {}
    lieu, zones = _extraire_lieu(job)

    job_id = (identifier.get("id") or "").strip()

    return {
        "id":             f"lba_{job_id}" if job_id else "",
        "source":         "La Bonne Alternance",
        "titre":          offer.get("title") or "(sans titre)",
        "entreprise":     _extraire_entreprise(job),
        "lieu":           lieu,
        "zones_geo":      zones,
        "url":            _extraire_url(job),
        "description":    offer.get("description") or "",
        "date_pub":       (pub.get("creation") or "")[:10],  # YYYY-MM-DD
        "categorie":      ", ".join(offer.get("rome_codes") or []),
        "requete_source": ROME_CODES,
        "contrat":        _extraire_contrat(job),
        "remote":         _extraire_remote(job),
    }


# ─── Récupération ─────────────────────────────────────────────────────────────

def recuperer() -> list[dict]:
    """
    Récupère les offres La Bonne Alternance via l'API v3.
    Nécessite LBA_API_KEY dans .env (gratuit sur https://api.apprentissage.beta.gouv.fr).
    """
    if not API_KEY:
        print(
            "[La Bonne Alternance] ⚠ LBA_API_KEY non configurée — source ignorée.\n"
            "  -> Inscription gratuite : https://api.apprentissage.beta.gouv.fr/fr/compte/profil\n"
            "  -> Ajoutez LBA_API_KEY=votre_cle dans le fichier .env",
            file=sys.stderr,
        )
        return []

    url = f"{BASE_URL}/job/v1/search"
    params = {
        "latitude":  LATITUDE,
        "longitude": LONGITUDE,
        "radius":    RADIUS_KM,
        "romes":     ROME_CODES,
    }
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Accept":        "application/json",
    }

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.Timeout:
        print("[La Bonne Alternance] TIMEOUT — serveur trop lent.", file=sys.stderr)
        return []
    except requests.RequestException as e:
        print(f"[La Bonne Alternance] ERREUR RÉSEAU : {e}", file=sys.stderr)
        return []

    if resp.status_code == 401:
        print(
            "[La Bonne Alternance] HTTP 401 — LBA_API_KEY invalide ou expirée.\n"
            "  -> Verifiez votre token sur https://api.apprentissage.beta.gouv.fr/fr/compte/profil",
            file=sys.stderr,
        )
        return []
    if resp.status_code == 403:
        print(
            "[La Bonne Alternance] HTTP 403 — accès refusé (quota ou droits insuffisants).",
            file=sys.stderr,
        )
        return []
    if resp.status_code == 429:
        print("[La Bonne Alternance] HTTP 429 — quota API atteint.", file=sys.stderr)
        return []
    if not resp.ok:
        print(
            f"[La Bonne Alternance] HTTP {resp.status_code} — réponse inattendue : "
            f"{resp.text[:200]}",
            file=sys.stderr,
        )
        return []

    try:
        data = resp.json()
    except Exception:
        print(
            "[La Bonne Alternance] PARSE_ERROR — réponse non-JSON. "
            f"Début : {resp.text[:200]}",
            file=sys.stderr,
        )
        return []

    # La réponse v3 contient "jobs" (offres directes) et "recruiters" (entreprises LBA)
    jobs_bruts = data.get("jobs") or []
    warnings   = data.get("warnings") or []

    if warnings:
        for w in warnings:
            print(f"[La Bonne Alternance] Warning API : {w.get('message', w)}", file=sys.stderr)

    if not isinstance(jobs_bruts, list):
        print(
            f"[La Bonne Alternance] PARSE_ERROR — 'jobs' n'est pas une liste. "
            f"Structure reçue : {list(data.keys())}",
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

    print(f"[La Bonne Alternance] {len(offres)} offre(s) récupérée(s).")
    return offres

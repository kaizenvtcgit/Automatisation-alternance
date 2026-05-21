"""Source Adzuna — agrégateur d'offres d'emploi français."""

import os
import re
import sys

import requests

from ._common import nettoyer_html

# ─── Configuration ────────────────────────────────────────────────────────────

APP_ID  = os.environ.get("ADZUNA_APP_ID", "")
APP_KEY = os.environ.get("ADZUNA_APP_KEY", "")
CLOUD_MODE = (os.environ.get("ALTERNANCE_CLOUD_MODE", "0").strip() == "1")

API_URL            = "https://api.adzuna.com/v1/api/jobs/fr/search/1"
RESULTATS_PAR_PAGE = 50
WHERE              = "Paris"
DISTANCE_KM        = 90

REQUETES: list[str] = [
    "alternance motion designer",
    "alternance motion design",
    "alternance graphiste animation",
    "alternance after effects",
    "alternance designer vidéo",
    "alternance UX designer",
    "alternance UI designer",
    "alternance product designer",
    "alternance web designer",
    "alternance design numérique",
]

ACTIVE_REQUETES: list[str] = REQUETES[:2] if CLOUD_MODE else REQUETES
ACTIVE_RESULTS_PER_PAGE = 15 if CLOUD_MODE else RESULTATS_PAR_PAGE
REQUEST_TIMEOUT = 10 if CLOUD_MODE else 30


# ─── Normalisation ────────────────────────────────────────────────────────────

def _vers_offre(job: dict, requete: str) -> dict:
    company  = job.get("company") or {}
    location = job.get("location") or {}
    category = job.get("category") or {}
    return {
        "id":             str(job.get("id") or ""),
        "source":         "Adzuna",
        "titre":          job.get("title") or "(sans titre)",
        "entreprise":     company.get("display_name") or "",
        "lieu":           location.get("display_name") or "",
        "zones_geo":      list(location.get("area") or []),
        "url":            job.get("redirect_url") or "",
        "description":    nettoyer_html(job.get("description") or ""),
        "date_pub":       job.get("created") or "",
        "categorie":      category.get("label") or "",
        "requete_source": requete,
        "contrat":        "",
        "remote":         False,
    }


# ─── Récupération ─────────────────────────────────────────────────────────────

def recuperer() -> list[dict]:
    """Récupère les offres Adzuna. Retourne une liste vide en cas d'erreur."""
    if not APP_ID or not APP_KEY:
        print(
            "[Adzuna] ⚠ ADZUNA_APP_ID / ADZUNA_APP_KEY non configurés dans .env — source ignorée.",
            file=sys.stderr,
        )
        return []

    vues: dict[str, dict] = {}

    for requete in ACTIVE_REQUETES:
        params = {
            "app_id":           APP_ID,
            "app_key":          APP_KEY,
            "what":             requete,
            "results_per_page": ACTIVE_RESULTS_PER_PAGE,
            "where":            WHERE,
            "distance":         DISTANCE_KM,
        }
        try:
            resp = requests.get(API_URL, params=params, timeout=REQUEST_TIMEOUT)
        except requests.Timeout:
            print(f"[Adzuna] TIMEOUT sur '{requete}'", file=sys.stderr)
            continue
        except requests.RequestException as e:
            print(f"[Adzuna] ERREUR RÉSEAU sur '{requete}': {e}", file=sys.stderr)
            continue

        if resp.status_code == 401:
            print(
                "[Adzuna] HTTP 401 — clés ADZUNA_APP_ID / ADZUNA_APP_KEY invalides.",
                file=sys.stderr,
            )
            return []
        if resp.status_code == 429:
            print("[Adzuna] HTTP 429 — quota journalier atteint.", file=sys.stderr)
            return list(vues.values())
        if not resp.ok:
            print(f"[Adzuna] HTTP {resp.status_code} sur '{requete}'", file=sys.stderr)
            continue

        try:
            jobs = resp.json().get("results") or []
        except Exception:
            print(f"[Adzuna] Réponse non-JSON sur '{requete}'", file=sys.stderr)
            continue

        for job in jobs:
            offre = _vers_offre(job, requete)
            cle = offre["id"] or offre["url"] or offre["titre"]
            if cle not in vues:
                vues[cle] = offre

    print(f"[Adzuna] {len(vues)} offre(s) récupérée(s).")
    return list(vues.values())

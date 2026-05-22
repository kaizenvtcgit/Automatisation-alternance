"""Source Remotive — offres remote internationales (bonus)."""

import os
import sys

import requests

from ._common import build_search_queries, nettoyer_html

# ─── Configuration ────────────────────────────────────────────────────────────

API_URL      = "https://remotive.com/api/remote-jobs"
RESULT_LIMIT = 50
CLOUD_MODE   = (os.environ.get("ALTERNANCE_CLOUD_MODE", "0").strip() == "1")

REQUETES: list[str] = [
    "apprenticeship",
    "internship",
]

ACTIVE_RESULT_LIMIT = 15 if CLOUD_MODE else RESULT_LIMIT
REQUEST_TIMEOUT = 10 if CLOUD_MODE else 30


def _queries() -> list[str]:
    return build_search_queries(REQUETES, locale="en")


# ─── Normalisation ────────────────────────────────────────────────────────────

def _vers_offre(job: dict, requete: str) -> dict:
    lieu = (job.get("candidate_required_location") or "").strip()
    return {
        "id":             f"remotive_{job.get('id') or ''}",
        "source":         "Remotive",
        "titre":          job.get("title") or "(sans titre)",
        "entreprise":     job.get("company_name") or "",
        "lieu":           lieu or "Remote",
        "zones_geo":      [lieu] if lieu else ["Remote"],
        "url":            job.get("url") or "",
        "description":    nettoyer_html(job.get("description") or ""),
        "date_pub":       job.get("publication_date") or "",
        "categorie":      job.get("category") or "",
        "requete_source": requete,
        "contrat":        "",
        "remote":         True,
    }


# ─── Récupération ─────────────────────────────────────────────────────────────

def recuperer() -> list[dict]:
    """Récupère les offres Remotive (remote). Retourne une liste vide en cas d'erreur."""
    if CLOUD_MODE:
        print("[Remotive] Mode cloud leger actif — source bonus ignoree.")
        return []
    queries = _queries()
    if not queries:
        print("[Remotive] Aucun poste cible defini - source ignoree.", file=sys.stderr)
        return []
    vues: dict[str, dict] = {}

    for requete in queries:
        params = {"search": requete, "limit": ACTIVE_RESULT_LIMIT}
        try:
            resp = requests.get(API_URL, params=params, timeout=REQUEST_TIMEOUT)
        except requests.Timeout:
            print(f"[Remotive] TIMEOUT sur '{requete}'", file=sys.stderr)
            continue
        except requests.RequestException as e:
            print(f"[Remotive] ERREUR RÉSEAU sur '{requete}': {e}", file=sys.stderr)
            continue

        if resp.status_code == 429:
            print("[Remotive] HTTP 429 — quota atteint.", file=sys.stderr)
            break
        if not resp.ok:
            print(f"[Remotive] HTTP {resp.status_code} sur '{requete}'", file=sys.stderr)
            continue

        try:
            jobs = resp.json().get("jobs") or []
        except Exception:
            print(f"[Remotive] Réponse non-JSON sur '{requete}'", file=sys.stderr)
            continue

        for job in jobs:
            offre = _vers_offre(job, requete)
            cle = offre["id"] or offre["url"]
            if cle not in vues:
                vues[cle] = offre

    print(f"[Remotive] {len(vues)} offre(s) récupérée(s).")
    return list(vues.values())

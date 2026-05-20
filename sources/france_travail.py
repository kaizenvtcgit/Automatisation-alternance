"""Source France Travail — API officielle Pôle Emploi / France Travail."""

import os
import sys

import requests

# ─── Configuration ────────────────────────────────────────────────────────────

CLIENT_ID     = os.environ.get("FT_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("FT_CLIENT_SECRET", "")

TOKEN_URL  = "https://entreprise.francetravail.fr/connexion/oauth2/access_token"
SEARCH_URL = "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"
SCOPE      = "api_offresdemploiv2 o2dsoffre"

# Départements Île-de-France
DEPARTEMENTS_IDF: list[str] = ["75", "77", "78", "91", "92", "93", "94", "95"]

REQUETES: list[str] = [
    "motion designer",
    "motion design",
    "graphiste animation",
    "after effects",
    "designer video",
    "ux designer",
    "ui designer",
    "product designer",
    "web designer",
    "design numérique",
    "alternance design",
]


# ─── Auth ─────────────────────────────────────────────────────────────────────

def _get_token() -> tuple[str | None, str]:
    if not CLIENT_ID or not CLIENT_SECRET:
        return None, "non_configure"

    url = f"{TOKEN_URL}?realm=/partenaire"
    tentatives = [
        ("form", {
            "data": {
                "grant_type":    "client_credentials",
                "client_id":     CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "scope":         SCOPE,
            },
            "headers": {"Content-Type": "application/x-www-form-urlencoded"},
        }),
        ("basic", {
            "data": {"grant_type": "client_credentials", "scope": SCOPE},
            "auth": (CLIENT_ID, CLIENT_SECRET),
        }),
    ]

    dernier_code = None
    for mode, kwargs in tentatives:
        try:
            resp = requests.post(url, timeout=15, **kwargs)
        except Exception as e:
            print(f"[France Travail] Impossible d'obtenir un token ({mode}): {e}", file=sys.stderr)
            continue
        if resp.ok:
            return resp.json().get("access_token"), "ok"
        dernier_code = resp.status_code

    if dernier_code in (400, 401):
        return None, "auth_invalide"
    return None, "erreur_token"


# ─── Normalisation ────────────────────────────────────────────────────────────

def _vers_offre(item: dict, requete: str) -> dict:
    lieu_info      = item.get("lieuTravail") or {}
    entreprise_info = item.get("entreprise") or {}
    offre_id       = item.get("id") or ""
    lieu_libelle   = lieu_info.get("libelle") or ""

    # Détecter si l'offre est en alternance via typeContrat
    type_contrat = (item.get("typeContrat") or "").upper()
    contrat = "alternance" if type_contrat in ("E1", "E2") else item.get("typeContratLibelle") or ""

    return {
        "id":             f"ft_{offre_id}",
        "source":         "France Travail",
        "titre":          item.get("intitule") or "(sans titre)",
        "entreprise":     entreprise_info.get("nom") or "",
        "lieu":           lieu_libelle,
        "zones_geo":      [lieu_libelle] if lieu_libelle else [],
        "url":            f"https://candidat.francetravail.fr/offres/recherche/detail/{offre_id}",
        "description":    item.get("description") or "",
        "date_pub":       item.get("dateCreation") or "",
        "categorie":      item.get("romeLibelle") or "",
        "requete_source": requete,
        "contrat":        contrat,
        "remote":         False,
    }


# ─── Récupération ─────────────────────────────────────────────────────────────

def recuperer() -> list[dict]:
    """Récupère les offres France Travail. Retourne une liste vide en cas d'erreur."""
    token, statut = _get_token()

    if statut == "non_configure":
        print(
            "[France Travail] ⚠ FT_CLIENT_ID / FT_CLIENT_SECRET non configurés — source ignorée.\n"
            "  -> Inscription gratuite : https://francetravail.io",
            file=sys.stderr,
        )
        return []
    if statut == "auth_invalide":
        print(
            "[France Travail] HTTP 401 — identifiants invalides (FT_CLIENT_ID / FT_CLIENT_SECRET).",
            file=sys.stderr,
        )
        return []
    if not token:
        print(f"[France Travail] Impossible d'obtenir un token ({statut}).", file=sys.stderr)
        return []

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    vues: dict[str, dict] = {}

    for requete in REQUETES:
        for dept in DEPARTEMENTS_IDF:
            params = {
                "motsCles":   requete,
                "departement": dept,
                "range":      "0-49",
            }
            try:
                resp = requests.get(SEARCH_URL, headers=headers, params=params, timeout=30)
            except requests.Timeout:
                print(f"[France Travail] TIMEOUT '{requete}' dept {dept}", file=sys.stderr)
                continue
            except requests.RequestException as e:
                print(f"[France Travail] ERREUR RÉSEAU '{requete}' dept {dept}: {e}", file=sys.stderr)
                continue

            if resp.status_code == 401:
                print("[France Travail] HTTP 401 en cours de requête — token expiré ?", file=sys.stderr)
                return list(vues.values())
            if resp.status_code == 429:
                print("[France Travail] HTTP 429 — quota atteint.", file=sys.stderr)
                return list(vues.values())
            if resp.status_code not in (200, 206):
                print(
                    f"[France Travail] HTTP {resp.status_code} '{requete}' dept {dept}",
                    file=sys.stderr,
                )
                continue

            try:
                resultats = resp.json().get("resultats") or []
            except Exception:
                print(
                    f"[France Travail] Réponse non-JSON '{requete}' dept {dept}",
                    file=sys.stderr,
                )
                continue

            for item in resultats:
                offre = _vers_offre(item, requete)
                cle = offre["id"]
                if cle not in vues:
                    vues[cle] = offre

    print(f"[France Travail] {len(vues)} offre(s) récupérée(s).")
    return list(vues.values())

"""Source France Travail - API officielle France Travail."""

import os
import sys

import requests

from ._common import dynamic_search_scope, dynamic_search_terms

CLIENT_ID = os.environ.get("FT_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("FT_CLIENT_SECRET", "")
CLOUD_MODE = os.environ.get("ALTERNANCE_CLOUD_MODE", "0").strip() == "1"

TOKEN_URL = "https://entreprise.francetravail.fr/connexion/oauth2/access_token"
SEARCH_URL = "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"
SCOPE = "api_offresdemploiv2 o2dsoffre"

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
    "design numerique",
    "alternance design",
]

ACTIVE_DEPARTEMENTS_IDF: list[str] = DEPARTEMENTS_IDF[:2] if CLOUD_MODE else DEPARTEMENTS_IDF
SEARCH_RANGE = "0-14" if CLOUD_MODE else "0-49"
SEARCH_TIMEOUT = 10 if CLOUD_MODE else 30


def _queries() -> list[str]:
    dynamic_roles = dynamic_search_terms().get("postes_cibles", [])
    if dynamic_roles:
        queries: list[str] = []
        for role in dynamic_roles:
            role_text = str(role).strip()
            if not role_text:
                continue
            lowered = role_text.lower()
            if any(marker in lowered for marker in ("alternance", "apprentissage", "apprenticeship")):
                queries.append(role_text)
            else:
                queries.append(f"alternance {role_text}")
        return queries
    return REQUETES


def _search_departements() -> list[str | None]:
    scope = dynamic_search_scope()
    zone_mode = str(scope.get("zone_mode") or "").strip().lower()
    if zone_mode in ("", "idf"):
        return ACTIVE_DEPARTEMENTS_IDF
    return [None]


def _get_token() -> tuple[str | None, str]:
    if not CLIENT_ID or not CLIENT_SECRET:
        return None, "non_configure"

    url = f"{TOKEN_URL}?realm=/partenaire"
    tentatives = [
        (
            "form",
            {
                "data": {
                    "grant_type": "client_credentials",
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "scope": SCOPE,
                },
                "headers": {"Content-Type": "application/x-www-form-urlencoded"},
            },
        ),
        (
            "basic",
            {
                "data": {"grant_type": "client_credentials", "scope": SCOPE},
                "auth": (CLIENT_ID, CLIENT_SECRET),
            },
        ),
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


def _vers_offre(item: dict, requete: str) -> dict:
    lieu_info = item.get("lieuTravail") or {}
    entreprise_info = item.get("entreprise") or {}
    offre_id = item.get("id") or ""
    lieu_libelle = lieu_info.get("libelle") or ""

    type_contrat = (item.get("typeContrat") or "").upper()
    contrat = "alternance" if type_contrat in ("E1", "E2") else item.get("typeContratLibelle") or ""

    return {
        "id": f"ft_{offre_id}",
        "source": "France Travail",
        "titre": item.get("intitule") or "(sans titre)",
        "entreprise": entreprise_info.get("nom") or "",
        "lieu": lieu_libelle,
        "zones_geo": [lieu_libelle] if lieu_libelle else [],
        "url": f"https://candidat.francetravail.fr/offres/recherche/detail/{offre_id}",
        "description": item.get("description") or "",
        "date_pub": item.get("dateCreation") or "",
        "categorie": item.get("romeLibelle") or "",
        "requete_source": requete,
        "contrat": contrat,
        "remote": False,
    }


def recuperer() -> list[dict]:
    """Recupere les offres France Travail."""
    token, statut = _get_token()

    if statut == "non_configure":
        print(
            "[France Travail] FT_CLIENT_ID / FT_CLIENT_SECRET non configures - source ignoree.\n"
            "  -> Inscription gratuite : https://francetravail.io",
            file=sys.stderr,
        )
        return []
    if statut == "auth_invalide":
        print(
            "[France Travail] HTTP 401 - identifiants invalides (FT_CLIENT_ID / FT_CLIENT_SECRET).",
            file=sys.stderr,
        )
        return []
    if not token:
        print(f"[France Travail] Impossible d'obtenir un token ({statut}).", file=sys.stderr)
        return []

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    vues: dict[str, dict] = {}

    queries = _queries()
    if CLOUD_MODE:
        queries = queries[:3]

    departements = _search_departements()
    for requete in queries:
        for dept in departements:
            params = {
                "motsCles": requete,
                "range": SEARCH_RANGE,
            }
            zone_label = f"dept {dept}" if dept else "zone active"
            if dept:
                params["departement"] = dept
            try:
                resp = requests.get(SEARCH_URL, headers=headers, params=params, timeout=SEARCH_TIMEOUT)
            except requests.Timeout:
                print(f"[France Travail] TIMEOUT '{requete}' {zone_label}", file=sys.stderr)
                continue
            except requests.RequestException as e:
                print(f"[France Travail] ERREUR RESEAU '{requete}' {zone_label}: {e}", file=sys.stderr)
                continue

            if resp.status_code == 401:
                print("[France Travail] HTTP 401 en cours de requete - token expire ?", file=sys.stderr)
                return list(vues.values())
            if resp.status_code == 429:
                print("[France Travail] HTTP 429 - quota atteint.", file=sys.stderr)
                return list(vues.values())
            if resp.status_code not in (200, 206):
                print(
                    f"[France Travail] HTTP {resp.status_code} '{requete}' {zone_label}",
                    file=sys.stderr,
                )
                continue

            try:
                resultats = resp.json().get("resultats") or []
            except Exception:
                print(
                    f"[France Travail] Reponse non-JSON '{requete}' {zone_label}",
                    file=sys.stderr,
                )
                continue

            for item in resultats:
                offre = _vers_offre(item, requete)
                cle = offre["id"]
                if cle not in vues:
                    vues[cle] = offre

    print(f"[France Travail] {len(vues)} offre(s) recuperee(s).")
    return list(vues.values())

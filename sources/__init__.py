"""
Orchestrateur des sources d'offres.
Appelle chaque source, fusionne et dédoublonne les résultats.
"""

import os
import sys
from datetime import datetime
from typing import Callable

from ._common import cle_deduplication, filtrer_zone_idf
from . import adzuna, france_travail, la_bonne_alternance, remotive

CLOUD_MODE = (os.environ.get("ALTERNANCE_CLOUD_MODE", "0").strip() == "1")

# Sources dans l'ordre d'appel (les premières ont priorité en cas de conflit de clé)
_SOURCES: list[tuple[str, Callable[[], list[dict]]]] = [
    ("France Travail",         france_travail.recuperer),
    ("La Bonne Alternance",    la_bonne_alternance.recuperer),
    ("Adzuna",                 adzuna.recuperer),
]

if not CLOUD_MODE:
    _SOURCES.append(("Remotive (bonus)", remotive.recuperer))


def _dedoublonner(offres: list[dict]) -> tuple[list[dict], int]:
    """
    Dédoublonne les offres toutes sources confondues.
    Stratégie : ID propre > URL > titre normalisé + entreprise normalisée.
    La première occurrence gagne (ordre = priorité source).
    """
    vues: dict[str, dict] = {}
    for offre in offres:
        cle = cle_deduplication(offre)
        if cle and cle not in vues:
            vues[cle] = offre
    doublons = len(offres) - len(vues)
    return list(vues.values()), doublons


def recuperer_toutes_offres_detail() -> dict:
    """
    Récupère les offres de toutes les sources configurées avec métadonnées de scan.
    Chaque source échoue indépendamment sans bloquer les autres.
    """
    toutes: list[dict] = []
    nb_par_source: dict[str, int] = {}
    erreurs_par_source: dict[str, str] = {}
    sources_meta: dict[str, dict] = {}

    for nom, fn in _SOURCES:
        try:
            offres = fn()
            uniques_source, doublons_source = _dedoublonner(offres)
            nb_par_source[nom] = len(offres)
            toutes.extend(offres)
            erreurs_par_source[nom] = ""
            sources_meta[nom] = {
                "count": len(offres),
                "unique_count": len(uniques_source),
                "duplicates": doublons_source,
                "error": "",
                "status": "ok",
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        except Exception as e:
            erreur = str(e)
            print(f"[{nom}] ERREUR non gérée : {erreur}", file=sys.stderr)
            nb_par_source[nom] = 0
            erreurs_par_source[nom] = erreur
            sources_meta[nom] = {
                "count": 0,
                "unique_count": 0,
                "duplicates": 0,
                "error": erreur,
                "status": "erreur",
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

    avant_dedup = len(toutes)
    toutes, nb_doublons = _dedoublonner(toutes)

    print(
        f"\nTotal brut : {avant_dedup} offre(s) | "
        f"Doublons supprimés : {nb_doublons} | "
        f"Après déduplication : {len(toutes)}"
    )

    return {
        "offres": toutes,
        "sources": {
            nom: {
                "count": nb_par_source.get(nom, 0),
                "error": erreurs_par_source.get(nom, ""),
                "ok": not bool(erreurs_par_source.get(nom, "")),
                "status": sources_meta.get(nom, {}).get("status", "ok"),
                "duplicates": sources_meta.get(nom, {}).get("duplicates", 0),
                "timestamp": sources_meta.get(nom, {}).get("timestamp"),
            }
            for nom, _ in _SOURCES
        },
        "total_raw": avant_dedup,
        "total_deduped": len(toutes),
        "duplicates_removed": nb_doublons,
    }


def recuperer_toutes_offres() -> list[dict]:
    """
    Compatibilité ascendante : retourne uniquement les offres fusionnées.
    """
    return recuperer_toutes_offres_detail()["offres"]

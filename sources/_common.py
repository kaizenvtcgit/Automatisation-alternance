"""Utilitaires partagés : format commun, filtres métier, filtres géographiques."""

import json
import os
import re
import unicodedata
from pathlib import Path


def _read_profile_json() -> dict:
    """
    Lit profil_recherche.json en direct sans cache global.
    Pas de cache pour éviter les race conditions en multi-thread gunicorn.
    Retourne {} si absent ou invalide — les defaults internes prennent le relais.
    """
    try:
        profile_path = Path(__file__).resolve().parent.parent / "export" / "profil_recherche.json"
        if profile_path.exists():
            data = json.loads(profile_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _normalize_tag_list(value) -> list[str]:
    """Normalise une valeur tag (list ou str CSV) en list[str] propre."""
    if isinstance(value, list):
        items = value
    elif isinstance(value, str):
        items = [v.strip() for v in value.split(",")]
    else:
        return []
    return [str(item).strip() for item in items if str(item or "").strip()]

# ─── Paramètres globaux ───────────────────────────────────────────────────────

INCLURE_OFFRES_REMOTE: bool = os.environ.get(
    "INCLURE_OFFRES_REMOTE", "1"
).strip().lower() not in {"0", "false", "non", "no"}


def _env_list(name: str) -> list[str]:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return []
    items = [item.strip() for item in raw.split("|||")]
    deduped: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def dynamic_search_terms() -> dict:
    """
    Retourne les termes de recherche actifs.
    Priorité : profil_recherche.json > variables d'environnement > liste vide.
    Lecture directe du JSON à chaque appel (pas de cache, safe en multi-thread).
    """
    profile = _read_profile_json()

    def _pick(profile_key: str, env_key: str, lowercase: bool = False) -> list[str]:
        # Priorité profil JSON
        val = _normalize_tag_list(profile.get(profile_key, []))
        if not val:
            val = _env_list(env_key)
        return [item.lower() for item in val] if lowercase else val

    return {
        "postes_cibles": _pick("postes_cibles", "ALTERNANCE_TARGET_ROLES"),
        "mots_cles_positifs": _pick("mots_cles_positifs", "ALTERNANCE_POSITIVE_KEYWORDS"),
        "mots_cles_negatifs": _pick("mots_cles_negatifs", "ALTERNANCE_NEGATIVE_KEYWORDS"),
        "types_contrat": _pick("types_contrat", "ALTERNANCE_CONTRACT_TYPES", lowercase=True),
    }


def dynamic_search_scope() -> dict:
    """
    Retourne le périmètre géographique actif.
    Priorité : profil_recherche.json > variables d'environnement > valeurs par défaut.
    """
    profile = _read_profile_json()

    # zone_mode
    zone_mode = (
        str(profile.get("zone_mode") or "").strip().lower()
        or str(os.environ.get("ALTERNANCE_ZONE_MODE", "") or "").strip().lower()
    )
    # zone_geo
    zone_geo = (
        str(profile.get("zone_geo") or "").strip()
        or str(os.environ.get("ALTERNANCE_ZONE_GEO", "") or "").strip()
    )
    # radius_km
    radius_raw = str(
        profile.get("radius_km")
        or os.environ.get("ALTERNANCE_RADIUS_KM", "30")
        or "30"
    ).strip()
    try:
        radius_km = max(10, min(100, int(radius_raw)))
    except ValueError:
        radius_km = 30
    # include_remote : profil JSON > env var globale
    profile_remote = profile.get("include_remote")
    if profile_remote is not None:
        include_remote = bool(profile_remote)
    else:
        include_remote = INCLURE_OFFRES_REMOTE

    return {
        "zone_mode": zone_mode,
        "zone_geo": zone_geo,
        "radius_km": radius_km,
        "include_remote": include_remote,
    }


ZONE_PRESETS: dict[str, dict[str, object]] = {
    "idf": {
        "terms": [
            "ile de france", "paris", "seine et marne", "yvelines", "essonne",
            "hauts de seine", "seine saint denis", "val de marne", "val d oise",
            "villejuif", "issy les moulineaux", "boulogne billancourt", "montreuil",
        ],
        "latitude": 48.8566,
        "longitude": 2.3522,
        "radius_km": 60,
    },
    "lyon": {
        "terms": ["lyon", "villeurbanne", "rhone", "venissieux", "bron", "caluire", "vaulx en velin"],
        "latitude": 45.7640,
        "longitude": 4.8357,
        "radius_km": 50,
    },
    "bordeaux": {
        "terms": ["bordeaux", "gironde", "merignac", "pessac", "talence", "begles", "cenon"],
        "latitude": 44.8378,
        "longitude": -0.5792,
        "radius_km": 50,
    },
}

REMOTE_MARKERS: tuple[str, ...] = (
    "remote", "hybrid remote", "teletravail", "travail a distance",
    "work from home", "worldwide", "anywhere", "hybride",
)

CONTRACT_TYPE_MARKERS: dict[str, tuple[str, ...]] = {
    "alternance": (
        "alternance", "apprentissage", "apprenticeship", "apprentice",
        "work study", "work-study", "contrat de professionnalisation",
        "professionnalisation", "contrat pro",
    ),
    "stage": ("stage", "stagiaire", "internship", "intern"),
    "cdd": ("cdd", "contrat a duree determinee", "fixed term"),
    "cdi": ("cdi", "contrat a duree indeterminee", "permanent"),
}


def _dedupe_ci(items: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if not text:
            continue
        key = _texte_sans_accents(text)
        if key in seen:
            continue
        seen.add(key)
        output.append(text)
    return output


def active_search_terms(search: dict | None = None) -> dict:
    source = search if isinstance(search, dict) else dynamic_search_terms()
    return {
        "postes_cibles": _dedupe_ci(list(source.get("postes_cibles") or [])),
        "mots_cles_positifs": _dedupe_ci(list(source.get("mots_cles_positifs") or [])),
        "mots_cles_negatifs": _dedupe_ci(list(source.get("mots_cles_negatifs") or [])),
        "types_contrat": [str(item).strip().lower() for item in _dedupe_ci(list(source.get("types_contrat") or []))],
    }


def build_search_queries(
    fallback: list[str],
    *,
    locale: str = "fr",
) -> list[str]:
    dynamic = active_search_terms()
    roles = dynamic.get("postes_cibles", [])
    positives = dynamic.get("mots_cles_positifs", [])
    contract_types = dynamic.get("types_contrat", [])

    prefix_map = {
        "fr": {
            "alternance": ["alternance", "apprentissage"],
            "stage": ["stage"],
            "cdd": ["cdd"],
            "cdi": ["cdi"],
        },
        "en": {
            "alternance": ["apprenticeship", "work study"],
            "stage": ["internship"],
            "cdd": ["fixed term"],
            "cdi": ["permanent"],
        },
    }
    prefixes: list[str] = []
    locale_map = prefix_map.get(locale, prefix_map["fr"])
    for contract_type in contract_types or ["alternance"]:
        prefixes.extend(locale_map.get(contract_type, [contract_type]))
    prefixes = _dedupe_ci(prefixes)

    seeds = roles or positives
    if not seeds:
        return []

    queries: list[str] = []
    extras = positives[:2]
    for seed in seeds[:6]:
        seed_text = str(seed).strip()
        if not seed_text:
            continue
        seed_norm = _texte_sans_accents(seed_text)
        query_bases: list[str] = []
        for prefix in prefixes or [""]:
            prefix_norm = _texte_sans_accents(prefix)
            if prefix_norm and prefix_norm not in seed_norm:
                query_bases.append(f"{prefix} {seed_text}".strip())
            else:
                query_bases.append(seed_text)
        for base in query_bases:
            queries.append(base)
            for extra in extras:
                extra_text = str(extra).strip()
                extra_norm = _texte_sans_accents(extra_text)
                if not extra_text or extra_norm in _texte_sans_accents(base):
                    continue
                queries.append(f"{base} {extra_text}".strip())
    return _dedupe_ci(queries) or fallback


def offer_matches_contract_types(text: str, contract_types: list[str] | None) -> bool:
    wanted = [str(item).strip().lower() for item in (contract_types or []) if str(item).strip()]
    if not wanted:
        return True
    blob = _texte_sans_accents(text)
    for contract_type in wanted:
        markers = CONTRACT_TYPE_MARKERS.get(contract_type, (contract_type,))
        if any(_texte_sans_accents(marker) in blob for marker in markers):
            return True
    return False


def scope_zone_terms(scope: dict | None = None) -> list[str]:
    current_scope = scope if isinstance(scope, dict) else dynamic_search_scope()
    zone_mode = str(current_scope.get("zone_mode") or "").strip().lower()
    zone_geo = str(current_scope.get("zone_geo") or "").strip()
    terms: list[str] = []
    preset = ZONE_PRESETS.get(zone_mode)
    if preset:
        terms.extend(list(preset.get("terms") or []))
    if zone_geo:
        terms.append(zone_geo)
    return [_texte_sans_accents(term) for term in _dedupe_ci(terms)]


def search_scope_coordinates(scope: dict | None = None) -> dict[str, float | int] | None:
    current_scope = scope if isinstance(scope, dict) else dynamic_search_scope()
    zone_mode = str(current_scope.get("zone_mode") or "").strip().lower()
    if zone_mode in {"", "france", "remote"}:
        return None

    preset = ZONE_PRESETS.get(zone_mode)
    if not preset:
        zone_geo = _texte_sans_accents(str(current_scope.get("zone_geo") or ""))
        for candidate in ZONE_PRESETS.values():
            if any(term in zone_geo for term in candidate.get("terms", [])):
                preset = candidate
                break
    if not preset:
        return None

    radius = max(10, min(100, int(current_scope.get("radius_km") or preset.get("radius_km") or 30)))
    return {
        "latitude": float(preset["latitude"]),
        "longitude": float(preset["longitude"]),
        "radius_km": radius,
    }


def search_scope_label(scope: dict | None = None) -> str:
    current_scope = scope if isinstance(scope, dict) else dynamic_search_scope()
    zone_mode = str(current_scope.get("zone_mode") or "").strip().lower()
    zone_geo = str(current_scope.get("zone_geo") or "").strip()
    if zone_mode == "france":
        return "France entiere"
    if zone_mode == "remote":
        return "Remote"
    if zone_geo:
        return zone_geo
    if zone_mode in ZONE_PRESETS:
        return zone_mode.upper()
    return "France entiere"

# ─── Mots-clés positifs ───────────────────────────────────────────────────────

MOTION_KEYWORDS: list[str] = [
    "motion design",
    "motion designer",
    "motion graphics",
    "after effects",
    "animation",
    "animation 2d",
    "animation 3d",
    "2d animation",
    "3d animation",
    "designer vidéo",
    "graphiste animation",
    "cinégraphiste",
    "vidéo design",
    "video design",
]

DESIGN_KEYWORDS: list[str] = [
    "ux",
    "ui",
    "ux/ui",
    "user experience",
    "user interface",
    "product designer",
    "product design",
    "ui designer",
    "ux designer",
    "interface",
    "figma",
    "webdesign",
    "web design",
    "designer",
    "design graphique",
    "graphiste",
    "webflow",
    "design system",
    "parcours utilisateur",
    "prototypage",
    "wireframes",
    "wireframe",
    "maquette",
    "digital design",
    "design numérique",
    "front-end créatif",
    "frontend créatif",
    "intégrateur créatif",
    "creative developer",
    "infographiste",
]

DESIGN_CORE_TITLE_KEYWORDS: list[str] = [
    "ux designer",
    "ui designer",
    "ui/ux designer",
    "ux/ui designer",
    "product designer",
    "web designer",
    "motion designer",
    "motion design",
    "graphic designer",
    "designer graphique",
    "designer digital",
    "design system",
    "graphiste",
    "infographiste",
]

DESIGN_CORE_DESCRIPTION_KEYWORDS: list[str] = [
    "figma",
    "wireframe",
    "wireframes",
    "prototype",
    "prototypage",
    "design system",
    "parcours utilisateur",
    "user flow",
    "experience utilisateur",
    "user experience",
    "user interface",
    "maquette",
    "maquettes",
    "ui kit",
]

CONTRAT_MARKERS: list[str] = [
    "alternance",
    "apprentissage",
    "apprenticeship",
    "apprentice",
    "work study",
    "work-study",
    "contrat de professionnalisation",
    "professionnalisation",
    "contrat pro",
]

# Mots courts nécessitant une correspondance mot entier (\bMOT\b)
_MOTS_LIMITE_MOT: frozenset[str] = frozenset({"ux", "ui", "motion", "animation", "maquette"})

# ─── Exclusions ───────────────────────────────────────────────────────────────
# Titres clairement hors-sujet (correspondance mot entier dans le titre)
_TITRES_EXCLUS: list[str] = [
    "vendeur",
    "vendeuse",
    "conseiller de vente",
    "conseillère de vente",
    "commercial terrain",
    "technico-commercial",
    "assistant administratif",
    "assistante administrative",
    "chargé de recrutement",
    "chargée de recrutement",
    "comptable",
    "juriste",
    "responsable rh",
    "développeur backend",
    "développeur back-end",
    "data scientist",
    "data analyst",
    "actuaire",
    "charge de communication",
    "chargee de communication",
    "communication 360",
    "assistant communication",
    "assistante communication",
    "community manager",
    "content manager",
    "social media manager",
    "charge marketing",
    "chargee marketing",
    "assistant marketing",
    "assistante marketing",
]

# Indicateurs de niveau senior dans le titre
_TITRES_SENIORS: list[str] = [
    "senior",
    "confirmé",
    "confirmée",
    "head of design",
    "directeur créatif",
    "directrice créative",
]

# Indicateurs print-only dans la description (non digital)
_DESC_PRINT_PUR: list[str] = [
    "impression offset",
    "sérigraphie",
    "imprimerie",
    "prépresse",
    "pao print",
    "impression numérique grand format",
]


# ─── Fonctions texte ──────────────────────────────────────────────────────────

def _texte_sans_accents(s: str) -> str:
    nfd = unicodedata.normalize("NFD", s or "")
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn").lower()


def nettoyer_html(texte: str) -> str:
    sans_balises = re.sub(r"<[^>]+>", " ", texte or "")
    return re.sub(r"\s+", " ", sans_balises).strip()


def texte_contient_mot_cle(texte: str, mot_cle: str) -> bool:
    t = _texte_sans_accents(texte)
    mc = _texte_sans_accents(mot_cle)
    if mc in _MOTS_LIMITE_MOT or len(mc) <= 3:
        return re.search(rf"\b{re.escape(mc)}\b", t) is not None
    return mc in t


def texte_motion_design(texte: str) -> bool:
    return any(texte_contient_mot_cle(texte, m) for m in MOTION_KEYWORDS)


def texte_design_ux_ui(texte: str) -> bool:
    return any(texte_contient_mot_cle(texte, m) for m in DESIGN_KEYWORDS)


def texte_design_central(texte: str) -> bool:
    return any(texte_contient_mot_cle(texte, m) for m in DESIGN_CORE_TITLE_KEYWORDS + DESIGN_CORE_DESCRIPTION_KEYWORDS)


def est_contrat_alternance(texte: str) -> bool:
    return offer_matches_contract_types(texte, ["alternance"])


def text_matches_search_scope(text: str, scope: dict | None = None) -> bool:
    current_scope = scope if isinstance(scope, dict) else dynamic_search_scope()
    zone_mode = str(current_scope.get("zone_mode") or "").strip().lower()
    include_remote = bool(current_scope.get("include_remote", True))
    blob = _texte_sans_accents(text)
    is_remote = any(marker in blob for marker in REMOTE_MARKERS)

    if zone_mode == "remote":
        return is_remote
    if zone_mode in {"", "france"}:
        return include_remote or not is_remote

    terms = scope_zone_terms(current_scope)
    if zone_mode == "idf" and not terms:
        terms = scope_zone_terms({"zone_mode": "idf", "zone_geo": "", "include_remote": include_remote})

    if any(term and term in blob for term in terms):
        return True
    if include_remote and is_remote:
        return True
    return False


def offer_matches_search_settings(
    titre: str,
    entreprise: str,
    lieu: str,
    description: str,
    contrat: str = "",
    search: dict | None = None,
) -> bool:
    active = active_search_terms(search)
    scope = search if isinstance(search, dict) else dynamic_search_scope()
    haystack = " ".join([titre, entreprise, lieu, description, contrat])

    negative_terms = active.get("mots_cles_negatifs", [])
    if any(texte_contient_mot_cle(haystack, term) for term in negative_terms):
        return False

    if not offer_matches_contract_types(haystack, active.get("types_contrat", [])):
        return False

    positive_terms = [*active.get("postes_cibles", []), *active.get("mots_cles_positifs", [])]
    if positive_terms and not any(texte_contient_mot_cle(haystack, term) for term in positive_terms):
        return False

    location_blob = " ".join([lieu, description])
    if not text_matches_search_scope(location_blob, scope):
        return False

    return True


def famille_poste(titre: str, description: str) -> str:
    text = f"{titre} {description}"
    active = active_search_terms()
    preferred_terms = [*active.get("postes_cibles", []), *active.get("mots_cles_positifs", [])]
    for term in preferred_terms:
        if texte_contient_mot_cle(text, term):
            return term
    return "—"


def motion_en_priorite(titre: str, description: str) -> bool:
    text = f"{titre} {description}"
    active = active_search_terms()
    for term in active.get("postes_cibles", []):
        if texte_contient_mot_cle(text, term):
            return True
    return False


def est_offre_exclue(titre: str, description: str) -> bool:
    titre_norm = titre.lower()

    # Exclusion dynamique : mots_cles_negatifs du profil utilisateur
    # (défensif — offer_matches_search_settings() fait déjà ce check sur le haystack complet)
    profile_negatifs = _normalize_tag_list(_read_profile_json().get("mots_cles_negatifs", []))
    for kw in profile_negatifs:
        if texte_contient_mot_cle(titre_norm, kw):
            return True

    # Exclusion sur le titre : emplois clairement hors-sujet
    for kw in _TITRES_EXCLUS:
        if kw in titre_norm:
            return True

    # Exclusion senior dans le titre
    for kw in _TITRES_SENIORS:
        if kw in titre_norm:
            return True

    return False


def is_relevant_offer(titre: str, description: str, contrat: str = "") -> bool:
    """
    Retourne True si l'offre est pertinente pour la recherche.
    Le champ `contrat` permet de bypasser la vérification textuelle
    pour les sources qui filtrent déjà par type de contrat (ex. LBA).
    """
    texte_complet = (titre + " " + description + " " + contrat).lower()
    dynamic = dynamic_search_terms()
    positive_terms = [*dynamic.get("postes_cibles", []), *dynamic.get("mots_cles_positifs", [])]
    negative_terms = dynamic.get("mots_cles_negatifs", [])
    contract_types = dynamic.get("types_contrat", [])

    if negative_terms and any(texte_contient_mot_cle(texte_complet, term) for term in negative_terms):
        return False

    if positive_terms:
        if not offer_matches_contract_types(texte_complet, contract_types):
            return False
        if not any(texte_contient_mot_cle(texte_complet, term) for term in positive_terms):
            return False
        return not est_offre_exclue(titre, description)

    # Sans criteres utilisateur explicites, on reste volontairement neutre :
    # alternance + pas d'exclusion forte.
    if not est_contrat_alternance(texte_complet):
        return False

    return not est_offre_exclue(titre, description)


# ─── Filtrage géographique ────────────────────────────────────────────────────

def _blob_geographique(offre: dict) -> str:
    parts: list[str] = list(offre.get("zones_geo") or [])
    parts.append(offre.get("lieu") or "")
    return _texte_sans_accents(" | ".join(parts))


def est_offre_remote(offre: dict) -> bool:
    b = _texte_sans_accents(
        " | ".join(
            [
                _blob_geographique(offre),
                str(offre.get("description") or ""),
                str(offre.get("titre") or ""),
            ]
        )
    )
    return any(m in b for m in REMOTE_MARKERS)


def est_paris_ou_banlieue_idf(offre: dict) -> bool:
    zones = offre.get("zones_geo") or []
    lieu = (offre.get("lieu") or "").strip()
    if not zones and not lieu:
        return False

    b = _blob_geographique(offre).replace("-", " ").replace("'", " ")
    b = re.sub(r"\s+", " ", b)

    if "ile de france" in b:
        return True
    if re.search(r"\bparis\b", b):
        return True
    # Format France Travail : "75 - PARIS", "92 - HAUTS-DE-SEINE", etc.
    if re.search(r"^(27|28|45|60|75|77|78|91|92|93|94|95)\b", b.strip()):
        return True

    idf_departements = (
        "yvelines", "essonne", "val doise", "val d oise",
        "seine et marne", "seine saint denis", "hauts de seine", "val de marne",
        "oise", "eure", "eure et loir", "loiret",
    )
    for dept in idf_departements:
        if dept in b:
            return True

    if "vesinet" in b:
        return True

    if INCLURE_OFFRES_REMOTE and est_offre_remote(offre):
        return True

    return False


def filtrer_zone_idf(offres: list[dict]) -> tuple[list[dict], int]:
    gardes = [o for o in offres if est_paris_ou_banlieue_idf(o)]
    return gardes, len(offres) - len(gardes)


def filtrer_offres_selon_recherche(offres: list[dict]) -> tuple[list[dict], int]:
    scope = dynamic_search_scope()
    zone_mode = scope.get("zone_mode", "")

    if zone_mode in {"", "idf"}:
        return filtrer_zone_idf(offres)

    gardes: list[dict] = []
    for offre in offres:
        scope_blob = " ".join(
            [
                _blob_geographique(offre),
                str(offre.get("description") or ""),
                str(offre.get("titre") or ""),
            ]
        )
        if text_matches_search_scope(scope_blob, scope):
            gardes.append(offre)

    return gardes, len(offres) - len(gardes)


# ─── Déduplication ────────────────────────────────────────────────────────────

def _normaliser_cle(s: str) -> str:
    """Normalise une chaîne pour comparaison : sans accents, sans ponctuation, minuscules."""
    s = _texte_sans_accents(s or "")
    s = re.sub(r"[^a-z0-9 ]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def cle_deduplication(offre: dict) -> str:
    """
    Clé de déduplication robuste.
    Priorité : ID propre > URL > titre normalisé + entreprise normalisée.
    """
    oid = (offre.get("id") or "").strip()
    if oid:
        return oid

    url = (offre.get("url") or "").strip().rstrip("/")
    if url:
        return url

    titre = _normaliser_cle(offre.get("titre", ""))
    entreprise = _normaliser_cle(offre.get("entreprise", ""))
    return f"{titre}|{entreprise}"

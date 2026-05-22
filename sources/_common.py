"""Utilitaires partagés : format commun, filtres métier, filtres géographiques."""

import os
import re
import unicodedata

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
    return {
        "postes_cibles": _env_list("ALTERNANCE_TARGET_ROLES"),
        "mots_cles_positifs": _env_list("ALTERNANCE_POSITIVE_KEYWORDS"),
        "mots_cles_negatifs": _env_list("ALTERNANCE_NEGATIVE_KEYWORDS"),
        "types_contrat": [item.lower() for item in _env_list("ALTERNANCE_CONTRACT_TYPES")],
    }

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
    t = texte.lower()
    mc = mot_cle.lower()
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
    t = texte.lower()
    return any(m in t for m in CONTRAT_MARKERS)


def famille_poste(titre: str, description: str) -> str:
    text = (titre + " " + description).lower()
    m = texte_motion_design(text)
    d = texte_design_ux_ui(text)
    if m and d:
        return "Motion design + UX / UI & design"
    if m:
        return "Motion design (prioritaire)"
    if d:
        return "UX / UI & design graphique"
    return "—"


def motion_en_priorite(titre: str, description: str) -> bool:
    return texte_motion_design((titre + " " + description).lower())


def est_offre_exclue(titre: str, description: str) -> bool:
    titre_norm = titre.lower()
    desc_norm = description.lower()

    # Exclusion sur le titre : emplois clairement hors-sujet
    for kw in _TITRES_EXCLUS:
        if kw in titre_norm:
            return True

    # Exclusion senior dans le titre
    for kw in _TITRES_SENIORS:
        if kw in titre_norm:
            return True

    # Exclusion print-only : présent dans description ET aucun mot-clé digital
    has_print = any(kw in desc_norm for kw in _DESC_PRINT_PUR)
    if has_print:
        has_digital = texte_motion_design(desc_norm) or texte_design_ux_ui(desc_norm)
        if not has_digital:
            return True

    communication_markers = [
        "communication",
        "marketing",
        "community",
        "reseaux sociaux",
        "social media",
        "brand content",
        "contenu editorial",
    ]
    if any(marker in titre_norm for marker in communication_markers):
        title_has_strong_design = texte_motion_design(titre_norm) or any(
            texte_contient_mot_cle(titre_norm, kw) for kw in DESIGN_CORE_TITLE_KEYWORDS
        )
        if not title_has_strong_design:
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
        if contract_types and "alternance" in contract_types and not est_contrat_alternance(texte_complet):
            return False
        if not any(texte_contient_mot_cle(texte_complet, term) for term in positive_terms):
            return False
        return not est_offre_exclue(titre, description)

    # Doit contenir une mention de contrat alternance
    if not est_contrat_alternance(texte_complet):
        return False

    # Doit contenir au moins un mot-clé design ou motion
    if not (texte_motion_design(texte_complet) or texte_design_ux_ui(texte_complet)):
        return False

    titre_norm = titre.lower()
    desc_norm = description.lower()

    strong_title_signal = texte_motion_design(titre_norm) or any(
        texte_contient_mot_cle(titre_norm, kw) for kw in DESIGN_CORE_TITLE_KEYWORDS
    )
    strong_description_signal = any(
        texte_contient_mot_cle(desc_norm, kw) for kw in DESIGN_CORE_DESCRIPTION_KEYWORDS
    )

    # On ne garde pas les offres qui n'ont qu'un signal design trop vague.
    if not (strong_title_signal or strong_description_signal):
        return False

    # Exclusion des offres hors-sujet
    if est_offre_exclue(titre, description):
        return False

    return True


# ─── Filtrage géographique ────────────────────────────────────────────────────

def _blob_geographique(offre: dict) -> str:
    parts: list[str] = list(offre.get("zones_geo") or [])
    parts.append(offre.get("lieu") or "")
    return _texte_sans_accents(" | ".join(parts))


def est_offre_remote(offre: dict) -> bool:
    b = _blob_geographique(offre)
    remote_markers = (
        "remote", "hybrid remote", "teletravail", "travail a distance",
        "work from home", "worldwide", "anywhere",
    )
    return any(m in b for m in remote_markers)


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

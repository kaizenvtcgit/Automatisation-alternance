"""
Alternance Auto — orchestrateur principal.

Commandes :
  python main.py                              → récupérer les offres et exporter
  python main.py marquer --id ID --url URL   → marquer une postulation manuelle
  python main.py historique                  → afficher les postulations
  python main.py lettres                     → générer les lettres via IA
  python main.py test-ft                     → diagnostiquer France Travail
"""

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock

# Forcer UTF-8 sur stdout/stderr pour éviter les crashes sur emoji ou accents
# (le terminal Windows utilise cp1252 par défaut)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import requests

_scan_state_lock = RLock()
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# Importer toutes les sources
from sources import recuperer_toutes_offres_detail
from sources._common import (
    famille_poste,
    filtrer_zone_idf,
    is_relevant_offer,
    motion_en_priorite,
    nettoyer_html,
)
from storage_service import (
    BASE_DIR as STORAGE_BASE_DIR,
    CSV_PATH,
    EXPORT_ROOT,
    HISTORIQUE_PATH,
    LETTRES_PATH,
    MESSAGES_DIR,
    SCAN_STATE_PATH,
    SCORES_PATH,
    read_json as storage_read_json,
    write_json_atomic,
)

# ─── Chemins ─────────────────────────────────────────────────────────────────

_BASE            = STORAGE_BASE_DIR

# Alias de compatibilite pour les modules plus anciens.
HISTORIQUE_POSTULATIONS = HISTORIQUE_PATH

CSV_DELIMITEUR = ";"
MAX_OFFER_AGE_DAYS = 21

COLONNES_CSV = [
    "ID annonce",
    "Source",
    "Intitulé du poste",
    "Entreprise",
    "Ville ou zone",
    "Lien vers l'annonce",
    "Date de publication",
    "Catégorie",
    "Famille détectée (motion / UX…)",
    "Requête qui a trouvé l'annonce",
    "Description (texte complet)",
]

# ─── Profil candidat ─────────────────────────────────────────────────────────

CANDIDAT_PRENOM = os.environ.get("CANDIDAT_PRENOM", "[Prénom]")
CANDIDAT_NOM    = os.environ.get("CANDIDAT_NOM", "[Nom]")
CANDIDAT_EMAIL  = os.environ.get("CANDIDAT_EMAIL", "[email@exemple.com]")
CANDIDAT_TEL    = os.environ.get("CANDIDAT_TEL", "[téléphone]")
CANDIDAT_LIEN   = os.environ.get("CANDIDAT_PORTFOLIO", os.environ.get("CANDIDAT_LIEN", "[URL]"))

_PROFIL_CANDIDAT = """
Candidat : Julien Ledouble
Domaine visé : UX/UI Design, Product Design, Web Design, Design numérique
Portfolio : https://julienledouble-lab.github.io/

Projets notables :
- Impulsion : plateforme de collaboration musicale entre artistes (conception UX complète,
  parcours utilisateur, wireframes, prototypage Figma)

Compétences : UX/UI, prototypage, parcours utilisateur, wireframes, Figma, Webflow, HTML/CSS,
conception d'interfaces, tests utilisateurs, architecture de l'information

Parcours : reconversion vers le design numérique ; expérience antérieure en relation client
(utile pour comprendre les besoins utilisateurs et travailler avec des parties prenantes)
"""

# ─── Groq ─────────────────────────────────────────────────────────────────────

_MODELES_GROQ = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
]

SCAN_STATE_VERSION = 1
FINAL_PIPELINE_STATUSES = {"postule", "refusee"}
PIPELINE_STATUSES = (
    "nouvelle",
    "a_analyser",
    "interessante",
    "lettre_generee",
    "postule",
    "refusee",
)
STATUS_ALIASES = {
    "new": "nouvelle",
    "nouveau": "nouvelle",
    "nouvelle": "nouvelle",
    "a_analyser": "a_analyser",
    "analyse": "a_analyser",
    "analyzed": "a_analyser",
    "analysee": "a_analyser",
    "analysed": "a_analyser",
    "generated": "lettre_generee",
    "lettre": "lettre_generee",
    "lettre_generee": "lettre_generee",
    "interessante": "interessante",
    "interesting": "interessante",
    "applied": "postule",
    "postule": "postule",
    "postulé": "postule",
    "envoye": "postule",
    "envoyé": "postule",
    "entretien": "postule",
    "rejected": "refusee",
    "rejected_offer": "refusee",
    "refusee": "refusee",
    "refusée": "refusee",
    "refus": "refusee",
    "ignoree": "refusee",
    "ignorée": "refusee",
    "ignore": "refusee",
    "ignored": "refusee",
    "echec": "refusee",
    "échec": "refusee",
    "failed": "refusee",
    "manuel_requis": "a_analyser",
    "en_attente": "a_analyser",
    "pending": "a_analyser",
    "": "a_analyser",
}


def _construire_prompt_lettre(titre: str, entreprise: str, description: str) -> str:
    return f"""Tu es un expert en candidatures françaises pour des postes en design numérique.

PROFIL CANDIDAT :
{_PROFIL_CANDIDAT}

OFFRE :
Poste : {titre}
Entreprise : {entreprise}
Description : {description[:1200]}

---

MISSION : Rédige une lettre de motivation personnalisée pour cette offre.

ÉTAPE 1 — Analyse silencieuse (ne pas écrire cette partie) :
- Identifie les 2-3 missions principales du poste
- Identifie les compétences clés demandées
- Identifie le secteur et le ton de l'entreprise
- Sélectionne les 2-3 arguments du profil candidat les plus pertinents pour CETTE offre

ÉTAPE 2 — Rédige la lettre selon ces règles strictes :

STYLE :
- Professionnel mais naturel et direct
- Humain, sans phrases génériques ni formules lourdes
- Sans flatterie excessive
- Adapter le ton au secteur de l'entreprise

INTERDITS ABSOLUS :
- Ne jamais commencer une phrase par "Je" au premier paragraphe
- Ne jamais écrire : "Passionné depuis toujours", "Votre entreprise leader",
  "Je me permets de vous adresser", "Je suis vivement intéressé par votre offre"
- Ne jamais inventer une compétence, expérience, diplôme ou chiffre absent du profil

STRUCTURE (250-350 mots) :

Paragraphe 1 — Accroche directe et personnalisée :
Pourquoi CETTE offre et CETTE entreprise précisément. Référence au poste ou aux missions.
Ne pas commencer par "Je".

Paragraphe 2 — Lien profil / besoins de l'entreprise :
1 ou 2 expériences/projets concrets du candidat (Impulsion si pertinent).
Ce que Julien sait faire : concevoir des parcours, créer des interfaces, prototyper, structurer une UX.

Paragraphe 3 — Apport en alternance :
Sérieux, progression rapide, regard utilisateur, expérience relation client, envie de contribuer
à un produit réel.

Conclusion :
Disponibilité pour un échange. Formule simple, pas exagérée.

FORMAT DE SORTIE ATTENDU (respecter exactement) :
---
Objet : Candidature alternance {titre}

Madame, Monsieur,

[lettre]

Cordialement,
Julien Ledouble
---

Retourne UNIQUEMENT le texte formaté ci-dessus, sans commentaire ni explication."""


def _generer_lettre_groq(titre: str, entreprise: str, description: str) -> str:
    from groq import Groq
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        raise ValueError(
            "GROQ_API_KEY manquante dans .env — inscription gratuite sur console.groq.com"
        )
    client = Groq(api_key=api_key)
    prompt = _construire_prompt_lettre(titre, entreprise, description)
    derniere_erreur = None
    for modele in _MODELES_GROQ:
        print(f"  Modèle : {modele}", flush=True)
        try:
            resp = client.chat.completions.create(
                model=modele,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=800,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            msg = str(e)
            if "429" in msg or "rate_limit" in msg.lower():
                print(f"  → {modele} : rate limit, essai du modèle suivant...", flush=True)
                derniere_erreur = e
            else:
                raise
    raise RuntimeError(
        f"Tous les modèles Groq ont atteint leur limite.\n"
        f"Dernière erreur : {derniere_erreur}\n"
        "Vérifiez GROQ_API_KEY dans .env (console.groq.com)"
    )


# ─── Lettres ─────────────────────────────────────────────────────────────────

def _charger_lettres() -> dict:
    loaded = storage_read_json(LETTRES_PATH, {})
    return loaded if isinstance(loaded, dict) else {}


def _charger_scores() -> dict:
    loaded = storage_read_json(SCORES_PATH, {})
    return loaded if isinstance(loaded, dict) else {}


def _sauvegarder_lettre(offre_id: str, lettre: str, titre: str, entreprise: str) -> None:
    lettres = _charger_lettres()
    existing = lettres.get(offre_id, {})
    lettres[offre_id] = {
        **existing,
        "lettre":     lettre,
        "titre":      titre,
        "entreprise": entreprise,
        "date_gen":   datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    write_json_atomic(LETTRES_PATH, lettres)
    if offre_id:
        mark_offer_letter_generated(offre_id, titre=titre, entreprise=entreprise)


def _utc_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_offer_publication_date(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None

    normalized = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        pass

    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return None


def is_offer_within_max_age(date_value: str, max_age_days: int = MAX_OFFER_AGE_DAYS) -> bool:
    published_at = parse_offer_publication_date(date_value)
    if published_at is None:
        return True
    if published_at.tzinfo is not None:
        published_at = published_at.astimezone().replace(tzinfo=None)
    cutoff = datetime.now() - timedelta(days=max_age_days)
    return published_at >= cutoff


def _slug_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^a-zA-Z0-9]+", " ", value).strip().lower()
    return re.sub(r"\s+", " ", value)


def normalize_status(status: str | None, default: str = "a_analyser") -> str:
    key = _slug_text(status or "").replace(" ", "_")
    normalized = STATUS_ALIASES.get(key)
    if normalized:
        return normalized
    return default if default in PIPELINE_STATUSES else "a_analyser"


def _normalized_source_name(value: str) -> str:
    text = _slug_text(value).replace(" ", "_")
    return text or "unknown"


def _safe_company_name(value: str) -> str:
    text = str(value or "").strip()
    return "" if text in {"—", "-", "?"} else text


def _extract_offer_field(offer: dict, *keys: str) -> str:
    for key in keys:
        value = offer.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _default_scan_state() -> dict:
    return {
        "version": SCAN_STATE_VERSION,
        "last_scan_at": None,
        "last_success_at": None,
        "last_scan": {
            "started_at": None,
            "finished_at": None,
            "status": "never",
            "offers_found": 0,
            "new_offers": 0,
            "duplicates_ignored": 0,
            "exported_offers": 0,
            "sources_scanned": {},
            "errors": [],
        },
        "history": [],
        "seen_offer_keys": [],
        "analyzed_offer_keys": [],
        "letter_offer_keys": [],
        "offers": {},
    }


def _scan_state_with_defaults(raw: dict | None) -> dict:
    state = _default_scan_state()
    if not isinstance(raw, dict):
        return state

    state["version"] = raw.get("version", SCAN_STATE_VERSION)
    state["last_scan_at"] = raw.get("last_scan_at")
    state["last_success_at"] = raw.get("last_success_at")

    if isinstance(raw.get("last_scan"), dict):
        state["last_scan"].update(raw["last_scan"])

    if isinstance(raw.get("history"), list):
        state["history"] = raw["history"][-50:]

    for key in ("seen_offer_keys", "analyzed_offer_keys", "letter_offer_keys"):
        values = raw.get(key, [])
        if isinstance(values, list):
            state[key] = [str(v) for v in values if str(v).strip()]

    offers = raw.get("offers", {})
    if isinstance(offers, dict):
        state["offers"] = {
            str(k): v for k, v in offers.items()
            if isinstance(v, dict) and str(k).strip()
        }

    return state


def load_scan_state() -> dict:
    with _scan_state_lock:
        SCAN_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        if not SCAN_STATE_PATH.exists():
            state = _default_scan_state()
            save_scan_state(state)
            return state

        try:
            raw = json.loads(SCAN_STATE_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[scan_state] Fichier corrompu ou illisible, reinitialisation : {exc}", file=sys.stderr)
            state = _default_scan_state()
            save_scan_state(state)
            return state

        state = _scan_state_with_defaults(raw)
        if state != raw:
            save_scan_state(state)
        return state


def save_scan_state(state: dict) -> None:
    with _scan_state_lock:
        compact = _scan_state_with_defaults(state)
        compact["history"] = compact["history"][-50:]
        compact["seen_offer_keys"] = compact["seen_offer_keys"][-5000:]
        compact["analyzed_offer_keys"] = compact["analyzed_offer_keys"][-5000:]
        compact["letter_offer_keys"] = compact["letter_offer_keys"][-5000:]
        if len(compact["offers"]) > 5000:
            recent_keys = sorted(
                compact["offers"],
                key=lambda key: compact["offers"][key].get("last_seen_at", ""),
                reverse=True,
            )[:5000]
            compact["offers"] = {key: compact["offers"][key] for key in recent_keys}
        write_json_atomic(SCAN_STATE_PATH, compact)


def build_offer_signature(offer: dict) -> str:
    source = _normalized_source_name(_extract_offer_field(offer, "source", "Source"))
    source_id = _slug_text(
        _extract_offer_field(offer, "sourceId", "source_id", "id", "ID annonce", "id_adzuna", "pipeline_id")
    )
    url = normaliser_url(_extract_offer_field(offer, "url", "Lien vers l'annonce", "Lien vers l\u0027annonce"))
    titre = _slug_text(_extract_offer_field(offer, "titre", "title", "Intitulé du poste", "IntitulÃ© du poste"))
    entreprise = _slug_text(_safe_company_name(_extract_offer_field(offer, "entreprise", "company", "Entreprise")))
    localisation = _slug_text(_extract_offer_field(offer, "lieu", "location", "Ville ou zone", "ville_ou_zone"))

    if source_id:
        payload = f"source={source}|source_id={source_id}"
    elif url:
        payload = f"source={source}|url={url}"
    else:
        payload = f"source={source}|title={titre}|company={entreprise}|location={localisation}"
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:24]
    return f"off_{digest}"


def offer_aliases(offer: dict, canonical_key: str | None = None) -> set[str]:
    aliases = {
        canonical_key or build_offer_signature(offer),
        _extract_offer_field(offer, "id", "ID annonce", "id_adzuna", "pipeline_id"),
        normaliser_url(_extract_offer_field(offer, "url", "Lien vers l'annonce", "Lien vers l\u0027annonce")),
    }
    titre = _slug_text(_extract_offer_field(offer, "titre", "title", "Intitulé du poste", "IntitulÃ© du poste"))
    entreprise = _slug_text(_extract_offer_field(offer, "entreprise", "company", "Entreprise"))
    localisation = _slug_text(_extract_offer_field(offer, "lieu", "location", "Ville ou zone"))
    if titre or entreprise or localisation:
        aliases.add(f"meta:{titre}|{entreprise}|{localisation}")
    return {alias for alias in aliases if alias}


def _offer_key_from_parts(
    offre_id: str = "",
    url: str = "",
    titre: str = "",
    entreprise: str = "",
    source: str = "",
    lieu: str = "",
) -> str:
    return build_offer_signature(
        {
            "id": offre_id,
            "url": url,
            "titre": titre,
            "entreprise": entreprise,
            "source": source,
            "lieu": lieu,
        }
    )


def offer_key(offre: dict) -> str:
    return _offer_key_from_parts(
        offre.get("id", ""),
        offre.get("url", ""),
        offre.get("titre", ""),
        offre.get("entreprise", ""),
        offre.get("source", ""),
        offre.get("lieu", ""),
    )


def offer_key_from_export_row(row: dict) -> str:
    return _offer_key_from_parts(
        row.get("ID annonce", ""),
        row.get("Lien vers l'annonce", ""),
        row.get("Intitulé du poste", ""),
        row.get("Entreprise", ""),
        row.get("Source", ""),
        row.get("Ville ou zone", ""),
    )


def _append_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def backup_json_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = path.with_name(f"{path.stem}.backup_{timestamp}{path.suffix}")
    backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    backup_entries: list[tuple[float, Path]] = []
    for candidate in path.parent.glob(f"{path.stem}.backup_*{path.suffix}"):
        try:
            backup_entries.append((candidate.stat().st_mtime, candidate))
        except OSError:
            continue
    backups = [candidate for _mtime, candidate in sorted(backup_entries, key=lambda item: item[0], reverse=True)]
    for old_backup in backups[10:]:
        try:
            old_backup.unlink()
        except OSError:
            pass
    return backup


def _build_alias_index(candidates: list[dict]) -> dict[str, str]:
    alias_index: dict[str, str] = {}
    for candidate in candidates:
        signature = build_offer_signature(candidate)
        for alias in offer_aliases(candidate, signature):
            alias_index.setdefault(alias, signature)
    return alias_index


def _resolve_offer_signature(existing_key: str, metadata: dict | None, alias_index: dict[str, str]) -> str:
    metadata = metadata or {}
    candidate = dict(metadata)
    aliases = list(offer_aliases(candidate)) if candidate else []
    aliases.insert(0, existing_key)
    for alias in aliases:
        if alias in alias_index:
            return alias_index[alias]
    if candidate:
        return build_offer_signature(candidate)
    if existing_key.startswith("off_"):
        return existing_key
    return build_offer_signature({"id": existing_key, "source": "legacy"})


def _status_is_user_locked(status: str) -> bool:
    return normalize_status(status) in {"postule", "refusee", "interessante"}


def _contains_any(text: str, keywords: list[str]) -> list[str]:
    found = []
    padded = f" {text} "
    for keyword in keywords:
        normalized_keyword = _slug_text(keyword)
        if not normalized_keyword:
            continue
        pattern = rf"(?<![a-z0-9]){re.escape(normalized_keyword)}(?![a-z0-9])"
        if re.search(pattern, padded):
            found.append(keyword)
    return found


def _score_level(score: int) -> str:
    if score >= 75:
        return "excellent"
    if score >= 60:
        return "bon"
    if score >= 40:
        return "moyen"
    return "faible"


def score_offer_fit(offer: dict) -> dict:
    titre = _extract_offer_field(offer, "titre", "title", "Intitulé du poste", "IntitulÃ© du poste")
    entreprise = _safe_company_name(_extract_offer_field(offer, "entreprise", "company", "Entreprise"))
    description = _extract_offer_field(offer, "description", "Description (texte complet)")
    contrat = _extract_offer_field(offer, "contrat", "typeContrat", "Type de contrat")
    localisation = _extract_offer_field(offer, "lieu", "location", "Ville ou zone")
    source = _extract_offer_field(offer, "source", "Source")

    text = _slug_text(" ".join([titre, description, contrat, localisation, entreprise, source]))
    positive: list[str] = []
    negative: list[str] = []
    warnings: list[str] = []
    detected: set[str] = set()
    score = 0

    def add(points: int, reason: str, keywords: list[str] | None = None) -> None:
        nonlocal score
        score += points
        if points >= 0:
            positive.append(reason)
        else:
            negative.append(reason)
        for keyword in keywords or []:
            detected.add(keyword)

    alternance_hits = _contains_any(text, ["alternance", "apprentissage", "contrat de professionnalisation", "contrat pro"])
    if alternance_hits:
        add(24, "Offre en alternance ou apprentissage", alternance_hits)
    else:
        add(-28, "Contrat d'alternance non clair")
        warnings.append("Contrat non identifie comme alternance")

    idf_hits = _contains_any(text, ["paris", "ile de france", "boulogne", "nanterre", "montreuil", "saint denis", "val de marne", "hauts de seine"])
    if idf_hits:
        add(10, "Localisation compatible Paris / Ile-de-France", idf_hits[:2])
    remote_hits = _contains_any(text, ["hybride", "teletravail", "remote", "distanc", "partiel"])
    if remote_hits:
        add(5, "Mode hybride ou teletravail partiel", remote_hits[:2])

    design_hits = _contains_any(text, [
        "ux", "ui", "ux ui", "product design", "product designer", "web design", "web designer",
        "design numerique", "designer d interface", "design system", "motion design", "motion designer",
        "interface", "parcours utilisateur", "experience utilisateur", "user research", "recherche utilisateur"
    ])
    if design_hits:
        add(min(30, 8 + len(set(design_hits)) * 3), "Missions design proches du profil cible", list(dict.fromkeys(design_hits))[:5])
    else:
        add(-18, "Mission design peu visible")
        warnings.append("Peu de signaux UX/UI ou design produit")

    tool_hits = _contains_any(text, [
        "figma", "webflow", "html css", "html", "css", "prototype", "prototypage",
        "wireframe", "wireframes", "mobile", "application web", "application mobile"
    ])
    if tool_hits:
        add(min(22, len(set(tool_hits)) * 4), "Outils et livrables alignes avec tes competences", list(dict.fromkeys(tool_hits))[:5])

    product_hits = _contains_any(text, ["produit", "saas", "startup", "app", "application", "interface", "dashboard", "parcours utilisateur"])
    if product_hits:
        add(min(12, len(set(product_hits)) * 2), "Contexte produit digital pertinent", list(dict.fromkeys(product_hits))[:4])

    relation_hits = _contains_any(text, ["utilisateur", "besoin", "test utilisateur", "experience", "parcours"])
    if relation_hits:
        add(6, "Approche orientee experience utilisateur", list(dict.fromkeys(relation_hits))[:3])

    if "impulsion" in text or "musique" in text or "creative" in text:
        add(3, "Univers creatif potentiellement valorisable", ["creative"] if "creative" in text else [])

    if _contains_any(text, ["senior", "lead", "head of", "manager", "confirme"]):
        add(-30, "Niveau d'experience trop eleve")
        warnings.append("Poste potentiellement senior")
    if _contains_any(text, ["cdi", "temps plein", "full time"]) and not alternance_hits:
        add(-35, "Type de contrat incompatible (CDI / temps plein)")
    if _contains_any(text, ["cdd"]) and not alternance_hits:
        add(-25, "CDD non recherche")
    if _contains_any(text, ["stage"]) and not alternance_hits:
        add(-18, "Stage non alternance")
    if _contains_any(text, ["commercial", "vente", "business developer", "prospection"]):
        add(-28, "Orientation commerciale trop forte")
    if _contains_any(text, ["assistant administratif", "administratif", "back office"]) and not design_hits:
        add(-26, "Profil administratif hors cible")
    if _contains_any(text, ["community manager", "social media manager"]) and not design_hits:
        add(-20, "Communication reseaux sociaux sans design produit")
    if _contains_any(text, ["marketing", "communication"]) and not design_hits:
        add(-14, "Marketing / communication peu design")
    if _contains_any(text, ["print", "imprimerie", "serigraphie", "catalogue"]) and not _contains_any(text, ["digital", "web", "ui", "ux", "figma"]):
        add(-18, "Graphisme print uniquement")
    if _contains_any(text, ["developpeur", "developer", "integration", "integrateur"]) and not _contains_any(text, ["ux", "ui", "design", "figma", "front end creatif"]):
        add(-16, "Role trop technique sans composante design claire")

    if not idf_hits and not remote_hits:
        warnings.append("Localisation peu claire pour Paris / IDF")
    if not tool_hits:
        warnings.append("Peu d'outils design explicites")

    score = max(0, min(100, score))
    return {
        "signature": build_offer_signature(offer),
        "score": score,
        "level": _score_level(score),
        "positiveReasons": positive[:8],
        "negativeReasons": negative[:8],
        "detectedKeywords": sorted(detected),
        "warnings": warnings[:6],
        "date": datetime.now().strftime("%Y-%m-%d"),
    }


def _get_offer_record(state: dict, offer_key_value: str) -> dict:
    record = state["offers"].get(offer_key_value)
    if not isinstance(record, dict):
        record = {
            "status": "nouvelle",
            "manual_status": "",
            "first_seen_at": None,
            "last_seen_at": None,
            "last_scan_at": None,
            "times_seen": 0,
            "analyzed": False,
            "letter_generated": False,
            "sources": [],
            "score": None,
            "score_level": None,
            "score_reasons": [],
            "positive_reasons": [],
            "negative_reasons": [],
            "detected_keywords": [],
            "warnings": [],
        }
        state["offers"][offer_key_value] = record
    record["status"] = normalize_status(record.get("status"), "nouvelle")
    record["manual_status"] = normalize_status(record.get("manual_status"), "") if record.get("manual_status") else ""
    return record


def sync_pipeline_status(offer_key_value: str, status: str) -> None:
    if not offer_key_value:
        return
    state = load_scan_state()
    record = _get_offer_record(state, offer_key_value)
    normalized = normalize_status(status, "") if status else ""
    record["manual_status"] = normalized
    if normalized:
        record["status"] = normalized
    save_scan_state(state)


def mark_offer_analyzed(offer_key_value: str, evaluation: dict | None = None) -> None:
    if not offer_key_value:
        return
    state = load_scan_state()
    record = _get_offer_record(state, offer_key_value)
    record["analyzed"] = True
    if evaluation:
        record["score"] = int(evaluation.get("score", 0))
        record["score_level"] = evaluation.get("level")
        record["score_reasons"] = list(evaluation.get("positiveReasons", []))
        record["positive_reasons"] = list(evaluation.get("positiveReasons", []))
        record["negative_reasons"] = list(evaluation.get("negativeReasons", []))
        record["detected_keywords"] = list(evaluation.get("detectedKeywords", []))
        record["warnings"] = list(evaluation.get("warnings", []))
    if not record.get("manual_status") and record.get("status") not in FINAL_PIPELINE_STATUSES:
        if (record.get("score") or 0) >= 75 and record.get("status") in {"nouvelle", "a_analyser", ""}:
            record["status"] = "interessante"
        elif record.get("status") == "nouvelle":
            record["status"] = "a_analyser"
    _append_unique(state["analyzed_offer_keys"], offer_key_value)
    save_scan_state(state)


def mark_offer_letter_generated(offer_key_value: str, titre: str = "", entreprise: str = "") -> None:
    if not offer_key_value:
        return
    state = load_scan_state()
    record = _get_offer_record(state, offer_key_value)
    record["letter_generated"] = True
    if titre:
        record["title"] = titre
    if entreprise:
        record["company"] = entreprise
    if not record.get("manual_status") and record.get("status") not in FINAL_PIPELINE_STATUSES:
        record["status"] = "lettre_generee"
    _append_unique(state["letter_offer_keys"], offer_key_value)
    save_scan_state(state)


def _build_pipeline_status(
    record: dict,
    offer_key_value: str,
    is_new: bool,
    refus_ids: set[str],
    histo_status_by_key: dict[str, str],
) -> str:
    manual = normalize_status(record.get("manual_status"), "") if record.get("manual_status") else ""
    if manual:
        return manual
    if offer_key_value in refus_ids:
        return "refusee"
    histo_status = normalize_status(histo_status_by_key.get(offer_key_value), "a_analyser")
    if histo_status == "postule":
        return "postule"
    if record.get("letter_generated"):
        return "lettre_generee"
    if record.get("analyzed") and (record.get("score") or 0) >= 75:
        return "interessante"
    if is_new:
        return "nouvelle"
    return "a_analyser"


def _collect_offer_candidates(
    csv_rows: list[dict] | None = None,
    historique: list[dict] | None = None,
    scan_state: dict | None = None,
) -> list[dict]:
    candidates: list[dict] = []
    for row in csv_rows or []:
        candidates.append(row)
    for row in historique or []:
        candidates.append(
            {
                "id": row.get("id_adzuna", ""),
                "url": row.get("url", ""),
                "titre": row.get("titre", ""),
                "entreprise": row.get("entreprise", ""),
                "source": row.get("source", ""),
                "lieu": row.get("lieu", ""),
            }
        )
    for key, record in (scan_state or {}).get("offers", {}).items():
        if isinstance(record, dict):
            candidates.append(
                {
                    "pipeline_id": key,
                    "id": record.get("id", ""),
                    "url": record.get("url", ""),
                    "titre": record.get("title", ""),
                    "entreprise": record.get("company", ""),
                    "source": record.get("source", ""),
                    "lieu": record.get("location", ""),
                }
            )
    return candidates


def _merge_json_entries(target: dict, key: str, payload: dict) -> None:
    existing = target.get(key, {})
    if isinstance(existing, dict):
        target[key] = {**existing, **payload}
    else:
        target[key] = payload


def _normalize_source_scan_summary(name: str, meta: dict, timestamp: str) -> dict:
    count = int(meta.get("count", 0) or 0)
    error = str(meta.get("error", "") or "")
    status = meta.get("status")
    if not status:
        status = "erreur" if error else "ok"
    if count and error and status == "erreur":
        status = "partiel"
    return {
        "source": name,
        "status": status,
        "offers_found": count,
        "new_offers": int(meta.get("new_offers", 0) or 0),
        "duplicates": int(meta.get("duplicates", 0) or 0),
        "error_message": error,
        "timestamp": meta.get("timestamp") or timestamp,
    }


def migrate_data_files() -> dict:
    csv_rows = _lire_csv_rows()
    historique = charger_historique_postulations()
    raw_scan_state = _read_scan_state_raw()
    alias_index = _build_alias_index(_collect_offer_candidates(csv_rows, historique, raw_scan_state))

    migration_report = {
        "backups": [],
        "historique_updated": False,
        "scores_updated": False,
        "lettres_updated": False,
        "scan_state_updated": False,
    }

    normalized_historique = []
    historique_changed = False
    for row in historique:
        new_row = dict(row)
        canonical = _resolve_offer_signature(
            str(row.get("offer_signature") or row.get("id_adzuna") or row.get("url") or ""),
            {
                "id": row.get("id_adzuna", ""),
                "url": row.get("url", ""),
                "titre": row.get("titre", ""),
                "entreprise": row.get("entreprise", ""),
                "source": row.get("source", ""),
                "lieu": row.get("lieu", ""),
            },
            alias_index,
        )
        normalized_status = normalize_status(row.get("statut"))
        new_row["offer_signature"] = canonical
        new_row["statut"] = normalized_status
        if normalized_status == "postule" and not new_row.get("date_relance_prevue"):
            new_row["date_relance_prevue"] = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
        normalized_historique.append(new_row)
        if new_row != row:
            historique_changed = True

    if historique_changed:
        backup = backup_json_file(HISTORIQUE_PATH)
        if backup:
            migration_report["backups"].append(str(backup))
        write_json_atomic(HISTORIQUE_PATH, normalized_historique)
        migration_report["historique_updated"] = True
        historique = normalized_historique

    alias_index = _build_alias_index(_collect_offer_candidates(csv_rows, historique, raw_scan_state))

    lettres = _charger_lettres()
    normalized_lettres: dict[str, dict] = {}
    lettres_changed = False
    for key, value in lettres.items():
        payload = dict(value) if isinstance(value, dict) else {"lettre": str(value)}
        canonical = _resolve_offer_signature(
            key,
            {
                "id": key,
                "titre": payload.get("titre", ""),
                "entreprise": payload.get("entreprise", ""),
                "source": payload.get("source", ""),
                "url": payload.get("url", ""),
            },
            alias_index,
        )
        payload["signature"] = canonical
        _merge_json_entries(normalized_lettres, canonical, payload)
        if canonical != key or payload != value:
            lettres_changed = True

    if lettres_changed:
        backup = backup_json_file(LETTRES_PATH)
        if backup:
            migration_report["backups"].append(str(backup))
        write_json_atomic(LETTRES_PATH, normalized_lettres)
        migration_report["lettres_updated"] = True

    scores = _charger_scores()
    offer_by_signature = {offer_key_from_export_row(row): row for row in csv_rows}
    normalized_scores: dict[str, dict] = {}
    scores_changed = False
    for key, value in scores.items():
        payload = dict(value) if isinstance(value, dict) else {"score": value}
        canonical = _resolve_offer_signature(
            key,
            {
                "id": key,
                "titre": payload.get("titre", ""),
                "entreprise": payload.get("entreprise", ""),
                "source": payload.get("source", ""),
                "url": payload.get("url", ""),
            },
            alias_index,
        )
        offer_row = offer_by_signature.get(canonical)
        if offer_row:
            evaluation = score_offer_fit(
                {
                    "id": canonical,
                    "source": offer_row.get("Source", ""),
                    "titre": offer_row.get("Intitulé du poste", offer_row.get("IntitulÃ© du poste", "")),
                    "entreprise": offer_row.get("Entreprise", ""),
                    "lieu": offer_row.get("Ville ou zone", ""),
                    "description": offer_row.get("Description (texte complet)", ""),
                    "url": offer_row.get("Lien vers l'annonce", ""),
                }
            )
            evaluation["signature"] = canonical
            normalized_scores[canonical] = evaluation
        else:
            normalized_scores[canonical] = {
                "signature": canonical,
                "score": int(payload.get("score", 0) * 10 if int(payload.get("score", 0)) <= 10 else payload.get("score", 0)),
                "level": payload.get("level", _score_level(int(payload.get("score", 0) * 10 if int(payload.get("score", 0)) <= 10 else payload.get("score", 0)))),
                "positiveReasons": list(payload.get("positiveReasons", payload.get("raisons", []))),
                "negativeReasons": list(payload.get("negativeReasons", [])),
                "detectedKeywords": list(payload.get("detectedKeywords", [])),
                "warnings": list(payload.get("warnings", [])),
                "date": payload.get("date", datetime.now().strftime("%Y-%m-%d")),
            }
        if canonical != key or normalized_scores[canonical] != payload:
            scores_changed = True

    if scores_changed:
        backup = backup_json_file(SCORES_PATH)
        if backup:
            migration_report["backups"].append(str(backup))
        write_json_atomic(SCORES_PATH, normalized_scores)
        migration_report["scores_updated"] = True

    scan_state = load_scan_state()
    normalized_offers: dict[str, dict] = {}
    scan_changed = False
    timestamp = _utc_now()
    for key, record in (scan_state.get("offers") or {}).items():
        if not isinstance(record, dict):
            continue
        canonical = _resolve_offer_signature(
            key,
            {
                "pipeline_id": key,
                "id": record.get("id", ""),
                "url": record.get("url", ""),
                "titre": record.get("title", ""),
                "entreprise": record.get("company", ""),
                "source": record.get("source", ""),
                "lieu": record.get("location", ""),
            },
            alias_index,
        )
        next_record = dict(record)
        next_record["status"] = normalize_status(record.get("status"), "nouvelle")
        next_record["manual_status"] = normalize_status(record.get("manual_status"), "") if record.get("manual_status") else ""
        next_record["signature"] = canonical
        next_record["score_reasons"] = list(record.get("positive_reasons", record.get("score_reasons", [])))
        _merge_json_entries(normalized_offers, canonical, next_record)
        if canonical != key or next_record != record:
            scan_changed = True

    scan_state["offers"] = normalized_offers
    for list_key in ("seen_offer_keys", "analyzed_offer_keys", "letter_offer_keys"):
        normalized_keys = []
        for value in scan_state.get(list_key, []):
            normalized_keys.append(_resolve_offer_signature(str(value), {"id": str(value)}, alias_index))
        deduped = list(dict.fromkeys(normalized_keys))
        if deduped != scan_state.get(list_key, []):
            scan_state[list_key] = deduped
            scan_changed = True

    last_scan = scan_state.get("last_scan") or {}
    if isinstance(last_scan.get("sources_scanned"), dict):
        normalized_sources = {
            name: _normalize_source_scan_summary(name, meta if isinstance(meta, dict) else {}, last_scan.get("finished_at") or timestamp)
            for name, meta in last_scan["sources_scanned"].items()
        }
        if normalized_sources != last_scan.get("sources_scanned"):
            scan_state["last_scan"]["sources_scanned"] = normalized_sources
            scan_changed = True

    normalized_history = []
    for item in scan_state.get("history", []):
        if not isinstance(item, dict):
            continue
        entry = dict(item)
        if isinstance(entry.get("sources_scanned"), dict):
            entry["sources_scanned"] = {
                name: _normalize_source_scan_summary(name, meta if isinstance(meta, dict) else {}, entry.get("finished_at") or timestamp)
                for name, meta in entry["sources_scanned"].items()
            }
        normalized_history.append(entry)
    if normalized_history != scan_state.get("history", []):
        scan_state["history"] = normalized_history
        scan_changed = True

    if scan_changed:
        backup = backup_json_file(SCAN_STATE_PATH)
        if backup:
            migration_report["backups"].append(str(backup))
        save_scan_state(scan_state)
        migration_report["scan_state_updated"] = True

    return migration_report


def _read_scan_state_raw() -> dict:
    if not SCAN_STATE_PATH.exists():
        return {}
    try:
        raw = json.loads(SCAN_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _lire_csv_rows() -> list[dict]:
    if not CSV_PATH.exists():
        return []
    with CSV_PATH.open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle, delimiter=CSV_DELIMITEUR))
    return [
        row for row in rows
        if is_offer_within_max_age(row.get("Date de publication", ""))
        and is_relevant_offer(
            row.get("Intitulé du poste", row.get("IntitulÃ© du poste", "")),
            row.get("Description (texte complet)", ""),
            row.get("Type de contrat", ""),
        )
    ]


def _ensure_export_scores(csv_rows: list[dict]) -> dict:
    scores = _charger_scores()
    changed = False

    for row in csv_rows:
        offer_key_value = offer_key_from_export_row(row)
        if not offer_key_value:
            continue

        existing = scores.get(offer_key_value)
        if isinstance(existing, dict) and existing.get("score") is not None:
            continue

        evaluation = score_offer_fit(
            {
                "id": offer_key_value,
                "pipeline_id": offer_key_value,
                "source": row.get("Source", ""),
                "url": row.get("Lien vers l'annonce", ""),
                "titre": row.get("Intitulé du poste", row.get("IntitulÃ© du poste", "")),
                "entreprise": row.get("Entreprise", ""),
                "lieu": row.get("Ville ou zone", ""),
                "description": row.get("Description (texte complet)", ""),
                "contrat": row.get("Type de contrat", ""),
            }
        )
        evaluation["signature"] = offer_key_value
        scores[offer_key_value] = evaluation
        changed = True

    if changed:
        write_json_atomic(SCORES_PATH, scores)

    return scores


def refresh_scan_state_from_exports() -> dict:
    migrate_data_files()
    state = load_scan_state()
    csv_rows = _lire_csv_rows()
    lettres = _charger_lettres()
    scores = _ensure_export_scores(csv_rows)

    for offer_key_value, meta in lettres.items():
        record = _get_offer_record(state, offer_key_value)
        record["letter_generated"] = bool(meta.get("lettre"))
        record["title"] = meta.get("titre", record.get("title", ""))
        record["company"] = meta.get("entreprise", record.get("company", ""))
        _append_unique(state["letter_offer_keys"], offer_key_value)

    for offer_key_value, meta in scores.items():
        record = _get_offer_record(state, offer_key_value)
        record["analyzed"] = True
        record["score"] = meta.get("score")
        record["score_level"] = meta.get("level")
        record["score_reasons"] = meta.get("positiveReasons", meta.get("raisons", []))
        record["positive_reasons"] = meta.get("positiveReasons", meta.get("raisons", []))
        record["negative_reasons"] = meta.get("negativeReasons", [])
        record["detected_keywords"] = meta.get("detectedKeywords", [])
        record["warnings"] = meta.get("warnings", [])
        _append_unique(state["analyzed_offer_keys"], offer_key_value)

    save_scan_state(state)
    return state


def generer_lettres_batch() -> None:
    if not CSV_PATH.exists():
        print(f"CSV introuvable : {CSV_PATH}\nLance d'abord : python main.py")
        return

    offres = _lire_csv_rows()

    lettres = _charger_lettres()
    a_generer = [
        o for o in offres
        if (oid := offer_key_from_export_row(o)) and oid not in lettres
    ]

    print(f"{len(a_generer)} lettre(s) à générer ({len(lettres)} déjà présente(s)).")
    if not a_generer:
        print("Toutes les lettres sont déjà générées.")
        return

    ok = 0
    for i, offre in enumerate(a_generer, 1):
        offre_id    = offer_key_from_export_row(offre)
        titre       = offre.get("Intitulé du poste", offre.get("IntitulÃ© du poste", ""))
        entreprise  = offre.get("Entreprise", "")
        description = offre.get("Description (texte complet)", "")
        print(f"[{i}/{len(a_generer)}] {titre[:50]} - {entreprise[:25]}...", flush=True)
        try:
            lettre = _generer_lettre_groq(titre, entreprise, description)
            _sauvegarder_lettre(offre_id, lettre, titre, entreprise)
            print("  → OK", flush=True)
            ok += 1
        except Exception as e:
            print(f"  → ERREUR : {e}", flush=True)

    print(f"\n{ok}/{len(a_generer)} lettre(s) générée(s) → {LETTRES_PATH}")


# ─── Historique postulations ──────────────────────────────────────────────────

def normaliser_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


def charger_historique_postulations() -> list[dict]:
    brut = storage_read_json(HISTORIQUE_PATH, [])
    return brut if isinstance(brut, list) else []


def _index_historique(records: list[dict]) -> tuple[set[str], set[str], set[str]]:
    signatures: set[str] = set()
    ids:  set[str] = set()
    urls: set[str] = set()
    for r in records:
        signature = str(r.get("offer_signature") or "").strip()
        if signature:
            signatures.add(signature)
        i = str(r.get("id_adzuna") or "").strip()
        if i:
            ids.add(i)
        u = normaliser_url(r.get("url") or "")
        if u:
            urls.add(u)
    return signatures, ids, urls


def deja_postule(offre: dict, signatures_historique: set[str], ids_historique: set[str], urls_historique: set[str]) -> bool:
    if offer_key(offre) in signatures_historique:
        return True
    oid = str(offre.get("id") or "").strip()
    if oid and oid in ids_historique:
        return True
    u = normaliser_url(offre.get("url") or "")
    if u and u in urls_historique:
        return True
    return False


def filtrer_deja_postule(offres: list[dict]) -> tuple[list[dict], int]:
    rec = charger_historique_postulations()
    signatures_vues, ids_vus, urls_vues = _index_historique(rec)
    gardes = [o for o in offres if not deja_postule(o, signatures_vues, ids_vus, urls_vues)]
    return gardes, len(offres) - len(gardes)


def ajouter_postulation(id_offre: str, url: str, titre: str) -> None:
    id_offre = str(id_offre or "").strip()
    url      = (url or "").strip()
    titre    = (titre or "").strip() or "(sans titre)"

    if not id_offre and not url:
        print("Indique au moins --id ou --url.")
        sys.exit(1)

    rec = charger_historique_postulations()
    signatures_vues, ids_vus, urls_vues = _index_historique(rec)
    signature = _offer_key_from_parts(id_offre, url, titre, "")

    if signature in signatures_vues:
        print("Déjà enregistré (signature identique).")
        return
    if id_offre and id_offre in ids_vus:
        print(f"Déjà enregistré (ID « {id_offre} »).")
        return
    nu = normaliser_url(url)
    if nu and nu in urls_vues:
        print("Déjà enregistré (cette URL).")
        return

    rec.append({
        "id_adzuna":       id_offre,
        "url":             url,
        "titre":           titre,
        "offer_signature": signature,
        "statut":          "postule",
        "date_postulation": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    write_json_atomic(HISTORIQUE_PATH, rec)
    sync_pipeline_status(signature, "postule")
    print(f"Postulation enregistrée dans : {HISTORIQUE_PATH}")


def afficher_historique_postulations() -> None:
    rec = charger_historique_postulations()
    if not rec:
        print(
            "Aucune postulation enregistrée.\n"
            '  python main.py marquer --id "ID" --url "https://..." --titre "Intitulé"'
        )
        return
    print(f"Historique ({len(rec)} postulation(s)) :\n")
    for r in rec:
        print(r.get("date_postulation", "—"), "|", r.get("titre", "—"))
        print("  ID  :", r.get("id_adzuna") or "—")
        u = r.get("url") or ""
        print("  URL :", u if len(u) < 100 else u[:97] + "…")
        print()


# ─── Export CSV ───────────────────────────────────────────────────────────────

def nom_fichier_safe(titre: str, max_len: int = 45) -> str:
    s = re.sub(r'[<>:"/\\|?*]', "", titre)
    s = re.sub(r"\s+", "_", s.strip())
    return (s[:max_len] or "offre").rstrip("._")


def _ligne_csv(offre: dict) -> dict:
    fam = famille_poste(offre["titre"], offre["description"])
    return {
        "ID annonce":                    offre["id"],
        "Source":                        offre.get("source", ""),
        "Intitulé du poste":             offre["titre"],
        "Entreprise":                    offre["entreprise"],
        "Ville ou zone":                 offre["lieu"],
        "Lien vers l'annonce":           offre["url"],
        "Date de publication":           offre["date_pub"],
        "Catégorie":                     offre["categorie"],
        "Famille détectée (motion / UX…)": fam,
        "Requête qui a trouvé l'annonce": offre.get("requete_source", ""),
        "Description (texte complet)":   offre["description"],
    }


def _ecrire_fichier_csv(chemin: Path, offres: list[dict]) -> None:
    with chemin.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f, fieldnames=COLONNES_CSV,
            delimiter=CSV_DELIMITEUR, quoting=csv.QUOTE_MINIMAL,
        )
        writer.writeheader()
        for o in offres:
            writer.writerow(_ligne_csv(o))


def exporter_csv(offres: list[dict], chemin: Path) -> Path:
    chemin.parent.mkdir(parents=True, exist_ok=True)
    try:
        _ecrire_fichier_csv(chemin, offres)
        return chemin
    except PermissionError:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        alternatif = chemin.parent / f"offres_filtrees_{ts}.csv"
        print(
            f"Fichier verrouillé ({chemin.name} ouvert dans Excel ?).\n"
            f"Fichier alternatif créé : {alternatif}"
        )
        _ecrire_fichier_csv(alternatif, offres)
        return alternatif


def contenu_squelette(offre: dict) -> str:
    titre = offre["titre"]
    url   = offre["url"]
    ent   = offre.get("entreprise") or "(à compléter depuis l'annonce)"
    lieu  = offre.get("lieu") or "—"
    desc  = offre.get("description") or ""
    fam   = famille_poste(titre, desc)

    return f"""================================================================================
CANDIDATURE — {titre}
================================================================================

Annonce (lien) : {url}
Entreprise     : {ent}
Lieu           : {lieu}
Profil ciblé   : {fam}

--- Résumé annonce ---
{desc}

--------------------------------------------------------------------------------
OBJET E-MAIL
--------------------------------------------------------------------------------
Candidature en alternance — {titre} — {CANDIDAT_PRENOM} {CANDIDAT_NOM}

--------------------------------------------------------------------------------
LETTRE DE MOTIVATION (squelette)
--------------------------------------------------------------------------------
Madame, Monsieur,

Je me permets de vous adresser ma candidature pour l'alternance « {titre} ».

[Paragraphe 1 — Présentation courte : formation, année, ce que tu cherches.]

[Paragraphe 2 — Lien avec CETTE entreprise : projet, valeurs, secteur.]

[Paragraphe 3 — Compétences motion design / UX-UI / Figma — selon l'annonce.]

Je serais ravi d'échanger avec vous sur ce poste.

Cordialement,
{CANDIDAT_PRENOM} {CANDIDAT_NOM}
{CANDIDAT_EMAIL}
{CANDIDAT_TEL}
{CANDIDAT_LIEN}

--------------------------------------------------------------------------------
CHECKLIST avant envoi
--------------------------------------------------------------------------------
[ ] Adapté chaque paragraphe (pas de copier-coller générique).
[ ] Pièces jointes : CV, portfolio si demandé.
[ ] Objet et corps cohérents avec le canal (e-mail vs formulaire site).
"""


def generer_fichiers_candidature(offres: list[dict], dossier: Path) -> None:
    dossier.mkdir(parents=True, exist_ok=True)
    for i, offre in enumerate(offres, start=1):
        slug = nom_fichier_safe(offre["titre"])
        suffixe_id = offre.get("id") or str(i)
        nom = f"{i:02d}_{suffixe_id}_{slug}.txt"
        (dossier / nom).write_text(contenu_squelette(offre), encoding="utf-8")


def afficher_offres(offres: list[dict], max_desc: int = 400) -> None:
    if not offres:
        print("Aucune offre à afficher.")
        return
    for off in offres:
        desc = off["description"]
        if len(desc) > max_desc:
            desc = desc[:max_desc] + "…"
        fam = famille_poste(off["titre"], off["description"])
        print(off["titre"])
        print("   ", fam)
        if off.get("entreprise"):
            print("   ", off["entreprise"], "—", off.get("lieu") or "")
        print(off["url"])
        print(desc)
        print("-" * 60)


# ─── Filtrage et tri ──────────────────────────────────────────────────────────

def filtrer_offres_pertinentes(offres: list[dict]) -> list[dict]:
    return [
        o for o in offres
        if is_offer_within_max_age(o.get("date_pub", ""))
        and is_relevant_offer(o["titre"], o["description"], o.get("contrat", ""))
    ]


def trier_offres_par_priorite(offres: list[dict]) -> None:
    offres.sort(key=lambda o: (
        0 if motion_en_priorite(o["titre"], o["description"]) else 1,
        o["titre"].lower(),
    ))


def build_pipeline_diagnostic() -> dict:
    state = refresh_scan_state_from_exports()
    offres = _lire_csv_rows()
    historique = charger_historique_postulations()
    lettres = _charger_lettres()
    scores = _charger_scores()
    signatures = [offer_key_from_export_row(offre) for offre in offres]
    duplicate_count = len(signatures) - len(set(signatures))

    by_status: dict[str, int] = {}
    unknown_status = 0
    refus_ids: set[str] = set()
    try:
        refus_path = EXPORT_ROOT / "offres_refusees.json"
        if refus_path.exists():
            loaded_refus = json.loads(refus_path.read_text(encoding="utf-8"))
            if isinstance(loaded_refus, list):
                refus_ids = {str(x) for x in loaded_refus}
    except Exception:
        refus_ids = set()

    histo_status_by_key = {
        str(row.get("offer_signature") or _offer_key_from_parts(
            row.get("id_adzuna", ""),
            row.get("url", ""),
            row.get("titre", ""),
            row.get("entreprise", ""),
            row.get("source", ""),
            row.get("lieu", ""),
        )): normalize_status(row.get("statut"))
        for row in historique
    }
    new_offer_keys = set(((state.get("last_scan") or {}).get("new_offer_keys", []) or []))
    for offre in offres:
        key = offer_key_from_export_row(offre)
        record = (state.get("offers") or {}).get(key, {})
        status = _build_pipeline_status(record, key, key in new_offer_keys, refus_ids, histo_status_by_key)
        if status not in PIPELINE_STATUSES:
            unknown_status += 1
        by_status[status] = by_status.get(status, 0) + 1

    offers_set = set(signatures)
    score_keys = set(scores.keys())
    letter_keys = set(lettres.keys())
    histo_keys = {str(row.get("offer_signature") or _offer_key_from_parts(row.get("id_adzuna", ""), row.get("url", ""), row.get("titre", ""), row.get("entreprise", ""))) for row in historique}

    return {
        "total_offres": len(offres),
        "offres_par_statut": by_status,
        "offres_sans_score": len([sig for sig in offers_set if sig not in score_keys]),
        "offres_sans_lettre": len([sig for sig in offers_set if sig not in letter_keys]),
        "offres_avec_statut_inconnu": unknown_status,
        "doublons_probables": duplicate_count,
        "scores_sans_offre": sorted(score_keys - offers_set)[:50],
        "lettres_sans_offre": sorted(letter_keys - offers_set)[:50],
        "historique_sans_offre": sorted(histo_keys - offers_set)[:50],
        "scan_state_sans_offre": sorted(set((state.get("offers") or {}).keys()) - offers_set)[:50],
    }


def afficher_diagnostic() -> None:
    diagnostic = build_pipeline_diagnostic()
    print(json.dumps(diagnostic, ensure_ascii=False, indent=2))


# ─── CLI ─────────────────────────────────────────────────────────────────────

def cli_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Offres alternance — export CSV + lettres IA.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--ignorer-historique", action="store_true",
        help="Affiche aussi les offres déjà dans historique_postulations.json.",
    )
    sub = p.add_subparsers(dest="cmd", metavar="{marquer,historique,lettres,diagnostic,test-ft}")

    pm = sub.add_parser("marquer", help="Enregistrer une postulation manuelle.")
    pm.add_argument("--id",    default="", help="ID de l'annonce")
    pm.add_argument("--url",   default="", help="Lien de l'annonce")
    pm.add_argument("--titre", default="", help="Intitulé du poste")

    sub.add_parser("historique", help="Afficher les postulations enregistrées.")
    sub.add_parser("lettres",    help="Générer les lettres de motivation (Groq).")
    sub.add_parser("diagnostic", help="Verifier la coherence du pipeline et des fichiers JSON.")
    sub.add_parser("test-ft",    help="Tester la configuration France Travail.")
    ppost = sub.add_parser("postuler", help="Lancer l'agent de candidature automatisée.")
    ppost.add_argument(
        "--auto",
        action="store_true",
        help="Lance l'agent sans demander de confirmation avant envoi.",
    )

    return p


def tester_france_travail() -> None:
    from sources.france_travail import CLIENT_ID, CLIENT_SECRET, _get_token
    print("=== Test France Travail ===")
    print(f"FT_CLIENT_ID    : {'OK' if CLIENT_ID else 'ABSENT'}")
    print(f"FT_CLIENT_SECRET: {'OK' if CLIENT_SECRET else 'ABSENT'}")
    token, statut = _get_token()
    print(f"Statut token    : {statut}")
    print(f"Token obtenu    : {'OUI' if token else 'NON'}")
    if token:
        from sources.france_travail import recuperer
        offres = recuperer()
        print(f"Offres récupérées : {len(offres)}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main(ignorer_historique: bool = False) -> None:
    state = refresh_scan_state_from_exports()
    scan_started_at = _utc_now()
    detail = recuperer_toutes_offres_detail()
    offres = detail["offres"]
    n_brut = len(offres)
    seen_before = set(state.get("seen_offer_keys", []))
    new_offer_keys: list[str] = []
    source_new_counts: dict[str, int] = {}

    for offre in offres:
        offer_key_value = offer_key(offre)
        record = _get_offer_record(state, offer_key_value)
        record["id"] = offre.get("id", "")
        record["title"] = offre.get("titre", "")
        record["company"] = offre.get("entreprise", "")
        record["url"] = offre.get("url", "")
        record["source"] = offre.get("source", "")
        record["description_preview"] = (offre.get("description") or "")[:400]
        record["date_pub"] = offre.get("date_pub", "")
        record["times_seen"] = int(record.get("times_seen") or 0) + 1
        record["last_scan_at"] = scan_started_at
        record["last_seen_at"] = scan_started_at
        if not record.get("first_seen_at"):
            record["first_seen_at"] = scan_started_at
        _append_unique(record["sources"], offre.get("source", ""))
        if offer_key_value not in seen_before:
            new_offer_keys.append(offer_key_value)
            source_name = offre.get("source", "")
            source_new_counts[source_name] = source_new_counts.get(source_name, 0) + 1
        _append_unique(state["seen_offer_keys"], offer_key_value)

    def finaliser_scan(exported_offers: int, status: str, extra_errors: list[str] | None = None) -> None:
        errors = [f"{nom}: {meta['error']}" for nom, meta in detail["sources"].items() if meta.get("error")]
        if extra_errors:
            errors.extend(extra_errors)
        finished_at = _utc_now()
        sources_scanned = {}
        for nom, meta in detail["sources"].items():
            source_entry = dict(meta)
            source_entry["source"] = nom
            source_entry["new_offers"] = source_new_counts.get(nom, 0)
            source_entry["duplicates"] = int(meta.get("duplicates", 0) or 0)
            source_entry["offers_found"] = int(meta.get("count", 0) or 0)
            source_entry["error_message"] = meta.get("error", "")
            source_entry["timestamp"] = meta.get("timestamp") or finished_at
            if source_entry.get("status") == "ok" and source_entry["new_offers"] == 0 and source_entry["offers_found"] > 0:
                source_entry["status"] = "partiel"
            sources_scanned[nom] = source_entry
        state["last_scan_at"] = scan_started_at
        state["last_success_at"] = finished_at
        state["last_scan"] = {
            "started_at": scan_started_at,
            "finished_at": finished_at,
            "status": status,
            "offers_found": detail["total_deduped"],
            "new_offers": len(new_offer_keys),
            "new_offer_keys": new_offer_keys[-1000:],
            "duplicates_ignored": detail["duplicates_removed"],
            "exported_offers": exported_offers,
            "sources_scanned": sources_scanned,
            "errors": errors,
        }
        state["history"].append(state["last_scan"])
        save_scan_state(state)

    offres, nb_hors_idf = filtrer_zone_idf(offres)
    if nb_hors_idf:
        print(f"   {nb_hors_idf} exclue(s) : hors Ile-de-France (ou remote non inclus).")
    print(f"   -> {len(offres)} annonce(s) apres filtre geographique.\n")

    if not offres:
        print("Aucune annonce retenue après filtre géographique.")
        finaliser_scan(0, "completed_with_no_idf_results")
        return

    offres_filtrees = filtrer_offres_pertinentes(offres)
    trier_offres_par_priorite(offres_filtrees)

    avant_historique = len(offres_filtrees)
    if ignorer_historique:
        print("Historique ignoré : affichage de toutes les offres filtrées.\n")
    else:
        offres_filtrees, nb_deja = filtrer_deja_postule(offres_filtrees)
        if nb_deja:
            print(f"   {nb_deja} offre(s) ignorée(s) : déjà dans l'historique de postulation.\n")

    if not offres_filtrees and avant_historique > 0:
        print("Toutes les offres pertinentes sont déjà dans l'historique.")
        finaliser_scan(0, "completed_no_new_export")
        return

    if not offres_filtrees:
        print(
            f"{len(offres)} offre(s) en IDF, aucune ne passe le filtre métier "
            "(alternance + design/motion)."
        )
        finaliser_scan(0, "completed_no_matching_offers")
        return

    csv_ecrit = exporter_csv(offres_filtrees, CSV_PATH)
    generer_fichiers_candidature(offres_filtrees, MESSAGES_DIR)

    refus_ids: set[str] = set()
    try:
        refus_path = EXPORT_ROOT / "offres_refusees.json"
        if refus_path.exists():
            loaded_refus = json.loads(refus_path.read_text(encoding="utf-8"))
            if isinstance(loaded_refus, list):
                refus_ids = {str(x) for x in loaded_refus}
    except Exception:
        refus_ids = set()

    histo = charger_historique_postulations()
    histo_status_by_key = {
        str(r.get("offer_signature") or _offer_key_from_parts(
            r.get("id_adzuna", ""),
            r.get("url", ""),
            r.get("titre", ""),
            r.get("entreprise", ""),
        )): normalize_status(r.get("statut"))
        for r in histo
    }
    exported_keys = []
    new_offer_keys_set = set(new_offer_keys)
    for offre in offres_filtrees:
        offer_key_value = offer_key(offre)
        exported_keys.append(offer_key_value)
        record = _get_offer_record(state, offer_key_value)
        record["status"] = _build_pipeline_status(
            record,
            offer_key_value,
            offer_key_value in new_offer_keys_set,
            refus_ids,
            histo_status_by_key,
        )

    finaliser_scan(
        len(offres_filtrees),
        "completed_with_warnings" if any(meta.get("error") for meta in detail["sources"].values()) else "completed",
    )

    print(f"Scan state : {SCAN_STATE_PATH}")
    print(f"Nouvelles offres detectees : {len(new_offer_keys)}")
    print(f"CSV exporté : {csv_ecrit}")
    print(f"Squelettes  : {MESSAGES_DIR} ({len(offres_filtrees)} fichier(s))\n")

    afficher_offres(offres_filtrees)


if __name__ == "__main__":
    parser = cli_parser()
    args   = parser.parse_args()

    if args.cmd == "marquer":
        ajouter_postulation(args.id, args.url, args.titre)
    elif args.cmd == "historique":
        afficher_historique_postulations()
    elif args.cmd == "lettres":
        generer_lettres_batch()
    elif args.cmd == "diagnostic":
        afficher_diagnostic()
    elif args.cmd == "test-ft":
        tester_france_travail()
    elif args.cmd == "postuler":
        from agent_candidature import lancer_agent
        lancer_agent(confirmation=not getattr(args, "auto", False))
    else:
        main(ignorer_historique=args.ignorer_historique)

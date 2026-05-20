"""
Agent de candidature automatisée — moteur Google Gemini (gratuit).

Flux :
  1. Lit le CSV généré par main.py
  2. Pour chaque offre non encore traitée, ouvre la page dans un navigateur visible
  3. Envoie un screenshot à Gemini Flash (vision gratuite) pour détecter la méthode de candidature
  4. Remplit les champs / uploade le CV / génère une lettre de motivation
  5. Demande confirmation avant d'envoyer (mode par défaut)
  6. Enregistre le statut + date de relance dans historique_postulations.json

Clé gratuite : https://aistudio.google.com  →  "Get API key"

Commandes (via main.py) :
  python main.py postuler          ← agent avec confirmation manuelle
  python main.py postuler --auto   ← mode automatique SANS confirmation (prudence)
  python main.py suivi             ← statut des candidatures + relances à faire
"""

import base64
import csv
import hashlib
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
try:
    from google import genai
except Exception:
    genai = None

load_dotenv(Path(__file__).parent / ".env")

# Quand lancé depuis l'interface web, les input() sont ignorés (mode non-interactif)
WEB_MODE = os.environ.get("ALTERNANCE_WEB_MODE") == "1"

from PIL import Image
from playwright.sync_api import Browser, Page, sync_playwright

# ── Import depuis main.py ────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from main import (
    CSV_DELIMITEUR,
    CSV_PATH,
    HISTORIQUE_POSTULATIONS,
    LETTRES_PATH,
    _charger_lettres,
    _sauvegarder_lettre,
    charger_historique_postulations,
    offer_key_from_export_row,
    normaliser_url,
    _index_historique,
)

# ── Infos candidat ───────────────────────────────────────────────────────────
CANDIDAT = {
    "prenom":    os.environ.get("CANDIDAT_PRENOM",    "Julien"),
    "nom":       os.environ.get("CANDIDAT_NOM",       "Ledouble"),
    "email":     os.environ.get("CANDIDAT_EMAIL",     "julienledouble@gmail.com"),
    "tel":       os.environ.get("CANDIDAT_TEL",       "06 26 48 18 01"),
    "portfolio": os.environ.get("CANDIDAT_PORTFOLIO", "https://julienledouble-lab.github.io/"),
    "cv_path":   os.environ.get("CV_PATH",            "C:/Users/Ju/Desktop/cv final.pdf"),
}

# ── Gestion des comptes créés par l'agent ────────────────────────────────────
_BASE = Path(__file__).parent
COMPTES_PATH = _BASE / "comptes_crees.json"

_MOTS_INSCRIPTION = [
    "créer un compte", "create account", "sign up", "s'inscrire", "register",
    "inscription", "nouveau compte", "créez votre compte", "create your account",
]
_MOTS_CONNEXION = [
    "connexion requise", "login required", "se connecter", "log in",
    "connectez-vous", "already have an account", "déjà un compte",
]
_MOTS_VERIF_EMAIL = [
    "vérifiez votre email", "verify your email", "confirm your email",
    "check your inbox", "vérifiez votre boîte", "lien de confirmation",
    "confirmation link", "activation link",
]
_SELECTEURS_CAPTCHA = [
    'iframe[src*="recaptcha"]',
    'iframe[src*="hcaptcha"]',
    'iframe[title*="captcha" i]',
    '.g-recaptcha',
    '.h-captcha',
    '[data-sitekey]',
    '#captcha:visible',
]

_SELECTEURS_COOKIES_REFUS = [
    # Onetrust
    '#onetrust-reject-all-handler',
    'button#onetrust-reject-all-handler',
    # Didomi
    '#didomi-notice-disagree-button',
    'button[id*="didomi-notice-disagree"]',
    # Axeptio
    '.__axeptio_btn_dismiss',
    # Tarteaucitron
    '#tarteaucitronDenyAllCta',
    '.tarteaucitronDeny',
    # Usercentrics
    '[data-testid="uc-deny-all-button"]',
    # Texte français
    'button:has-text("Tout refuser")',
    'button:has-text("Refuser tout")',
    'button:has-text("Refuser")',
    'button:has-text("Je refuse")',
    'button:has-text("Refuser les cookies")',
    'button:has-text("Non merci")',
    'button:has-text("Refuser et fermer")',
    'button:has-text("Continuer sans accepter")',
    'button:has-text("Continuer sans consentir")',
    # Texte anglais
    'button:has-text("Reject all")',
    'button:has-text("Decline all")',
    'button:has-text("Refuse all")',
    'button:has-text("No thanks")',
    # Attributs génériques
    'button[id*="reject-all" i]',
    'button[id*="decline-all" i]',
    '[aria-label*="refuser" i]',
    '[aria-label*="reject all" i]',
    '[data-qa-id="reject-all"]',
]

_BOUTONS_GOOGLE_SIGNIN = [
    'button:has-text("Continuer avec Google")',
    'button:has-text("Se connecter avec Google")',
    'button:has-text("S\'inscrire avec Google")',
    'button:has-text("Continue with Google")',
    'button:has-text("Sign in with Google")',
    'button:has-text("Sign up with Google")',
    'a:has-text("Se connecter avec Google")',
    'a:has-text("Continuer avec Google")',
    'a:has-text("Sign in with Google")',
    '[data-provider="google"]',
    'button[class*="google-login" i]',
    'div[class*="google-signin" i]',
    '.google-oauth-button',
]


def _refuser_cookies(page: Page) -> bool:
    """Détecte et clique sur 'Refuser' dans les bannières cookies. Retourne True si réussi."""
    page.wait_for_timeout(1200)
    for sel in _SELECTEURS_COOKIES_REFUS:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=400):
                el.click()
                print("    ✓ Cookies refusés automatiquement")
                page.wait_for_timeout(600)
                return True
        except Exception:
            pass
    return False


def _tenter_connexion_google(page: Page, ctx) -> bool:
    """Clique sur 'Se connecter avec Google' si disponible et gère le flux OAuth."""
    for sel in _BOUTONS_GOOGLE_SIGNIN:
        try:
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_visible(timeout=600):
                print("  Connexion Google détectée → clic…")
                try:
                    with page.expect_popup(timeout=6000) as popup_info:
                        btn.click()
                    popup = popup_info.value
                    popup.wait_for_load_state("domcontentloaded", timeout=15_000)
                    try:
                        popup.wait_for_event("close", timeout=60_000)
                        print("  ✓ Connexion Google terminée")
                    except Exception:
                        print("  ⚠  Le popup Google est resté ouvert.")
                        input("  → Finalise la connexion dans le navigateur, puis Entrée : ")
                except Exception:
                    btn.click()
                    page.wait_for_load_state("domcontentloaded", timeout=15_000)
                    print("  ✓ Connexion Google (navigation directe)")
                page.wait_for_timeout(2000)
                return True
        except Exception:
            pass
    return False


def _domaine(url: str) -> str:
    return urlparse(url).netloc.replace("www.", "")


def _charger_comptes() -> dict:
    if COMPTES_PATH.exists():
        try:
            return json.loads(COMPTES_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _sauvegarder_compte(domaine: str, email: str, mdp: str) -> None:
    comptes = _charger_comptes()
    comptes[domaine] = {"email": email, "mdp": mdp, "date": datetime.now().strftime("%Y-%m-%d %H:%M")}
    COMPTES_PATH.write_text(json.dumps(comptes, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"    ✓ Compte sauvegardé pour {domaine} dans comptes_crees.json")


def _generer_mdp(domaine: str) -> str:
    """Génère un mot de passe fort et reproductible par domaine."""
    base = hashlib.sha256(f"JulienLedouble{domaine}alternance2026".encode()).hexdigest()[:10]
    return f"Jl{base.capitalize()}!9"


def _captcha_visible(page: Page) -> bool:
    """True uniquement si un CAPTCHA est visuellement présent sur la page."""
    for sel in _SELECTEURS_CAPTCHA:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=500):
                return True
        except Exception:
            pass
    return False


def _creer_compte(page: Page, ctx) -> bool:
    """
    Tente de créer un compte sur la page courante (Google en priorité).
    Retourne True si le compte a été créé (ou existait déjà), False sinon.
    """
    domaine = _domaine(page.url)
    comptes = _charger_comptes()
    email = CANDIDAT["email"]
    mdp = comptes.get(domaine, {}).get("mdp") or _generer_mdp(domaine)

    # ── Google sign-in en priorité ────────────────────────────────────────
    if _tenter_connexion_google(page, ctx):
        _sauvegarder_compte(domaine, email, "via-google")
        return True

    print(f"\n  Tentative de création de compte sur {domaine}…")
    print(f"  Email : {email}  |  Mot de passe : {mdp}")

    html = page.content().lower()

    # ── Remplir les champs d'inscription ─────────────────────────────────
    champs_inscription = [
        (["input[name*='firstname' i]", "input[name*='prenom' i]", "input[id*='firstname' i]",
          "input[placeholder*='prénom' i]", "input[placeholder*='first' i]"],
         CANDIDAT["prenom"], "Prénom"),
        (["input[name*='lastname' i]", "input[name*='nom' i]", "input[id*='lastname' i]",
          "input[placeholder*='nom' i]", "input[placeholder*='last' i]"],
         CANDIDAT["nom"], "Nom"),
        (["input[name*='fullname' i]", "input[placeholder*='nom complet' i]",
          "input[placeholder*='full name' i]"],
         f"{CANDIDAT['prenom']} {CANDIDAT['nom']}", "Nom complet"),
        (["input[type='email']", "input[name*='email' i]", "input[placeholder*='email' i]"],
         email, "Email"),
        (["input[type='tel']", "input[name*='phone' i]", "input[name*='tel' i]"],
         CANDIDAT["tel"], "Téléphone"),
        (["input[type='password'][name*='confirm' i]", "input[id*='confirm' i]",
          "input[name*='confirm' i]", "input[placeholder*='confirmer' i]",
          "input[placeholder*='confirm' i]", "input[placeholder*='répéter' i]"],
         mdp, "Confirmation mot de passe"),
        (["input[type='password']"],
         mdp, "Mot de passe"),
    ]

    remplis = 0
    for selecteurs, valeur, label in champs_inscription:
        for sel in selecteurs:
            try:
                el = page.locator(sel).first
                if el.count() > 0 and el.is_visible(timeout=800):
                    el.fill(valeur)
                    print(f"    ✓ {label}")
                    remplis += 1
                    break
            except Exception:
                pass

    # Cases à cocher CGU / RGPD
    for cb in page.locator("input[type='checkbox']").all():
        try:
            if cb.is_visible(timeout=300) and not cb.is_checked():
                label = _label_du_champ(page, cb).lower()
                if any(k in label for k in ["cgu", "terms", "condition", "privacy", "rgpd", "accept", "consent"]):
                    cb.check()
                    print(f"    ✓ Case cochée : {label or 'CGU'}")
        except Exception:
            pass

    if remplis == 0:
        print("  Aucun champ d'inscription rempli.")
        return False

    # ── Soumettre l'inscription ───────────────────────────────────────────
    for sel in ['button[type="submit"]', 'button:has-text("S\'inscrire")',
                'button:has-text("Créer")', 'button:has-text("Register")',
                'button:has-text("Sign up")', 'button:has-text("Create")',
                'input[type="submit"]']:
        try:
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_visible(timeout=800):
                btn.click()
                page.wait_for_timeout(3000)
                break
        except Exception:
            pass

    # ── Vérification email requise ────────────────────────────────────────
    html3 = page.content().lower()
    if any(m in html3 for m in _MOTS_VERIF_EMAIL):
        print(f"\n  📧  Vérification email requise.")
        print(f"  → Ouvre ta boîte Gmail ({email}) dans le navigateur et clique sur le lien.")
        # Ouvre Gmail dans un nouvel onglet
        gmail_page = ctx.new_page()
        gmail_page.goto("https://mail.google.com", wait_until="domcontentloaded", timeout=15_000)
        input("  → Reviens ici après avoir cliqué sur le lien de confirmation (Entrée) : ")
        try:
            gmail_page.close()
        except Exception:
            pass
        page.wait_for_timeout(2000)

    _sauvegarder_compte(domaine, email, mdp)
    print(f"  ✓ Compte créé sur {domaine}")
    return True


_CLIENT: Any | None = None
_MODELE = "gemini-2.0-flash-lite"


def _get_client():
    global _CLIENT
    if _CLIENT is None:
        if genai is None:
            raise RuntimeError("google-genai non installe")
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY manquante")
        _CLIENT = genai.Client(api_key=api_key)
    return _CLIENT


def _appel_gemini(contents, tentatives: int = 3):
    """Appel Gemini avec retry automatique sur erreur 429 (rate limit)."""
    for i in range(tentatives):
        try:
            return _get_client().models.generate_content(model=_MODELE, contents=contents)
        except Exception as e:
            msg = str(e)
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                if "limit: 0" in msg:
                    print(
                        "\n  Quota Gemini = 0 sur ce projet.\n"
                        "  → Va sur https://aistudio.google.com/apikey\n"
                        "  → Crée une nouvelle clé API directement depuis AI Studio\n"
                        "     (pas depuis Google Cloud Console)\n"
                        "  → Remplace $env:GEMINI_API_KEY par la nouvelle clé\n"
                    )
                    raise
                # Extraire le délai suggéré par l'API (max 120s)
                match = re.search(r'retryDelay["\s:]+(\d+)', msg)
                if not match:
                    match = re.search(r"retry in (\d+)", msg, re.IGNORECASE)
                attente = min(int(match.group(1)) + 5, 120) if match else 60
                print(f"  Rate limit Gemini — attente {attente}s avant retry ({i+1}/{tentatives})…")
                time.sleep(attente)
            else:
                raise
    raise RuntimeError("Gemini : trop de tentatives échouées (rate limit persistant).")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _screenshot_pil(page: Page) -> Image.Image:
    """Capture la page et retourne une image PIL (pour Gemini vision)."""
    img_bytes = page.screenshot(full_page=False)
    return Image.open(io.BytesIO(img_bytes))


def _lire_csv() -> list[dict]:
    if not CSV_PATH.exists():
        print(f"CSV introuvable : {CSV_PATH}\nLance d'abord : python main.py")
        sys.exit(1)
    with CSV_PATH.open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f, delimiter=CSV_DELIMITEUR))


# ── Analyse de page (heuristique HTML + Gemini optionnel) ────────────────────

def _analyser_page_heuristique(page: Page, url: str) -> dict:
    """Détection sans IA : analyse le HTML pour trouver formulaires, boutons, emails."""
    html = page.content().lower()
    url_lower = url.lower()

    # Détection plateforme externe
    if "linkedin.com" in url_lower:
        return {"methode": "linkedin", "description": "Page LinkedIn", "peut_automatiser": False, "raison_si_non": "LinkedIn", "email_contact": None, "texte_bouton": None, "champs": []}
    if "indeed.com" in url_lower:
        return {"methode": "indeed", "description": "Page Indeed", "peut_automatiser": False, "raison_si_non": "Indeed", "email_contact": None, "texte_bouton": None, "champs": []}

    # Email dans la page
    emails = re.findall(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", page.content())
    emails = [e for e in emails if not e.endswith((".png", ".jpg", ".svg"))]
    email_contact = emails[0] if emails else None

    # Formulaire présent ?
    a_formulaire = "<form" in html
    a_input = 'type="text"' in html or 'type="email"' in html or "<input" in html
    a_file = 'type="file"' in html

    # Bouton postuler visible ?
    mots_bouton = ["postuler", "candidater", "apply", "envoyer", "soumettre", "submit", "je postule"]
    texte_bouton = None
    for mot in mots_bouton:
        if mot in html:
            texte_bouton = mot.capitalize()
            break

    champs = []
    if a_input:
        for mot in ["nom", "prénom", "email", "téléphone", "cv", "lettre", "motivation"]:
            if mot in html:
                champs.append(mot)

    if a_formulaire and a_input:
        return {"methode": "formulaire", "description": f"Formulaire détecté ({len(champs)} champ(s))", "peut_automatiser": True, "raison_si_non": None, "email_contact": email_contact, "texte_bouton": texte_bouton, "champs": champs}
    if texte_bouton:
        return {"methode": "bouton_postuler", "description": f"Bouton « {texte_bouton} » détecté", "peut_automatiser": True, "raison_si_non": None, "email_contact": email_contact, "texte_bouton": texte_bouton, "champs": champs}
    if email_contact:
        return {"methode": "email", "description": f"Email trouvé : {email_contact}", "peut_automatiser": True, "raison_si_non": None, "email_contact": email_contact, "texte_bouton": None, "champs": []}

    return {"methode": "inconnu", "description": "Aucune méthode détectée", "peut_automatiser": False, "raison_si_non": "Ni formulaire, ni bouton, ni email trouvé", "email_contact": None, "texte_bouton": None, "champs": []}


def analyser_page(page: Page, url: str, titre: str) -> dict:
    """Tente Gemini (vision), repli automatique sur heuristique HTML si indisponible."""
    try:
        img = _screenshot_pil(page)
        prompt = (
            f"Tu analyses une page web d'offre d'emploi.\nOffre : {titre}\nURL : {url}\n\n"
            "Réponds UNIQUEMENT en JSON valide (sans markdown) :\n"
            '{"methode":"formulaire|email|bouton_postuler|linkedin|indeed|inconnu",'
            '"description":"...","email_contact":null,"texte_bouton":null,'
            '"champs":[],"peut_automatiser":true,"raison_si_non":null}'
        )
        resp = _appel_gemini([img, prompt])
        texte = resp.text.strip()
        if "```" in texte:
            parties = texte.split("```")
            for partie in parties:
                partie = partie.strip().lstrip("json").strip()
                if partie.startswith("{"):
                    texte = partie
                    break
        return json.loads(texte)
    except Exception:
        print("  (Gemini indisponible → analyse HTML)")
        return _analyser_page_heuristique(page, url)


# ── Génération de lettre de motivation ───────────────────────────────────────

def generer_lettre(titre: str, entreprise: str, description: str, offre_id: str = "") -> str:
    # Utilise la lettre pré-générée si disponible (depuis le tableau de bord)
    if offre_id:
        lettres = _charger_lettres()
        if offre_id in lettres and lettres[offre_id].get("lettre"):
            print("    ✓ Lettre personnalisée récupérée (pré-générée)")
            return lettres[offre_id]["lettre"]

    prompt = (
        "Écris une lettre de motivation courte (3 paragraphes, ~150 mots) pour cette alternance.\n"
        "Candidat : Julien Ledouble — étudiant en motion design / design graphique.\n"
        "Portfolio : https://julienledouble-lab.github.io/\n\n"
        f"Offre : {titre}\nEntreprise : {entreprise}\n"
        f"Description : {description[:600]}\n\n"
        "Ton sobre et professionnel. Mets en avant la passion pour la vidéo et l'animation.\n"
        "Commence directement par « Madame, Monsieur, »."
    )
    try:
        resp = _appel_gemini(prompt)
        lettre = resp.text.strip()
        if offre_id:
            _sauvegarder_lettre(offre_id, lettre, titre, entreprise)
        return lettre
    except Exception:
        return (
            f"Madame, Monsieur,\n\n"
            f"Je me permets de vous adresser ma candidature pour le poste de {titre} au sein de {entreprise}.\n\n"
            f"Actuellement en formation en motion design et design graphique, je suis passionné par la création "
            f"visuelle, l'animation et la vidéo. Mon portfolio illustre mes compétences : "
            f"https://julienledouble-lab.github.io/\n\n"
            f"Je serais ravi d'échanger avec vous sur cette opportunité.\n\n"
            f"Cordialement,\nJulien Ledouble\njulienledouble@gmail.com — 06 26 48 18 01"
        )


# ── Historique / suivi ───────────────────────────────────────────────────────

def _sauvegarder(id_adzuna: str, url: str, titre: str, statut: str, notes: str = "") -> None:
    rec = charger_historique_postulations()
    nu = normaliser_url(url)
    date_relance = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d") if statut == "envoyé" else None
    entree = {
        "id_adzuna": id_adzuna,
        "url": url,
        "titre": titre,
        "date_postulation": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "statut": statut,
        "notes": notes,
        "date_relance_prevue": date_relance,
    }
    for i, r in enumerate(rec):
        meme_id = id_adzuna and str(r.get("id_adzuna", "")) == id_adzuna
        meme_url = nu and normaliser_url(r.get("url", "")) == nu
        if meme_id or meme_url:
            rec[i] = entree
            break
    else:
        rec.append(entree)
    HISTORIQUE_POSTULATIONS.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")


def afficher_suivi() -> None:
    rec = charger_historique_postulations()
    if not rec:
        print("Aucune postulation enregistrée.")
        return

    aujourd_hui = datetime.now().strftime("%Y-%m-%d")
    icones = {"envoyé": "✓", "echec": "✗", "manuel_requis": "!", "ignoré": "–"}

    a_relancer = [
        r for r in rec
        if r.get("statut") == "envoyé" and r.get("date_relance_prevue", "9999") <= aujourd_hui
    ]

    if a_relancer:
        print(f"\n{'='*60}")
        print(f"  {len(a_relancer)} candidature(s) À RELANCER")
        print(f"{'='*60}")
        for r in a_relancer:
            print(f"  • {r.get('titre', '—')}  ({r.get('date_postulation', '—')})")
            print(f"    {r.get('url', '—')}")
        print()

    print(f"{'='*60}")
    print(f"  Historique complet ({len(rec)} candidature(s))")
    print(f"{'='*60}")
    for r in rec:
        s = r.get("statut", "?")
        print(f"  [{icones.get(s, '?')}] {r.get('titre', '—')}  —  {r.get('date_postulation', '—')}")
        if r.get("notes"):
            print(f"      {r['notes']}")
    print()


# ── Remplissage de formulaire ─────────────────────────────────────────────────

def _remplir_champ(page: Page, selecteurs: list[str], valeur: str, label: str) -> bool:
    for sel in selecteurs:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=1000):
                el.fill(valeur)
                print(f"    ✓ {label}")
                return True
        except Exception:
            pass
    return False


def _label_du_champ(page: Page, element) -> str:
    """Récupère le texte du label associé à un champ (for=id, parent, aria-label, placeholder)."""
    try:
        field_id = element.get_attribute("id")
        if field_id:
            label = page.locator(f"label[for='{field_id}']").first
            if label.count() > 0:
                return label.inner_text().strip().lower()
    except Exception:
        pass
    try:
        aria = element.get_attribute("aria-label") or ""
        if aria:
            return aria.lower()
    except Exception:
        pass
    try:
        placeholder = element.get_attribute("placeholder") or ""
        if placeholder:
            return placeholder.lower()
    except Exception:
        pass
    try:
        name = element.get_attribute("name") or ""
        return name.lower()
    except Exception:
        return ""


_LABELS_A_IGNORER = [
    "alerte", "alert", "newsletter", "abonnement", "subscribe", "notification",
    "recevoir les offres", "être informé", "informé des offres", "job alert",
    "offres similaires", "offres d'emploi par", "promotions", "actualités",
]


def _est_champ_a_ignorer(label: str, el, page: Page) -> bool:
    """True si le champ est pour une alerte/newsletter et ne doit pas être rempli."""
    l = label.lower()
    if any(k in l for k in _LABELS_A_IGNORER):
        return True
    # Vérifier le contexte autour du champ (section parente)
    try:
        section = el.evaluate("""el => {
            let p = el.parentElement;
            for (let i = 0; i < 4; i++) {
                if (!p) break;
                const t = (p.innerText || p.textContent || '').toLowerCase();
                if (t.includes('alerte') || t.includes('newsletter') || t.includes('alert')
                    || t.includes('notification') || t.includes('abonnement')) return true;
                p = p.parentElement;
            }
            return false;
        }""")
        if section:
            return True
    except Exception:
        pass
    return False


def _valeur_pour_label(label: str) -> str | None:
    """Associe un label à la valeur candidat correspondante."""
    l = label.lower()
    # Ne jamais remplir les champs d'alerte/newsletter
    if any(k in l for k in _LABELS_A_IGNORER):
        return None
    if any(k in l for k in ["prénom", "prenom", "first name", "firstname", "given"]):
        return CANDIDAT["prenom"]
    if any(k in l for k in ["nom", "last name", "lastname", "surname", "family"]) and "prénom" not in l and "prenom" not in l:
        return CANDIDAT["nom"]
    if any(k in l for k in ["nom complet", "full name", "fullname", "votre nom"]):
        return f"{CANDIDAT['prenom']} {CANDIDAT['nom']}"
    if any(k in l for k in ["email", "mail", "courriel", "e-mail"]):
        return CANDIDAT["email"]
    if any(k in l for k in ["téléphone", "telephone", "phone", "mobile", "portable", "tel"]):
        return CANDIDAT["tel"]
    if any(k in l for k in ["portfolio", "site web", "website", "linkedin", "url", "lien"]):
        return CANDIDAT["portfolio"]
    return None


def _remplir_formulaire(page: Page, titre: str, entreprise: str, description: str, offre_id: str = "") -> int:
    remplis = 0
    lettre = None

    # ── 1. Parcourir tous les inputs texte visibles ──────────────────────────
    inputs = page.locator("input[type='text'], input[type='email'], input[type='tel'], input:not([type])").all()
    for el in inputs:
        try:
            if not el.is_visible(timeout=500):
                continue
            label = _label_du_champ(page, el)
            if _est_champ_a_ignorer(label, el, page):
                print(f"    ⊘ Ignoré (alerte/newsletter) : {label or 'champ'}")
                continue
            valeur = _valeur_pour_label(label)
            if valeur:
                el.fill(valeur)
                print(f"    ✓ {label or 'champ'} → {valeur}")
                remplis += 1
        except Exception:
            pass

    # ── 2. Textareas ─────────────────────────────────────────────────────────
    textareas = page.locator("textarea").all()
    for ta in textareas:
        try:
            if not ta.is_visible(timeout=500):
                continue
            label = _label_du_champ(page, ta)
            # Lettre de motivation / message
            if any(k in label for k in ["lettre", "motivation", "message", "cover", "présent", "present", "why", "pourquoi", "à propos", "about"]):
                if lettre is None:
                    print("    Génération de la lettre de motivation…")
                    lettre = generer_lettre(titre, entreprise, description, offre_id)
                ta.fill(lettre)
                print(f"    ✓ {label or 'textarea'} → lettre de motivation")
                remplis += 1
            elif not label or len(label) < 3:
                # Textarea sans label clair → on y met la lettre si pas encore fait
                if lettre is None:
                    print("    Génération de la lettre de motivation…")
                    lettre = generer_lettre(titre, entreprise, description, offre_id)
                ta.fill(lettre)
                print(f"    ✓ textarea (sans label) → lettre de motivation")
                remplis += 1
        except Exception:
            pass

    # ── 3. Selects (menus déroulants) ────────────────────────────────────────
    selects = page.locator("select").all()
    for sel_el in selects:
        try:
            if not sel_el.is_visible(timeout=500):
                continue
            label = _label_du_champ(page, sel_el)
            # Niveau d'études / formation
            if any(k in label for k in ["formation", "niveau", "étude", "diplôme", "degree", "education"]):
                sel_el.select_option(index=1)
                print(f"    ✓ {label} → option 1 sélectionnée")
                remplis += 1
            # Disponibilité / date de début
            elif any(k in label for k in ["disponib", "début", "start", "date"]):
                sel_el.select_option(index=1)
                print(f"    ✓ {label} → option 1 sélectionnée")
                remplis += 1
        except Exception:
            pass

    # ── 4. Upload CV ─────────────────────────────────────────────────────────
    cv_path = Path(CANDIDAT["cv_path"])
    if cv_path.exists():
        file_inputs = page.locator('input[type="file"]').all()
        for fi in file_inputs:
            try:
                fi.set_input_files(str(cv_path))
                print(f"    ✓ CV uploadé ({cv_path.name})")
                remplis += 1
                break
            except Exception:
                pass
    else:
        print(f"    ⚠  CV introuvable : {cv_path}")

    # ── 5. Cases à cocher RGPD / conditions ──────────────────────────────────
    checkboxes = page.locator("input[type='checkbox']").all()
    for cb in checkboxes:
        try:
            if not cb.is_visible(timeout=500):
                continue
            label = _label_du_champ(page, cb)
            if any(k in label for k in ["rgpd", "cgu", "condition", "terms", "privacy", "consent", "accept", "accord", "politique"]):
                if not cb.is_checked():
                    cb.check()
                    print(f"    ✓ Case cochée : {label or 'RGPD/conditions'}")
                    remplis += 1
        except Exception:
            pass

    print(f"    → {remplis} champ(s) rempli(s) au total.")
    return remplis


_MOTS_LOGIN = [
    "créer un compte", "create account", "sign up", "s'inscrire",
    "connexion requise", "login required", "se connecter pour postuler",
    "log in to apply", "connectez-vous pour", "register to apply",
]
_PLATEFORMES = {
    "linkedin.com": "LinkedIn", "indeed.com": "Indeed",
    "workday.com": "Workday", "myworkdayjobs.com": "Workday",
    "taleo.net": "Taleo", "greenhouse.io": "Greenhouse",
    "lever.co": "Lever", "smartrecruiters.com": "SmartRecruiters",
}
_BOUTONS_SUIVANT = [
    "Suivant", "Next", "Continuer", "Continue", "Étape suivante",
    "Step next", "Suivante", "Próximo",
]
_BOUTONS_SUBMIT = [
    'button[type="submit"]', 'input[type="submit"]',
    'button:has-text("Envoyer ma candidature")', 'button:has-text("Soumettre")',
    'button:has-text("Candidater")', 'button:has-text("Submit")',
    'button:has-text("Apply")', 'button:has-text("Send")',
    'button:has-text("Envoyer")', 'button:has-text("Postuler")',
]


def _pause_manuelle(page: Page, message: str) -> str:
    """Affiche un message, laisse l'utilisateur agir dans le navigateur, retourne sa réponse."""
    print(f"\n{'─'*60}")
    print(message)
    print(f"   Page ouverte : {page.url[:80]}")
    print("─"*60)
    if WEB_MODE:
        print("  [Mode web : continuation automatique — interviens dans le navigateur si besoin]")
        page.wait_for_timeout(3000)
        return ""
    rep = input("  → Entrée pour continuer  /  q = ignorer cette offre : ").strip().lower()
    try:
        page.wait_for_load_state("domcontentloaded", timeout=8_000)
    except Exception:
        pass
    page.wait_for_timeout(1000)
    return rep


def _detecter_mur(page: Page) -> str | None:
    """Retourne le nom de la plateforme si un mur de connexion est détecté, sinon None."""
    url = page.url
    for domaine, nom in _PLATEFORMES.items():
        if domaine in url:
            return nom
    html = page.content().lower()
    if any(m in html for m in _MOTS_LOGIN):
        return "plateforme externe"
    return None


def _cliquer_suivant(page: Page) -> bool:
    """Clique sur le bouton 'Suivant/Next' si présent. Retourne True si cliqué."""
    for texte in _BOUTONS_SUIVANT:
        try:
            btn = page.get_by_role("button", name=texte, exact=False).first
            if btn.count() > 0 and btn.is_visible(timeout=500):
                btn.click()
                page.wait_for_timeout(2000)
                return True
        except Exception:
            pass
    return False


def _cliquer_submit(page: Page) -> bool:
    """Clique sur le bouton de soumission finale. Retourne True si cliqué."""
    for sel in _BOUTONS_SUBMIT:
        try:
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_visible(timeout=500):
                btn.click()
                page.wait_for_timeout(3000)
                return True
        except Exception:
            pass
    return False


def _parcourir_candidature(page: Page, titre: str, entreprise: str,
                           description: str, confirmation: bool,
                           ctx=None, offre_id: str = "") -> str:
    """
    Boucle multi-étapes :
      - détecte mur de connexion → pause manuelle
      - remplit les champs de l'étape courante
      - signale les champs vides → pause manuelle
      - clique sur Suivant ou Soumettre selon la page
    """
    MAX_ETAPES = 12

    for etape in range(1, MAX_ETAPES + 1):
        print(f"\n  [Étape {etape}] {page.url[:70]}")

        # ── Refus cookies ─────────────────────────────────────────────────
        _refuser_cookies(page)

        # ── Mur de connexion / inscription ───────────────────────────────
        html_etape = page.content().lower()
        besoin_inscription = any(m in html_etape for m in _MOTS_INSCRIPTION)
        besoin_connexion = any(m in html_etape for m in _MOTS_CONNEXION)
        plateforme = _detecter_mur(page)

        if besoin_inscription and ctx:
            print(f"  Formulaire d'inscription détecté sur {_domaine(page.url)}")
            comptes = _charger_comptes()
            domaine = _domaine(page.url)
            if domaine in comptes:
                print(f"  Compte existant trouvé ({comptes[domaine]['email']}) → connexion manuelle requise.")
                rep = _pause_manuelle(page, f"⚠  Connecte-toi avec ton compte existant sur {domaine}.\n   Email : {comptes[domaine]['email']}  |  MDP : {comptes[domaine]['mdp']}")
                if rep == "q":
                    return "ignoré"
            else:
                ok = _creer_compte(page, ctx)
                if not ok:
                    rep = _pause_manuelle(page, "⚠  Création de compte échouée — fais-le manuellement.")
                    if rep == "q":
                        return "ignoré"
        elif (besoin_connexion or plateforme) and not besoin_inscription:
            domaine = _domaine(page.url)
            comptes = _charger_comptes()
            # Essayer Google en priorité
            if ctx and _tenter_connexion_google(page, ctx):
                _sauvegarder_compte(domaine, CANDIDAT["email"], "via-google")
                page.wait_for_timeout(2000)
                continue
            if domaine in comptes:
                mdp = comptes[domaine]["mdp"]
                print(f"  Compte existant : {CANDIDAT['email']} / {mdp}")
                # Tenter connexion auto
                for sel, val in [
                    (["input[type='email']", "input[name*='email' i]"], CANDIDAT["email"]),
                    (["input[type='password']"], mdp),
                ]:
                    for s in sel:
                        try:
                            el = page.locator(s).first
                            if el.count() > 0 and el.is_visible(timeout=800):
                                el.fill(val)
                                break
                        except Exception:
                            pass
                for sel_btn in ['button[type="submit"]', 'button:has-text("Connexion")',
                                'button:has-text("Se connecter")', 'button:has-text("Login")']:
                    try:
                        btn = page.locator(sel_btn).first
                        if btn.count() > 0 and btn.is_visible(timeout=800):
                            btn.click()
                            page.wait_for_timeout(2000)
                            break
                    except Exception:
                        pass
            else:
                rep = _pause_manuelle(
                    page,
                    f"⚠  Connexion requise ({plateforme or domaine}).\n"
                    "   → Connecte-toi ou crée un compte dans le navigateur.\n"
                    "   → Reviens ici une fois sur le formulaire."
                )
                if rep == "q":
                    return "ignoré"
            if _detecter_mur(page):
                return "manuel_requis"

        # ── Remplissage auto ──────────────────────────────────────────────
        nb = _remplir_formulaire(page, titre, entreprise, description, offre_id)
        print(f"  {nb} champ(s) rempli(s) automatiquement.")

        # ── Champs encore vides → pause manuelle ──────────────────────────
        vides = _champs_vides(page)
        if vides:
            rep = _pause_manuelle(
                page,
                f"⚠  {len(vides)} champ(s) non rempli(s) : {', '.join(vides[:8])}\n"
                "   → Remplis-les manuellement dans le navigateur."
            )
            if rep == "q":
                return "ignoré"

        # ── Bouton Suivant → page suivante ────────────────────────────────
        if _cliquer_suivant(page):
            print("  → Page suivante…")
            continue

        # ── Bouton Soumettre → confirmation + envoi ───────────────────────
        if confirmation:
            print("\n[Vérification finale : regarde le navigateur]")
            rep = input("  Soumettre la candidature ? (o/n) : ").strip().lower()
            if rep != "o":
                return "ignoré"

        if _cliquer_submit(page):
            print("  Candidature soumise ✓")
            return "envoyé"

        # ── Aucun bouton trouvé → laisser la main ─────────────────────────
        rep = _pause_manuelle(
            page,
            "⚠  Aucun bouton Suivant/Soumettre détecté.\n"
            "   → Avance manuellement dans le formulaire puis reviens ici."
        )
        if rep == "q":
            return "ignoré"

    return "echec"


def _champs_vides(page: Page) -> list[str]:
    """Retourne les labels des champs visibles encore vides."""
    vides = []
    for el in page.locator(
        "input[type='text'], input[type='email'], input[type='tel'], input:not([type]), textarea"
    ).all():
        try:
            if not el.is_visible(timeout=300):
                continue
            if not (el.input_value() or "").strip():
                label = _label_du_champ(page, el) or el.get_attribute("name") or "?"
                vides.append(label)
        except Exception:
            pass
    return vides


# ── Traitement d'une offre ───────────────────────────────────────────────────

def _traiter_offre(offre: dict, ctx, confirmation: bool) -> str:
    url         = offre.get("Lien vers l'annonce", "")
    titre       = offre.get("Intitulé du poste", "(sans titre)")
    entreprise  = offre.get("Entreprise", "")
    description = offre.get("Description (texte complet)", "")
    id_adzuna   = offre.get("ID annonce", offre.get("ID annonce (Adzuna)", ""))

    print(f"\n{'─'*60}")
    print(f"Offre      : {titre}")
    print(f"Entreprise : {entreprise}")
    print(f"URL        : {url}")
    print(f"{'─'*60}")

    page = ctx.new_page()
    try:
        # ── 1. Navigation vers la page Adzuna ────────────────────────────
        print("Navigation…")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        except Exception:
            pass
        page.wait_for_timeout(2000)
        _refuser_cookies(page)

        # ── 2. Cliquer sur "Postuler" sur Adzuna pour atteindre le vrai site
        if "adzuna.fr" in page.url:
            print("  Page Adzuna → clic sur « Postuler »…")
            cliqué = False
            for sel in [
                'a:has-text("Postuler")', 'a:has-text("Apply")',
                'button:has-text("Postuler")', '[class*="apply" i]',
                '[data-testid*="apply" i]',
            ]:
                try:
                    btn = page.locator(sel).first
                    if btn.count() > 0 and btn.is_visible(timeout=2000):
                        href = btn.get_attribute("href") or ""
                        if href.startswith("http"):
                            page.goto(href, wait_until="domcontentloaded", timeout=20_000)
                        else:
                            with page.expect_navigation(wait_until="domcontentloaded", timeout=20_000):
                                btn.click()
                        page.wait_for_timeout(2000)
                        cliqué = True
                        break
                except Exception:
                    pass
            if not cliqué:
                print("  Bouton postuler non trouvé sur Adzuna.")

        try:
            page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except Exception:
            pass
        page.wait_for_timeout(1500)
        _refuser_cookies(page)

        # ── 3. Détecter email direct ──────────────────────────────────────
        analyse = analyser_page(page, page.url, titre)
        if analyse.get("methode") == "email":
            email_contact = analyse.get("email_contact")
            lettre = generer_lettre(titre, entreprise, description, id_adzuna)
            print(f"\nEmail de contact : {email_contact}")
            print("\n--- Lettre générée ---\n" + lettre + "\n--- Fin ---\n")
            if confirmation:
                rep = input(f"Ouvrir le client mail → {email_contact} ? (o/n) : ").strip().lower()
                if rep != "o":
                    _sauvegarder(id_adzuna, url, titre, "ignoré", "Non envoyé")
                    return "ignoré"
            import webbrowser
            sujet = f"Candidature alternance — {titre} — Julien Ledouble"
            corps = lettre.replace("\n", "%0A").replace(" ", "%20")
            webbrowser.open(f"mailto:{email_contact}?subject={sujet}&body={corps}")
            _sauvegarder(id_adzuna, url, titre, "envoyé", f"mailto:{email_contact}")
            return "envoyé"

        # ── 4. Boucle multi-pages de candidature ─────────────────────────
        statut = _parcourir_candidature(page, titre, entreprise, description, confirmation, ctx, offre_id=id_adzuna)
        _sauvegarder(id_adzuna, url, titre, statut, f"URL finale : {page.url[:80]}")
        return statut

    except SystemExit:
        _sauvegarder(id_adzuna, url, titre, "ignoré", "Arrêt utilisateur")
        return "ignoré"
    except Exception as e:
        print(f"Erreur inattendue : {e}")
        _sauvegarder(id_adzuna, url, titre, "echec", str(e))
        return "echec"
    finally:
        page.close()


# ── Point d'entrée ───────────────────────────────────────────────────────────

def lancer_agent(confirmation: bool = True) -> None:
    offres = _lire_csv()
    rec = charger_historique_postulations()
    signatures_vues, ids_vus, urls_vues = _index_historique(rec)

    a_traiter = [
        o for o in offres
        if not (
            (offer_key_from_export_row(o) and offer_key_from_export_row(o) in signatures_vues)
            or (o.get("ID annonce", "") and o.get("ID annonce", "") in ids_vus)
            or (o.get("ID annonce (Adzuna)", "") and o["ID annonce (Adzuna)"] in ids_vus)
            or normaliser_url(o.get("Lien vers l'annonce", "")) in urls_vues
        )
    ]

    print(f"\n{len(a_traiter)} offre(s) à traiter (sur {len(offres)} dans le CSV).")
    if not a_traiter:
        print("Toutes les offres ont déjà été traitées.")
        return

    stats: dict[str, int] = {"envoyé": 0, "echec": 0, "manuel_requis": 0, "ignoré": 0}

    # Profil persistant → Google et autres sessions sont mémorisés entre les runs
    profile_dir = str(_BASE / "browser_profile")
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=False,
            slow_mo=400,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            for i, offre in enumerate(a_traiter, 1):
                print(f"\n[{i}/{len(a_traiter)}]")
                if confirmation and i > 1:
                    rep = input("Passer à l'offre suivante ? (o/n/q=quitter) : ").strip().lower()
                    if rep in ("n", "q"):
                        print("Arrêt de l'agent.")
                        break
                statut = _traiter_offre(offre, ctx, confirmation)
                stats[statut] = stats.get(statut, 0) + 1
        finally:
            ctx.close()

    print(f"\n{'='*60}")
    print("Résumé de la session")
    print(f"  Envoyées      : {stats['envoyé']}")
    print(f"  Manuelles     : {stats['manuel_requis']}")
    print(f"  Ignorées      : {stats['ignoré']}")
    print(f"  Échecs        : {stats['echec']}")
    print(f"{'='*60}")

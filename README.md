# Alternance Auto

Alternance Auto est un outil local en Python/Flask pour rechercher, scorer et suivre des offres d'alternance, avec generation de lettres, pipeline de suivi et agent de candidature assiste.

## Fonctions principales

- scan multi-sources d'offres
- filtrage metier et geographique
- scoring de compatibilite
- dashboard Flask avec pipeline, historique et parametres
- generation de lettres via IA
- coach IA pour aider a definir la recherche
- agent de candidature Playwright pour les envois assistes

## Stack

- Python 3.12
- Flask
- Playwright
- Groq API
- Gemini API
- Bootstrap 5

## Installation locale

1. Cloner le depot
2. Creer un environnement virtuel Python
3. Installer les dependances :

```bash
pip install -r requirements.txt
playwright install
```

4. Copier `.env.example` en `.env`
5. Renseigner les cles API et le profil candidat
6. Lancer l'application :

```bash
python app.py
```

Puis ouvrir `http://127.0.0.1:5001`.

## Partage local simple

Pour un ami qui recupere le projet en local, le plus simple est :

1. double-cliquer sur `installer_partage_local.bat`
2. attendre la fin de l'installation
3. ouvrir l'application
4. completer l'onglet `Parametres`

Le lanceur `Alternance Auto` peut aussi ouvrir automatiquement l'installateur si l'environnement local n'est pas encore pret.

Si tu veux preparer un dossier propre a envoyer :

1. double-clique sur `preparer_pack_ami.bat`
2. recupere le dossier `partage_local/alternance-auto`
3. compresse ce dossier en `.zip`
4. envoie ce zip a ton ami

Le pack genere exclut les donnees perso et runtime :

- `.env`
- historique local
- exports JSON / CSV
- logs
- `.venv`

## Premier lancement

Pour un nouvel utilisateur, le plus important est de completer d'abord :

- le profil candidat
- un lien pro (`portfolio`, `LinkedIn` ou `GitHub`)
- le chemin du CV
- au moins une source d'offres
- Groq pour les lettres
- Gemini pour l'agent de candidature

L'interface affiche maintenant un bloc `Configuration initiale` pour aider a voir rapidement ce qu'il manque.

## Preparation hebergement

Le projet reste d'abord concu pour tourner en local. Pour preparer un futur hebergement sans casser le mode local :

- `wsgi.py` expose l'application Flask pour `gunicorn`
- `APP_HOST`, `APP_PORT`, `PORT` et `ALTERNANCE_CLOUD_MODE` permettent d'adapter le demarrage
- `STORAGE_BACKEND=local` conserve le stockage actuel sur fichiers
- en local, le comportement par defaut reste `127.0.0.1:5001`

Exemple de commande compatible cloud :

```bash
gunicorn wsgi:app
```

## Donnees locales

Les fichiers personnels et de runtime ne sont pas versionnes :

- `.env`
- exports JSON / CSV
- historique des candidatures
- logs
- profil navigateur Playwright

Un exemple de profil de recherche est fourni dans `export/profil_recherche.example.json`.

## Notes

- Le serveur est concu pour tourner en local sur `127.0.0.1`.
- L'agent de candidature automatique reste plus fiable en local qu'en hebergement serverless.

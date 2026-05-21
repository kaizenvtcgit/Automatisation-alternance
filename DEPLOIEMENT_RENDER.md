# Déploiement contrôlé sur Render

Ce lot sert à partager Alternance Auto avec quelques amis sans casser le mode local.

## Architecture recommandée

- `Render` : backend Flask public
- `Supabase` : base distante / miroir
- `GitHub` : dépôt source
- `Playwright` : reste local au début pour la candidature automatique

## Important

Le service Render a un filesystem éphémère. Les fichiers locaux peuvent disparaître à chaque redéploiement ou redémarrage.

Pour cette première phase :

- le tableau de bord en ligne sert à consulter et piloter
- `Supabase` sert de miroir durable
- l'agent Playwright reste plus fiable en local

## Ce qui est déjà prêt dans le repo

- `render.yaml`
- `.python-version`
- `wsgi.py`
- `healthz` sur `/healthz`
- mode cloud via `ALTERNANCE_CLOUD_MODE=1`
- verrou d'accès optionnel via `APP_SECRET`

## Étapes Render

1. Va sur Render et connecte ton dépôt GitHub.
2. Choisis `New` puis `Blueprint`.
3. Sélectionne le dépôt `Automatisation-alternance`.
4. Render détectera `render.yaml`.
5. Vérifie que le service proposé est :
   - `name`: `alternance-auto`
   - `runtime`: `python`
   - `plan`: `starter`
   - `region`: `frankfurt`
6. Ajoute les variables secrètes demandées :
   - `APP_SECRET`
   - `APP_SESSION_SECRET`
   - `SUPABASE_URL`
   - `SUPABASE_SERVICE_ROLE_KEY`
   - `FT_CLIENT_ID`
   - `FT_CLIENT_SECRET`
   - `ADZUNA_APP_ID`
   - `ADZUNA_APP_KEY`
   - `LBA_API_KEY`
   - `GROQ_API_KEY`
   - `GEMINI_API_KEY`
7. Lance le déploiement.

## Vérifications après déploiement

1. Ouvre l'URL Render.
2. Vérifie que le verrou d'accès demande bien le token `APP_SECRET`.
3. Vérifie `/healthz`.
4. Ouvre le dashboard.
5. Vérifie la carte `Sante du projet`.
6. Vérifie la carte `Synchroniser Supabase`.

## Conseils de prudence

- garde `SUPABASE_SYNC_AUTO=1`
- ne t'appuie pas sur le stockage local Render pour des données durables
- n'active pas l'arrêt serveur depuis l'interface en cloud : c'est déjà neutralisé
- garde l'agent de candidature sur machine locale tant que tu n'as pas un worker dédié

## Ce que je recommande pour les amis

Pour les premiers tests :

- partage l'URL Render
- donne le token `APP_SECRET` seulement aux personnes de confiance
- garde le local comme version maître au début
- utilise Supabase comme point de stabilisation

# Preparation Supabase

Cette etape ne branche encore rien en base distante. Elle documente seulement la cible de migration pour rester fidele au fonctionnement actuel.

## Principe

Le projet continue d'utiliser les fichiers locaux comme source principale :

- `historique_postulations.json`
- `export/lettres.json`
- `export/scores.json`
- `export/scan_state.json`
- `export/profil_recherche.json`
- `export/offres_refusees.json`
- `export/offres_filtrees.csv`

L'objectif est de migrer plus tard, par petites etapes, vers des tables Supabase sans casser les fonctionnalites existantes.

## Mapping recommande

- `export/offres_filtrees.csv` -> `offers`
- `export/scores.json` -> `offer_scores`
- `export/lettres.json` -> `offer_letters`
- `historique_postulations.json` -> `applications_history`
- `export/offres_refusees.json` -> `refused_offers`
- `export/scan_state.json` -> `scan_runs` + `scan_run_sources`
- `export/profil_recherche.json` -> `search_profiles`
- settings applicatifs / secrets non sensibles -> `app_settings`

## Choix de conception

- `signature` reste la cle principale metier des offres
- les payloads riches sont gardes en `jsonb` pour preserver la compatibilite
- les champs critiques restent aussi en colonnes dediees pour les filtres, tris et statistiques
- aucune fonctionnalite n'est supprimee : on garde le modele actuel, on le range juste mieux

## Strategie de migration recommandee

1. Garder `STORAGE_BACKEND=local`
2. Creer les tables Supabase avec `schema.sql`
3. Ajouter plus tard un script de synchronisation `local -> supabase`
4. Verifier que l'interface lit toujours les memes donnees
5. Passer eventuellement en double ecriture
6. Ne changer la source principale qu'une fois la synchronisation stable

## Ce qu'on ne migre pas tout de suite

- l'agent Playwright en execution
- les profils navigateur
- les logs temporaires
- les fichiers uploades sensibles sans strategie de stockage claire

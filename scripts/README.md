# Scripts

## `sync_to_supabase.py`

Script de preparation pour synchroniser les donnees locales vers Supabase.

Par defaut :

- il lit les fichiers locaux
- il construit les payloads cibles
- il affiche un apercu
- il n'envoie rien

Execution d'aperçu :

```bash
python scripts/sync_to_supabase.py
```

Execution reelle plus tard, uniquement quand le projet Supabase existera :

```bash
python scripts/sync_to_supabase.py --execute
```

Variables requises au moment de l'execution reelle :

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

create extension if not exists pgcrypto;

create table if not exists app_settings (
    key text primary key,
    value jsonb not null default '{}'::jsonb,
    updated_at timestamptz not null default now()
);

create table if not exists search_profiles (
    id uuid primary key default gen_random_uuid(),
    slug text not null unique,
    name text not null,
    is_active boolean not null default false,
    profile_data jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists offers (
    signature text primary key,
    source_offer_id text,
    source text,
    title text not null,
    company text,
    location text,
    offer_url text,
    published_at timestamptz,
    contract_type text,
    category text,
    detected_family text,
    found_query text,
    description text,
    pipeline_status text not null default 'a_analyser',
    is_refused boolean not null default false,
    first_seen_at timestamptz,
    last_seen_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists idx_offers_source on offers(source);
create index if not exists idx_offers_status on offers(pipeline_status);
create index if not exists idx_offers_published_at on offers(published_at desc);

create table if not exists offer_scores (
    offer_signature text primary key references offers(signature) on delete cascade,
    score integer not null default 0,
    level text not null default 'faible',
    score_payload jsonb not null default '{}'::jsonb,
    scored_at timestamptz,
    updated_at timestamptz not null default now()
);

create table if not exists offer_letters (
    offer_signature text primary key references offers(signature) on delete cascade,
    title text,
    company text,
    letter_text text not null,
    letter_payload jsonb not null default '{}'::jsonb,
    generated_at timestamptz,
    updated_at timestamptz not null default now()
);

create table if not exists applications_history (
    id uuid primary key default gen_random_uuid(),
    offer_signature text references offers(signature) on delete set null,
    source_offer_id text,
    offer_url text,
    title text not null,
    status text not null default 'a_analyser',
    notes text,
    applied_at timestamptz,
    followup_due_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists idx_applications_status on applications_history(status);
create index if not exists idx_applications_followup on applications_history(followup_due_at);

create table if not exists refused_offers (
    offer_signature text primary key references offers(signature) on delete cascade,
    refused_at timestamptz not null default now()
);

create table if not exists scan_runs (
    id uuid primary key default gen_random_uuid(),
    status text not null,
    started_at timestamptz,
    finished_at timestamptz,
    offers_found integer not null default 0,
    new_offers integer not null default 0,
    duplicates_ignored integer not null default 0,
    exported_offers integer not null default 0,
    errors jsonb not null default '[]'::jsonb,
    new_offer_keys jsonb not null default '[]'::jsonb,
    raw_payload jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

create index if not exists idx_scan_runs_started_at on scan_runs(started_at desc);

create table if not exists scan_run_sources (
    id uuid primary key default gen_random_uuid(),
    scan_run_id uuid not null references scan_runs(id) on delete cascade,
    source text not null,
    status text not null,
    offers_found integer not null default 0,
    new_offers integer not null default 0,
    duplicates integer not null default 0,
    error_message text,
    source_timestamp timestamptz,
    raw_payload jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

create index if not exists idx_scan_run_sources_scan_run_id on scan_run_sources(scan_run_id);

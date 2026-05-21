alter table if exists offer_scores
    add column if not exists id uuid default gen_random_uuid();

alter table if exists offer_letters
    add column if not exists id uuid default gen_random_uuid();

alter table if exists refused_offers
    add column if not exists id uuid default gen_random_uuid();

do $$
begin
    if exists (
        select 1 from information_schema.table_constraints
        where table_name = 'offer_scores' and constraint_type = 'PRIMARY KEY' and constraint_name = 'offer_scores_pkey'
    ) then
        alter table offer_scores drop constraint offer_scores_pkey;
    end if;
exception when undefined_table then
    null;
end $$;

do $$
begin
    if exists (
        select 1 from information_schema.table_constraints
        where table_name = 'offer_letters' and constraint_type = 'PRIMARY KEY' and constraint_name = 'offer_letters_pkey'
    ) then
        alter table offer_letters drop constraint offer_letters_pkey;
    end if;
exception when undefined_table then
    null;
end $$;

do $$
begin
    if exists (
        select 1 from information_schema.table_constraints
        where table_name = 'refused_offers' and constraint_type = 'PRIMARY KEY' and constraint_name = 'refused_offers_pkey'
    ) then
        alter table refused_offers drop constraint refused_offers_pkey;
    end if;
exception when undefined_table then
    null;
end $$;

do $$
begin
    if not exists (
        select 1 from information_schema.table_constraints
        where table_name = 'offer_scores' and constraint_type = 'PRIMARY KEY' and constraint_name = 'offer_scores_id_pkey'
    ) then
        alter table offer_scores add constraint offer_scores_id_pkey primary key (id);
    end if;
exception when undefined_table then
    null;
end $$;

do $$
begin
    if not exists (
        select 1 from information_schema.table_constraints
        where table_name = 'offer_letters' and constraint_type = 'PRIMARY KEY' and constraint_name = 'offer_letters_id_pkey'
    ) then
        alter table offer_letters add constraint offer_letters_id_pkey primary key (id);
    end if;
exception when undefined_table then
    null;
end $$;

do $$
begin
    if not exists (
        select 1 from information_schema.table_constraints
        where table_name = 'refused_offers' and constraint_type = 'PRIMARY KEY' and constraint_name = 'refused_offers_id_pkey'
    ) then
        alter table refused_offers add constraint refused_offers_id_pkey primary key (id);
    end if;
exception when undefined_table then
    null;
end $$;

create unique index if not exists uq_offer_scores_signature_owner
    on offer_scores(offer_signature, owner_user_id);

create unique index if not exists uq_offer_letters_signature_owner
    on offer_letters(offer_signature, owner_user_id);

create unique index if not exists uq_refused_offers_signature_owner
    on refused_offers(offer_signature, owner_user_id);

create unique index if not exists uq_offer_scores_signature_global
    on offer_scores(offer_signature)
    where owner_user_id is null;

create unique index if not exists uq_offer_letters_signature_global
    on offer_letters(offer_signature)
    where owner_user_id is null;

create unique index if not exists uq_refused_offers_signature_global
    on refused_offers(offer_signature)
    where owner_user_id is null;

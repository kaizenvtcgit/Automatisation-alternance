alter table if exists app_settings
    add column if not exists owner_user_id uuid references auth.users(id) on delete cascade;

alter table if exists search_profiles
    add column if not exists owner_user_id uuid references auth.users(id) on delete cascade;

alter table if exists offer_scores
    add column if not exists owner_user_id uuid references auth.users(id) on delete cascade;

alter table if exists offer_letters
    add column if not exists owner_user_id uuid references auth.users(id) on delete cascade;

alter table if exists applications_history
    add column if not exists owner_user_id uuid references auth.users(id) on delete cascade;

alter table if exists refused_offers
    add column if not exists owner_user_id uuid references auth.users(id) on delete cascade;

create index if not exists idx_app_settings_owner_user_id on app_settings(owner_user_id);
create index if not exists idx_search_profiles_owner_user_id on search_profiles(owner_user_id);
create index if not exists idx_offer_scores_owner_user_id on offer_scores(owner_user_id);
create index if not exists idx_offer_letters_owner_user_id on offer_letters(owner_user_id);
create index if not exists idx_applications_history_owner_user_id on applications_history(owner_user_id);
create index if not exists idx_refused_offers_owner_user_id on refused_offers(owner_user_id);

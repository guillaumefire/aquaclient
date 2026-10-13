-- AquaClient — schéma Supabase (idempotent)
create table if not exists public.clients (
  id bigint primary key,
  nom text not null default '',
  prenom text not null default '',
  adresse text not null default '',
  latitude double precision,
  longitude double precision,
  pompe text default '',
  filtre text default '',
  robot text default '',
  skimmer text default '',
  vanne_vidange text default '',
  injecteurs text default '',
  autres text default '',
  type_entretien text default '',
  frequence_passage text default '',
  date_debut_passage text default '',
  identifiant text default '',
  email text default '',
  telephone text default '',
  password_hash text default '',
  compte_actif text default '0',
  auth_uid text default ''
);

alter table public.clients add column if not exists identifiant text default '';
alter table public.clients add column if not exists email text default '';
alter table public.clients add column if not exists telephone text default '';
alter table public.clients add column if not exists password_hash text default '';
alter table public.clients add column if not exists compte_actif text default '0';
alter table public.clients add column if not exists auth_uid text default '';

create unique index if not exists clients_identifiant_uidx
  on public.clients (identifiant)
  where identifiant is not null and identifiant <> '';
create unique index if not exists clients_email_uidx
  on public.clients (email)
  where email is not null and email <> '';
create unique index if not exists clients_telephone_uidx
  on public.clients (telephone)
  where telephone is not null and telephone <> '';

create table if not exists public.notes (
  id bigint primary key,
  client_id bigint not null references public.clients(id) on delete cascade,
  date text default '',
  date_ref text default '',
  texte text default ''
);

create table if not exists public.analyses (
  id bigint primary key,
  client_id bigint not null references public.clients(id) on delete cascade,
  date text default '',
  date_ref text default '',
  texte text default ''
);

create table if not exists public.fournitures (
  id bigint primary key,
  client_id bigint not null references public.clients(id) on delete cascade,
  date text default '',
  date_ref text default '',
  description text default '',
  quantite text default '1',
  envoyee text default 'non',
  payee text default 'non'
);

create table if not exists public.visites (
  id bigint primary key,
  client_id bigint not null references public.clients(id) on delete cascade,
  date text default '',
  type text default '',
  checklist_json text default '[]',
  debut text default '',
  fin text default '',
  duree_minutes text default ''
);

create index if not exists notes_client_id_idx on public.notes(client_id);
create index if not exists analyses_client_id_idx on public.analyses(client_id);
create index if not exists fournitures_client_id_idx on public.fournitures(client_id);
create index if not exists visites_client_id_idx on public.visites(client_id);

-- Accès Data API (clé publishable / anon) depuis le backend AquaClient
grant usage on schema public to anon, authenticated, service_role;
grant all on table public.clients to anon, authenticated, service_role;
grant all on table public.notes to anon, authenticated, service_role;
grant all on table public.analyses to anon, authenticated, service_role;
grant all on table public.fournitures to anon, authenticated, service_role;
grant all on table public.visites to anon, authenticated, service_role;

alter table public.clients enable row level security;
alter table public.notes enable row level security;
alter table public.analyses enable row level security;
alter table public.fournitures enable row level security;
alter table public.visites enable row level security;

drop policy if exists aquaclient_clients_all on public.clients;
create policy aquaclient_clients_all on public.clients
  for all to anon, authenticated using (true) with check (true);

drop policy if exists aquaclient_notes_all on public.notes;
create policy aquaclient_notes_all on public.notes
  for all to anon, authenticated using (true) with check (true);

drop policy if exists aquaclient_analyses_all on public.analyses;
create policy aquaclient_analyses_all on public.analyses
  for all to anon, authenticated using (true) with check (true);

drop policy if exists aquaclient_fournitures_all on public.fournitures;
create policy aquaclient_fournitures_all on public.fournitures
  for all to anon, authenticated using (true) with check (true);

drop policy if exists aquaclient_visites_all on public.visites;
create policy aquaclient_visites_all on public.visites
  for all to anon, authenticated using (true) with check (true);

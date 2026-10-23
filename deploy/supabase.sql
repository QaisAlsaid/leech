create table if not exists public.leech_kv (
    ns         text        not null,
    k          text        not null,
    v          jsonb       not null,
    updated_at timestamptz not null default now(),
    primary key (ns, k)
);

create table if not exists public.leech_log (
    id   bigint generated always as identity primary key,
    ns   text        not null,
    v    jsonb       not null,
    ts   timestamptz not null default now()
);

create index if not exists leech_log_ns_id on public.leech_log (ns, id);

alter table public.leech_kv   enable row level security;
alter table public.leech_log  enable row level security;

create or replace function public.leech_touch_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists leech_kv_touch on public.leech_kv;
create trigger leech_kv_touch
    before update on public.leech_kv
    for each row execute function public.leech_touch_updated_at();

-- AED Care / Supabase PostgreSQL schema
-- Run this once in Supabase Dashboard > SQL Editor.

begin;

create schema if not exists extensions;
create extension if not exists pgcrypto with schema extensions;
create extension if not exists postgis with schema extensions;

create table if not exists public.aed_locations (
  id uuid primary key default extensions.gen_random_uuid(),
  source text not null default 'safemap' check (source in ('safemap','manual','csv')),
  source_record_id text not null,
  name text not null,
  address text not null default '',
  detail_address text not null default '',
  sido text not null default '',
  gugun text not null default '',
  postal_code_1 text not null default '',
  postal_code_2 text not null default '',
  latitude double precision not null check (latitude between 33 and 39),
  longitude double precision not null check (longitude between 124 and 132),
  geom extensions.geometry(Point,4326)
    generated always as (extensions.st_setsrid(extensions.st_makepoint(longitude, latitude), 4326)) stored,
  source_maker text not null default '',
  source_model text not null default '',
  source_manager text not null default '',
  source_manager_tel text not null default '',
  raw_payload jsonb not null default '{}'::jsonb,
  is_active boolean not null default true,
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (source, source_record_id)
);

create index if not exists aed_locations_geom_gix on public.aed_locations using gist (geom);
create index if not exists aed_locations_region_idx on public.aed_locations (sido, gugun);
create index if not exists aed_locations_model_idx on public.aed_locations (source_maker, source_model);
create index if not exists aed_locations_active_idx on public.aed_locations (is_active) where is_active;

-- SafeMap 원본과 분리된 기관 관리 값. 재동기화가 이 값을 덮어쓰지 않습니다.
create table if not exists public.aed_management (
  aed_id uuid primary key references public.aed_locations(id) on delete cascade,
  organization_id uuid,
  internal_model_code text,
  building text,
  floor text,
  spot text,
  operating_hours text,
  access_info text,
  manager_name text,
  manager_phone text,
  battery_expiry date,
  pad_expiry date,
  installed_on date,
  inspection_cycle_days integer not null default 180 check (inspection_cycle_days > 0),
  needs_post_use_check boolean not null default false,
  updated_at timestamptz not null default now()
);

create table if not exists public.aed_inspections (
  id uuid primary key default extensions.gen_random_uuid(),
  aed_id uuid not null references public.aed_locations(id) on delete cascade,
  inspected_on date not null,
  result text not null,
  inspector text,
  memo text,
  photo_path text,
  created_at timestamptz not null default now()
);
create index if not exists aed_inspections_aed_date_idx on public.aed_inspections (aed_id, inspected_on desc);

create table if not exists public.aed_usage_events (
  id uuid primary key default extensions.gen_random_uuid(),
  aed_id uuid references public.aed_locations(id) on delete set null,
  used_at timestamptz not null,
  latitude double precision,
  longitude double precision,
  call_119_status text not null default 'unknown',
  manager_notification_status text not null default 'not_integrated',
  recovery_status text not null default 'pending',
  note text,
  recovered_at timestamptz,
  recovered_by text,
  recovery_checks jsonb not null default '{}'::jsonb,
  recovery_memo text,
  recovery_photo_path text,
  created_at timestamptz not null default now()
);
create index if not exists aed_usage_events_status_idx on public.aed_usage_events (recovery_status, used_at desc);

create table if not exists public.aed_action_tasks (
  id uuid primary key default extensions.gen_random_uuid(),
  aed_id uuid references public.aed_locations(id) on delete set null,
  usage_event_id uuid references public.aed_usage_events(id) on delete set null,
  task_type text not null default 'risk',
  title text not null,
  reason text,
  assignee text,
  due_on date,
  status text not null default '미처리',
  work_detail text,
  photo_path text,
  history jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists aed_action_tasks_status_idx on public.aed_action_tasks (status, due_on);

create table if not exists public.safemap_sync_runs (
  id uuid primary key default extensions.gen_random_uuid(),
  status text not null default 'running',
  total_expected integer,
  processed integer not null default 0,
  upserted integer not null default 0,
  failed integer not null default 0,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  error_message text
);

-- 공개 지도는 담당자 전화, 원본 payload, 내부 관리·점검 정보를 반환하지 않습니다.
create or replace function public.aeds_in_bbox(
  p_min_lng double precision,
  p_min_lat double precision,
  p_max_lng double precision,
  p_max_lat double precision,
  p_limit integer default 1000,
  p_user_lng double precision default null,
  p_user_lat double precision default null
)
returns table (
  id uuid,
  name text,
  address text,
  detail_address text,
  sido text,
  gugun text,
  latitude double precision,
  longitude double precision,
  source_maker text,
  source_model text,
  distance_m double precision
)
language sql
stable
security definer
set search_path = public, extensions
as $$
  select
    a.id, a.name, a.address, a.detail_address, a.sido, a.gugun,
    a.latitude, a.longitude, a.source_maker, a.source_model,
    case
      when p_user_lng is null or p_user_lat is null then null
      else extensions.st_distance(
        a.geom::extensions.geography,
        extensions.st_setsrid(extensions.st_makepoint(p_user_lng, p_user_lat), 4326)::extensions.geography
      )
    end as distance_m
  from public.aed_locations a
  where a.is_active
    and p_min_lng between 124 and 132 and p_max_lng between 124 and 132
    and p_min_lat between 33 and 39 and p_max_lat between 33 and 39
    and p_min_lng < p_max_lng and p_min_lat < p_max_lat
    and a.geom && extensions.st_makeenvelope(p_min_lng, p_min_lat, p_max_lng, p_max_lat, 4326)
  order by
    case when p_user_lng is null or p_user_lat is null then 0 else 1 end desc,
    case when p_user_lng is null or p_user_lat is null then null
      else a.geom <-> extensions.st_setsrid(extensions.st_makepoint(p_user_lng, p_user_lat), 4326)
    end,
    a.id
  limit least(greatest(coalesce(p_limit, 1000), 1), 2000);
$$;

alter table public.aed_locations enable row level security;
alter table public.aed_management enable row level security;
alter table public.aed_inspections enable row level security;
alter table public.aed_usage_events enable row level security;
alter table public.aed_action_tasks enable row level security;
alter table public.safemap_sync_runs enable row level security;

revoke all on public.aed_locations, public.aed_management, public.aed_inspections,
  public.aed_usage_events, public.aed_action_tasks, public.safemap_sync_runs
  from anon, authenticated;
revoke all on function public.aeds_in_bbox(double precision,double precision,double precision,double precision,integer,double precision,double precision) from public;
grant execute on function public.aeds_in_bbox(double precision,double precision,double precision,double precision,integer,double precision,double precision) to anon, authenticated;
grant all on public.aed_locations, public.aed_management, public.aed_inspections,
  public.aed_usage_events, public.aed_action_tasks, public.safemap_sync_runs
  to service_role;

commit;

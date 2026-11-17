-- Initial schema (guide Rev 2, Step 2): 6 tables, RLS, updated_at triggers,
-- and an atomic booking function that prevents double bookings.

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------------------
-- agents: one configured agent per business line
-- ---------------------------------------------------------------------------
create table public.agents (
    id uuid primary key default gen_random_uuid(),
    owner_id uuid not null references auth.users(id) on delete cascade,
    name text not null,
    industry text not null,                     -- template key; validated in the API (Step 6), no CHECK list on purpose
    business_name text not null,
    business_phone text,
    business_email text,
    business_address text,
    timezone text not null default 'Europe/Berlin',
    language text not null default 'en',
    voice_id text,
    greeting text,
    custom_instructions text,
    knowledge text,
    -- day -> list of [start, end] ranges; [] = closed. Lists allow split shifts (lunch + dinner).
    business_hours jsonb not null default '{
        "monday":    [["09:00", "17:00"]],
        "tuesday":   [["09:00", "17:00"]],
        "wednesday": [["09:00", "17:00"]],
        "thursday":  [["09:00", "17:00"]],
        "friday":    [["09:00", "17:00"]],
        "saturday":  [],
        "sunday":    []
    }'::jsonb,
    services jsonb not null default '[]'::jsonb,
    capacity int not null default 1 check (capacity between 1 and 500),
    slot_interval_min int not null default 30 check (slot_interval_min between 5 and 240),
    max_party_size int,
    is_public boolean not null default false,
    enabled_tools text[],                       -- null = template defaults; must be a subset of the template's tools
    elevenlabs_agent_id text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create index on public.agents (owner_id);

-- ---------------------------------------------------------------------------
-- customers: callers, created or updated when they book
-- ---------------------------------------------------------------------------
create table public.customers (
    id uuid primary key default gen_random_uuid(),
    owner_id uuid not null references auth.users(id) on delete cascade,
    name text,
    phone text,
    email text,
    notes text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (owner_id, phone)
);

-- ---------------------------------------------------------------------------
-- calls: every voice or chat session
-- ---------------------------------------------------------------------------
create table public.calls (
    id uuid primary key default gen_random_uuid(),
    owner_id uuid not null references auth.users(id) on delete cascade,
    agent_id uuid not null references public.agents(id) on delete cascade,
    provider text not null default 'elevenlabs',
    external_id text,                           -- e.g. ElevenLabs conversation id
    channel text not null default 'web_voice',
    status text not null default 'active',
    started_at timestamptz not null default now(),
    ended_at timestamptz,
    duration_sec int,
    transcript jsonb not null default '[]'::jsonb,
    summary text,
    outcome text,
    cost_usd numeric(10,4),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create unique index on public.calls (provider, external_id) where external_id is not null;
create index on public.calls (agent_id, started_at desc);

-- ---------------------------------------------------------------------------
-- appointments: bookings; details holds industry fields (stylist, address...)
-- ---------------------------------------------------------------------------
create table public.appointments (
    id uuid primary key default gen_random_uuid(),
    owner_id uuid not null references auth.users(id) on delete cascade,
    agent_id uuid not null references public.agents(id) on delete cascade,
    customer_id uuid references public.customers(id) on delete set null,
    call_id uuid references public.calls(id) on delete set null,
    service text not null,
    starts_at timestamptz not null,
    ends_at timestamptz not null,
    party_size int,
    status text not null default 'booked'
        check (status in ('booked','cancelled','completed','no_show')),
    notes text,
    details jsonb not null default '{}'::jsonb,
    source text not null default 'voice',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check (ends_at > starts_at)
);
create index on public.appointments (agent_id, starts_at) where status = 'booked';

-- ---------------------------------------------------------------------------
-- inbox_items: work the agent hands to people
-- ---------------------------------------------------------------------------
create table public.inbox_items (
    id uuid primary key default gen_random_uuid(),
    owner_id uuid not null references auth.users(id) on delete cascade,
    agent_id uuid not null references public.agents(id) on delete cascade,
    call_id uuid references public.calls(id) on delete set null,
    kind text not null default 'message'
        check (kind in ('message','escalation','emergency','ticket','lead','callback')),
    caller_name text,
    caller_phone text,
    body text not null,
    urgency text not null default 'normal' check (urgency in ('low','normal','high')),
    status text not null default 'open' check (status in ('open','done')),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create index on public.inbox_items (agent_id, status, created_at desc);

-- ---------------------------------------------------------------------------
-- status_records: order / job statuses the agent may read out (lookup_status)
-- ---------------------------------------------------------------------------
create table public.status_records (
    id uuid primary key default gen_random_uuid(),
    owner_id uuid not null references auth.users(id) on delete cascade,
    agent_id uuid not null references public.agents(id) on delete cascade,
    kind text not null default 'order' check (kind in ('order','job')),
    reference text not null,                    -- order number, job number
    customer_phone text,
    customer_email text,
    status text not null,                       -- 'shipped', 'technician on the way'...
    details jsonb not null default '{}'::jsonb, -- tracking link, ETA, items
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (agent_id, kind, reference)
);

-- ---------------------------------------------------------------------------
-- updated_at triggers + row-level security on every table
-- ---------------------------------------------------------------------------
create or replace function public.touch_updated_at() returns trigger
language plpgsql as $$
begin
    new.updated_at = now();
    return new;
end $$;

do $$
declare t text;
begin
    foreach t in array array['agents','customers','calls','appointments','inbox_items','status_records'] loop
        execute format('create trigger touch_%1$s before update on public.%1$I
                        for each row execute function public.touch_updated_at()', t);
        execute format('alter table public.%I enable row level security', t);
        execute format('create policy "owner_all" on public.%I for all
                        using (owner_id = auth.uid()) with check (owner_id = auth.uid())', t);
    end loop;
end $$;

-- ---------------------------------------------------------------------------
-- book_appointment: atomic insert that refuses when capacity is full.
-- One advisory lock per agent serialises concurrent bookings.
-- Called by the API with the service-role key only.
-- owner_id and capacity are read from the agents row, never taken from the
-- caller: security definer bypasses RLS, so the function must not trust input.
-- ---------------------------------------------------------------------------
create or replace function public.book_appointment(p jsonb)
returns public.appointments
language plpgsql security definer set search_path = public as $$
declare
    v_agent_id uuid := (p->>'agent_id')::uuid;  -- cast normalises the UUID text form
    v_owner_id uuid;
    v_capacity int;
    taken int;
    rec public.appointments;
begin
    -- Lock first, so capacity and existing bookings are read in one protected window.
    -- Hash the normalised uuid text: 'A1B2...' and 'a1b2...' must share one lock.
    perform pg_advisory_xact_lock(hashtext(v_agent_id::text));

    select owner_id, capacity into v_owner_id, v_capacity
    from agents
    where id = v_agent_id;

    if not found then
        raise exception 'agent % not found', v_agent_id using errcode = 'P0002';
    end if;

    select count(*) into taken
    from appointments
    where agent_id = v_agent_id
      and status = 'booked'
      and starts_at < (p->>'ends_at')::timestamptz
      and ends_at   > (p->>'starts_at')::timestamptz;

    if taken >= v_capacity then
        return null;
    end if;

    insert into appointments (owner_id, agent_id, customer_id, call_id, service,
                              starts_at, ends_at, party_size, notes, details, source)
    values (v_owner_id, v_agent_id, (p->>'customer_id')::uuid,
            (p->>'call_id')::uuid, p->>'service', (p->>'starts_at')::timestamptz,
            (p->>'ends_at')::timestamptz, (p->>'party_size')::int, p->>'notes',
            coalesce(p->'details', '{}'::jsonb), coalesce(p->>'source', 'voice'))
    returning * into rec;

    return rec;
end $$;

revoke execute on function public.book_appointment(jsonb) from public, anon, authenticated;

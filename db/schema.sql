-- deterministic-trader: the whole database, for a new Supabase project.
--
-- Run this file once, top to bottom, in the Supabase SQL Editor (or with psql
-- against the direct or session-pooler connection; the transaction pooler
-- cannot run pg_cron). It is one transaction, followed by the pg_cron jobs.
--
-- It needs the `pg_cron` and `pg_net` extensions, which it creates; if your
-- project blocks that, enable both under Database > Extensions first. `vault`
-- ships enabled. After it runs, set the Vault secrets the README lists.
--
-- It is written for an empty database. To change the schema later, add a new
-- file beside this one and apply it by hand.

begin;

create extension if not exists pg_cron;

create extension if not exists pg_net;

create table if not exists edge_alerts (
  id                    bigserial primary key,
  created_at            timestamptz not null default now(),
  title                 text        not null,
  body                  text        not null,
  priority              text        not null default 'default',
  cycle_id              text,
  telegram_delivered_at timestamptz,
  telegram_attempts     integer     not null default 0,
  claimed_at            timestamptz
);

create index if not exists edge_alerts_undelivered
  on edge_alerts (created_at)
  where telegram_delivered_at is null;

create table if not exists edge_alert_dispatch (
  request_id  bigint      primary key,
  alert_id    bigint      not null references edge_alerts (id) on delete cascade,
  channel     text        not null check (channel = 'telegram'),
  sent_at     timestamptz not null default now()
);

create index if not exists edge_alert_dispatch_alert on edge_alert_dispatch (alert_id);

alter table edge_alerts          enable row level security;

alter table edge_alert_dispatch  enable row level security;

create or replace function edge_secret(secret_name text)
returns text
language sql
security definer
set search_path = vault, public
as $$
  select decrypted_secret from vault.decrypted_secrets where name = secret_name limit 1;
$$;

create or replace function edge_alert_reap()
returns integer
language plpgsql
security definer
set search_path = public, net
as $$
declare
  stamped integer := 0;
begin
  with settled as (
    select d.alert_id,
           bool_or(d.channel = 'telegram') as telegram_ok
      from edge_alert_dispatch d
      join net._http_response r on r.id = d.request_id
     where r.status_code between 200 and 299
     group by d.alert_id
  ),
  marked as (
    update edge_alerts a
       set telegram_delivered_at = case when s.telegram_ok
                                        then coalesce(a.telegram_delivered_at, now())
                                        else a.telegram_delivered_at end,
           claimed_at            = null
      from settled s
     where a.id = s.alert_id
       and a.telegram_delivered_at is null
    returning a.id
  )
  select count(*) into stamped from marked;

  delete from edge_alert_dispatch where sent_at < now() - interval '1 hour';

  return stamped;
end;
$$;

create table if not exists edge_control (
  key         text primary key,
  value       text,
  payload     jsonb,
  cycle_type  text,

  updated_at  timestamptz not null default now()
);

insert into edge_control (key, value, payload, cycle_type) values
  ('cycle_in_progress',             'none',  null, null),
  ('trading_enabled',               'FALSE', null, null),
  ('last_full_cycle',               null,    null, null),
  ('last_overnight_cycle',          null,    null, null),
  ('high_water_mark',               null,    null, null),
  ('positions_held',                '0',     null, null),
  ('has_whole_share_position',      'false', null, null),
  ('unreconciled',                  'false', null, null),
  ('pending_events',                'false', null, null),
  ('entries_blocked',               'false', null, null),
  ('entries_blocked_reason',        null,    null, null),
  ('entries_blocked_until_cleared', 'false', null, null),
  ('divergence_streak',             '0',     null, null),
  ('last_scorecard_date',           null,    null, null),
  ('last_drawdown_alert',           null,    null, null),
  ('new_positions',                 null,    '{"date": null, "count": 0}'::jsonb, null),
  ('open_order_cycles',             null,    '{}'::jsonb, null),
  ('retired_order_ids',             null,    '[]'::jsonb, null),
  ('last_data_fresh_at',            null,    '{"trading_day": null, "fetched_at": null}'::jsonb, null)
on conflict (key) do nothing;

create table if not exists edge_runlog (
  id            bigserial primary key,
  created_at    timestamptz not null default now(),

  cycle_id      text        not null,
  cycle_type    text        not null,
  trading_day   date        not null,
  command       text        not null,
  stdout        jsonb,
  placements    jsonb       not null default '[]'::jsonb,
  rejections    jsonb       not null default '[]'::jsonb,
  book          jsonb
);

create index if not exists edge_runlog_cycle
  on edge_runlog (cycle_id);

create index if not exists edge_runlog_recent
  on edge_runlog (trading_day desc, id desc);

create table if not exists edge_play_state (
  play_id        text primary key,
  regime_fit     numeric     not null default 0.0,
  reason         text,
  closed_trades  integer     not null default 0,
  realized_pnl   numeric     not null default 0.0,

  updated_at     timestamptz not null default now(),
  near_flat_trades integer not null default 0
);

insert into edge_play_state (play_id, regime_fit, reason) values
  ('CATALYST', 0.0, 'seeded dark at table creation; no premarket cycle has run'),
  ('DIP',      0.0, 'seeded dark at table creation; no premarket cycle has run'),
  ('PEAD',     0.0, 'seeded dark at table creation; no premarket cycle has run'),
  ('MACRO',    0.0, 'seeded dark at table creation; no premarket cycle has run')
on conflict (play_id) do nothing;

create table if not exists edge_benchmark (
  trading_day   date primary key,

  equity        numeric     not null,
  net_deposits  numeric     not null,
  spy_close     numeric,
  frozen_value  numeric,

  updated_at    timestamptz not null default now()
);

create table if not exists edge_loss_closes (
  id           bigserial primary key,
  created_at   timestamptz not null default now(),

  symbol       text        not null,
  close_date   date        not null,
  loss_amount  numeric,

  cycle_id     text,
  basis_unknown boolean,
  backfilled boolean not null default false
);

create index if not exists edge_loss_closes_symbol_date
  on edge_loss_closes (symbol, close_date desc);

alter table edge_control      enable row level security;

alter table edge_runlog       enable row level security;

alter table edge_play_state   enable row level security;

alter table edge_benchmark    enable row level security;

alter table edge_loss_closes  enable row level security;

create or replace function edge_cycle_claim(
  p_cycle_id   text,
  p_cycle_type text,
  p_timeout    interval default interval '20 minutes'
) returns jsonb language plpgsql as $$
declare
  held_by text;
  stale   boolean;
begin
  select value, updated_at < now() - p_timeout
    into held_by, stale
    from edge_control
   where key = 'cycle_in_progress';

  if not found then
    return jsonb_build_object('claimed', false, 'reason', 'no cycle_in_progress row');
  end if;

  held_by := coalesce(held_by, 'none');

  update edge_control
     set value = p_cycle_id, cycle_type = p_cycle_type, updated_at = now()
   where key = 'cycle_in_progress'
     and (value = 'none' or value is null or updated_at < now() - p_timeout);

  if not found then
    return jsonb_build_object('claimed', false, 'held_by', held_by);
  end if;

  return jsonb_build_object(
    'claimed', true,
    'stole_from', case when held_by <> 'none' and stale then held_by end
  );
end $$;

create or replace function edge_cycle_renew(p_cycle_id text)
returns boolean language plpgsql as $$
begin
  update edge_control set updated_at = now()
   where key = 'cycle_in_progress' and value = p_cycle_id;
  return found;
end $$;

create or replace function edge_control_json()
returns jsonb language sql stable as $$
  select jsonb_object_agg(key, typed) || jsonb_build_object('readable', true) from (
    select key,
           case
             when payload is not null then payload
             when value is null then 'null'::jsonb
             when lower(value) in ('true', 'false')  then to_jsonb(lower(value)::boolean)
             when lower(value) in ('TRUE', 'FALSE')  then to_jsonb(lower(value)::boolean)
             when value ~ '^-?\d+$'                  then to_jsonb(value::bigint)
             when value ~ '^-?\d*\.\d+$'             then to_jsonb(value::numeric)
             else to_jsonb(value)
           end as typed
      from edge_control
  ) shaped;
$$;

create or replace function edge_prior(p_trading_day date)
returns jsonb language sql stable as $$
  select jsonb_build_object(
    'placements', coalesce((
      select placements from edge_runlog
       where placements <> '[]'::jsonb
       order by id desc limit 1
    ), '[]'::jsonb),
    'prior_book', coalesce((
      select book from edge_runlog
       where book is not null
       order by id desc limit 1
    ), 'null'::jsonb),
    'rejections', coalesce((
      select jsonb_agg(row_value)
        from edge_runlog, lateral jsonb_array_elements(rejections) as row_value
       where trading_day = p_trading_day
    ), '[]'::jsonb)
  );
$$;

create or replace function edge_control_apply(p_updates jsonb)
returns integer language plpgsql as $$
declare
  k       text;
  v       jsonb;
  applied integer := 0;
begin
  for k, v in select * from jsonb_each(p_updates) loop
    if jsonb_typeof(v) in ('object', 'array') then
      insert into edge_control (key, value, payload, updated_at)
        values (k, null, v, now())
      on conflict (key) do update
        set payload = excluded.payload, value = null, updated_at = now();
    else
      insert into edge_control (key, value, payload, updated_at)
        values (k, case when jsonb_typeof(v) = 'null' then null else v #>> '{}' end,
                null, now())
      on conflict (key) do update
        set value = excluded.value, payload = null, updated_at = now();
    end if;
    applied := applied + 1;
  end loop;
  return applied;
end $$;

create or replace function edge_runlog_write(
  p_cycle_id    text,
  p_cycle_type  text,
  p_trading_day date,
  p_command     text,
  p_stdout      jsonb default null,
  p_placements  jsonb default '[]'::jsonb,
  p_rejections  jsonb default '[]'::jsonb,
  p_book        jsonb default null
) returns bigint language sql as $$
  insert into edge_runlog (cycle_id, cycle_type, trading_day, command,
                           stdout, placements, rejections, book)
  values (p_cycle_id, p_cycle_type, p_trading_day, p_command,
          p_stdout, coalesce(p_placements, '[]'::jsonb),
          coalesce(p_rejections, '[]'::jsonb), p_book)
  returning id;
$$;

create or replace function edge_benchmark_upsert(p_row jsonb)
returns date language sql as $$
  insert into edge_benchmark (trading_day, equity, net_deposits, spy_close,
                              frozen_value, updated_at)
  values (
    (p_row->>'trading_day')::date,
    (p_row->>'equity')::numeric,
    (p_row->>'net_deposits')::numeric,
    nullif(p_row->>'spy_close', '')::numeric,
    nullif(p_row->>'frozen_value', '')::numeric,
    now()
  )
  on conflict (trading_day) do update set
    equity       = excluded.equity,
    net_deposits = excluded.net_deposits,
    spy_close    = excluded.spy_close,
    frozen_value = excluded.frozen_value,
    updated_at   = now()
  returning trading_day;
$$;

create or replace function edge_play_state_apply(p_updates jsonb)
returns integer language plpgsql as $$
declare
  play    text;
  v       jsonb;
  applied integer := 0;
begin
  for play, v in select * from jsonb_each(p_updates) loop
    insert into edge_play_state (play_id, regime_fit, reason, closed_trades,
                                 realized_pnl, updated_at)
    values (
      play,
      coalesce((v->>'regime_fit')::numeric, 0.0),
      v->>'reason',
      coalesce((v->>'closed_trades')::integer, 0),
      coalesce((v->>'realized_pnl')::numeric, 0.0),
      now()
    )
    on conflict (play_id) do update set
      regime_fit    = coalesce((v->>'regime_fit')::numeric, edge_play_state.regime_fit),
      reason        = coalesce(v->>'reason', edge_play_state.reason),
      closed_trades = coalesce((v->>'closed_trades')::integer, edge_play_state.closed_trades),
      realized_pnl  = coalesce((v->>'realized_pnl')::numeric, edge_play_state.realized_pnl),
      updated_at    = now();
    applied := applied + 1;
  end loop;
  return applied;
end $$;

create table if not exists edge_earnings_reactions (
  symbol           text        not null,
  report_date      date        not null,
  timing           text,

  reaction_session date        not null,
  prior_close      numeric     not null,
  reaction_close   numeric     not null,
  reaction_pct     numeric     not null,

  computed_at      timestamptz not null default now(),

  primary key (symbol, report_date)
);

create index if not exists edge_earnings_reactions_recent
  on edge_earnings_reactions (reaction_session desc);

create or replace function edge_earnings_reactions_recent(p_days integer default 7)
returns jsonb language sql stable as $$
  select coalesce(jsonb_agg(jsonb_build_object(
    'symbol',       symbol,
    'report_date',  report_date,
    'reaction_pct', reaction_pct
  )), '[]'::jsonb)
  from edge_earnings_reactions
  where reaction_session >= current_date - p_days;
$$;

create or replace function edge_earnings_reactions_write(p_rows jsonb)
returns integer language plpgsql as $$
declare
  row_count integer := 0;
begin
  insert into edge_earnings_reactions (
    symbol, report_date, timing, reaction_session,
    prior_close, reaction_close, reaction_pct
  )
  select
    r->>'symbol',
    (r->>'report_date')::date,
    r->>'timing',
    (r->>'reaction_session')::date,
    (r->>'prior_close')::numeric,
    (r->>'reaction_close')::numeric,
    (r->>'reaction_pct')::numeric
  from jsonb_array_elements(p_rows) as r
  on conflict (symbol, report_date) do update
    set timing           = excluded.timing,
        reaction_session = excluded.reaction_session,
        prior_close      = excluded.prior_close,
        reaction_close   = excluded.reaction_close,
        reaction_pct     = excluded.reaction_pct,
        computed_at      = now();

  get diagnostics row_count = row_count;
  return row_count;
end $$;

alter table edge_earnings_reactions enable row level security;

create table if not exists edge_day_context (
  trading_day  date primary key,
  market_data  jsonb       not null default '{}'::jsonb,

  fetched_at   timestamptz not null default now()
);

create or replace function edge_day_context_recent(p_trading_day date)
returns jsonb language sql stable as $$
  select coalesce(
    (select market_data from edge_day_context where trading_day = p_trading_day),
    '{}'::jsonb
  );
$$;

alter table edge_day_context enable row level security;

create or replace function edge_day_context_write(p_trading_day date, p_market_data jsonb)
returns void language sql as $$
  insert into edge_day_context (trading_day, market_data, fetched_at)
    values (p_trading_day, p_market_data, now())
  on conflict (trading_day) do update
    set market_data = excluded.market_data, fetched_at = excluded.fetched_at;
$$;

create table if not exists edge_mc_check_cache (
  trading_day  date primary key,
  market_data  jsonb       not null default '{}'::jsonb,

  fetched_at   timestamptz not null default now()
);

create or replace function edge_mc_check_recent(p_trading_day date)
returns jsonb language sql stable as $$
  select coalesce(
    (select market_data from edge_mc_check_cache where trading_day = p_trading_day),
    '{}'::jsonb
  );
$$;

create or replace function edge_mc_check_write_symbol(
  p_trading_day date,
  p_symbol text,
  p_symbol_data jsonb
)
returns void language sql as $$
  insert into edge_mc_check_cache (trading_day, market_data, fetched_at)
    values (
      p_trading_day,
      jsonb_build_object(p_symbol, p_symbol_data),
      now()
    )
  on conflict (trading_day) do update
    set market_data = jsonb_set(
          edge_mc_check_cache.market_data,
          array[p_symbol],
          p_symbol_data,
          true
        ),
        fetched_at = now();
$$;

alter table edge_mc_check_cache enable row level security;

create table if not exists strategy_symbol_lock (
  symbol      text primary key,

  owner       text        not null check (owner in ('core', 'dip_recovery')),

  locked_at   timestamptz not null default now()
);

create or replace function edge_symbol_lock_json()
returns jsonb language sql stable as $$
  select coalesce(jsonb_object_agg(symbol, owner), '{}'::jsonb)
    from strategy_symbol_lock;
$$;

create or replace function edge_symbol_lock_claim(p_symbol text, p_owner text)
returns boolean language plpgsql as $$
begin
  insert into strategy_symbol_lock (symbol, owner)
    values (p_symbol, p_owner)
  on conflict (symbol) do nothing;

  return exists (
    select 1 from strategy_symbol_lock
     where symbol = p_symbol and owner = p_owner
  );
end $$;

create or replace function edge_symbol_lock_release(p_symbol text, p_owner text)
returns boolean language plpgsql as $$
begin
  delete from strategy_symbol_lock
   where symbol = p_symbol and owner = p_owner;
  return found;
end $$;

alter table strategy_symbol_lock enable row level security;

create table if not exists dip_recovery_config (
  key         text primary key,
  value       text,
  payload     jsonb,
  updated_at  timestamptz not null default now()
);

insert into dip_recovery_config (key, value, payload) values
  ('core_strategies_enabled',      'TRUE',  null),
  ('dip_recovery_enabled',         'FALSE', null),
  ('max_hold_trading_days',        '47',    null),
  ('scan_interval_minutes',        '90',    null),
  ('entry_thresholds',             null,
    '{"min_dip_pct_intraday": 2.0, "min_dip_pct_5day": 4.0, "rsi_max": 35, "relvol_min": 1.2, "dip_score_min": 70}'::jsonb),
  ('sizing_thresholds',            null,
    '{"max_active_positions": 5, "max_new_entries_per_day": 3, "max_fills_before_stop_averaging": 4, "average_down_trigger_pct": -3.0, "average_down_min_price_drop_pct": 2.5, "average_down_min_minutes_since_fill": 30}'::jsonb),
  ('exit_thresholds',              null,
    '{"profit_target_pct": 5.0, "cost_buffer_pct": 0.25}'::jsonb)
on conflict (key) do nothing;

create or replace function edge_dip_recovery_config_json()
returns jsonb language sql stable as $$
  select coalesce(jsonb_object_agg(key, typed), '{}'::jsonb) from (
    select key,
           case
             when payload is not null then payload
             when value is null then 'null'::jsonb
             when lower(value) in ('true', 'false') then to_jsonb(lower(value)::boolean)
             when value ~ '^-?\d+$'                  then to_jsonb(value::bigint)
             when value ~ '^-?\d*\.\d+$'             then to_jsonb(value::numeric)
             else to_jsonb(value)
           end as typed
      from dip_recovery_config
  ) shaped;
$$;

create table if not exists dip_recovery_evaluations (
  id                bigserial primary key,
  created_at        timestamptz not null default now(),

  trading_day       date        not null,
  symbol            text        not null,
  cycle_id          text,
  score             numeric,
  passed            boolean     not null,
  factors           jsonb,
  rejection_reasons jsonb       not null default '[]'::jsonb
);

create index if not exists dip_recovery_evaluations_recent
  on dip_recovery_evaluations (trading_day desc, id desc);

create or replace function edge_dip_recovery_evaluations_append(p_rows jsonb)
returns integer language sql as $$
  with inserted as (
    insert into dip_recovery_evaluations
      (trading_day, symbol, cycle_id, score, passed, factors, rejection_reasons)
    select (r->>'trading_day')::date, r->>'symbol', r->>'cycle_id',
           nullif(r->>'score', '')::numeric, (r->>'passed')::boolean,
           r->'factors', coalesce(r->'rejection_reasons', '[]'::jsonb)
    from jsonb_array_elements(coalesce(p_rows, '[]'::jsonb)) as r
    returning 1
  )
  select count(*)::integer from inserted;
$$;

alter table dip_recovery_config       enable row level security;

alter table dip_recovery_evaluations  enable row level security;

create table if not exists dip_recovery_positions (
  symbol              text primary key,

  state               text        not null default 'WATCHING',
  opened_trading_day  date,
  clock_trading_day   date,

  weighted_avg_cost   numeric,
  total_quantity      numeric     not null default 0,
  total_fills         integer     not null default 0,
  latest_fill_at      timestamptz,
  fills               jsonb       not null default '[]'::jsonb,

  updated_at          timestamptz not null default now()
);

create or replace function edge_dip_recovery_positions_json()
returns jsonb language sql stable as $$
  select coalesce(
    jsonb_object_agg(
      symbol,
      jsonb_build_object(
        'state', state,
        'opened_trading_day', opened_trading_day,
        'clock_trading_day', clock_trading_day,
        'weighted_avg_cost', weighted_avg_cost,
        'total_quantity', total_quantity,
        'total_fills', total_fills,
        'latest_fill_at', latest_fill_at,
        'fills', fills
      )
    ),
    '{}'::jsonb
  )
  from dip_recovery_positions;
$$;

create or replace function edge_dip_recovery_positions_apply(p_updates jsonb)
returns integer language plpgsql as $$
declare
  sym     text;
  v       jsonb;
  applied integer := 0;
begin
  for sym, v in select * from jsonb_each(p_updates) loop
    if v->>'state' = 'WATCHING' then
      delete from dip_recovery_positions where symbol = sym;
    else
      insert into dip_recovery_positions
        (symbol, state, opened_trading_day, clock_trading_day,
         weighted_avg_cost, total_quantity, total_fills, latest_fill_at,
         fills, updated_at)
      values (
        sym,
        v->>'state',
        nullif(v->>'opened_trading_day', '')::date,
        nullif(v->>'clock_trading_day', '')::date,
        nullif(v->>'weighted_avg_cost', '')::numeric,
        coalesce((v->>'total_quantity')::numeric, 0),
        coalesce((v->>'total_fills')::integer, 0),
        nullif(v->>'latest_fill_at', '')::timestamptz,
        coalesce(v->'fills', '[]'::jsonb),
        now()
      )
      on conflict (symbol) do update set
        state              = excluded.state,
        opened_trading_day = excluded.opened_trading_day,
        clock_trading_day  = excluded.clock_trading_day,
        weighted_avg_cost  = excluded.weighted_avg_cost,
        total_quantity     = excluded.total_quantity,
        total_fills        = excluded.total_fills,
        latest_fill_at     = excluded.latest_fill_at,
        fills              = excluded.fills,
        updated_at         = now();
    end if;
    applied := applied + 1;
  end loop;
  return applied;
end $$;

alter table dip_recovery_positions enable row level security;

update dip_recovery_config
   set payload = '{"bollinger_period": 20, "bollinger_k": 2.0}'::jsonb || payload,
       updated_at = now()
 where key = 'entry_thresholds';

insert into dip_recovery_config (key, value, payload) values
  ('concentration', null,
    '{"position_cap_pct": 0.24, "sector_cap_pct": 0.30}'::jsonb)
on conflict (key) do nothing;

insert into dip_recovery_config (key, value, payload) values
  ('universe_thresholds',         null,
    '{"watchlist_size": 5, "min_dollar_volume": 20000000, "min_atr_pct": 1.5, "max_atr_pct": 8.0, "max_scan_survivors": 30}'::jsonb)
on conflict (key) do nothing;

create or replace function edge_dip_recovery_config_apply(p_updates jsonb)
returns integer language plpgsql as $$
declare
  k       text;
  v       jsonb;
  applied integer := 0;
begin
  for k, v in select * from jsonb_each(p_updates) loop
    if jsonb_typeof(v) in ('object', 'array') then
      insert into dip_recovery_config (key, value, payload, updated_at)
        values (k, null, v, now())
      on conflict (key) do update
        set payload = excluded.payload, value = null, updated_at = now();
    else
      insert into dip_recovery_config (key, value, payload, updated_at)
        values (k, case when jsonb_typeof(v) = 'null' then null else v #>> '{}' end,
                null, now())
      on conflict (key) do update
        set value = excluded.value, payload = null, updated_at = now();
    end if;
    applied := applied + 1;
  end loop;
  return applied;
end $$;

update dip_recovery_config
   set payload = '{"trend_band_pct": 0.10, "freefall_drop_pct": 15.0, "freefall_window_sessions": 10}'::jsonb || payload,
       updated_at = now()
 where key = 'entry_thresholds';

insert into edge_control (key, value, payload, cycle_type) values
  ('full_send_enabled', 'FALSE', null, null)
on conflict (key) do nothing;

create or replace function edge_loss_closes_recent(p_days integer default 45)
returns jsonb language sql stable as $$
  select coalesce(jsonb_agg(jsonb_build_object(
    'symbol',        symbol,
    'close_date',    close_date,
    'loss_amount',   loss_amount,
    'basis_unknown', basis_unknown
  )), '[]'::jsonb)
  from edge_loss_closes
  where close_date >= current_date - p_days;
$$;

create table if not exists edge_session_stats (
  cycle_id     text primary key,
  cycle_type   text,
  trading_day  date,
  started_at   timestamptz,
  finished_at  timestamptz,
  turns        integer,
  stale_count  integer,
  fresh_count  integer,
  tool_calls   integer,
  bytes_in     bigint,
  model        text,
  notes        text,
  status text,
  current_step text,
  current_step_at timestamptz,
  last_beat_at timestamptz,
  step_count integer not null default 0,
  rows_written integer not null default 0,
  runner_turns integer,
  runner_tool_calls integer,
  runner_bytes_in bigint
);

create or replace function edge_session_start(
  p_cycle_id    text,
  p_cycle_type  text,
  p_trading_day date,
  p_model       text
) returns void language sql as $$
  insert into edge_session_stats (cycle_id, cycle_type, trading_day, model, started_at)
  values (p_cycle_id, p_cycle_type, p_trading_day, p_model, now())
  on conflict (cycle_id) do update
    set cycle_type  = excluded.cycle_type,
        trading_day  = excluded.trading_day,
        model        = excluded.model,
        started_at   = now();
$$;

create table if not exists edge_fill_quality (
  id              bigint generated always as identity primary key,
  cycle_id        text not null,
  trading_day     date,
  symbol          text not null,
  side            text not null,
  intended_qty    numeric,
  filled_qty      numeric,
  limit_price     numeric,
  avg_fill_price  numeric,
  minutes_to_fill numeric,
  slippage        numeric generated always as (
    case
      when limit_price is null or limit_price = 0 or avg_fill_price is null then null
      when side = 'buy'  then (avg_fill_price - limit_price) / limit_price
      when side = 'sell' then (limit_price - avg_fill_price) / limit_price
      else null
    end
  ) stored,

  created_at      timestamptz not null default now()
);

create or replace function edge_fill_quality_write(
  p_rows        jsonb,
  p_cycle_id    text,
  p_trading_day date
) returns integer language sql as $$
  with inserted as (
    insert into edge_fill_quality (cycle_id, trading_day, symbol, side,
                                   intended_qty, filled_qty, limit_price,
                                   avg_fill_price, minutes_to_fill)
    select p_cycle_id, p_trading_day, r->>'symbol', r->>'side',
           nullif(r->>'intended_qty',    '')::numeric,
           nullif(r->>'filled_qty',      '')::numeric,
           nullif(r->>'limit_price',     '')::numeric,
           nullif(r->>'avg_fill_price',  '')::numeric,
           nullif(r->>'minutes_to_fill', '')::numeric
    from jsonb_array_elements(coalesce(p_rows, '[]'::jsonb)) as r
    returning 1
  )
  select count(*)::integer from inserted;
$$;

create or replace function edge_fill_quality_recent(p_days integer default 7)
returns jsonb language sql stable as $$
  select coalesce(jsonb_agg(jsonb_build_object(
    'symbol',          symbol,
    'side',            side,
    'intended_qty',    intended_qty,
    'filled_qty',      filled_qty,
    'limit_price',     limit_price,
    'avg_fill_price',  avg_fill_price,
    'minutes_to_fill', minutes_to_fill,
    'slippage',        slippage
  )), '[]'::jsonb)
  from edge_fill_quality
  where trading_day >= current_date - p_days;
$$;

alter table edge_session_stats enable row level security;

alter table edge_fill_quality  enable row level security;

create or replace function edge_day_context_write_symbol(
  p_trading_day date,
  p_symbol text,
  p_symbol_data jsonb
)
returns void language sql as $$
  insert into edge_day_context (trading_day, market_data, fetched_at)
    values (
      p_trading_day,
      jsonb_build_object(p_symbol, p_symbol_data),
      now()
    )
  on conflict (trading_day) do update
    set market_data = jsonb_set(
          edge_day_context.market_data,
          array[p_symbol],
          coalesce(edge_day_context.market_data -> p_symbol, '{}'::jsonb) || p_symbol_data,
          true
        ),
        fetched_at = now();
$$;

create table if not exists edge_cycle_input (
  id          bigserial primary key,
  cycle_id    text        not null,
  kind        text        not null,
  symbol      text,
  payload     jsonb,
  created_at  timestamptz not null default now()
);

create index if not exists edge_cycle_input_cycle
  on edge_cycle_input (cycle_id, id);

create index if not exists edge_cycle_input_age
  on edge_cycle_input (created_at);

create table if not exists edge_cycle_output (
  cycle_id        text primary key,
  status          text,
  plan            jsonb,
  protection      jsonb,
  cancels         jsonb,
  control_updates jsonb,
  alerts_summary  jsonb,
  detail          text,
  trace           text,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  symbols jsonb,
  stale jsonb,
  statics_stale jsonb,
  fundamentals_needed jsonb
);

alter table edge_cycle_input  enable row level security;

alter table edge_cycle_output enable row level security;

create or replace function edge_cycle_run(p_cycle_id text)
returns jsonb language plpgsql as $$
declare
  sent jsonb := edge_cycle_dispatch(p_cycle_id, 'decide');
begin
  if not (sent->>'dispatched')::boolean then
    return jsonb_build_object('cycle_id', p_cycle_id, 'status', 'failed',
                              'detail', sent->>'reason');
  end if;
  return edge_cycle_await(p_cycle_id, 60, 'decide');
end $$;

create or replace function edge_cycle_finalize(p_cycle_id text)
returns jsonb language plpgsql as $$
declare
  sent jsonb := edge_cycle_dispatch(p_cycle_id, 'finalize');
begin
  if not (sent->>'dispatched')::boolean then
    return jsonb_build_object('cycle_id', p_cycle_id, 'status', 'failed',
                              'detail', sent->>'reason');
  end if;
  return edge_cycle_await(p_cycle_id, 60, 'finalize');
end $$;

create table if not exists edge_cycle_schedule (
  trading_day date        not null,
  slot_et     time        not null,
  cycle_type  text        not null,
  fetch_plan  jsonb,
  primary key (trading_day, slot_et),
  slot_at timestamp
);

alter table edge_cycle_schedule enable row level security;

revoke execute on function edge_cycle_run(text)                            from anon, authenticated, public;

revoke execute on function edge_cycle_finalize(text)                       from anon, authenticated, public;

create table if not exists edge_regime (
  trading_day date        primary key,
  payload     jsonb       not null default '{}'::jsonb,
  computed_at timestamptz not null default now()
);

create or replace function edge_regime_write(p_trading_day date, p_payload jsonb)
returns void language sql as $$
  insert into edge_regime (trading_day, payload, computed_at)
    values (p_trading_day, coalesce(p_payload, '{}'::jsonb), now())
  on conflict (trading_day) do update
    set payload = excluded.payload, computed_at = excluded.computed_at;
$$;

create or replace function edge_regime_recent(p_trading_day date)
returns jsonb language sql stable as $$
  select coalesce(
    (select payload from edge_regime where trading_day = p_trading_day),
    '{}'::jsonb
  );
$$;

alter table edge_regime enable row level security;

revoke execute on function edge_regime_write(date, jsonb)             from anon, authenticated, public;

revoke execute on function edge_regime_recent(date)                   from anon, authenticated, public;

create table if not exists edge_premarket_state (
  trading_day  date primary key,
  plan         jsonb       not null default '[]'::jsonb,
  next_index   integer     not null default 0,
  accum        jsonb       not null default '{}'::jsonb,
  credits_used integer     not null default 0,
  status       text        not null default 'in_progress',
  detail       text,
  started_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),
  finished_at  timestamptz
);

create or replace function edge_premarket_state_write(p_trading_day date, p_state jsonb)
returns void language sql as $$
  insert into edge_premarket_state (
    trading_day, plan, next_index, accum, credits_used, status, detail, finished_at
  )
  values (
    p_trading_day,
    coalesce(p_state->'plan', '[]'::jsonb),
    coalesce((p_state->>'next_index')::integer, 0),
    coalesce(p_state->'accum', '{}'::jsonb),
    coalesce((p_state->>'credits_used')::integer, 0),
    coalesce(p_state->>'status', 'in_progress'),
    p_state->>'detail',
    case when p_state->>'status' in ('ok', 'failed') then now() else null end
  )
  on conflict (trading_day) do update
    set plan         = excluded.plan,
        next_index   = excluded.next_index,
        accum        = excluded.accum,
        credits_used = excluded.credits_used,
        status       = excluded.status,
        detail       = excluded.detail,
        updated_at   = now(),
        finished_at  = excluded.finished_at;
$$;

alter table edge_premarket_state enable row level security;

revoke execute on function edge_premarket_state_write(date, jsonb) from anon, authenticated, public;

create or replace function edge_cycle_input_reap()
returns integer language plpgsql as $$
declare
  removed integer;
begin
  with gone as (
    delete from edge_cycle_input where created_at < now() - interval '7 days'
    returning 1
  )
  select count(*)::integer into removed from gone;

  delete from edge_cycle_output where created_at < now() - interval '30 days';
  delete from edge_cycle_schedule where trading_day < current_date - 7;
  delete from edge_premarket_state where trading_day < current_date - 7;

  return removed;
end $$;

revoke execute on function edge_cycle_input_reap()                         from anon, authenticated, public;

create table if not exists edge_protection (
  symbol      text not null,
  kind        text not null check (kind in ('stop', 'target')),
  price       numeric not null,
  quantity    numeric,
  play_id     text,
  reason      text,
  updated_at  timestamptz not null default now(),
  primary key (symbol, kind)
);

alter table edge_protection enable row level security;

create or replace function edge_protection_apply(p_orders jsonb)
returns integer language plpgsql as $$
declare
  written integer;
begin
  delete from edge_protection where true;

  insert into edge_protection (symbol, kind, price, quantity, play_id, reason)
  select
    o->>'symbol',
    o->>'kind',
    (o->>'stop_price')::numeric,
    (o->>'quantity')::numeric,
    o->>'play_id',
    o->>'reason'
  from jsonb_array_elements(coalesce(p_orders, '[]'::jsonb)) as o
  where o->>'kind' = 'stop';

  insert into edge_protection (symbol, kind, price, quantity, play_id, reason)
  select
    o->>'symbol',
    o->>'kind',
    (o->>'limit_price')::numeric,
    (o->>'quantity')::numeric,
    o->>'play_id',
    o->>'reason'
  from jsonb_array_elements(coalesce(p_orders, '[]'::jsonb)) as o
  where o->>'kind' = 'target';

  get diagnostics written = row_count;
  return written;
end $$;

revoke execute on function edge_protection_apply(jsonb) from anon, authenticated, public;

create or replace function edge_watch_dispatch()
returns jsonb
language plpgsql
security definer
set search_path = public, net
as $$
declare
  et_now  time := (now() at time zone 'America/New_York')::time;
  base    text := edge_secret('edge_core_url');
  token   text := edge_secret('edge_core_token');
  request bigint;
begin
  if et_now < time '10:00' or et_now > time '15:30' then
    return jsonb_build_object(
      'dispatched', false,
      'reason', format('%s ET is outside the 10:00-15:30 monitor window', et_now)
    );
  end if;

  if base is null or token is null then
    return jsonb_build_object(
      'dispatched', false,
      'reason', 'edge_core_url or edge_core_token is not in the vault'
    );
  end if;

  select net.http_post(
    url     := base || '/api/watch',
    headers := jsonb_build_object('Content-Type', 'application/json'),
    body    := jsonb_build_object('token', token),
    timeout_milliseconds := 60000
  ) into request;

  return jsonb_build_object('dispatched', true, 'request_id', request);
end $$;

revoke execute on function edge_watch_dispatch() from anon, authenticated, public;

insert into edge_control (key, value, payload, cycle_type)
  values ('needs_session', null, null, null)
  on conflict (key) do nothing;

create table if not exists edge_shadow_trades (
  variant        text not null,
  symbol         text not null,
  entry_day      date not null,
  play_id        text,
  entry_cycle_id text,
  quantity       numeric,
  entry          numeric,
  exit_day       date,
  exit           numeric,
  pnl_pct        numeric,
  reason         text not null,
  bars_source    text,
  updated_at     timestamptz not null default now(),
  primary key (variant, symbol, entry_day),
  pair_symbol text not null default '',
  atr numeric,
  fill_day date
);

create index if not exists edge_shadow_trades_window
  on edge_shadow_trades (entry_day);

create or replace function edge_shadow_recent(p_days integer default 7)
returns jsonb language sql stable as $fn$
  select coalesce(jsonb_agg(jsonb_build_object(
    'variant',   variant,
    'symbol',    symbol,
    'entry_day', entry_day,
    'exit_day',  exit_day,
    'pnl_pct',   pnl_pct,
    'reason',    reason
  ) order by exit_day nulls last, entry_day, variant, symbol), '[]'::jsonb)
  from edge_shadow_trades
  where entry_day >= (current_date - make_interval(days => greatest(p_days, 1)))::date;
$fn$;

create or replace function edge_shadow_cycles(p_days integer default 10)
returns jsonb language sql stable as $fn$
  select coalesce(jsonb_agg(jsonb_build_object(
    'cycle_id', cycle_id, 'first_seen', first_seen
  ) order by first_seen), '[]'::jsonb)
  from (
    select cycle_id, min(created_at) as first_seen
    from edge_cycle_input
    where created_at >= now() - make_interval(days => greatest(p_days, 1) * 2)
    group by cycle_id
  ) as cycles;
$fn$;

alter table edge_shadow_trades enable row level security;

create or replace function edge_shadow_dispatch()
returns jsonb
language plpgsql
security definer
set search_path = public, net
as $fn$
declare
  base    text := edge_secret('edge_core_url');
  token   text := edge_secret('edge_core_token');
  request bigint;
begin
  if base is null or token is null then
    return jsonb_build_object(
      'dispatched', false,
      'reason', 'edge_core_url or edge_core_token is not in the vault'
    );
  end if;

  select net.http_post(
    url     := base || '/api/shadow',
    headers := jsonb_build_object('Content-Type', 'application/json'),
    body    := jsonb_build_object('token', token, 'days', 10),
    timeout_milliseconds := 300000
  ) into request;

  return jsonb_build_object('dispatched', true, 'request_id', request);
end $fn$;

revoke execute on function edge_shadow_dispatch() from anon, authenticated, public;

revoke execute on function edge_shadow_recent(integer)      from anon, authenticated, public;

revoke execute on function edge_shadow_cycles(integer)      from anon, authenticated, public;

create or replace function edge_cycle_schedule_apply(p_rows jsonb)
returns integer language plpgsql as $$
declare
  written integer;
begin
  insert into edge_cycle_schedule (trading_day, slot_et, slot_at, cycle_type, fetch_plan)
  select
    (r->>'trading_day')::date,
    (r->>'slot_et')::time,
    (r->>'slot_at')::timestamp,
    r->>'cycle_type',
    coalesce(r->'fetch_plan', '[]'::jsonb)
  from jsonb_array_elements(coalesce(p_rows, '[]'::jsonb)) as r
  on conflict (trading_day, slot_et) do update set
    slot_at    = excluded.slot_at,
    cycle_type = excluded.cycle_type,
    fetch_plan = excluded.fetch_plan;

  get diagnostics written = row_count;
  return written;
end $$;

create or replace function edge_schedule_dispatch()
returns jsonb
language plpgsql
security definer
set search_path = public, net
as $$
declare
  et_now  time := (now() at time zone 'America/New_York')::time;
  base    text := edge_secret('edge_core_url');
  token   text := edge_secret('edge_core_token');
  request bigint;
begin
  if et_now < time '04:15' or et_now > time '05:15' then
    return jsonb_build_object(
      'dispatched', false,
      'reason', format('%s ET is outside the 04:15-05:15 schedule window', et_now)
    );
  end if;

  if base is null or token is null then
    return jsonb_build_object(
      'dispatched', false,
      'reason', 'edge_core_url or edge_core_token is not in the vault'
    );
  end if;

  select net.http_post(
    url     := base || '/api/schedule',
    headers := jsonb_build_object('Content-Type', 'application/json'),
    body    := jsonb_build_object('token', token),
    timeout_milliseconds := 60000
  ) into request;

  return jsonb_build_object('dispatched', true, 'request_id', request);
end $$;

create or replace function edge_cycle_release(
  p_cycle_id text,
  p_failure  jsonb default null
)
returns boolean language plpgsql as $$
declare
  released   boolean;
  et_now     timestamp := now() at time zone 'America/New_York';
  stats      edge_session_stats%rowtype;
  routine    text;
  stage      text;
  detail     text;
  next_slot  text;
begin
  update edge_control
     set value = 'none', cycle_type = null, updated_at = now()
   where key = 'cycle_in_progress' and value = p_cycle_id;
  released := found;

  if p_failure is null then
    return released;
  end if;

  select * into stats from edge_session_stats where cycle_id = p_cycle_id;

  routine := case stats.cycle_type
               when 'full'      then 'Full cycle'
               when 'monitor'   then 'Monitor cycle'
               when 'postclose' then 'Post-close cycle'
               when 'overnight' then 'Overnight cycle'
               when 'premarket' then 'Pre-market cycle'
               else 'Cycle'
             end;

  if jsonb_typeof(p_failure) = 'object' then
    stage  := p_failure->>'stage';
    detail := p_failure->>'detail';
  else
    detail := p_failure #>> '{}';
  end if;

  select to_char(coalesce(s.slot_at, s.trading_day + s.slot_et), 'Dy DD Mon HH24:MI')
    into next_slot
    from edge_cycle_schedule s
   where coalesce(s.slot_at, s.trading_day + s.slot_et) > et_now
   order by coalesce(s.slot_at, s.trading_day + s.slot_et)
   limit 1;

  perform edge_alerts_enqueue(jsonb_build_array(jsonb_build_object(
    'title', routine || ' failed',
    'body', concat_ws(' ',
      to_char(et_now, 'HH24:MI') || ' ET ' || to_char(et_now, 'Dy DD Mon') || '.',
      case when stage is not null then 'Stopped in ' || stage || ':' else 'Stopped:' end,
      case
        when coalesce(nullif(detail, ''), '') = '' then 'no detail was reported.'
        when right(trim(detail), 1) in ('.', '!', '?') then trim(detail)
        else trim(detail) || '.'
      end,
      'Nothing further was placed.',
      case when next_slot is not null
           then 'Next automatic attempt: ' || next_slot || ' ET.'
           else 'No further slot is scheduled — edge_cycle_schedule is empty '
                || 'past now; check the 04:30 ET schedule job.'
      end,
      'cycle ' || left(p_cycle_id, 8)
    ),
    'priority', 'high',
    'cycle_id', p_cycle_id
  )));

  insert into edge_session_stats (cycle_id, status, finished_at)
  values (p_cycle_id, 'failed', now())
  on conflict (cycle_id) do update
    set status = 'failed', finished_at = now();

  return released;
end $$;

create or replace function edge_watchdog()
returns jsonb language sql stable as $fn$
  select jsonb_build_object(
    'observed_at', now(),

    'queue', (
      select jsonb_build_object(
        'undelivered', count(*) filter (
          where telegram_delivered_at is null and telegram_attempts < 5
        ),
        'oldest_undelivered_age_s', coalesce(extract(epoch from now() - min(created_at) filter (
          where telegram_delivered_at is null and telegram_attempts < 5
        )), 0)::int,
        'telegram_dead', count(*) filter (where telegram_delivered_at is null and telegram_attempts >= 5)
      )
      from edge_alerts
      where created_at > now() - interval '7 days'
    ),

    'routine', (
      select jsonb_build_object(
        'last_at', max(created_at),
        'last_age_s', coalesce(extract(epoch from now() - max(created_at)), -1)::int,
        'last_cycle_type', (select cycle_type from edge_runlog order by id desc limit 1),
        'rows_today', count(*) filter (where trading_day = current_date)
      )
      from edge_runlog
    ),

    'lock', (
      select jsonb_build_object(
        'held_by', value,
        'held_for_s', case when value = 'none' then 0
                      else extract(epoch from now() - updated_at)::int end
      )
      from edge_control where key = 'cycle_in_progress'
    ),

    'control', (
      select jsonb_object_agg(key, value) from edge_control
      where key in ('trading_enabled', 'unreconciled', 'entries_blocked',
                    'high_water_mark', 'positions_held', 'divergence_streak',
                    'max_new_positions_per_day')
    ),
    'schedule', (
      select jsonb_build_object(
        'last_trading_day', max(s.trading_day),
        'days_ahead', count(distinct s.trading_day) filter (
          where s.trading_day >= (now() at time zone 'America/New_York')::date),
        'slots_today', count(*) filter (
          where s.trading_day = (now() at time zone 'America/New_York')::date),
        'missed', coalesce((
          select jsonb_agg(jsonb_build_object(
                   'trading_day', m.trading_day,
                   'slot_et', to_char(m.slot_et, 'HH24:MI'),
                   'cycle_type', m.cycle_type,
                   'late_minutes',
                     (extract(epoch from (now() at time zone 'America/New_York')
                              - coalesce(m.slot_at, m.trading_day + m.slot_et)) / 60)::int))
            from edge_cycle_schedule m
           where m.cycle_type = 'postclose'
             and m.trading_day = (now() at time zone 'America/New_York')::date
             and coalesce(m.slot_at, m.trading_day + m.slot_et)
                   < (now() at time zone 'America/New_York') - interval '30 minutes'
             and not exists (
                   select 1 from edge_session_stats ss
                    where ss.trading_day = m.trading_day
                      and ss.started_at is not null
                      and (ss.started_at at time zone 'America/New_York')
                            >= coalesce(m.slot_at, m.trading_day + m.slot_et)
                      and (ss.started_at at time zone 'America/New_York')
                            < coalesce(m.slot_at, m.trading_day + m.slot_et) + interval '30 minutes')
        ), '[]'::jsonb),
        'failed_today', (
          select count(*) from edge_session_stats
           where status = 'failed'
             and trading_day = (now() at time zone 'America/New_York')::date
        )
      )
      from edge_cycle_schedule s
    ),
    'benchmark', jsonb_build_object(
      'last_day', (select max(trading_day) from edge_benchmark),
      'days_behind', coalesce(
        (now() at time zone 'America/New_York')::date
          - (select max(trading_day) from edge_benchmark), -1),
      'sessions_behind', case
        when (select max(trading_day) from edge_benchmark) is null then -1
        else (
          select count(distinct s2.trading_day)::int
            from edge_cycle_schedule s2
           where s2.trading_day > (select max(trading_day) from edge_benchmark)
             and s2.trading_day < (now() at time zone 'America/New_York')::date
        )
      end
    )
  );
$fn$;

revoke execute on function edge_schedule_dispatch()         from anon, authenticated, public;

revoke execute on function edge_cycle_schedule_apply(jsonb) from anon, authenticated, public;

revoke execute on function edge_cycle_release(text, jsonb)  from anon, authenticated, public;

create or replace function edge_should_run(p_now timestamptz default now())
returns jsonb
language plpgsql
stable
as $$
declare
  et_now    timestamp := p_now at time zone 'America/New_York';
  slot      edge_cycle_schedule%rowtype;
  control   jsonb := edge_control_json();
  held      integer;
  cap       integer;
  remaining integer;
  day       date;
  needs     jsonb;
  has_needs boolean;
  full_today boolean;
  fresh_candidates boolean;
begin
  if control is null then
    return jsonb_build_object('run', false, 'reason', 'edge_control unreadable');
  end if;

  if coalesce((control->>'trading_enabled')::boolean, false) is not true then
    return jsonb_build_object('run', false, 'reason', 'trading_enabled is not true');
  end if;

  select * into slot
    from edge_cycle_schedule s
   where coalesce(s.slot_at, s.trading_day + s.slot_et) <= et_now
     and coalesce(s.slot_at, s.trading_day + s.slot_et) >  (et_now - interval '30 minutes')
   order by coalesce(s.slot_at, s.trading_day + s.slot_et) desc
   limit 1;

  if not found then
    return jsonb_build_object(
      'run', false,
      'reason', format('no scheduled slot within 30 minutes of %s ET', et_now)
    );
  end if;

  day       := slot.trading_day;
  held      := coalesce((control->>'positions_held')::integer, 0);
  cap       := coalesce((control->>'max_new_positions_per_day')::integer, 2);
  remaining := coalesce((control->'new_positions'->>'count')::integer, 0);
  if (control->'new_positions'->>'date') is distinct from day::text then
    remaining := cap;
  else
    remaining := greatest(0, cap - remaining);
  end if;

  if slot.cycle_type = 'overnight'
     and coalesce((control->>'has_whole_share_position')::boolean, false) is not true then
    return jsonb_build_object(
      'run', false, 'cycle_type', 'noop',
      'reason', 'no whole-share position, so no overnight exit is placeable (P1)'
    );
  end if;

  needs     := control->'needs_session';
  has_needs := needs is not null and needs <> 'null'::jsonb;

  if slot.cycle_type = 'monitor' and has_needs then
    return jsonb_build_object(
      'run',         true,
      'cycle_type',  slot.cycle_type,
      'trading_day', day,
      'fetch',       coalesce(slot.fetch_plan, '[]'::jsonb),
      'reason',      format('state change: %s', coalesce(needs->>'reason', 'needs_session set'))
    );
  end if;

  if slot.cycle_type = 'monitor' and not has_needs then
    full_today := (control->>'last_full_cycle') is not null
      and ((control->>'last_full_cycle')::timestamptz
             at time zone 'America/New_York')::date = day;

    fresh_candidates := coalesce(
      jsonb_array_length(control->'dip_recovery_watchlist'->'symbols') > 0, false);

    if full_today and (remaining = 0 or not fresh_candidates) then
      return jsonb_build_object(
        'run', false, 'cycle_type', 'noop',
        'reason', 'quiet: no state change, full cycle already ran today, '
                  || 'and entries/candidates are exhausted'
      );
    end if;
  end if;

  if slot.cycle_type <> 'postclose'
     and held = 0
     and coalesce((control->>'unreconciled')::boolean, false) is not true
     and coalesce((control->>'pending_events')::boolean, false) is not true
     and remaining = 0 then
    return jsonb_build_object(
      'run', false, 'cycle_type', 'noop',
      'reason', 'nothing held, nothing pending, nothing reconcilable, no entries left'
    );
  end if;

  return jsonb_build_object(
    'run',         true,
    'cycle_type',  slot.cycle_type,
    'trading_day', day,
    'fetch',       coalesce(slot.fetch_plan, '[]'::jsonb),
    'reason',      format('scheduled %s slot', slot.cycle_type)
  );
end $$;

alter table edge_session_stats
  add constraint edge_session_stats_status_check
  check (status is null or status in (
    'success', 'success_with_warnings', 'partial_success', 'blocked',
    'failed', 'skipped', 'stale_output', 'invalid_output'
  ));

create or replace function edge_session_status_apply(
  p_cycle_id text,
  p_status   text
) returns void language sql as $$
  insert into edge_session_stats (cycle_id, status)
  values (p_cycle_id, p_status)
  on conflict (cycle_id) do update
    set status = excluded.status;
$$;

revoke execute on function edge_session_status_apply(text, text) from anon, authenticated, public;

create table if not exists edge_data_files (
  name       text primary key,
  as_of      date not null,
  payload    jsonb not null,
  updated_at timestamptz not null default now()
);

alter table edge_data_files enable row level security;

create or replace function edge_data_file_upsert(
  p_name    text,
  p_as_of   date,
  p_payload jsonb
) returns void language sql as $$
  insert into edge_data_files (name, as_of, payload, updated_at)
  values (p_name, p_as_of, p_payload, now())
  on conflict (name) do update
    set as_of      = excluded.as_of,
        payload    = excluded.payload,
        updated_at = now();
$$;

revoke execute on function edge_data_file_upsert(text, date, jsonb) from anon, authenticated, public;

create or replace function edge_benchmark_latest()
returns jsonb
language sql
stable
as $$
  select coalesce(
    jsonb_build_object('trading_day', trading_day, 'equity', equity),
    'null'::jsonb
  )
  from edge_benchmark
  order by trading_day desc
  limit 1;
$$;

revoke execute on function public.edge_secret(text) from anon, authenticated, public;

revoke execute on function public.edge_alert_reap() from anon, authenticated, public;

do $$
declare
  r record;
begin
  for r in
    select p.oid, p.proname,
           pg_get_function_identity_arguments(p.oid) as args
      from pg_proc p
      join pg_namespace n on n.oid = p.pronamespace
     where n.nspname = 'public'
       and p.proname like 'edge\_%' escape '\'
       and not exists (
             select 1 from unnest(coalesce(p.proconfig, '{}'::text[])) c
              where c like 'search_path=%'
           )
  loop
    execute format(
      'alter function public.%I(%s) set search_path = public',
      r.proname, r.args
    );
  end loop;
end
$$;

alter default privileges in schema public
  revoke execute on functions from public;

create unique index if not exists edge_loss_closes_dedup
  on edge_loss_closes (
    symbol,
    close_date,
    coalesce(loss_amount, 'NaN'::numeric),
    coalesce(basis_unknown, false)
  );

create or replace function edge_loss_closes_append(p_rows jsonb, p_cycle_id text)
returns integer language sql set search_path = public as $$
  with inserted as (
    insert into edge_loss_closes (symbol, close_date, loss_amount, basis_unknown, cycle_id)
    select r->>'symbol', (r->>'close_date')::date,
           nullif(r->>'loss_amount', '')::numeric,
           (r->>'basis_unknown')::boolean,
           p_cycle_id
    from jsonb_array_elements(coalesce(p_rows, '[]'::jsonb)) as r
    on conflict (symbol, close_date, coalesce(loss_amount, 'NaN'::numeric), coalesce(basis_unknown, false))
    do nothing
    returning 1
  )
  select count(*)::integer from inserted;
$$;

create table if not exists edge_realized_trades (
  id             bigint generated always as identity primary key,
  symbol         text        not null,
  traded_at      timestamptz not null,
  trading_day    date        not null,
  side           text        not null default 'sell',
  quantity       numeric     not null,
  price          numeric,
  realized_gain  numeric     not null,
  cost_basis     numeric,
  play_id        text,
  near_flat      boolean     not null default false,
  entry_disputed boolean     not null default false,
  cycle_id       text,
  backfilled     boolean     not null default false,
  created_at     timestamptz not null default now(),
  unique (symbol, traded_at, quantity)
);

alter table edge_realized_trades enable row level security;

create or replace function edge_realized_trades_append(p_rows jsonb, p_cycle_id text)
returns integer language plpgsql set search_path = public as $$
declare
  added integer;
begin
  with inserted as (
    insert into edge_realized_trades (
      symbol, traded_at, trading_day, side, quantity, price, realized_gain,
      cost_basis, play_id, near_flat, entry_disputed, cycle_id)
    select r->>'symbol',
           (r->>'traded_at')::timestamptz,
           (r->>'trading_day')::date,
           coalesce(nullif(r->>'side', ''), 'sell'),
           (r->>'quantity')::numeric,
           nullif(r->>'price', '')::numeric,
           (r->>'realized_gain')::numeric,
           nullif(r->>'cost_basis', '')::numeric,
           nullif(r->>'play_id', ''),
           coalesce((r->>'near_flat')::boolean, false),
           coalesce((r->>'entry_disputed')::boolean, false),
           p_cycle_id
    from jsonb_array_elements(coalesce(p_rows, '[]'::jsonb)) as r
    on conflict (symbol, traded_at, quantity) do nothing
    returning play_id, realized_gain, near_flat
  ),
  credit as (
    select play_id,
           sum(realized_gain)                 as gain,
           count(*) filter (where near_flat)  as near_flat
    from inserted
    where play_id is not null and play_id <> 'MANUAL'
    group by play_id
  ),
  applied as (
    insert into edge_play_state (play_id, realized_pnl, near_flat_trades, updated_at)
    select play_id, gain, near_flat, now() from credit
    on conflict (play_id) do update set
      realized_pnl     = edge_play_state.realized_pnl + excluded.realized_pnl,
      near_flat_trades = edge_play_state.near_flat_trades + excluded.near_flat_trades,
      updated_at       = now()
    returning 1
  )
  select count(*)::integer into added from inserted;
  return added;
end $$;

create or replace function edge_realized_trades_recent(p_days integer default 7)
returns jsonb language sql stable set search_path = public as $$
  select coalesce(jsonb_agg(jsonb_build_object(
    'symbol',         symbol,
    'trading_day',    trading_day,
    'realized_gain',  realized_gain,
    'play_id',        play_id,
    'near_flat',      near_flat,
    'entry_disputed', entry_disputed
  ) order by traded_at), '[]'::jsonb)
  from edge_realized_trades
  where trading_day >= current_date - p_days;
$$;

create or replace function edge_play_state_json()
returns jsonb language sql stable as $$
  select coalesce(jsonb_object_agg(play_id, jsonb_build_object(
    'regime_fit',       regime_fit,
    'reason',           reason,
    'closed_trades',    closed_trades,
    'realized_pnl',     realized_pnl,
    'near_flat_trades', near_flat_trades
  )), '{}'::jsonb) from edge_play_state;
$$;

revoke execute on function edge_realized_trades_append(jsonb, text) from anon, authenticated, public;

revoke execute on function edge_realized_trades_recent(integer)     from anon, authenticated, public;

create or replace function edge_cycle_dispatch(p_cycle_id text, p_phase text default 'decide'::text)
returns jsonb
language plpgsql
security definer
set search_path to 'public', 'net'
as $function$
declare
  base    text := edge_secret('edge_core_url');
  token   text := edge_secret('edge_core_token');
  request bigint;
begin
  if base is null or token is null then
    return jsonb_build_object(
      'dispatched', false,
      'reason', 'edge_core_url or edge_core_token is not in the vault'
    );
  end if;

  if p_phase not in ('resolve', 'decide', 'finalize') then
    return jsonb_build_object('dispatched', false, 'reason', 'unknown phase');
  end if;

  select net.http_post(
    url     := base || '/api/cycle',
    headers := jsonb_build_object('Content-Type', 'application/json'),
    body    := jsonb_build_object(
                 'cycle_id', p_cycle_id,
                 'token',    token,
                 'phase',    p_phase
               ),
    timeout_milliseconds := 60000
  ) into request;

  return jsonb_build_object(
    'dispatched', true, 'cycle_id', p_cycle_id,
    'phase', p_phase, 'request_id', request
  );
end $function$;

revoke execute on function edge_cycle_dispatch(text, text)                 from anon, authenticated, public;

create or replace function edge_alert_send(batch integer default 10)
returns integer
language plpgsql
security definer
set search_path = public, net
as $$
declare
  alert          edge_alerts%rowtype;
  bot_token      text := edge_secret('edge_telegram_bot_token');
  chat_id        text := edge_secret('edge_telegram_chat_id');
  request        bigint;
  sent           integer := 0;
begin
  for alert in
    with claimed as (
      update edge_alerts a
         set claimed_at = now()
       where a.id in (
         select id
           from edge_alerts
          where telegram_delivered_at is null and telegram_attempts < 5
            and (claimed_at is null or claimed_at < now() - interval '2 minutes')
          order by created_at
          limit batch
            for update skip locked
       )
      returning a.*
    )
    select * from claimed
  loop
    if alert.telegram_delivered_at is null and alert.telegram_attempts < 5
       and bot_token is not null and chat_id is not null then
      select net.http_post(
        url     := 'https://api.telegram.org/bot' || bot_token || '/sendMessage',
        headers := jsonb_build_object('Content-Type', 'application/json'),
        body    := jsonb_build_object(
                     'chat_id', chat_id,
                     'text',    alert.title || E'\n' || alert.body
                   ),
        timeout_milliseconds := 15000
      ) into request;

      insert into edge_alert_dispatch (request_id, alert_id, channel)
        values (request, alert.id, 'telegram')
        on conflict do nothing;

      update edge_alerts set telegram_attempts = telegram_attempts + 1 where id = alert.id;
      sent := sent + 1;
    end if;
  end loop;

  return sent;
end;
$$;

revoke execute on function public.edge_alert_send(integer) from anon, authenticated, public;

insert into edge_control (key, value, payload, cycle_type) values
  ('active_sectors', null, '[]'::jsonb, null)
on conflict (key) do nothing;

create or replace function edge_day_context_write_symbols(p_trading_day date, p_symbols jsonb)
returns integer
language plpgsql
set search_path = public
as $$
declare
  merged  jsonb;
  sym     text;
  data    jsonb;
  written integer := 0;
begin
  if p_symbols is null or jsonb_typeof(p_symbols) <> 'object' then
    return 0;
  end if;

  insert into edge_day_context (trading_day, market_data, fetched_at)
    values (p_trading_day, '{}'::jsonb, now())
  on conflict (trading_day) do nothing;

  select market_data into merged
    from edge_day_context where trading_day = p_trading_day for update;

  for sym, data in select * from jsonb_each(p_symbols) loop
    merged := jsonb_set(
      coalesce(merged, '{}'::jsonb),
      array[sym],
      coalesce(merged -> sym, '{}'::jsonb) || data,
      true
    );
    written := written + 1;
  end loop;

  update edge_day_context
     set market_data = merged, fetched_at = now()
   where trading_day = p_trading_day;

  return written;
end $$;

revoke execute on function edge_day_context_write_symbols(date, jsonb) from anon, authenticated, public;

grant  execute on function edge_day_context_write_symbols(date, jsonb) to service_role;

create or replace function edge_premarket_dispatch()
returns jsonb
language plpgsql
security definer
set search_path = public, net
as $$
declare
  et_now  time := (now() at time zone 'America/New_York')::time;
  base    text := edge_secret('edge_core_url');
  token   text := edge_secret('edge_core_token');
  request bigint;
begin
  if et_now < time '04:30' or et_now > time '07:15' then
    return jsonb_build_object(
      'dispatched', false,
      'reason', format('%s ET is outside the 04:30-07:15 pre-market window', et_now)
    );
  end if;

  if base is null or token is null then
    return jsonb_build_object(
      'dispatched', false,
      'reason', 'edge_core_url or edge_core_token is not in the vault'
    );
  end if;

  select net.http_post(
    url     := base || '/api/premarket',
    headers := jsonb_build_object('Content-Type', 'application/json'),
    body    := jsonb_build_object('token', token),
    timeout_milliseconds := 60000
  ) into request;

  return jsonb_build_object('dispatched', true, 'request_id', request);
end $$;

revoke execute on function edge_premarket_dispatch()                  from anon, authenticated, public;

create table if not exists edge_step_kind (
  step        text primary key,
  ord         integer not null,
  phase       text not null,
  ceiling_s   integer not null default 900,
  kinds       text[] not null default '{}',
  source      text not null default 'trigger',
  description text
);

alter table edge_step_kind enable row level security;

create table if not exists edge_cycle_step (
  id       bigint generated always as identity primary key,
  cycle_id text not null,
  step     text not null,
  event    text not null check (event in ('start', 'end', 'beat', 'warn', 'abort')),
  detail   jsonb,
  at       timestamptz not null default clock_timestamp(),
  source   text not null check (source in ('trigger', 'core', 'runner', 'watchdog'))
);

create index if not exists edge_cycle_step_cycle_idx on edge_cycle_step (cycle_id, id);

create index if not exists edge_cycle_step_at_idx    on edge_cycle_step (at desc);

alter table edge_cycle_step enable row level security;

create or replace function edge_step_stamp(
  p_cycle_id text,
  p_step     text,
  p_event    text,
  p_detail   jsonb default null,
  p_source   text  default 'core'
) returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  t timestamptz := clock_timestamp();
begin
  if p_cycle_id is null or p_cycle_id in ('', 'none') then
    return;
  end if;
  begin
    insert into edge_cycle_step (cycle_id, step, event, detail, at, source)
    values (p_cycle_id, p_step, p_event, p_detail, t, p_source);

    insert into edge_session_stats as s (cycle_id, current_step, current_step_at, last_beat_at, step_count)
    values (p_cycle_id, p_step, t, t, 1)
    on conflict (cycle_id) do update set
      current_step_at = case when s.current_step is distinct from excluded.current_step
                             then t else s.current_step_at end,
      current_step    = excluded.current_step,
      last_beat_at    = t,
      step_count      = s.step_count + 1;
  exception when others then
    null;
  end;
end $$;

create or replace function edge_current_cycle() returns text
language sql stable set search_path = public as $$
  select value from edge_control
   where key = 'cycle_in_progress' and value is not null and value <> 'none';
$$;

create or replace function edge_step_for_kind(p_kind text) returns text
language sql stable set search_path = public as $$
  select coalesce(
    (select step from edge_step_kind where p_kind = any (kinds) order by ord limit 1),
    'S2.post_batch'
  );
$$;

create or replace function edge_step_on_input() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  begin
    perform edge_step_beat(new.cycle_id, edge_step_for_kind(new.kind),
                           jsonb_build_object('kind', new.kind), 1);
  exception when others then
    null;
  end;
  return null;
end $$;

drop trigger if exists edge_step_input_trg on edge_cycle_input;

create trigger edge_step_input_trg
  after insert on edge_cycle_input
  for each row execute function edge_step_on_input();

create or replace function edge_step_on_output() returns trigger
language plpgsql security definer set search_path = public as $$
declare
  v_step   text;
  v_event  text := 'beat';
  v_detail jsonb;
begin
  begin
    v_step := case new.status
                when 'resolved'  then 'S2a.resolve'
                when 'decided'   then 'S3.decide'
                when 'noop'      then 'S3.decide'
                when 'finalized' then 'S6.finalize'
                when 'failed'    then 'ABORT'
                else null
              end;
    if v_step is null then
      return null;
    end if;
    v_detail := jsonb_build_object('status', new.status);
    if new.status = 'resolved' then
      v_detail := v_detail || jsonb_build_object(
        'symbols',       case when jsonb_typeof(new.symbols) = 'array' then jsonb_array_length(new.symbols) else 0 end,
        'stale',         case when jsonb_typeof(new.stale) = 'array' then jsonb_array_length(new.stale) else 0 end,
        'statics_stale', case when jsonb_typeof(new.statics_stale) = 'array' then jsonb_array_length(new.statics_stale) else 0 end
      );
    elsif new.status = 'failed' then
      v_event  := 'abort';
      v_detail := v_detail || jsonb_build_object('error', left(coalesce(new.detail, ''), 300));
    end if;
    perform edge_step_stamp(new.cycle_id, v_step, v_event, v_detail, 'trigger');
  exception when others then
    null;
  end;
  return null;
end $$;

drop trigger if exists edge_step_output_ins on edge_cycle_output;

create trigger edge_step_output_ins
  after insert on edge_cycle_output
  for each row execute function edge_step_on_output();

drop trigger if exists edge_step_output_upd on edge_cycle_output;

create trigger edge_step_output_upd
  after update on edge_cycle_output
  for each row when (old.status is distinct from new.status)
  execute function edge_step_on_output();

create or replace function edge_step_on_day_context() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  begin
    perform edge_step_beat(edge_current_cycle(), 'S2a.day_context', null, 1);
  exception when others then
    null;
  end;
  return null;
end $$;

drop trigger if exists edge_step_day_context_trg on edge_day_context;

create trigger edge_step_day_context_trg
  after insert or update on edge_day_context
  for each row execute function edge_step_on_day_context();

create or replace function edge_step_on_runlog() returns trigger
language plpgsql security definer set search_path = public as $$
declare
  v_step text := 'core.' || new.command;
begin
  begin
    if exists (select 1 from edge_step_kind where step = v_step) then
      perform edge_step_stamp(new.cycle_id, v_step, 'beat',
                              jsonb_build_object('runlog_id', new.id), 'trigger');
    end if;
  exception when others then
    null;
  end;
  return null;
end $$;

drop trigger if exists edge_step_runlog_trg on edge_runlog;

create trigger edge_step_runlog_trg
  after insert on edge_runlog
  for each row execute function edge_step_on_runlog();

create or replace function edge_step_on_lock() returns trigger
language plpgsql security definer set search_path = public as $$
declare
  v_step text;
begin
  begin
    if new.value is distinct from old.value then
      if new.value is not null and new.value <> 'none' then
        perform edge_step_stamp(
          new.value, 'S1.claim', 'beat',
          jsonb_build_object(
            'cycle_type', new.cycle_type,
            'stole_from', case when old.value is not null and old.value <> 'none' then old.value end
          ),
          'trigger'
        );
        if old.value is not null and old.value <> 'none' then
          perform edge_step_stamp(old.value, 'S6.release', 'warn',
                                  jsonb_build_object('stolen_by', new.value), 'trigger');
        end if;
      elsif old.value is not null and old.value <> 'none' then
        perform edge_step_stamp(old.value, 'S6.release', 'beat', null, 'trigger');
      end if;
    elsif new.value is not null and new.value <> 'none' then
      select coalesce(current_step, 'S1.claim') into v_step
        from edge_session_stats where cycle_id = new.value;
      perform edge_step_beat(new.value, coalesce(v_step, 'S1.claim'),
                             jsonb_build_object('lock_renewed', true), 0);
    end if;
  exception when others then
    null;
  end;
  return null;
end $$;

drop trigger if exists edge_step_lock_trg on edge_control;

create trigger edge_step_lock_trg
  after update on edge_control
  for each row when (new.key = 'cycle_in_progress')
  execute function edge_step_on_lock();

create or replace function edge_step_on_stats() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  begin
    if new.started_at is not null and (tg_op = 'INSERT' or new.started_at is distinct from old.started_at) then
      perform edge_step_stamp(new.cycle_id, 'S1.session_start', 'beat',
                              jsonb_build_object('cycle_type', new.cycle_type, 'model', new.model), 'trigger');
    end if;
    if new.finished_at is not null and (tg_op = 'INSERT' or new.finished_at is distinct from old.finished_at) then
      perform edge_step_stamp(new.cycle_id, 'S6.session_finish', 'beat',
                              jsonb_build_object('status', new.status), 'trigger');
    end if;
    if new.status = 'failed' and (tg_op = 'INSERT' or old.status is distinct from new.status) then
      perform edge_step_stamp(new.cycle_id, 'ABORT', 'abort',
                              jsonb_build_object('status', new.status, 'notes', left(coalesce(new.notes, ''), 300)),
                              'trigger');
    end if;
  exception when others then
    null;
  end;
  return null;
end $$;

drop trigger if exists edge_step_stats_ins on edge_session_stats;

create trigger edge_step_stats_ins
  after insert on edge_session_stats
  for each row
  when (new.started_at is not null or new.finished_at is not null or new.status is not null)
  execute function edge_step_on_stats();

drop trigger if exists edge_step_stats_upd on edge_session_stats;

create trigger edge_step_stats_upd
  after update on edge_session_stats
  for each row
  when (new.started_at is distinct from old.started_at
        or new.finished_at is distinct from old.finished_at
        or new.status is distinct from old.status)
  execute function edge_step_on_stats();

create or replace view edge_cycle_progress with (security_invoker = on) as
select s.cycle_id,
       s.cycle_type,
       s.trading_day,
       s.started_at,
       s.current_step,
       s.current_step_at,
       case when s.finished_at is null
            then round(extract(epoch from clock_timestamp() - s.current_step_at))::integer
       end                                                            as seconds_in_step,
       s.last_beat_at,
       round(extract(epoch from coalesce(s.finished_at, clock_timestamp()) - s.started_at) / 60.0, 1)
                                                                      as elapsed_min,
       s.rows_written,
       s.step_count,
       s.status,
       s.finished_at,
       (c.value = s.cycle_id)                                         as lock_held
  from edge_session_stats s
  left join edge_control c on c.key = 'cycle_in_progress'
 where coalesce(s.started_at, s.current_step_at) > now() - interval '48 hours'
 order by coalesce(s.started_at, s.current_step_at) desc;

create or replace function edge_cycle_timeline(p_cycle_id text)
returns table (
  step         text,
  ord          integer,
  first_at     timestamptz,
  last_at      timestamptz,
  seconds      numeric,
  gap_before_s numeric,
  events       integer,
  warns        integer,
  last_detail  jsonb
)
language sql stable set search_path = public as $$
  with g as (
    select l.step,
           min(l.at)                                            as first_at,
           max(l.at)                                            as last_at,
           count(*)::integer                                    as events,
           (count(*) filter (where l.event in ('warn', 'abort')))::integer as warns,
           (array_agg(l.detail order by l.id desc) filter (where l.detail is not null))[1] as last_detail
      from edge_cycle_step l
     where l.cycle_id = p_cycle_id
     group by l.step
  )
  select g.step,
         coalesce(k.ord, 999),
         g.first_at,
         g.last_at,
         round(extract(epoch from g.last_at - g.first_at)::numeric, 1),
         greatest(0, round(extract(epoch from g.first_at - max(g.last_at) over (
           order by g.first_at rows between unbounded preceding and 1 preceding))::numeric, 1)),
         g.events,
         g.warns,
         g.last_detail
    from g
    left join edge_step_kind k on k.step = g.step
   order by g.first_at, coalesce(k.ord, 999);
$$;

create or replace function edge_cycle_step_reap() returns integer
language sql security definer set search_path = public as $$
  with gone as (delete from edge_cycle_step where at < now() - interval '30 days' returning 1)
  select count(*)::integer from gone;
$$;

revoke all on edge_cycle_step, edge_step_kind from anon, authenticated;

revoke all on edge_cycle_progress from anon, authenticated;

revoke execute on function edge_step_stamp(text, text, text, jsonb, text)   from anon, authenticated, public;

revoke execute on function edge_cycle_step_reap()                           from anon, authenticated, public;

revoke execute on function edge_cycle_timeline(text)                        from anon, authenticated, public;

revoke execute on function edge_step_on_input()       from anon, authenticated, public;

revoke execute on function edge_step_on_output()      from anon, authenticated, public;

revoke execute on function edge_step_on_day_context() from anon, authenticated, public;

revoke execute on function edge_step_on_runlog()      from anon, authenticated, public;

revoke execute on function edge_step_on_lock()        from anon, authenticated, public;

revoke execute on function edge_step_on_stats()       from anon, authenticated, public;

grant  execute on function edge_step_stamp(text, text, text, jsonb, text)   to service_role;

grant  execute on function edge_cycle_timeline(text)                        to service_role;

create or replace function edge_alerts_enqueue(p_alerts jsonb)
returns integer
language plpgsql
set search_path = public
as $$
declare
  a            jsonb;
  v_title      text;
  v_body       text;
  v_priority   text;
  v_cycle_id   text;
  prior        record;
  inserted     integer := 0;
  rank         integer;
  prior_rank   integer;
begin
  for a in select * from jsonb_array_elements(coalesce(p_alerts, '[]'::jsonb))
  loop
    v_title    := a->>'title';
    v_body     := a->>'body';
    v_priority := coalesce(a->>'priority', 'default');
    v_cycle_id := a->>'cycle_id';

    if v_title is null or not (
      v_title ~ '^(Full|Pre-market|Monitor|Post-close|Overnight) cycle '
                '(succeeded|succeeded with warnings|partial|blocked|failed|skipped)$'
      or v_title in (
        'Drawdown halt', 'Account read implausible', 'Weekly scorecard',
        'Benchmark integrity — unexplained equity move', 'Order refused',
        'Manual action required',
        'Cycle failed',
        'Pre-market data job degraded',
        'Cycle stalled', 'Cycle over budget', 'Step log gap',
        'Resolve scope trimmed'
      )
    ) then
      raise exception 'edge_alerts_enqueue: title not canonical: %', v_title;
    end if;

    rank := case v_priority
              when 'urgent'  then 3
              when 'high'    then 2
              when 'default' then 1
              else                0
            end;

    select ea.priority into prior
      from edge_alerts ea
     where ea.title = v_title
       and md5(ea.body) = md5(v_body)
       and (ea.created_at at time zone 'America/New_York')::date
         = (now() at time zone 'America/New_York')::date
     order by ea.created_at desc
     limit 1;

    if found then
      prior_rank := case prior.priority
                      when 'urgent'  then 3
                      when 'high'    then 2
                      when 'default' then 1
                      else                0
                    end;
      if rank <= prior_rank then
        continue;
      end if;
    end if;

    insert into edge_alerts (title, body, priority, cycle_id)
      values (v_title, v_body, v_priority, v_cycle_id);
    inserted := inserted + 1;
  end loop;

  return inserted;
end;
$$;

create or replace function edge_cycle_output_write(p_cycle_id text, p_fields jsonb)
returns void
language sql
set search_path to 'public'
as $function$
  insert into edge_cycle_output as o (
    cycle_id, status, plan, protection, cancels,
    control_updates, alerts_summary, detail, trace, symbols,
    stale, statics_stale, fundamentals_needed
  )
  values (
    p_cycle_id,
    p_fields->>'status',
    p_fields->'plan',
    p_fields->'protection',
    p_fields->'cancels',
    p_fields->'control_updates',
    p_fields->'alerts_summary',
    p_fields->>'detail',
    p_fields->>'trace',
    p_fields->'symbols',
    p_fields->'stale',
    p_fields->'statics_stale',
    p_fields->'fundamentals_needed'
  )
  on conflict (cycle_id) do update set
    status              = coalesce(excluded.status,              o.status),
    plan                = coalesce(excluded.plan,                o.plan),
    protection          = coalesce(excluded.protection,          o.protection),
    cancels             = coalesce(excluded.cancels,             o.cancels),
    control_updates     = coalesce(excluded.control_updates,     o.control_updates),
    alerts_summary      = coalesce(excluded.alerts_summary,      o.alerts_summary),
    detail              = coalesce(excluded.detail,              o.detail),
    trace               = coalesce(excluded.trace,               o.trace),
    symbols             = coalesce(excluded.symbols,             o.symbols),
    stale               = coalesce(excluded.stale,               o.stale),
    statics_stale       = coalesce(excluded.statics_stale,       o.statics_stale),
    fundamentals_needed = coalesce(excluded.fundamentals_needed, o.fundamentals_needed),
    updated_at          = now();
$function$;

revoke execute on function edge_cycle_output_write(text, jsonb)            from anon, authenticated, public;

create or replace function edge_batch_rule_mode() returns text
language sql stable set search_path = public as $$
  select case
           when lower(coalesce((select value from edge_control where key = 'enforce_batch_2a'), 'warn')) = 'reject'
             then 'reject'
           else 'warn'
         end;
$$;

create or replace function edge_batch_limit(p_key text, p_default integer) returns integer
language sql stable set search_path = public as $$
  select coalesce(
    (select value::integer from edge_control where key = p_key and value ~ '^\d+$'),
    p_default
  );
$$;

create or replace function edge_cycle_input_write(p_cycle_id text, p_kind text, p_symbol text, p_payload jsonb)
returns bigint
language plpgsql
set search_path to 'public'
as $function$
declare
  v_id bigint;
begin
  if p_kind in ('quotes', 'tradability', 'fundamentals', 'earnings', 'signals') then
    if edge_batch_rule_mode() = 'reject' then
      raise exception
        'edge_cycle_input_write: % rows must be posted in one edge_cycle_input_write_batch call '
        '(runner-prompt.md Step 2a; edge_control.enforce_batch_2a = reject)', p_kind;
    end if;
    perform edge_step_stamp(
      p_cycle_id, edge_step_for_kind(p_kind), 'warn',
      jsonb_build_object('per_row_write', true, 'kind', p_kind, 'symbol', p_symbol),
      'trigger'
    );
  end if;

  insert into edge_cycle_input (cycle_id, kind, symbol, payload)
  values (p_cycle_id, p_kind, nullif(p_symbol, ''), p_payload)
  returning id into v_id;
  return v_id;
end $function$;

revoke execute on function edge_cycle_input_write(text, text, text, jsonb) from anon, authenticated, public;

create or replace function edge_cycle_input_write_batch(p_cycle_id text, p_rows jsonb)
returns bigint[]
language plpgsql
set search_path to 'public'
as $function$
declare
  v_rows     integer := case when jsonb_typeof(p_rows) = 'array' then jsonb_array_length(p_rows) else 0 end;
  v_kb       numeric := octet_length(coalesce(p_rows, '[]'::jsonb)::text) / 1024.0;
  v_max_rows integer := edge_batch_limit('max_batch_rows', 200);
  v_max_kb   integer := edge_batch_limit('max_batch_kb', 200);
  v_ids      bigint[];
begin
  if v_rows > v_max_rows or v_kb > v_max_kb then
    if edge_batch_rule_mode() = 'reject' then
      raise exception
        'edge_cycle_input_write_batch: % rows / % KB is over the limit (max_batch_rows %, max_batch_kb %) '
        '— post one batch per kind (runner-prompt.md Step 2a; edge_control.enforce_batch_2a = reject)',
        v_rows, round(v_kb), v_max_rows, v_max_kb;
    end if;
    perform edge_step_stamp(
      p_cycle_id, edge_step_for_kind(p_rows -> 0 ->> 'kind'), 'warn',
      jsonb_build_object('oversize_batch', true, 'rows', v_rows, 'kb', round(v_kb),
                         'max_rows', v_max_rows, 'max_kb', v_max_kb),
      'trigger'
    );
  end if;

  with inserted as (
    insert into edge_cycle_input (cycle_id, kind, symbol, payload)
    select p_cycle_id, r.kind, nullif(r.symbol, ''), r.payload
    from jsonb_to_recordset(p_rows) as r(kind text, symbol text, payload jsonb)
    returning id
  )
  select array_agg(id) into v_ids from inserted;
  return v_ids;
end $function$;

revoke execute on function edge_batch_rule_mode()            from anon, authenticated, public;

revoke execute on function edge_batch_limit(text, integer)   from anon, authenticated, public;

create table if not exists edge_deployment_decision (
  cycle_id            text primary key,
  trading_day         date not null,
  cycle_type          text,
  posture             text,
  posture_enforced    boolean not null default false,
  breadth_200         numeric,
  breadth_50          numeric,
  funnel              jsonb not null default '{}'::jsonb,
  skip_counts         jsonb not null default '{}'::jsonb,
  cash                numeric,
  equity              numeric,
  target_invested_pct numeric,
  invested_pct        numeric,
  decision            text not null check (decision in ('DEPLOY', 'PARTIAL', 'NONE')),
  reason              text not null,
  detail              text,
  intended            boolean not null default false,
  placed              integer,
  shadow              jsonb not null default '{}'::jsonb,
  payload             jsonb not null default '{}'::jsonb,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);

create index if not exists edge_deployment_decision_day_idx
  on edge_deployment_decision (trading_day desc, created_at desc);

alter table edge_deployment_decision enable row level security;

create or replace function edge_deployment_decision_write(p_row jsonb)
returns text
language plpgsql
set search_path = public
as $$
declare
  v_cycle_id text := p_row->>'cycle_id';
begin
  if v_cycle_id is null or v_cycle_id = '' then
    raise exception 'edge_deployment_decision_write: cycle_id is required';
  end if;

  insert into edge_deployment_decision (
    cycle_id, trading_day, cycle_type, posture, posture_enforced,
    breadth_200, breadth_50, funnel, skip_counts, cash, equity,
    target_invested_pct, invested_pct, decision, reason, detail, intended,
    placed, shadow, payload
  ) values (
    v_cycle_id,
    (p_row->>'trading_day')::date,
    p_row->>'cycle_type',
    p_row->>'posture',
    coalesce((p_row->>'posture_enforced')::boolean, false),
    (p_row->>'breadth_200')::numeric,
    (p_row->>'breadth_50')::numeric,
    coalesce(p_row->'funnel', '{}'::jsonb),
    coalesce(p_row->'skip_counts', '{}'::jsonb),
    (p_row->>'cash')::numeric,
    (p_row->>'equity')::numeric,
    (p_row->>'target_invested_pct')::numeric,
    (p_row->>'invested_pct')::numeric,
    p_row->>'decision',
    p_row->>'reason',
    p_row->>'detail',
    coalesce((p_row->>'intended')::boolean, false),
    (p_row->>'placed')::integer,
    coalesce(p_row->'shadow', '{}'::jsonb),
    p_row
  )
  on conflict (cycle_id) do update set
    trading_day         = excluded.trading_day,
    cycle_type          = excluded.cycle_type,
    posture             = excluded.posture,
    posture_enforced    = excluded.posture_enforced,
    breadth_200         = excluded.breadth_200,
    breadth_50          = excluded.breadth_50,
    funnel              = excluded.funnel,
    skip_counts         = excluded.skip_counts,
    cash                = excluded.cash,
    equity              = excluded.equity,
    target_invested_pct = excluded.target_invested_pct,
    invested_pct        = excluded.invested_pct,
    decision            = excluded.decision,
    reason              = excluded.reason,
    detail              = excluded.detail,
    intended            = excluded.intended,
    placed              = excluded.placed,
    shadow              = excluded.shadow,
    payload             = excluded.payload,
    updated_at          = now();

  return v_cycle_id;
end;
$$;

revoke all on edge_deployment_decision from anon, authenticated;

revoke execute on function edge_deployment_decision_write(jsonb) from anon, authenticated, public;

grant  execute on function edge_deployment_decision_write(jsonb) to service_role;

create or replace function edge_cycle_budget_min(p_cycle_type text) returns integer
language sql stable set search_path = public as $$
  select coalesce(
    (select (payload ->> p_cycle_type)::integer
       from edge_control
      where key = 'cycle_budget_min' and payload ->> p_cycle_type ~ '^\d+$'),
    case p_cycle_type
      when 'full' then 40
      when 'monitor' then 20
      when 'dip-monitor' then 15
      when 'postclose' then 10
      when 'overnight' then 20
      else 30
    end
  );
$$;

create or replace function edge_cycle_budget(p_cycle_id text) returns jsonb
language sql set search_path = public as $$
  select coalesce(
    (select jsonb_build_object(
              'elapsed_min',
                round((extract(epoch from clock_timestamp() - s.started_at) / 60.0)::numeric, 1),
              'budget_min', edge_cycle_budget_min(s.cycle_type),
              'budget_left_min',
                round((edge_cycle_budget_min(s.cycle_type)
                       - extract(epoch from clock_timestamp() - s.started_at) / 60.0)::numeric, 1)
            )
       from edge_session_stats s
      where s.cycle_id = p_cycle_id and s.started_at is not null),
    jsonb_build_object('elapsed_min', null, 'budget_min', null, 'budget_left_min', null)
  );
$$;

create or replace function edge_cycle_await(p_cycle_id text, p_timeout integer default 60, p_phase text default 'decide'::text)
returns jsonb
language plpgsql
set search_path to 'public'
as $$
declare
  deadline timestamptz := clock_timestamp() + make_interval(secs => p_timeout);
  want     text := case p_phase
                     when 'finalize' then 'finalized'
                     when 'resolve'  then 'resolved'
                     else 'decided'
                   end;
  out_row  edge_cycle_output%rowtype;
begin
  loop
    select * into out_row from edge_cycle_output where cycle_id = p_cycle_id;

    if found and out_row.status is not null
       and (out_row.status = want or out_row.status in ('failed', 'noop')) then
      return jsonb_build_object(
        'cycle_id',            out_row.cycle_id,
        'status',              out_row.status,
        'symbols',             coalesce(out_row.symbols,             '[]'::jsonb),
        'stale',               coalesce(out_row.stale,               '[]'::jsonb),
        'statics_stale',       coalesce(out_row.statics_stale,       '[]'::jsonb),
        'fundamentals_needed', coalesce(out_row.fundamentals_needed, '[]'::jsonb),
        'plan', jsonb_build_object(
                  'orders',     coalesce(out_row.plan,       '[]'::jsonb),
                  'protection', coalesce(out_row.protection, '[]'::jsonb),
                  'cancels',    coalesce(out_row.cancels,    '[]'::jsonb)
                ),
        'alerts_summary', out_row.alerts_summary,
        'detail',         out_row.detail
      ) || edge_cycle_budget(p_cycle_id);
    end if;

    if clock_timestamp() >= deadline then
      return jsonb_build_object(
        'cycle_id', p_cycle_id, 'status', 'timeout',
        'detail', format('no %s row after %s s', want, p_timeout)
      ) || edge_cycle_budget(p_cycle_id);
    end if;

    perform pg_sleep(0.5);
  end loop;
end $$;

create or replace function edge_step_beat(p_cycle_id text, p_step text, p_detail jsonb default null::jsonb, p_rows integer default 1)
returns void
language plpgsql
security definer
set search_path to 'public'
as $$
declare
  t        timestamptz := clock_timestamp();
  v_first  boolean;
  v_recent boolean;
begin
  begin
    if p_cycle_id is null or p_cycle_id in ('', 'none') then
      return;
    end if;
    select not exists (select 1 from edge_cycle_step where cycle_id = p_cycle_id and step = p_step)
      into v_first;
    select exists (select 1 from edge_cycle_step
                    where cycle_id = p_cycle_id and step = p_step
                      and at > t - interval '20 seconds')
      into v_recent;
    update edge_session_stats
       set last_beat_at = t, rows_written = rows_written + p_rows
     where cycle_id = p_cycle_id;
    if v_first or not v_recent then
      perform edge_step_stamp(
        p_cycle_id, p_step, case when v_first then 'start' else 'beat' end, p_detail, 'trigger'
      );
    end if;
    if p_detail is null or not (p_detail ? 'lock_renewed') then
      update edge_control
         set updated_at = t
       where key = 'cycle_in_progress'
         and value = p_cycle_id
         and updated_at < t - interval '30 seconds';
    end if;
  exception when others then
    null;
  end;
end $$;

revoke execute on function edge_step_beat(text, text, jsonb, integer)       from anon, authenticated, public;

create or replace function edge_cycle_beat(p_cycle_id text, p_step text)
returns jsonb
language plpgsql
security definer
set search_path to 'public'
as $$
declare
  v_known boolean := false;
begin
  begin
    if p_cycle_id is not null and p_cycle_id not in ('', 'none') then
      select exists (select 1 from edge_step_kind where step = p_step and source = 'runner')
        into v_known;
      if v_known then
        perform edge_step_stamp(p_cycle_id, p_step, 'start', null, 'runner');
      end if;
      update edge_control
         set updated_at = clock_timestamp()
       where key = 'cycle_in_progress' and value = p_cycle_id;
    end if;
  exception when others then
    null;
  end;
  return edge_cycle_budget(p_cycle_id) || jsonb_build_object('stamped', v_known);
end $$;

create or replace function edge_session_finish(p_cycle_id text, p_turns integer, p_tool_calls integer, p_bytes_in bigint, p_notes text)
returns void
language plpgsql
set search_path to 'public'
as $$
declare
  v_steps  integer;
  v_events integer;
  v_bytes  bigint;
begin
  begin
    select count(distinct step), count(*)
      into v_steps, v_events
      from edge_cycle_step
     where cycle_id = p_cycle_id;
    select coalesce(sum(octet_length(payload::text)), 0)
      into v_bytes
      from edge_cycle_input
     where cycle_id = p_cycle_id;
  exception when others then
    v_steps := null; v_events := null; v_bytes := null;
  end;

  insert into edge_session_stats (
    cycle_id, turns, tool_calls, bytes_in,
    runner_turns, runner_tool_calls, runner_bytes_in, notes, finished_at
  )
  values (
    p_cycle_id, v_steps, v_events, v_bytes,
    p_turns, p_tool_calls, p_bytes_in, p_notes, now()
  )
  on conflict (cycle_id) do update
    set turns             = excluded.turns,
        tool_calls        = excluded.tool_calls,
        bytes_in          = excluded.bytes_in,
        runner_turns      = excluded.runner_turns,
        runner_tool_calls = excluded.runner_tool_calls,
        runner_bytes_in   = excluded.runner_bytes_in,
        notes             = excluded.notes,
        finished_at       = now();
end $$;

create or replace function edge_step_watchdog()
returns integer
language plpgsql
security definer
set search_path to 'public'
as $$
declare
  r         record;
  n         integer := 0;
  v_ceiling integer;
  v_budget  integer;
  v_quiet   numeric;
  v_body    text;
  v_ins     integer;
begin
  for r in
    select c.value                                            as cycle_id,
           coalesce(s.cycle_type, c.cycle_type)               as cycle_type,
           coalesce(s.started_at, c.updated_at)               as started_at,
           coalesce(s.current_step, 'S1.claim')               as step,
           greatest(coalesce(s.last_beat_at, c.updated_at), c.updated_at) as last_beat_at
      from edge_control c
      left join edge_session_stats s on s.cycle_id = c.value
     where c.key = 'cycle_in_progress'
       and c.value is not null and c.value <> 'none'
       and s.finished_at is null
  loop
    select k.ceiling_s into v_ceiling from edge_step_kind k where k.step = r.step;
    v_ceiling := case when coalesce(v_ceiling, 0) <= 0 then 900 else v_ceiling end;
    v_quiet   := extract(epoch from clock_timestamp() - r.last_beat_at);

    if v_quiet > v_ceiling then
      v_body := format(
        'Cycle %s (%s) has been quiet in %s since %s ET — no row for over %s min. '
        'Check edge_cycle_timeline(''%s'').',
        left(r.cycle_id, 8), coalesce(r.cycle_type, '?'), r.step,
        to_char(r.last_beat_at at time zone 'America/New_York', 'HH24:MI'),
        v_ceiling / 60, r.cycle_id
      );
      v_ins := edge_alerts_enqueue(jsonb_build_array(jsonb_build_object(
        'title', 'Cycle stalled', 'body', v_body, 'priority', 'high', 'cycle_id', r.cycle_id
      )));
      if v_ins > 0 then
        perform edge_step_stamp(r.cycle_id, r.step, 'warn',
                                jsonb_build_object('watchdog', 'stalled', 'quiet_s', round(v_quiet)),
                                'watchdog');
        n := n + 1;
      end if;
    end if;

    v_budget := edge_cycle_budget_min(r.cycle_type);
    if extract(epoch from clock_timestamp() - r.started_at) / 60.0 > v_budget then
      v_body := format(
        'Cycle %s (%s) started %s ET and is still running past its %s min budget. '
        'Check edge_cycle_timeline(''%s'').',
        left(r.cycle_id, 8), coalesce(r.cycle_type, '?'),
        to_char(r.started_at at time zone 'America/New_York', 'HH24:MI'),
        v_budget, r.cycle_id
      );
      v_ins := edge_alerts_enqueue(jsonb_build_array(jsonb_build_object(
        'title', 'Cycle over budget', 'body', v_body, 'priority', 'default', 'cycle_id', r.cycle_id
      )));
      if v_ins > 0 then
        perform edge_step_stamp(r.cycle_id, r.step, 'warn',
                                jsonb_build_object('watchdog', 'over_budget', 'budget_min', v_budget),
                                'watchdog');
        n := n + 1;
      end if;
    end if;
  end loop;

  return n;
end $$;

revoke execute on function edge_step_watchdog()                             from anon, authenticated, public;

-- BEGIN GENERATED step registry (services/core/core/steps.py: seed_sql)
insert into edge_step_kind (step, ord, phase, ceiling_s, kinds, source, description) values
  ('S1.session_start', 10, 'session', 300, '{}'::text[], 'trigger', 'edge_session_start opened the session-stats row'),
  ('S1.claim', 20, 'session', 1200, '{}'::text[], 'trigger', 'the cycle lock was claimed; the runner is fetching account/orders/scan'),
  ('S2.fetch', 25, 'fetch', 900, '{}'::text[], 'runner', 'runner beat: Step 2''s account, orders and scan fetches are starting'),
  ('S2.post_batch', 30, 'fetch', 900, '{}'::text[], 'trigger', 'Step 2''s batch landed in edge_cycle_input'),
  ('S2a.resolve', 40, 'resolve', 900, '{}'::text[], 'core', 'the core resolved the candidate symbols; the runner fetches them next'),
  ('S2a.fetch.quotes', 41, 'resolve', 600, '{}'::text[], 'runner', 'runner beat: fetching candidate quotes'),
  ('S2a.fetch.tradability', 42, 'resolve', 600, '{}'::text[], 'runner', 'runner beat: fetching candidate tradability'),
  ('S2a.fetch.earnings', 43, 'resolve', 1200, '{}'::text[], 'runner', 'runner beat: fetching earnings, one call per stale symbol'),
  ('S2a.fetch.fundamentals', 44, 'resolve', 600, '{}'::text[], 'runner', 'runner beat: fetching fundamentals for the names resolve asked for'),
  ('S2a.post.quotes', 50, 'resolve', 900, array['quotes']::text[], 'trigger', 'candidate quotes landed'),
  ('S2a.post.tradability', 51, 'resolve', 900, array['tradability']::text[], 'trigger', 'candidate tradability landed'),
  ('S2a.post.fundamentals', 52, 'resolve', 900, array['fundamentals']::text[], 'trigger', 'candidate fundamentals landed'),
  ('S2a.post.earnings', 53, 'resolve', 900, array['earnings']::text[], 'trigger', 'candidate earnings landed'),
  ('S2a.day_context', 60, 'resolve', 600, '{}'::text[], 'trigger', 'edge_day_context was written for the running cycle'),
  ('S3.decide', 70, 'decide', 900, '{}'::text[], 'core', 'the core''s decide phase; the runner places the plan next'),
  ('core.reconcile', 71, 'decide', 300, '{}'::text[], 'trigger', 'the reconcile runlog row was written'),
  ('core.decide', 72, 'decide', 300, '{}'::text[], 'trigger', 'the decide runlog row was written'),
  ('S4.place', 75, 'place', 900, '{}'::text[], 'runner', 'runner beat: placing the plan''s cancels, orders and protection'),
  ('S5.post_results', 80, 'place', 600, array['placements', 'placement_rejections', 'placement_alerts']::text[], 'trigger', 'the runner posted what it placed'),
  ('S6.finalize', 90, 'finalize', 300, '{}'::text[], 'core', 'the core''s finalize phase'),
  ('core.place', 91, 'finalize', 300, '{}'::text[], 'trigger', 'the placement runlog row was written'),
  ('core.report', 92, 'finalize', 300, '{}'::text[], 'trigger', 'the report runlog row was written'),
  ('S6.release', 95, 'session', 300, '{}'::text[], 'trigger', 'the cycle lock was released'),
  ('S6.session_finish', 96, 'session', 0, '{}'::text[], 'trigger', 'edge_session_finish closed the session-stats row'),
  ('ABORT', 99, 'session', 0, '{}'::text[], 'trigger', 'the cycle failed or was aborted')
on conflict (step) do update set ord = excluded.ord, phase = excluded.phase,
  ceiling_s = excluded.ceiling_s, kinds = excluded.kinds, source = excluded.source,
  description = excluded.description;
-- END GENERATED step registry
revoke execute on function edge_cycle_budget_min(text)   from anon, authenticated, public;

revoke execute on function edge_cycle_budget(text)       from anon, authenticated, public;

revoke execute on function edge_cycle_beat(text, text)   from anon, authenticated, public;

grant  execute on function edge_cycle_budget_min(text)   to service_role;

grant  execute on function edge_cycle_budget(text)       to service_role;

grant  execute on function edge_cycle_beat(text, text)   to service_role;

create table if not exists edge_decision_snapshot (
  cycle_id       text        not null,
  symbol         text        not null,
  play_id        text        not null,
  trading_day    date        not null,
  decided_at     timestamptz,
  cycle_type     text,
  verdict        text        not null check (verdict in
                   ('DROPPED', 'UNTRIGGERED', 'SKIPPED', 'UNSIZED', 'DENIED',
                    'RANKED', 'APPROVED')),
  reason_code    text,
  placed         boolean,
  sector         text,
  sector_etf     text,
  features       jsonb       not null default '{}'::jsonb,
  price_features jsonb,
  fwd            jsonb       not null default '{}'::jsonb,
  fwd_status     jsonb       not null default '{}'::jsonb,
  complete       boolean     not null default false,
  bars_source    text,
  backfilled_at  timestamptz,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  primary key (cycle_id, symbol, play_id),
  estimates jsonb
);

create index if not exists edge_decision_snapshot_day_idx
  on edge_decision_snapshot (trading_day, symbol);

create index if not exists edge_decision_snapshot_pending_idx
  on edge_decision_snapshot (trading_day) where not complete;

alter table edge_decision_snapshot enable row level security;

create or replace function edge_decision_snapshot_write(p_rows jsonb)
returns integer
language sql
set search_path = public
as $$
  with upserted as (
    insert into edge_decision_snapshot (
      cycle_id, symbol, play_id, trading_day, decided_at, cycle_type,
      verdict, reason_code, placed, sector, sector_etf, features
    )
    select r->>'cycle_id', r->>'symbol', r->>'play_id',
           (r->>'trading_day')::date,
           nullif(r->>'decided_at', '')::timestamptz,
           r->>'cycle_type', r->>'verdict', r->>'reason_code',
           (r->>'placed')::boolean,
           r->>'sector', r->>'sector_etf', r
    from jsonb_array_elements(coalesce(p_rows, '[]'::jsonb)) as r
    where coalesce(r->>'cycle_id', '') <> ''
      and coalesce(r->>'symbol', '') <> ''
      and coalesce(r->>'play_id', '') <> ''
    on conflict (cycle_id, symbol, play_id) do update
      set trading_day = excluded.trading_day,
          decided_at  = excluded.decided_at,
          cycle_type  = excluded.cycle_type,
          verdict     = excluded.verdict,
          reason_code = excluded.reason_code,
          placed      = excluded.placed,
          sector      = excluded.sector,
          sector_etf  = excluded.sector_etf,
          features    = excluded.features,
          updated_at  = now()
    returning 1
  )
  select count(*)::integer from upserted;
$$;

alter table edge_shadow_trades drop constraint if exists edge_shadow_trades_pkey;

alter table edge_shadow_trades
  add constraint edge_shadow_trades_pkey primary key (variant, symbol, entry_day, pair_symbol);

create or replace function edge_shadow_trades_write(p_rows jsonb)
returns integer language sql as $$
  with upserted as (
    insert into edge_shadow_trades (
      variant, symbol, entry_day, pair_symbol, play_id, entry_cycle_id, quantity,
      entry, exit_day, exit, pnl_pct, reason, bars_source, atr, fill_day, updated_at
    )
    select r->>'variant', r->>'symbol', (r->>'entry_day')::date,
           coalesce(r->>'pair_symbol', ''),
           nullif(r->>'play_id', ''), nullif(r->>'entry_cycle_id', ''),
           nullif(r->>'quantity', '')::numeric,
           nullif(r->>'entry',    '')::numeric,
           nullif(r->>'exit_day', '')::date,
           nullif(r->>'exit',     '')::numeric,
           nullif(r->>'pnl_pct',  '')::numeric,
           r->>'reason', nullif(r->>'bars_source', ''),
           nullif(r->>'atr', '')::numeric,
           nullif(r->>'fill_day', '')::date, now()
    from jsonb_array_elements(coalesce(p_rows, '[]'::jsonb)) as r
    on conflict (variant, symbol, entry_day, pair_symbol) do update
      set play_id        = excluded.play_id,
          entry_cycle_id = excluded.entry_cycle_id,
          quantity       = excluded.quantity,
          entry          = excluded.entry,
          exit_day       = excluded.exit_day,
          exit           = excluded.exit,
          pnl_pct        = excluded.pnl_pct,
          reason         = excluded.reason,
          bars_source    = excluded.bars_source,
          atr            = coalesce(excluded.atr, edge_shadow_trades.atr),
          fill_day       = coalesce(excluded.fill_day, edge_shadow_trades.fill_day),
          updated_at     = now()
    returning 1
  )
  select count(*)::integer from upserted;
$$;

revoke execute on function edge_shadow_trades_write(jsonb)  from anon, authenticated, public;

create or replace function edge_shadow_open(p_days integer default 90)
returns jsonb language sql stable as $$
  with recent as (
    select * from edge_shadow_trades
    where entry_day >= (current_date - make_interval(days => greatest(p_days, 1)))::date
  )
  select jsonb_build_object(
    'open', (
      select coalesce(jsonb_agg(jsonb_build_object(
        'variant', t.variant, 'symbol', t.symbol, 'entry_day', t.entry_day,
        'pair_symbol', t.pair_symbol, 'play_id', t.play_id,
        'entry_cycle_id', t.entry_cycle_id, 'quantity', t.quantity,
        'entry', t.entry, 'atr', t.atr, 'fill_day', t.fill_day,
        'reason', t.reason, 'updated_at', t.updated_at,
        'pair_exit_day', (
          select p.exit_day from edge_shadow_trades p
          where t.variant = 'B_hold_spy' and p.variant = 'R_signal_norsi'
            and p.symbol = t.pair_symbol and p.entry_day = t.entry_day
            and p.pair_symbol = '')
      ) order by t.updated_at, t.entry_day, t.variant, t.symbol), '[]'::jsonb)
      from recent t
      where t.reason in ('OPEN', 'NO_BARS', 'AWAITING_OPEN')
        and (t.entry is not null
             or t.variant in ('R_signal', 'R_signal_norsi', 'R_random_sector',
                              'R_random_sector_norsi', 'B_hold_spy'))),
    'closed', (
      select coalesce(jsonb_agg(jsonb_build_object(
        'variant', variant, 'symbol', symbol, 'entry_day', entry_day,
        'pair_symbol', pair_symbol)), '[]'::jsonb)
      from recent
      where reason in ('STOP', 'TARGET', 'TIME_STOP', 'SIGNAL'))
  );
$$;

create or replace function edge_shadow_read(p_from date, p_to date)
returns jsonb language sql stable as $$
  select coalesce(jsonb_agg(to_jsonb(t) order by t.entry_day, t.variant, t.symbol, t.pair_symbol),
                  '[]'::jsonb)
  from edge_shadow_trades t
  where t.entry_day between p_from and p_to;
$$;

create or replace function edge_shadow_trades_reap()
returns integer language sql as $$
  with deleted as (
    delete from edge_shadow_trades
     where entry_day < current_date - 120
       and variant not in ('R_signal', 'R_signal_norsi', 'R_random_sector',
                           'R_random_sector_norsi', 'B_hold_spy')
    returning 1
  )
  select count(*)::integer from deleted;
$$;

revoke execute on function edge_shadow_trades_reap()        from anon, authenticated, public;

create or replace function edge_evidence_dispatch()
returns jsonb
language plpgsql
security definer
set search_path = public, net
as $$
declare
  base    text := edge_secret('edge_core_url');
  token   text := edge_secret('edge_core_token');
  request bigint;
begin
  if base is null or token is null then
    return jsonb_build_object(
      'dispatched', false,
      'reason', 'edge_core_url or edge_core_token is not in the vault'
    );
  end if;

  select net.http_post(
    url     := base || '/api/evidence',
    headers := jsonb_build_object('Content-Type', 'application/json'),
    body    := jsonb_build_object('token', token),
    timeout_milliseconds := 300000
  ) into request;

  return jsonb_build_object('dispatched', true, 'request_id', request);
end $$;

revoke all on edge_decision_snapshot from anon, authenticated;

revoke execute on function edge_decision_snapshot_write(jsonb)      from anon, authenticated, public;

revoke execute on function edge_shadow_open(integer)                from anon, authenticated, public;

revoke execute on function edge_shadow_read(date, date)             from anon, authenticated, public;

revoke execute on function edge_evidence_dispatch()                 from anon, authenticated, public;

grant  execute on function edge_decision_snapshot_write(jsonb)      to service_role;

grant  execute on function edge_shadow_open(integer)                to service_role;

grant  execute on function edge_shadow_read(date, date)             to service_role;

grant  execute on function edge_shadow_trades_write(jsonb)          to service_role;

grant  execute on function edge_shadow_trades_reap()                to service_role;

create or replace function edge_decision_snapshot_pending(p_limit integer default 4000)
returns jsonb
language sql
stable
set search_path = public
as $$
  select coalesce(jsonb_agg(row_to_json(p)::jsonb order by p.trading_day, p.symbol), '[]'::jsonb)
  from (
    select cycle_id, symbol, play_id, trading_day, sector_etf, verdict,
           (price_features is not null) as has_features,
           (estimates is not null) as has_estimates,
           fwd_status
    from edge_decision_snapshot
    where not complete
    order by trading_day, symbol
    limit greatest(coalesce(p_limit, 4000), 1)
  ) as p;
$$;

revoke execute on function edge_decision_snapshot_pending(integer)  from anon, authenticated, public;

grant  execute on function edge_decision_snapshot_pending(integer)  to service_role;

create or replace function edge_decision_forward_write(p_rows jsonb)
returns integer
language sql
set search_path = public
as $$
  with updated as (
    update edge_decision_snapshot s
       set price_features = coalesce(s.price_features, nullif(r->'price_features', 'null'::jsonb)),
           estimates      = coalesce(s.estimates, nullif(r->'estimates', 'null'::jsonb)),
           fwd            = coalesce(nullif(r->'fwd', 'null'::jsonb), s.fwd),
           fwd_status     = coalesce(nullif(r->'fwd_status', 'null'::jsonb), s.fwd_status),
           complete       = coalesce((r->>'complete')::boolean, s.complete),
           bars_source    = coalesce(r->>'bars_source', s.bars_source),
           backfilled_at  = now(),
           updated_at     = now()
      from jsonb_array_elements(coalesce(p_rows, '[]'::jsonb)) as r
     where s.cycle_id = r->>'cycle_id'
       and s.symbol   = r->>'symbol'
       and s.play_id  = r->>'play_id'
    returning 1
  )
  select count(*)::integer from updated;
$$;

revoke execute on function edge_decision_forward_write(jsonb)       from anon, authenticated, public;

grant  execute on function edge_decision_forward_write(jsonb)       to service_role;

create or replace function edge_decision_snapshot_read(p_from date, p_to date)
returns jsonb
language sql
stable
set search_path = public
as $$
  select coalesce(jsonb_agg(
    features || jsonb_build_object(
      'cycle_id', cycle_id, 'symbol', symbol, 'play_id', play_id,
      'trading_day', trading_day, 'verdict', verdict,
      'reason_code', reason_code, 'placed', placed,
      'price_features', price_features, 'estimates', estimates, 'fwd', fwd,
      'fwd_status', fwd_status, 'complete', complete
    ) order by trading_day, cycle_id, symbol, play_id), '[]'::jsonb)
  from edge_decision_snapshot
  where trading_day between p_from and p_to;
$$;

revoke execute on function edge_decision_snapshot_read(date, date)  from anon, authenticated, public;

grant  execute on function edge_decision_snapshot_read(date, date)  to service_role;


commit;

select cron.schedule('edge-alert-send', '* * * * *', $$select edge_alert_send();$$);

select cron.schedule('edge-alert-reap', '* * * * *', $$select edge_alert_reap();$$);

select cron.schedule(
  'edge-cycle-input-reap', '10 4 * * *', $$select edge_cycle_input_reap();$$);

select cron.schedule('edge-premarket', '*/2 8-12 * * 1-5',
  $$select edge_premarket_dispatch();$$);

select cron.schedule(
  'edge-watch', '0,30 14-20 * * 1-5', $$select edge_watch_dispatch();$$);

select cron.schedule('edge-shadow', '0 5 * * 2-6', $job$select edge_shadow_dispatch();$job$);

select cron.schedule(
  'edge-shadow-trades-reap', '20 4 * * *', $job$select edge_shadow_trades_reap();$job$)
where not exists (
  select 1 from cron.job where jobname = 'edge-shadow-trades-reap');

select cron.schedule(
  'edge-schedule', '30 8,9 * * *', $$select edge_schedule_dispatch();$$);

select cron.schedule('edge-step-watchdog', '* * * * *', $$select edge_step_watchdog();$$);

select cron.schedule('edge-cycle-step-reap', '15 4 * * *', $$select edge_cycle_step_reap();$$);

select cron.schedule(
  'edge-evidence', '5,15,25,35,45,55 6 * * 2-6',
  $job$select edge_evidence_dispatch();$job$);

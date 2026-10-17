-- Air Liquide PG Order Intake PoC: lab stand-ins. Synthetic data only.
-- mailbox        -> Gmail (pulled after a Pub/Sub notification)
-- accounts, partner_roles, product_catalog, cut_offs, draft_orders -> Salesforce + OCB
-- sales_orders, esb_chaos -> TIBCO ESB mock and SAP S/4HANA
-- order_intake, step_log, metrics -> execution record, agent trace, dashboard
create schema if not exists al_poc;
set search_path = al_poc;

create table if not exists mailbox (
  email_id     text primary key,
  from_addr    text not null,
  subject      text not null,
  body_text    text not null,
  received_at  timestamptz not null default now(),
  attachments  jsonb not null default '[]'::jsonb   -- [{name, mime, base64}]
);

create table if not exists accounts (
  sold_to_code  text primary key,
  name          text not null,
  email_domain  text not null,
  contact_email text,
  sales_org     text not null,
  plant         text not null
);

create table if not exists partner_roles (
  sold_to_code text not null references accounts(sold_to_code),
  ship_to_code text not null,
  role         text not null default 'SH',
  name         text not null,
  street       text not null,
  city         text not null,
  postal_code  text not null,
  country      text not null,
  primary key (sold_to_code, ship_to_code)
);

create table if not exists product_catalog (
  product_code text primary key,
  name         text not null,
  aliases      text[] not null default '{}',
  unit         text not null default 'EA',
  category     text not null,
  is_cylinder  boolean not null default false
);

create table if not exists cut_offs (
  sales_org         text not null,
  plant             text not null,
  cutoff_local_time time not null,
  tz                text not null,
  primary key (sales_org, plant)
);

create table if not exists order_intake (
  order_id            text primary key,
  email_id            text not null,
  execution_id        text,
  intent              text,
  extracted           jsonb,
  product_validation  jsonb,
  shipping_validation jsonb,
  rules               jsonb,
  critical_match      boolean,
  confidence          numeric(5,3),
  decision            text,            -- auto | draft | skip
  status              text not null,   -- received | extracted | validated | pending_review | posted | rejected | failed | not_an_order
  sap_order_number    text,
  draft_order_id      bigint,
  started_at          timestamptz not null default now(),
  finished_at         timestamptz
);

create table if not exists step_log (
  id           bigserial primary key,
  order_id     text,
  execution_id text,
  step         text not null,
  agent        text,
  message      text,
  detail       jsonb,
  duration_ms  integer,
  at           timestamptz not null default now()
);
alter table step_log add column if not exists tokens_in integer;
alter table step_log add column if not exists tokens_out integer;
create index if not exists step_log_order_step_idx on step_log (order_id, step);
create unique index if not exists step_log_llm_call_uidx on step_log (execution_id, (detail->>'node'), (detail->>'run_index')) where step = 'llm_call';

create table if not exists draft_orders (
  id              bigserial primary key,
  order_id        text not null,
  sold_to_code    text,
  ship_to_code    text,
  lines           jsonb not null,
  reasons         text[] not null default '{}',
  ai_explanation  text,
  status          text not null default 'open',  -- open | approved | rejected
  created_at      timestamptz not null default now(),
  resolved_at     timestamptz
);

create table if not exists sales_orders (
  id               bigserial primary key,
  order_id         text not null unique,
  sap_order_number text not null unique,
  payload          jsonb not null,
  created_at       timestamptz not null default now()
);

create table if not exists esb_chaos (
  order_id           text primary key,
  remaining_failures integer not null,
  calls              integer not null default 0
);

create or replace view metrics as
select
  count(*) filter (where status not in ('not_an_order'))                       as orders_total,
  count(*) filter (where decision = 'auto' and status = 'posted')              as auto_posted,
  count(*) filter (where decision = 'draft')                                   as sent_to_review,
  count(*) filter (where status = 'failed')                                    as failed,
  round(100.0 * count(*) filter (where decision = 'auto' and status = 'posted')
        / nullif(count(*) filter (where status not in ('not_an_order')), 0), 1) as automation_rate_pct,
  round(avg(extract(epoch from (finished_at - started_at)))
        filter (where finished_at is not null and decision = 'auto'), 1)       as avg_auto_seconds,
  round(avg(confidence), 3)                                                    as avg_confidence,
  (select count(*) from draft_orders where status = 'open')                    as desk_open,
  (select round(avg(extract(epoch from (resolved_at - created_at))) / 60, 1)
     from draft_orders where resolved_at is not null)                          as avg_desk_wait_minutes,
  (select count(*) from step_log where step = 'guard')                         as guard_events,
  (select coalesce(sum(coalesce(tokens_in, 0) + coalesce(tokens_out, 0)), 0)
     from step_log where order_id is not null)                                 as tokens_total,
  (select round(sum(coalesce(tokens_in, 0) + coalesce(tokens_out, 0))::numeric / nullif(count(distinct order_id), 0))
     from step_log where step = 'llm_call' and order_id is not null)           as avg_tokens_per_order
from order_intake;

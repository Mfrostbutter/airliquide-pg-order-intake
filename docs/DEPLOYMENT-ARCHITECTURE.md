# Deployment architecture for 10,000 orders a day

Sizing and topology for the PG Order Intake flow at the volume Air Liquide gave us
on 2026-10-17: 10,000 purchase orders a day. The Q&A of 2026-07-22 spoke of "about
10,000 orders" as the production baseline without a period; this document takes the
daily figure as the design point. Written against n8n 2.35; verify each setting
name against the self-hosting docs for the version Morgan installs.

## 1. Load model

| Quantity | Value | Basis |
|---|---|---|
| Orders per day | 10,000 | Air Liquide, 2026-10-17 |
| Average rate | 7 per minute | 24/7 operation (Q&A row 8) |
| Peak rate, design | 50 per minute | 60 % of the day's orders in the four busiest hours gives 25 per minute; designed at twice that |
| Executions per order | 3 | intake, Product Agent, Shipping Agent. The order desk resumes the intake execution; no new execution |
| Executions per day | 30,000 | 11 M a year. The proposal priced 4 per order (14.6 M a year); see section 7 |
| Model calls per order | 6 to 9 | 1 extraction, 2 to 4 per agent, 1 explanation on the draft path; lab measured 8 on F1 |
| Tokens per order | 12,000 to 16,000 | lab F1: 8 calls, 13,500 tokens with the local model; Gemini will differ but not by an order of magnitude |
| Wall time per order | 20 to 40 s | lab: 16 to 36 s; Gemini Flash should be at or below the lab |
| Draft Orders waiting | 2,000 a day | at 80 % automation; a waiting execution costs no worker time |
| n8n CPU per order | under 0.5 s | lab run data: every non-model node together is 100 to 300 ms |

Concurrency at the design peak: 50 orders per minute at 30 s each is 25 orders in
flight. Sub-workflows called with "wait for completion" run inside the parent's
worker, so 25 in flight is 25 worker slots.

## 2. Topology (one region)

Queue mode, as proposed. Nothing in the PoC changes; only the runtime grows.

| Component | Count | Size | Notes |
|---|---|---|---|
| Main | 2 | 2 vCPU, 4 GB | Multi-main (Enterprise) for HA. Editor, API, schedule triggers, the telemetry harvester |
| Webhook processor | 2 | 1 vCPU, 2 GB | Receives Pub/Sub pushes and order-desk resumes; acknowledges in milliseconds; never runs workflows |
| Worker | 3 | 2 vCPU, 4 GB | `--concurrency=10` each: 30 slots for 25 in flight at peak with one worker spare. Scale horizontally; HPA on queue depth |
| Redis | 1 (HA) | 1 GB | Memorystore or Redis Sentinel. Queue only, no data |
| PostgreSQL (n8n) | 1 primary + replica | 4 vCPU, 16 GB, 250 GB SSD | Cloud SQL. Execution data pruned at 7 days (section 4) |
| PostgreSQL (application) | shared with the above or separate | small | `order_intake`, `step_log`, `draft_orders` mirror: about 5 KB per order, 50 MB a day |
| Binary data | bucket | | GCS (S3 mode) for attachments; never on the pod filesystem in queue mode |
| Task runners | sidecar per worker | | Code nodes run in the runner; default in 2.x |

Three regions as in proposal section 3.2 (Europe, Americas, APAC), each with the
same topology, its own mailboxes and its own cut-off set. The dashboards and
chargeback read all three.

Single-node fallback (the PoC EC2): one container with Postgres on the host handles
about 10 orders a minute with Gemini; enough for the demo and for a pilot, not for
the design peak.

## 3. Ingress and egress

- **Pub/Sub push** to the webhook processors through the load balancer, OIDC token
  on the push subscription, verified at the edge or in the webhook. Ack deadline 10 s;
  the webhook answers at once (response mode "on received"). Pub/Sub is at-least-once:
  the intake drops a notification for an order it already has (`OpenOrderRecord`,
  `IsNewNotification`).
- **Gmail watch** expires every 7 days; a scheduled workflow renews it daily.
- **Order-desk resume** calls from Salesforce land on the same webhook processors
  (`/webhook-waiting/<execution>`).
- **Egress allow-list:** Gmail API, Pub/Sub, Vertex AI private endpoint, Salesforce
  API, OCB, the TIBCO ESB, the secret manager. Nothing else.

## 4. Settings that matter at this volume

| Setting | Value | Why |
|---|---|---|
| `EXECUTIONS_MODE` | `queue` | workers scale independently of the mains |
| worker `--concurrency` | 10 | one order holds a slot while it waits on the model; CPU stays low |
| `EXECUTIONS_DATA_PRUNE` / `EXECUTIONS_DATA_MAX_AGE` | `true` / `168` | 30,000 executions a day at roughly 200 KB each is 6 GB a day; 7 days is about 42 GB. The durable trace is `step_log`, not n8n's execution table |
| `EXECUTIONS_DATA_SAVE_ON_SUCCESS` | `all` during the PoC, `none` for the agents in production once `step_log` is proven | execution data is the debugging view, `step_log` the audit trail |
| `N8N_DEFAULT_BINARY_DATA_MODE` | `s3` (GCS) | attachments off the pods |
| `N8N_CONCURRENCY_PRODUCTION_LIMIT` on mains | low | mains should not run production executions in queue mode |
| `N8N_MULTI_MAIN_SETUP_ENABLED` | `true` | HA for triggers and the API |
| `N8N_ENCRYPTION_KEY`, DB password, API keys | Secret Manager via external secrets | never in env files |
| `N8N_METRICS` | `true` | Prometheus scrape for queue depth and execution counts |
| OpenTelemetry exporter | on, to the APM | per-call LLM telemetry (`llm.tokens.*`) and traces, as committed in the answer grid |
| `N8N_PAYLOAD_SIZE_MAX` | 64 MB | attachments pulled from Gmail |
| `DB_POSTGRESDB_POOL_SIZE` | 4 per worker | 3 workers plus mains stay under Cloud SQL's connection limit |
| `WEBHOOK_URL` | the public LB hostname | resume URLs in the Draft Order must be reachable from Salesforce |

## 5. Model capacity

| Item | Value |
|---|---|
| Calls per day | 60,000 to 90,000 |
| Calls per minute at design peak | 300 to 450 |
| Tokens per day | 120 M to 160 M, mostly input |
| Vertex AI quota to request | 600 requests per minute and 2 M tokens per minute for the chosen Gemini model in the EU region |
| Indicative cost | Gemini 2.5 Flash list prices put this in the low thousands of euros a month; Air Liquide's own subscription applies (pass-through) |

Reduce calls before buying quota: the Product Agent spends most of its iterations
searching by name. A Salesforce product cache in the application database with
multilingual aliases (the lab's `product_catalog`) cuts those to one or two calls
per line.

## 6. Failure behaviour under load

| Failure | What happens | Recovery |
|---|---|---|
| Model endpoint slow or down | executions hold their slot up to the node timeout, retry twice, then the agent reports an error and the order goes to the desk (C6) | no data loss; queue depth rises; add workers or wait |
| ESB 5xx | per-order backoff 4, 8, 16, 32, 64 s; the Wait node releases the worker slot between attempts | after the fifth failure the order is escalated; replay by re-running from `BuildSalesOrderPayload`, idempotent on `order_id` |
| Salesforce rate limit (REST, per 24 h) | tool calls fail, agent error, order to the desk | cache the catalogue and partner roles; batch the Draft Order writes |
| Worker crash | the job returns to the queue and is re-run from the start of the execution; `OpenOrderRecord` sees the existing row and drops it as a duplicate | re-run manually from the last good node, or mark the order for reprocessing by deleting its row |
| Redis down | no new executions start; webhooks queue at the LB | HA Redis; nothing is lost once Redis returns |
| Postgres full | everything stops | pruning on, disk alerts at 70 % |

## 7. Commercial note

At 3 executions per order the yearly volume is about 11 M, inside the 12 M
Signature package but with no headroom for growth or for the other processes in the
Year-1 portfolio. The proposal assumed 4 per order (14.6 M). Manuel should know the
daily figure before the commercial conversation resumes.

## 8. Rehearsal

`scripts/load-test.py` sends N copies of a fixture with K in flight and reports
throughput and p50 / p95 seconds per order. On the lab the local model is the limit
(one request at a time), so it proves concurrency correctness, not throughput. The
throughput run belongs on the PoC EC2 with Gemini: 50 orders a minute for 20
minutes, pass if p95 is under 5 minutes, no duplicate SAP orders, no failed
executions, Postgres growth within the estimate above.

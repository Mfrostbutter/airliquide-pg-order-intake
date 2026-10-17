# Air Liquide PG Order Intake handoff

State as of 2026-10-17 evening: lab build complete and verified (six workflows), PoC
instance not yet installed. This document is the handoff from the lab phase to the PoC phase (Morgan
Lupton, Madison Seeley) and the skeleton of the handoff to Air Liquide GDO after the
20 Nov demo. `<poc>` marks what fills in once the EC2 exists.

## 1. What was built

**Intake PG order.** An email notification arrives from Pub/Sub and is acknowledged at
once. The workflow pulls the email and its first attachment, turns PDF, XLSX or CSV
into text, and reads the whole thing against a fixed schema: is it an order, who is
the customer, where should it go, which products and quantities, any empty cylinders
coming back, and how confident the reading is. Two specialist agents then validate
the products and the shipping data against Salesforce. A rules step decides: all
critical fields verified and confidence at or above 0.7 goes straight to SAP; anything
else becomes a pre-filled Draft Order for the order desk with a written reason. The
SAP call retries five times over two minutes on server errors, then escalates.

**Product Agent.** Reads each product line the customer wrote, looks it up in the
catalogue by code or by name, and returns exactly one catalogue code per line or says
it could not. It cannot invent a code: the output schema and a re-read of the
catalogue reject anything that did not come from a lookup. Lines over 80 units are
flagged as Elephant Orders.

**Shipping Agent.** Finds the Sold-To account from the sender's email, lists its
Ship-To partners, picks the one matching the delivery address written in the email,
and reads the plant's cut-off from the OCB stand-in to compute the earliest dispatch.

**Mock TIBCO ESB.** Lab only. Answers like the sales-order endpoint, with a switch to
force 503s so the retry path can be rehearsed.

**Metrics dashboard.** One page: orders received, automation rate, auto posted, sent
to the desk, failures, average seconds per order on the automated path, open desk queue
and average desk wait on the human path, guard events, LLM tokens per order, last 25
orders.

**LLM telemetry harvester.** Every minute, reads the finished executions through the
n8n API and records every model call with its token counts and duration against the
order. The chargeback figures come from here.

## 2. Architecture

| | Lab (now) | PoC (Nov 2026) | Production (proposal §3.2) |
|---|---|---|---|
| n8n | podman `n8n-lab`, 2.35.5, single container | one EC2 in AL AWS private cloud, Docker or Podman, Citrix access | 3 regional self-hosted instances on private GKE, queue mode |
| Database | `pg-lab` container | Postgres on the EC2 | Cloud SQL |
| Email in | `mailbox` table + fixture script | Gmail API + Pub/Sub push, or Gmail trigger polling if no inbound route | same |
| Validation data | `accounts`, `partner_roles`, `product_catalog`, `cut_offs` | Salesforce sandbox API + OCB API | Salesforce prod |
| Order out | Mock ESB workflow | TIBCO ESB → SAP S/4HANA sandbox | TIBCO → SAP |
| Review queue | `draft_orders` + Mailpit email | Draft Order in Salesforce | same |
| Model | LM Studio qwen3-coder-30b, local | Gemini via AL API key | Gemini on Vertex AI, private endpoint |
| Dashboards | HTML webhook | HTML webhook | Looker / Power BI on the same queries, Insights export for chargeback |

Diagram: `README.md`, section Architecture. Canvas screenshots: `docs/screenshots/`.

Four executions per order is what the proposal priced (intake, two agents, one
reserve). The lab build uses three per order; the Wait for the order desk resumes
the same execution. Air Liquide gave the production volume as 10,000 orders a day on
2026-10-17: topology, settings and model capacity in `DEPLOYMENT-ARCHITECTURE.md`.

Constraints that bind the design (sources: RFP, Q&A of 2026-07-22, bidder grid):

- Data plane private; public SaaS may not touch AL Google Workspace.
- Model is bring-your-own and pass-through. No n8n-provided model.
- RPA stays Automation Anywhere, orchestrated over its API.
- Dev / UAT / Prod environments are mandatory (Q&A row 25); Git-backed environments
  are the promotion mechanism we committed to in the grid (rows 34 to 36).
- Every transaction needs a correlation id across steps and per-call LLM telemetry
  (prompts, raw responses, tokens): grid rows 27 to 29. `order_id` is the correlation
  id, on every table row and on the execution's custom data; `step_log` holds the
  prompts, tool calls, outputs and, since 2026-10-17, one `llm_call` row per model
  call with tokens and duration.
- Rules, thresholds and routing configurable without code (RFP requirement 6): the
  four `AL_PG_*` variables in Settings > Variables.
- Confidence-score routing to a human (grid row 10) is rule R9.

## 3. Credentials and secrets

See `RUNBOOK.md`, Credentials. Summary: the lab has four n8n credentials (Postgres,
LM Studio, Mailpit, the n8n API for the harvester). The PoC needs Gmail/Pub/Sub service account, Gemini, Salesforce
API user, TIBCO ESB, and the instance Postgres. Air Liquide creates and rotates
every AL-side credential; we enter them in the n8n credential store and nowhere else.
Individual accounts only on the EC2 (Administrator Charter); no shared logins.

## 4. Promotion

1. Lab is the only place changes are made. The generator is the source; the JSON is
   built from it; the canvas is a view.
2. Before any change: `drift-check.sh dev`, `export-all.sh`, commit.
3. Validate (`validate.sh`), deploy to the lab with `--activate`, agents first.
4. Run the five fixtures and the golden set; every critical field must score 1.0, or
   promotion is blocked (proposal commitment, implemented as n8n Evaluations once the
   golden set arrives).
5. Deploy to the PoC with `deploy.sh <file> poc --confirm`, same order, after
   `deploy/creds.poc.json` maps the credential names.
6. Re-run the golden set on the PoC and read the dashboard. A deploy that returned 200
   is not a promotion; a green run is.

## 5. Runbook

`RUNBOOK.md`.

## 6. Evals

**Covered now:** five synthetic fixtures (`data/README.md`) covering clean French
order, German order with XLSX, PDF order with Elephant + unknown product + returns,
inquiry, and ESB retry. Run with `scripts/send-fixture.py`, verify with
`scripts/watch-order.py`. A failure means the order record's `status` or `decision`
differs from the expected column, or the elapsed time is over five minutes.

**Not yet:** the seventeen golden PG orders from the proposal, Air Liquide's
anonymised sample (arrives during the PoC, goes under `data/fixtures/al/`, never
committed), and n8n Evaluations wired to the golden set with the critical-field
check. Owner: Frosty, blocked on the dataset.

**Lab model caveat:** the lexical guard in the Product Agent exists because the local
model translates product words to English before searching. Re-check it against
Gemini; it may be loosened but must not be removed without a golden-set run.

## 7. Known limits and deferred items

| Item | Status | Owner |
|---|---|---|
| DOCX attachments and multi-attachment emails | Not parsed; first attachment only. DOCX and scans go to Document AI on the PoC | Romain Fassi to confirm scope |
| Gmail + Pub/Sub on the EC2 | Depends on an inbound route; fallback is the Gmail trigger polling | Tristan Schultz, Morgan |
| TIBCO payload and auth | Unknown; mock accepts `{order_id, sold_to, ship_to, lines}` | Vorith Top |
| Salesforce Draft Order object and catalogue queries | Unknown; stand-in tables mirror the fields we need | Moctar Ba |
| OCB cut-off API | Unknown; stand-in has two plants | Raphael Leitao, Luc Ebert |
| Gemini endpoint and model | API key vs Vertex, EU endpoint | Narayana Ayyagari |
| Token counts per LLM call | Logged by the harvester (PoC path); OpenTelemetry on the instance is the production path | Frosty, Morgan |
| Stand-ins to real systems | Node-by-node plan in `INTEGRATION-RUNBOOK.md`; blocked on the contracts above | Frosty |
| Chargeback view | Dashboard shows counts and times; cost lines and BI tool undecided | Karine Fessard |
| Non-order emails | Stop as `not_an_order`; no Salesforce case created | PG order team |
| Instance error workflow | None; failures go by email from inside the workflow | Frosty |
| Signavio BPMN import | Gap acknowledged in the proposal; out of PoC scope | Manuel |
| Prerequisites document | Draft in `POC-PREREQUISITES.md`, needs Morgan's review before Romain sees it | Morgan |
| Video walk-through | Spec in `VIDEO-WALKTHROUGH.md`; three recordings, week of 2026-10-20; links land here on upload | Frosty |

**Lab incident 2026-10-17:** the first harvester pulled 100 full executions per minute
through the API and the lab container was OOM-killed three times. Rewritten to list
metadata and fetch at most 10 executions one at a time; stable since. Lesson for the
PoC instance: never read execution data in bulk through the API; use OpenTelemetry.

## 8. Escalation

See `RUNBOOK.md`, Escalation. Weekly Monday check-in with Manuel Craciun and Morgan
Lupton; Slack `int-airliquide`; Google Chat space and shared Drive on the AL side.

## Contract and conduct notes (contract template, codes of conduct, read 2026-10-17)

- Workflows, prompts and code built under the services contract are "Specific
  Elements": ownership transfers to Air Liquide. Keep the generic harness tooling
  separate from the engagement deliverables.
- Air Liquide may audit once a year, including a security audit of the services.
- Reversibility: up to six months of transition assistance on request. The repo, the
  generator and this document are the reversibility package.
- Acceptance: fifteen working days after delivery notification.
- Subcontractors need Air Liquide's prior written approval.
- Personnel are bound by the Code of Conduct for Digital Protection and the
  Administrator Charter: individual credentials, no software on the EC2 outside
  approved standards without approval, no bypass of tracing, incidents reported to
  the AL Helpdesk and the Information Protection Coordinator.
- Supplier Code of Conduct §16: no use of the Air Liquide name or the fact of this
  engagement in any external material without written authorisation. Internal
  reference only until that exists.

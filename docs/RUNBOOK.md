# Air Liquide PG Order Intake runbook

For the people who keep the order intake running: the Air Liquide order desk, the
GDO Automation team, and n8n during the PoC. Lab values are filled in; PoC instance
values are marked `<poc>` and get filled when Morgan Lupton's install lands.

## Health

- **The one check:** open the metrics dashboard. If "orders received" moved in the
  last hour and "failures" did not, the intake is healthy.
  Lab: `http://localhost:5678/webhook/al-pg-order-metrics`. PoC: `<poc>/webhook/al-pg-order-metrics`.
- **Executions:** n8n > Executions, filter by workflow "AL PoC: Intake PG order".
  Every order record carries its `execution_id`, so the trace is one click away.
- **Traces and metrics:** `order_intake` (one row per email, status and decision),
  `step_log` (every extraction, tool call, rule, ESB attempt), `metrics` view.
  Lab: database `al_poc` on `pg-lab`. PoC: `<poc Postgres>`.
- **Order desk queue:** `draft_orders` with `status = 'open'`. Lab: the order-desk
  email in Mailpit. PoC: the Draft Order queue in Salesforce.

## Workflows

| Workflow | Trigger | What it does | Owner |
|---|---|---|---|
| AL PoC: Intake PG order (Pub/Sub to SAP or Draft Order) | `POST /webhook/al-pg-order-notify` (Pub/Sub push) | Pull the email, extract, validate with the agents, apply the rules, post to SAP or park a Draft Order | n8n FDE (Frosty), then AL GDO |
| AL PoC: Product Agent (resolve lines against catalog) | Called by the intake | Resolve each product line to a catalogue code, flag Elephant Orders, log tool calls | same |
| AL PoC: Shipping Agent (resolve Ship-To and cut-off) | Called by the intake | Sold-To from the sender, Ship-To from the partner roles, cut-off from OCB, earliest dispatch date | same |
| AL PoC: Mock TIBCO ESB (sales order API) | `POST /webhook/al-mock-tibco/sales-orders` | Lab stand-in only. Absent on the PoC instance; the real ESB endpoint takes its place | n8n FDE |
| AL PoC: Metrics dashboard (order intake) | `GET /webhook/al-pg-order-metrics` | HTML dashboard over `order_intake` and `step_log` | n8n FDE, then AL GDO |

Deploy order after any change: Product Agent, Shipping Agent, intake, dashboard.
n8n refuses to publish the intake while an agent it calls is unpublished.

## Order lifecycle

`received` → `extracted` → `validated` → `posted` (auto) or `pending_review` → `posted` / `rejected` (desk), or `not_an_order`, or `failed`.

Decision rules (the gate, `ApplyBusinessRules`):

| Rule | Meaning | Effect when it fails |
|---|---|---|
| C1 to C6 | Intent is order, sender matched to a Sold-To, every line verified, Ship-To verified, quantities positive integers, both agents answered | Draft Order, critical match 0 |
| R7 | No line over 80 units (Elephant Order) | Draft Order |
| R8 | No empty cylinder returns | Draft Order |
| R9 | Extraction confidence at or above 0.7 | Draft Order |
| I10 | Cut-off note | Informational only |

Thresholds are constants at the top of the generator (`AUTO_CONFIDENCE_MIN`,
`ELEPHANT_QTY`, `ESB_MAX_ATTEMPTS`, `ESB_BACKOFF_BASE_S`). Change them there, rebuild,
redeploy. Draft Order reasons use fixed wording; the model only writes the note.

## Three most likely failures

1. **Order stuck in `pending_review`, nobody saw it.** Cause: the order-desk email did
   not arrive, or the Approve link was never clicked. Fix: open `draft_orders` for the
   `order_id`, read `reasons`, then resume the execution from n8n > Executions (the
   execution is waiting on `WaitForOrderDesk`) or re-send the fixture. Each Approve
   link is single-use.
2. **Order `failed`, Draft Order reason "ESB unavailable after 5 attempts".** Cause:
   five consecutive 5xx from the ESB over about two minutes. Each attempt is in
   `step_log` (`esb_retry`). Fix:
   confirm the ESB is up, then re-post by re-running the execution from `BuildSalesOrderPayload`.
   The ESB call is idempotent on `order_id`; no duplicate SAP order is created.
3. **Draft Orders for products that are in the catalogue.** Cause: the agent matched
   a name the catalogue does not alias, or the lexical guard downgraded the match
   (customer words did not cover the catalogue name). Fix: add the customer's wording
   to `product_catalog.aliases`; no redeploy needed. Check `step_log` for the agent's
   `search_product_by_name` calls to see what it searched.

Other signals:

- **`NotifyOpsFailure` email** (lab: `n8n-ops@al-poc.lab`): a node raised an error.
  Subject carries the execution id, body the error. Open the execution; the order id
  is on the `OpenOrderRecord` node output.
- **Extraction returns `other` for real orders:** the model or its endpoint changed.
  Check `ExtractorModel` credentials and the `step_log` row `extraction`.
- **Every order goes to the desk with C2 failing:** the sender email is not on an
  account. Add the account or the email, then re-run.

## Credentials and secrets

| Credential | Type | Value source | Rotation owner |
|---|---|---|---|
| Lab Postgres al_poc | Postgres | Infisical n8n-FDE dev `PG_LAB_AL_POC_PASSWORD` | n8n FDE |
| LM Studio (lab) | OpenAI-compatible | local, no key | n8n FDE |
| Lab Mailpit SMTP | SMTP | local, no auth | n8n FDE |
| PoC Gmail / Pub/Sub | Google service account | AL GCP project, handed over by Tristan Schultz | AL |
| PoC Gemini | API key or Vertex AI | AL Google subscription | AL (Narayana Ayyagari) |
| PoC Salesforce API user | OAuth2 or password | AL Salesforce sandbox | AL (Moctar Ba) |
| PoC TIBCO ESB | per contract | AL | AL (Vorith Top) |
| PoC Postgres | Postgres | on the EC2, set at install | n8n (Morgan Lupton) |

All values live in the n8n credential store on the instance. None in workflow JSON,
`.env` files, chat, or tickets. Credential ids per environment are in
`deploy/creds.<env>.json`, gitignored.

## Promotion

1. `./scripts/drift-check.sh dev`, then `./scripts/export-all.sh` and commit if the
   lab and the repo disagree.
2. Edit `workflows/src/build_pg_order_intake.py`, rebuild, re-run the layout tool,
   `./scripts/validate.sh`.
3. Deploy to the lab with `--activate`, agents first. Run the five fixtures, watch each
   with `scripts/watch-order.py`.
4. For the PoC instance: fill `deploy/creds.poc.json`, then
   `./scripts/deploy.sh workflows/dev/<file> poc --confirm` in the same order. Re-run
   the golden set and read the dashboard before calling it promoted.

## Escalation

- Lab and build: Michael Frostbutter (n8n FDE), Slack `int-airliquide`.
- PoC instance (install, OS, network): Morgan Lupton (n8n SE).
- Air Liquide side: Romain Fassi (PoC Lead) for scope, Tristan Schultz (Tech Lead)
  for access and flows, Vorith Top for SAP/TIBCO, Moctar Ba for Salesforce.
- Security incident on the PoC instance: Air Liquide Helpdesk and the entity
  Information Protection Coordinator first (DS_GC01 §25), then Slack.
- Include: workflow name, `execution_id`, `order_id`, timestamp, what you expected,
  the `step_log` rows for that order.

# Video walk-through spec

Three short screen recordings of the lab build: what it does, how it is built, and what
changes to run it on a development instance and the PoC. Written 2026-10-17.

## Audience and rules

| | |
|---|---|
| Audience | Manuel Craciun, Morgan Lupton, Madison Seeley now; Air Liquide GDO (Romain Fassi, Moctar Ba, Vorith Top, Tristan Schultz) after Morgan's review |
| Distribution | Google Chat space and shared Drive on the AL side, Slack `int-airliquide` internally. Never public: Supplier Code of Conduct §16 |
| Data on screen | Synthetic fixtures only (`data/fixtures/`, invented customers, `.example` domains). No AL sample data even once it arrives |
| Never on screen | Credential edit pages, Settings > API, `.env`, `deploy/creds.*.json`, the Infisical CLI, the n8n executions list of other lab tenants |
| Format | 1080p, 16:9, voice over, one take per segment, cuts allowed between segments. Loom or QuickTime plus a trim. Under 10 minutes each |
| Naming | `AL-PG-intake-<n>-<slug>-2026-MM-DD.mp4`, kept in the engagement Drive folder, link in `HANDOFF.md` |

## Pre-record checklist

1. Lab up: `podman ps` shows `n8n-lab`, `pg-lab`, `mailpit`; LM Studio has qwen3-coder-30b loaded.
2. `./scripts/drift-check.sh dev --tag al-poc` says in sync.
3. Variables at defaults (0.7, 80, 5, 4). Mock ESB chaos off.
4. Fixture orders purged so the dashboard starts at zero: `send-fixture.py` purges on send, so send and purge f1 once, or truncate the four trace tables in the lab DB.
5. Mailpit emptied. Browser window 1920x1080, bookmarks bar hidden, n8n in light mode, zoom 100 %.
6. Tabs open in order: intake canvas, Product Agent, Shipping Agent, mock ESB, dashboard, telemetry harvester, Mailpit, a terminal at the engagement root.
7. Executions filtered to the `al-poc` tag.

## Video 1: What it does (target 7 minutes)

| Time | Screen | Say |
|---|---|---|
| 0:00 | `README.md` architecture diagram | The problem: PG orders arrive by email in several languages, with PDF, XLSX or CSV attachments. Today a person reads each one and keys it into SAP. The build reads the email, validates against Salesforce, and posts to SAP or hands a pre-filled draft to the order desk with a reason. |
| 0:45 | Terminal | `send-fixture.py f1-durand-clean-order`. One sentence on what f1 is: French body, two lines, address in the body. |
| 1:00 | Intake execution view, live | Walk the running execution left to right as it lights up: notification acked, email pulled, document assembled, extractor output (show the JSON: customer, lines, confidence), the two agents, rules, SAP post. Stop on `RecordSalesOrder`. About 30 seconds of run time; narrate over it. |
| 2:15 | Dashboard | One order received, one auto posted, seconds per order. |
| 2:30 | Terminal | `send-fixture.py f3-stmartin-pdf-elephant-unknown`. PDF, quantity 100, a product that is not in the catalogue, 40 empties returned. |
| 2:45 | Intake execution | Extractor reads the PDF. Product Agent returns one line unverified and one Elephant flag. Rules fail R7 and the unknown-product rule. Draft Order created with three written reasons. Workflow waits. |
| 3:45 | Mailpit | Open the order desk email: the reasons, the pre-filled lines, Approve and Reject links. Click Approve. |
| 4:15 | Intake execution | The wait resumes, the order posts, `sap_order_number` recorded. |
| 4:45 | Dashboard | Two received, one auto, one via desk, desk wait in minutes, guard events two, tokens per order. |
| 5:15 | Terminal | `send-fixture.py f4-durand-inquiry`. A price request: stops as `not_an_order`, no SAP call, logged. Ten seconds. |
| 5:45 | `step_log` query in the terminal or the dashboard's last-orders table | Every step, every prompt, every tool call and every model call with tokens, keyed by `order_id`. This is the audit trail from the answer grid. |
| 6:30 | README results table | The five fixtures and the three operational cases, all green on 2026-10-17. Close. |

## Video 2: How it is built (target 9 minutes)

| Time | Screen | Say |
|---|---|---|
| 0:00 | Intake canvas, fit to view | Six workflows. The intake is 46 nodes in six sticky-note zones; read the zones, not the nodes. Zone 1 receive and pull, 2 document, 3 extract, 4 validate, 5 decide and post, 6 trace and failures. |
| 1:00 | Zone 1 | Webhook responds on receipt (Pub/Sub ack), then pulls. `OpenOrderRecord` is idempotent on `order_id`: a redelivered notification is logged as a duplicate and dropped. Show it: `send-fixture.py f3 --no-purge`, execution stops at `LogDuplicateNotification`. |
| 2:00 | Zone 3 | Information Extractor with a fixed schema; show the schema fields and the confidence field. Model sub-node is a stand-in for Gemini (video 3). |
| 2:45 | Product Agent canvas | Two Postgres tools (by code, by name), a structured output parser with auto-fix, a verify step that re-reads the catalogue so the agent cannot invent a code, the lexical guard, Elephant flag over 80. Guard events are written to `step_log`. |
| 4:00 | Shipping Agent canvas | Sold-To from sender email, Ship-To partners, address match, plant cut-off, earliest dispatch. Same verify-after-agent shape. |
| 4:45 | Zone 5 | The rules node: list R1 to R9 on screen, point at the confidence threshold and the Elephant limit reading `$vars`. Change `AL_PG_ELEPHANT_QTY` to 5 in Settings > Variables, send f1, it goes to draft; set it back. No redeploy. |
| 6:00 | Zone 5, ESB loop | Retry with exponential backoff over the ESB. Mock ESB chaos 2: `send-fixture.py f5-durand-esb-retry --chaos 2`; show 503, 4 s, 503, 8 s, posted on attempt 3. |
| 7:15 | Zone 6 and the mock ESB canvas | Every fallible node's error output lands in one failure path: ops email with order id and execution id. Mock ESB is lab only, four nodes. |
| 7:45 | Telemetry harvester canvas | Runs every minute, lists finished executions through the n8n API, fetches each one, writes one `llm_call` row per model call with tokens and duration. Why a separate workflow: sub-node token usage is not reachable from inside the run. Mention the OOM lesson in one sentence. |
| 8:30 | Terminal, `workflows/src/` | The JSON is generated from `build_pg_order_intake.py`; the canvas is a view. Validate, deploy, drift check. Close. |

## Video 3: From the lab to a development instance and the PoC (target 10 minutes)

Source: `INTEGRATION-RUNBOOK.md`, `DEPLOYMENT-ARCHITECTURE.md`, `POC-PREREQUISITES.md`.

| Time | Screen | Say |
|---|---|---|
| 0:00 | `HANDOFF.md` architecture table (lab, PoC, production columns) | Nothing in the workflow shape changes. Seven stand-ins get swapped for the real system, node by node. |
| 0:45 | `INTEGRATION-RUNBOOK.md` section 1 | What Air Liquide provides, with owners: Gmail service account (Tristan), Gemini key or Vertex service account (Narayana), Salesforce integration user (Moctar), OCB access (Raphael, Luc), TIBCO ESB endpoint and auth (Vorith), the instance Postgres and n8n API key (Morgan). Credential names are fixed; the deploy maps them by name. |
| 2:00 | Section 2, Zone 1 on the canvas | Email in: Pub/Sub pushes a `historyId`, not a message id, so one new node lists new messages and the Postgres mailbox becomes the Gmail node. Watch renewal every 7 days. Fallback if no inbound route exists: Gmail Trigger polling, agreed with Romain first. |
| 3:15 | Section 3 | Attachments: DOCX and scanned PDFs go to Document AI, one HTTP node. Multi-attachment is a loop around `PrepareDocument`. |
| 3:45 | Section 4, a model sub-node | Six model sub-nodes become Google Gemini Chat Model nodes. Re-run the fixtures and the golden set; tokens drop; keep the lexical guard until the golden set says otherwise. |
| 4:45 | Section 5 | Salesforce: two options. Direct SOQL in the agent tools, or a nightly cache into the application Postgres so the agents do not change at all (recommended). Draft Order becomes a Salesforce record carrying the resume URL; a Flow calls it on approve or reject. |
| 6:15 | Section 6 | TIBCO: same HTTP node, real URL and credential, payload from the ESB contract, 4xx to the desk, 5xx retried. Mock ESB is not deployed. |
| 7:00 | Section 7 and 8 | Trace tables stay. OpenTelemetry on the instance is the production path; the harvester stays for chargeback. Variables per environment, Git-backed dev, uat, prod. |
| 7:45 | Section 0 and 9, terminal | The cut-over order: instance, credentials tested one by one, schema, variables, deploy agents first, one real email end to end, fixtures and golden set green. `deploy.sh <file> poc --confirm`. |
| 8:45 | `DEPLOYMENT-ARCHITECTURE.md` section 1 and 2 | Air Liquide said 10,000 orders a day. Three executions per order, 11 million a year. Queue mode, two mains, two webhook processors, three workers, Redis, Cloud SQL. The PoC EC2 is single-container and fine for the demo volume. |
| 9:30 | `POC-PREREQUISITES.md` | What must exist before the install week: sizing, egress to Gmail, Gemini, Salesforce, OCB, TIBCO; the software approval list; accounts. Close with the 2026-11-20 demo date. |

## Production

| Step | Owner | When |
|---|---|---|
| Record videos 1 and 2 on the lab | Frosty | week of 2026-10-20 |
| Record video 3 from the docs | Frosty | same week, after Morgan reviews `POC-PREREQUISITES.md` |
| Review for accuracy and for anything that must not leave n8n | Morgan | before any AL share |
| Share 1 and 2 with AL GDO; 3 after the prerequisites review | Manuel | at the Monday check-in |
| Link all three in `HANDOFF.md` section 1 | Frosty | on upload |

Done when: three files in the engagement Drive folder, Morgan's review noted in the
session log, links in `HANDOFF.md`, no credential or non-synthetic data on screen
(checked by scrubbing through each file at 2x before upload).

# Air Liquide RFP: what the PoC has to prove

Sources (Drive folder shared by Manuel Craciun, read 2026-10-16): RFP PDF dated
2026-06-30 (Emmanuel Delort), Q&A sheet of 2026-07-22, n8n proposal v5 of
2026-09-11, RFP presentation deck of 2026-09-24, PoC architecture deck "POC n8n"
(Romain Fassi, GDO). Newest source wins where they differ.

## The process

PG (packaged gases) order intake, order-to-cash. Customers email free-text orders
with PDF, XLSX, CSV or DOCX attachments; product names and delivery addresses vary.
Baseline about 10,000 addressable orders in production, 24/7, no global deadline;
cut-offs come from the Omnichannel Backend per sales org and plant (France 23:59
Paris, Germany 09:00 Berlin).

Scoped flow (RFP p.24): unstructured email ingestion, agentic intent and data
extraction, autonomous validation via the Salesforce API, ERP creation or
human-in-the-loop validation.

## Core requirements (RFP p.24-25) and where the lab build answers them

| # | Requirement | Lab build |
|---|---|---|
| 1 | Zero-shot extraction of intent (order vs inquiry), Ship-To, product lines from email plus PDF/XLSX/CSV/DOCX | Information Extractor with a fixed schema; Extract from File for PDF/XLSX/CSV. DOCX not parsed yet (Document AI on the customer side) |
| 2 | Agentic reasoning and tool use: query Salesforce for the catalogue and partner roles, apply rules (Elephant Order over 80, empty bottle returns) | Product Agent and Shipping Agent sub-workflows with Postgres tools (Salesforce stand-in); rules in the deterministic gate |
| 3 | Long-running orchestration, notify-then-pull (Pub/Sub notification, then REST fetch of the payload) | Webhook acknowledges the Pub/Sub envelope at once; the email is pulled afterwards |
| 4 | HITL dashboard: exceptions to a manual queue in Salesforce as a pre-filled Draft Order with an AI-written message | draft_orders row plus order-desk email with Approve / Reject links; Wait node sleeps the execution |
| 5 | Deterministic API call to TIBCO to create the SAP sales order, HTTP 500 handled with exponential backoff | Mock ESB; backoff 4, 8, 16, 32, 64 s, five retries, then escalation |
| 6 | Low-code: rules, agent specialisations and routing configurable in the UI or prompts | Two named agents with prompts and tool descriptions; thresholds as constants at the top of the gate |
| 7 | Monitored view of each step, logs of agent reflection, metrics on dynamic dashboards | step_log with every tool call; metrics view; HTML dashboard |

## Success criteria (RFP p.26-27)

- Under 5 minutes per order from email to SAP order or Draft Case. Lab: 17 to 28 s on
  auto orders, 25 s to a Draft Order, with a local 30B model.
- Critical fields at 1.0: order intent, Ship-To code, email address, product codes
  and quantities. Lab: these are the critical rules C1 to C5; any failure forces a
  Draft Order.
- AI reasoning confidence at or above 0.7. Lab: rule R9.
- Over 80% automation on simple order types.
- Up to 5 retries over 2 minutes on 5xx before escalating to a human.

## Evaluation and deliverables

- Shortlisted to three bidders; PoC runs on Air Liquide's ecosystem with real
  Salesforce, SAP and GCP connectors. Weekly sync with the core PoC team, a Google
  Chat space, a dedicated Drive.
- Deliverables: runnable code and configuration, open platform access, financial
  reporting with a chargeback view, the supplementary feature-tracking document.
- Timeline: kickoff 2026-10-12; access and workshops 13 to 23 Oct; build 19 Oct to
  20 Nov; demo 20 Nov; internal review 23 to 27 Nov; selection 30 Nov.

## Architecture for the PoC (deck "POC n8n")

Full on-premise: one EC2 in Air Liquide's AWS private cloud (dev) running n8n,
reached over Citrix. Google service account, IMAP or API key for Drive, Sheets,
Gmail; Gemini via API key; SAP via a dedicated service account (sales order
creation); Salesforce via a dedicated API account (catalogue check, order
creation). Air Liquide asked for our prerequisites on network and flows, runner
sizing, OS (Windows Server 2022 compatibility), Citrix accounts, and access level:
see `POC-PREREQUISITES.md`.

Production target (proposal section 3.2): three regional self-hosted instances on
private GKE in Air Liquide's GCP, queue mode, Cloud SQL, Memorystore, Secret
Manager, Gemini on Vertex AI over private endpoints. Control-plane SaaS is
acceptable to AL; the data plane must be private.

## Constraints that shape the build

- LLM is bring-your-own and pass-through: Gemini for the PoC. Model-agnostic
  design, swap the model sub-node only.
- Public SaaS may not access Google Workspace. Everything runs inside AL's tenant.
- RPA stays with Automation Anywhere, orchestrated over its Control Room API.
- Signavio BPMN import is a gap; a converter was offered for the PoC phase. Out of
  scope for the PG Order build.
- The 4:30 PM deadline in the RFP is an example of an SLA-driven process, not a rule.

## Proposal commitments worth remembering

- Golden dataset of seventeen PG orders re-evaluated before every promotion;
  promotion blocked if any critical field scores below 1.0 (Evaluations).
- Guardrails node before the model, allow-listed tools around it, deterministic
  checks after it.
- The order desk review lives in Salesforce as a Draft Order; n8n forms and email
  are alternatives.

# Integration runbook: from the lab build to Air Liquide's systems

How the five workflows move from lab stand-ins to Gmail, Pub/Sub, Gemini,
Salesforce, the OCB and the TIBCO ESB. Node by node, with what changes and what
does not. Written 2026-10-17 against n8n 2.35; node versions quoted are the current
ones in that release (Gmail Trigger 1.4, Salesforce 1.1, Google Gemini Chat Model
1.2, HTTP Request 4.2). Configure each node from the live instance, not from
memory: open the node, check the fields, then set them.

The rule for the whole exercise: **the graph stays, the edges change.** Extraction,
the two agents, the gate, the order desk wait and the backoff loop are untouched.
Only the nodes that touch a stand-in are swapped, and every swap is listed here.

## 0. Before touching a workflow

1. Instance ready per `DEPLOYMENT-ARCHITECTURE.md` section 2 (PoC: the single EC2).
2. Air Liquide credentials created and entered in n8n's credential store, never in a
   file. See the table in section 1. Names matter: `deploy/creds.poc.json` maps the
   lab credential names to the PoC credential ids, so use the names below verbatim.
3. Application database created from `data/schema.sql` (schema `al_poc`), seed
   omitted. The trace tables (`order_intake`, `step_log`, `draft_orders`) stay. The
   stand-in tables (`mailbox`, `accounts`, `partner_roles`, `product_catalog`,
   `cut_offs`, `sales_orders`, `esb_chaos`) can stay empty or be dropped; nothing
   reads them once the swaps below are done, except the optional catalogue cache.
4. n8n Variables created: `AL_PG_AUTO_CONFIDENCE_MIN`, `AL_PG_ELEPHANT_QTY`,
   `AL_PG_ESB_MAX_ATTEMPTS`, `AL_PG_ESB_BACKOFF_BASE_S`. The code reads these and
   falls back to the defaults (0.7, 80, 5, 4) if they are missing.
5. Deploy order unchanged: Product Agent, Shipping Agent, intake, dashboard,
   telemetry harvester. The mock ESB is not deployed.

## 1. Credentials

| n8n credential name | Type | Used by | Air Liquide provides |
|---|---|---|---|
| `AL Gmail service account` | Google Service Account (domain-wide delegation to the order mailbox) | Gmail nodes, history and attachment calls | Tristan Schultz |
| `AL Pub/Sub push` | none in n8n; the push subscription carries an OIDC token | webhook ingress | GCP project owner |
| `AL Gemini` | Google Gemini (PaLM) API key, or Google Vertex AI service account | all six model sub-nodes | Narayana Ayyagari |
| `AL Salesforce API` | Salesforce OAuth2 JWT (connected app, certificate, integration user) | agent tools, verify steps, Draft Order | Moctar Ba |
| `AL OCB` | per the OCB contract (header auth or OAuth2 client credentials) | `get_cutoff` tool, `VerifyShipTo` | Raphael Leitao, Luc Ebert |
| `AL TIBCO ESB` | per the ESB contract | `PostSalesOrderToEsb` | Vorith Top |
| `AL Postgres app` | Postgres | every trace node | instance install (Morgan) |
| `AL n8n API` | Header Auth, `X-N8N-API-KEY` | telemetry harvester | n8n owner account on the instance |
| `AL SMTP` (optional) | SMTP | `NotifyOpsFailure`, order-desk mail if kept | Air Liquide relay |

## 2. Email in: Gmail and Pub/Sub

### 2a. Push, as the RFP describes it (notify, then pull)

GCP side, once per mailbox:

1. Pub/Sub topic `pg-order-intake`; grant `gmail-api-push@system.gserviceaccount.com`
   the Publisher role on it.
2. Push subscription to `https://<instance>/webhook/al-pg-order-notify`, with an
   OIDC token (service account, audience the webhook URL), ack deadline 10 s.
3. `users.watch` on the mailbox with the topic and the label to watch. The watch
   expires after 7 days: a scheduled n8n workflow (`RenewGmailWatch`, daily, one
   Gmail API call through the HTTP Request node with the service account) renews it.

What Gmail actually pushes: `{emailAddress, historyId}`, not a message id. The lab
envelope carries `email_id` directly, so the first zone changes shape:

| Node | Lab | Air Liquide | Change |
|---|---|---|---|
| `ReceivePubSubNotification` | Webhook 2.1, POST, respond on received | same | Add authentication: verify the OIDC token (webhook JWT auth with Google's keys) or restrict ingress to Pub/Sub at the load balancer plus a secret in the path |
| `DecodeNotification` | Code: base64 payload to `email_id` | Code: base64 payload to `{emailAddress, historyId}` | New output; keep `message_id` for the duplicate log |
| new `ListNewMessages` | none | HTTP Request 4.2, `GET gmail/v1/users/{mailbox}/history?startHistoryId=<cursor>&historyTypes=messageAdded`, Google Service Account credential | Cursor (`last_history_id` per mailbox) in a small table; one output item per new message id |
| `PullEmailFromMailbox` | Postgres `mailbox` | Gmail 2.x, resource Message, operation Get, Simplify off, Download Attachments on, attachment prefix `attachment_` | `order_id` becomes `ORD-<gmail message id>`; `OpenOrderRecord` keeps the idempotency on it |
| `PrepareDocument` | reads `attachments[0]` base64 | reads binary `attachment_0` from the Gmail node | Map `fileName` and `mimeType` from the binary metadata; body text from `text` or `textHtml` |

One push can carry several new messages: `ListNewMessages` emits one item per
message and the rest of the intake runs per item (one execution per push, one
order record per message). If Air Liquide prefers one execution per order, put the
history step in a small front workflow that calls the intake once per message.

### 2b. Polling, if no inbound route to the instance exists

Replace `ReceivePubSubNotification` and `DecodeNotification` with a Gmail Trigger
1.4 (service account, poll every minute, filter on the order label, Simplify off,
Max Emails per Poll 50) feeding `PullEmailFromMailbox` as above. Semantics are the
same (the trigger is the notification, the Gmail get is the pull); the Pub/Sub
requirement is answered on paper, not in the build, so agree it with Romain Fassi
before choosing this path.

## 3. Attachments

| Node | Lab | Air Liquide | Change |
|---|---|---|---|
| `RouteByAttachmentType` | pdf, xlsx, csv, else none | same, plus `docx` and `scan` outputs | Two more rules |
| `ExtractPdfText` | Extract from File 1.1, PDF | same | If the text is under 200 characters the PDF is a scan: route to Document AI |
| new `ReadWithDocumentAi` | none | HTTP Request 4.2 to `documentai.googleapis.com/v1/projects/.../processors/<ocr>:process`, service account, base64 body | Output `text` joins `AssembleDocument` like the others |
| DOCX | marked unsupported | Document AI, or convert to PDF first | Air Liquide's choice; both are one node |
| Several attachments | first only | loop | Make `PrepareDocument` emit one item per attachment and `AssembleDocument` join them; the extractor reads one document per email either way |

## 4. The model: Gemini

Six sub-nodes carry the lab model: `ExtractorModel`, `ExplanationModel`,
`ProductAgentModel`, `ProductParserFixerModel`, `ShippingAgentModel`,
`ShippingParserFixerModel`. Each is an OpenAI Chat Model node pointing at LM Studio.

| Lab | Air Liquide | Settings |
|---|---|---|
| OpenAI Chat Model 1.3, base URL LM Studio | Google Gemini Chat Model 1.2, credential `AL Gemini`, model as agreed with Narayana (`gemini-2.5-flash` class for extraction and agents) | Temperature 0.1 for extraction and agents, 0.0 for the parser fixers, 0.3 for the explanation; max output tokens 4096 |
| | or Google Vertex Chat Model with the service account for the private endpoint | same |

Everything else about the AI nodes stays: the Information Extractor schema, the
agents' system messages and tools, the structured output parsers with auto-fix. Do
these two things after the swap:

1. Re-run the five fixtures and the golden set. Gemini answers tool calls in fewer
   iterations than the lab model, so the token figures on the dashboard will drop.
2. Review the lexical guard in `FinalizeProductValidation`. It exists because the
   lab model translated product words before searching. Keep it unless the golden
   set shows it blocking correct matches; never remove it without that evidence.

## 5. Salesforce: catalogue, accounts, Ship-To, Draft Order

Decide first where the catalogue and partner roles are read from. Two options:

- **Direct.** Every tool call is a SOQL query through the Salesforce node. Simple,
  but two to four API calls per order line against the daily REST limit, and
  multilingual aliases need a field.
- **Cache (recommended).** A nightly sync (one n8n workflow, Salesforce Get Many to
  Postgres upsert) fills `product_catalog`, `accounts`, `partner_roles` in the
  application database. The agents keep their Postgres tools unchanged; only the
  write-side (Draft Order) goes to Salesforce live. Zero changes to the agents and
  the alias column stays.

### 5a. Product Agent

| Node | Lab | Air Liquide (direct) | Air Liquide (cache) |
|---|---|---|---|
| `lookup_product_by_code` | Postgres Tool on `product_catalog` | Salesforce Tool 1.1, resource Search, SOQL `SELECT Id, ProductCode, Name, Family, QuantityUnitOfMeasure FROM Product2 WHERE IsActive = true AND ProductCode = '{{ $fromAI("product_code") }}'` | unchanged |
| `search_product_by_name` | Postgres Tool, token AND over name and aliases | Salesforce Tool, SOQL with `Name LIKE` per token; aliases need a custom multi-language field or a `Product_Alias__c` object | unchanged |
| `VerifyProductMatches` | Postgres | Salesforce node, SOQL `WHERE ProductCode IN (...)` | unchanged |

Object and field names above are the standard ones; Air Liquide's org has SAP
material codes on `Product2.ProductCode` or a custom field. Moctar Ba confirms.

### 5b. Shipping Agent

| Node | Lab | Air Liquide (direct) |
|---|---|---|
| `find_account_by_email` | Postgres on `accounts` | SOQL on Contact (`Email = sender`) to `AccountId`, fallback on an `Email_Domain__c` account field; return the SAP Sold-To number field and the sales org and plant fields |
| `list_ship_to_for_account` | Postgres on `partner_roles` | SOQL on the partner-function object Air Liquide syncs from SAP (Sold-To to Ship-To, role `SH`); standard `AccountPartner` is unlikely to be it |
| `get_cutoff` | Postgres on `cut_offs` | HTTP Request Tool to the OCB cut-off endpoint, `sales_org` and `plant` as query parameters, credential `AL OCB` |
| `VerifyShipTo` | Postgres join | Two calls: the partner-function query for the pair, then OCB for the cut-off; or the cache |

### 5c. Draft Order and the order desk

| Node | Lab | Air Liquide |
|---|---|---|
| `CreateDraftOrder` | Postgres insert into `draft_orders` | Salesforce 1.1, resource Custom Object, Create, on the Draft Order object (name from Moctar: `Draft_Order__c` or `Order` with `Status = Draft` plus `OrderItem` children). Fields: order id, Sold-To, Ship-To, lines (child records or JSON), reasons, the AI note, confidence, `Resume_URL__c` = `$execution.resumeUrl` |
| `LinkDraftToOrder` | unchanged | unchanged; store the Salesforce record id in `draft_order_id` |
| `NotifyOrderDesk` | email with Approve / Reject links | optional; the queue is the Salesforce list view. Keep the email for the demo if Air Liquide wants it |
| `WaitForOrderDesk` | Wait 1.1, resume on webhook, GET | unchanged; the resume URL is on the record |
| resume | the person clicks the link | Salesforce Flow or Apex trigger on status change calls `Resume_URL__c?decision=approve` or `reject` (HTTP callout, remote site setting for the instance hostname). Fallback: a Salesforce Trigger 1.x polling the object every minute and an HTTP Request to the stored resume URL |
| `MarkRejected`, `RecordSalesOrder` | Postgres | unchanged; add a Salesforce update of the Draft Order status if the Flow does not do it |

The Wait node's resume URL is the only coupling; everything the desk needs is on
the record. Draft Orders that nobody touches stay as waiting executions; set a
`limitWaitTime` of 14 days on the Wait node so they expire into a logged state.

## 6. SAP through the TIBCO ESB

| Node | Lab | Air Liquide |
|---|---|---|
| `BuildSalesOrderPayload` | generic payload | the ESB contract: likely SAP S/4 sales-order fields (order type, sales organisation, distribution channel, division, Sold-To, Ship-To, requested delivery date, items with material, quantity, unit). Keep `idempotency_key = order_id` |
| `PostSalesOrderToEsb` | HTTP Request 4.2 to the mock | same node, the ESB URL, credential `AL TIBCO ESB`, idempotency key header if the ESB supports one, timeout 15 s, no node-level retry (the loop is the retry) |
| `ComputeBackoff` | classifies by HTTP status | same; add the ESB's own error body to `error_message`. 5xx and timeouts retry; 4xx go straight to the desk with the ESB message as the reason (new If before `ComputeBackoff`) |
| `RecordSalesOrder` | reads `sap_order_number` from the mock | map the ESB response field |
| Mock TIBCO ESB workflow | deployed | not deployed |

Test with the ESB sandbox and a forced 503 (ask Vorith for a way to provoke one, or
point the URL at an unroutable host to rehearse the timeout path).

## 7. Trace, dashboard, telemetry

- `order_intake`, `step_log`, `draft_orders` stay in the application Postgres. They
  are the audit trail the answer grid promised (correlation id `order_id` on every
  row and on the execution's custom data).
- Dashboard: keep the HTML webhook for the demo; point Looker at the same tables for
  the chargeback view (cost lines: executions per order from n8n Insights, tokens
  per order from `step_log`).
- Telemetry: enable OpenTelemetry export on the instance; the harvester workflow is
  the PoC path and stays useful for the chargeback figures.
- Failure notifications: `NotifyOpsFailure` to the Air Liquide relay, or replace
  with a Google Chat webhook message in the PoC space.

## 8. Security and operations on the instance

- Credentials in the n8n store, external secrets from GCP Secret Manager when the
  instance is in GCP. Individual logins (Administrator Charter), SSO once available,
  a project `PG Order Intake` with the three agents' workflows and the credentials
  shared into it, evaluators as viewers.
- Variables per environment; Git-backed environments for dev, uat, prod with the
  repo's `workflows/<env>/` layout as the promotion path.
- Instance error workflow that posts to the ops channel for anything the per-node
  error paths miss.
- Execution pruning and binary data mode per `DEPLOYMENT-ARCHITECTURE.md`.

## 9. Cut-over checklist

1. Credentials entered and tested one by one with a throwaway workflow (Gmail get,
   SOQL, Gemini prompt, OCB call, ESB health).
2. Application database created, `metrics` view returns a row.
3. Variables set.
4. Workflows deployed in order; sub-workflows published first.
5. One real email from a test customer account through the whole path to a sandbox
   SAP order; one forced Draft Order resolved from Salesforce.
6. Five fixtures and the golden set green on the instance; dashboard and
   `step_log` show the run.
7. Pub/Sub watch renewal scheduled; expiry alarm set.
8. Ops knows the runbook (`RUNBOOK.md`), the escalation list and the rollback:
   deactivate the intake, which stops ingestion; nothing is lost because Gmail keeps
   the messages and the history cursor resumes where it stopped.

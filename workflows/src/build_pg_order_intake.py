#!/usr/bin/env python3
"""Build the Air Liquide PG Order Intake PoC workflows under workflows/dev/.

Source of truth for the node graphs. Run, then zone_layout.py adds the canvas stickies.
Usage: python3 workflows/src/build_pg_order_intake.py

Workflows:
  al-pg-order-intake.json      main flow: Pub/Sub notify -> pull email -> extract -> agents -> gate -> SAP or Draft Order
  al-product-agent.json        sub-workflow: Product Agent (catalog tools, structured output, deterministic verify)
  al-shipping-agent.json       sub-workflow: Shipping Agent (account, Ship-To, cut-off tools, deterministic verify)
  al-mock-tibco-esb.json       lab stand-in for the TIBCO sales-order API, with a chaos table for 5xx
  al-metrics-dashboard.json    webhook-served HTML dashboard over order_intake
"""
import json
import uuid
from pathlib import Path

DEV = Path(__file__).resolve().parents[1] / "dev"
NS = uuid.UUID("7a1d2c3b-0000-4000-8000-0000000000a1")

CRED_LLM = {"openAiApi": {"id": "MBlabLmStudio001", "name": "LM Studio (lab)"}}
CRED_PG = {"postgres": {"id": "mMndKTE8fwlVCQ5K", "name": "Lab Postgres al_poc"}}
CRED_SMTP = {"smtp": {"id": "Djo70AfUDvH8s2WQ", "name": "Lab Mailpit SMTP"}}

LM_BASE = "http://host.docker.internal:1234/v1"
LM_MODEL = "qwen/qwen3-coder-30b"
ESB_URL = "http://localhost:5678/webhook/al-mock-tibco/sales-orders"

MAIL_FROM = "pg-order-bot@al-poc.lab"
MAIL_ORDER_DESK = "order-desk@al-poc.lab"
MAIL_OPS = "n8n-ops@al-poc.lab"

WF_MAIN = "AL PoC: Intake PG order (Pub/Sub to SAP or Draft Order)"
WF_PRODUCT = "AL PoC: Product Agent (resolve lines against catalog)"
WF_SHIPPING = "AL PoC: Shipping Agent (resolve Ship-To and cut-off)"
WF_ESB = "AL PoC: Mock TIBCO ESB (sales order API)"
WF_METRICS = "AL PoC: Metrics dashboard (order intake)"

AUTO_CONFIDENCE_MIN = 0.7
ELEPHANT_QTY = 80
ESB_MAX_ATTEMPTS = 5
ESB_BACKOFF_BASE_S = 4   # 4, 8, 16, 32, 64 s = 124 s before escalation


def nid(name):
    return str(uuid.uuid5(NS, name))


def node(name, ntype, ver, pos, params, **extra):
    n = {"id": nid(name), "name": name, "type": ntype, "typeVersion": ver,
         "position": list(pos), "parameters": params}
    n.update(extra)
    return n


def pg(name, pos, query, replacement, **extra):
    return node(name, "n8n-nodes-base.postgres", 2.7, pos,
                {"operation": "executeQuery", "query": query,
                 "options": {"queryReplacement": replacement}},
                credentials=CRED_PG, retryOnFail=True, maxTries=3, waitBetweenTries=3000,
                onError="continueErrorOutput", **extra)


def pg_tool(name, pos, description, query, replacement):
    return node(name, "n8n-nodes-base.postgresTool", 2.7, pos,
                {"descriptionType": "manual", "toolDescription": description,
                 "operation": "executeQuery", "query": query,
                 "options": {"queryReplacement": replacement}},
                credentials=CRED_PG)


def llm(name, pos, temperature=0.1, max_tokens=4096, notes=None):
    return node(name, "@n8n/n8n-nodes-langchain.lmChatOpenAi", 1.3, pos,
                {"model": {"__rl": True, "mode": "id", "value": LM_MODEL},
                 "responsesApiEnabled": False,
                 "options": {"baseURL": LM_BASE, "temperature": temperature, "maxTokens": max_tokens}},
                credentials=CRED_LLM,
                notes=notes or "Lab model. Customer instance: Gemini on Air Liquide's own API key or Vertex AI, by replacing this sub-node.")


def if_node(name, pos, left, op_type, operation, right=None, single=False):
    cond = {"id": nid(name + "-c1"), "leftValue": left, "rightValue": right if right is not None else "",
            "operator": {"type": op_type, "operation": operation}}
    if single:
        cond["operator"]["singleValue"] = True
    return node(name, "n8n-nodes-base.if", 2.3, pos, {
        "conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
                       "conditions": [cond], "combinator": "and"},
        "options": {}})


def code(name, pos, js, **extra):
    return node(name, "n8n-nodes-base.code", 2, pos, {"jsCode": js.strip()}, **extra)


def step_log(name, pos, step, agent, message, detail, order_expr="$json.order_id"):
    """Agent-reflection log row. message and detail are JS expressions evaluated inside the n8n expression."""
    return pg(name, pos,
              "insert into al_poc.step_log (order_id, execution_id, step, agent, message, detail)\n"
              "values ($1, $2, $3, $4, $5, $6::jsonb)\nreturning id;",
              f"={{{{ [{order_expr}, $execution.id, '{step}', '{agent}', {message}, {detail}] }}}}",
              alwaysOutputData=True)


def sub_input_schema(fields):
    return [{"id": f, "displayName": f, "required": False, "defaultMatch": False, "display": True,
             "canBeUsedToMatch": True, "type": t} for f, t in fields]


def workflow(name, nodes, connections, tags):
    return {"name": name, "nodes": nodes, "connections": connections,
            "settings": {"executionOrder": "v1", "saveManualExecutions": True,
                         "saveDataSuccessExecution": "all", "saveDataErrorExecution": "all"},
            "tags": tags}


class Wiring:
    def __init__(self):
        self.c = {}

    def __call__(self, src, dst, src_index=0, ctype="main", dst_index=0):
        outs = self.c.setdefault(src, {}).setdefault(ctype, [])
        while len(outs) <= src_index:
            outs.append([])
        outs[src_index].append({"node": dst, "type": ctype, "index": dst_index})


def write(wf, filename):
    DEV.mkdir(parents=True, exist_ok=True)
    out = DEV / filename
    out.write_text(json.dumps(wf, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out.name} ({len(wf['nodes'])} nodes)")


# =============================================================================== mock TIBCO ESB
def build_mock_esb():
    R = 0
    nodes = [
        node("ReceiveSalesOrderPost", "n8n-nodes-base.webhook", 2.1, (0, R),
             {"httpMethod": "POST", "path": "al-mock-tibco/sales-orders", "responseMode": "responseNode", "options": {}},
             webhookId=nid("webhook-mock-tibco"),
             notes="Stand-in for the TIBCO ESB sales-order endpoint. Body: the payload BuildSalesOrderPayload produces."),
        pg("ConsumeChaosBudget", (208, R),
           "with c as (\n  update al_poc.esb_chaos set remaining_failures = remaining_failures - 1, calls = calls + 1\n"
           "  where order_id = $1 and remaining_failures > 0\n  returning remaining_failures\n)\n"
           "select exists (select 1 from c) as fail;",
           "={{ [$json.body.order_id || ''] }}",
           notes="esb_chaos.remaining_failures > 0 forces a 503 on this call and decrements. Lets a fixture rehearse the backoff loop."),
        if_node("ShouldFail", (416, R), "={{ $json.fail }}", "boolean", "true", single=True),
        node("RespondServiceUnavailable", "n8n-nodes-base.respondToWebhook", 1.5, (624, R + 272),
             {"respondWith": "json", "responseBody": "={{ JSON.stringify({ error: 'ESB temporarily unavailable', code: 'ESB_503' }) }}",
              "options": {"responseCode": 503}}),
        pg("CreateSapSalesOrder", (624, R),
           "insert into al_poc.sales_orders (order_id, sap_order_number, payload)\nvalues ($1, $2, $3::jsonb)\n"
           "on conflict (order_id) do update set payload = excluded.payload\nreturning order_id, sap_order_number;",
           "={{ [$('ReceiveSalesOrderPost').first().json.body.order_id, '00' + String(Date.now()).slice(-8), "
           "JSON.stringify($('ReceiveSalesOrderPost').first().json.body)] }}",
           notes="SAP S/4HANA stand-in. Idempotent on order_id: a retried post returns the same SAP number."),
        node("RespondCreated", "n8n-nodes-base.respondToWebhook", 1.5, (832, R),
             {"respondWith": "json",
              "responseBody": "={{ JSON.stringify({ status: 'created', order_id: $json.order_id, sap_order_number: $json.sap_order_number }) }}",
              "options": {"responseCode": 201}}),
        node("RespondInternalError", "n8n-nodes-base.respondToWebhook", 1.5, (832, R + 272),
             {"respondWith": "json", "responseBody": "={{ JSON.stringify({ error: 'mock ESB failure', detail: String($json.error?.message || $json.error || '') }) }}",
              "options": {"responseCode": 500}}),
    ]
    w = Wiring()
    w("ReceiveSalesOrderPost", "ConsumeChaosBudget")
    w("ConsumeChaosBudget", "ShouldFail", 0)
    w("ConsumeChaosBudget", "RespondInternalError", 1)
    w("ShouldFail", "RespondServiceUnavailable", 0)
    w("ShouldFail", "CreateSapSalesOrder", 1)
    w("CreateSapSalesOrder", "RespondCreated", 0)
    w("CreateSapSalesOrder", "RespondInternalError", 1)
    return workflow(WF_ESB, nodes, w.c, ["al-poc", "pg-order-intake", "lab-stand-in"])


# =============================================================================== product agent
PRODUCT_SCHEMA = {
    "type": "object",
    "properties": {
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line_no": {"type": "integer"},
                    "input_text": {"type": "string", "description": "The product exactly as the customer wrote it"},
                    "quantity": {"type": "number"},
                    "unit": {"type": "string"},
                    "product_code": {"type": "string", "description": "Catalogue product_code returned by a tool, or empty string"},
                    "product_name": {"type": "string"},
                    "match": {"type": "string", "enum": ["exact", "resolved", "ambiguous", "not_found"]},
                    "candidates": {"type": "array", "items": {"type": "string"}},
                    "explanation": {"type": "string", "description": "One sentence: which tool result justified the match, or why none did"},
                },
                "required": ["line_no", "input_text", "quantity", "product_code", "match", "explanation"],
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["lines", "summary"],
}

PRODUCT_SYSTEM = (
    "You are the Product Agent for Air Liquide packaged-gas order intake. For every order line you receive, "
    "find the single catalogue product it refers to, using only the tools. Rules: (1) If the customer gave a code, "
    "call lookup_product_by_code first. (2) Otherwise call search_product_by_name with two or three words taken from "
    "the customer's own line, in the customer's language (French, German or English): do not translate, the catalogue "
    "aliases are multilingual. A cylinder size alone (B50, B11) is not a product. Try at most three searches per line, "
    "then give up with not_found. (3) 'exact' means the customer wrote a code that matched a catalogue code (a cylinder "
    "size is not a code); 'resolved' means a search returned exactly one product whose name or alias describes the "
    "customer's line (same product family, not merely the same gas); "
    "'ambiguous' means several plausible products and you must list them in candidates; 'not_found' means no tool "
    "result fits. (4) Never invent a product_code: product_code must be a value a tool returned, or an empty string. "
    "(5) Keep quantity and unit as given. Return the JSON the output format requires and nothing else."
)

PRODUCT_FINALIZE = r"""
// Deterministic post-check over the Product Agent's answer: a code counts only if the catalogue returned it.
const agent = $('ResolveProductsWithAgent').first().json;
const out = agent.output || {};
const verified = new Map(($('VerifyProductMatches').all() || [])
  .map(i => i.json).filter(r => r && r.product_code).map(r => [r.product_code, r]));
const inputLines = $('StartProductAgent').first().json.lines || [];
// Lexical check: the catalogue row's name or one alias must be mostly present in the customer's words,
// or the customer must have written the code itself. Same gas in a different product does not pass.
const norm = s => String(s ?? '').toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '');
const toks = s => norm(s).split(/[^a-z0-9]+/).filter(Boolean);
const LEX_MIN = 0.6;
const lexical = (input, cat) => {
  const inToks = new Set(toks(input));
  const flat = norm(input).replace(/[^a-z0-9]/g, '');
  if (flat.includes(norm(cat.product_code).replace(/[^a-z0-9]/g, ''))) return { ok: true, score: 1, via: 'code' };
  let best = { ok: false, score: 0, via: '' };
  for (const cand of [cat.name, ...(cat.aliases || [])]) {
    const ct = toks(cand);
    if (!ct.length) continue;
    const hit = ct.filter(t => inToks.has(t)).length / ct.length;
    if (hit > best.score) best = { ok: hit >= LEX_MIN, score: Math.round(hit * 100) / 100, via: cand };
  }
  return best;
};
const lines = (out.lines || []).map((l, i) => {
  const code = String(l.product_code || '').trim();
  const cat = verified.get(code);
  const okMatch = ['exact', 'resolved'].includes(l.match);
  const lex = cat ? lexical(l.input_text || inputLines[i]?.product_text || '', cat) : { ok: false, score: 0, via: '' };
  let status, explanation = l.explanation || '';
  if (cat && okMatch && lex.ok) status = 'verified';
  else if (cat && okMatch && !lex.ok) { status = 'ambiguous'; explanation = `Agent chose ${code} but the customer's words do not describe it (lexical overlap ${lex.score}). ${explanation}`; }
  else if (l.match === 'ambiguous') status = 'ambiguous';
  else if (code && !cat) status = 'unverified_code';
  else status = 'not_found';
  const qty = Number(l.quantity ?? inputLines[i]?.quantity ?? 0);
  return {
    line_no: l.line_no ?? i + 1, input_text: l.input_text, quantity: qty, unit: l.unit || cat?.unit || inputLines[i]?.unit || 'EA',
    product_code: status === 'verified' ? cat.product_code : '', product_name: cat ? cat.name : (l.product_name || ''),
    match: l.match, status, candidates: status === 'ambiguous' && cat ? [cat.product_code, ...(l.candidates || [])] : (l.candidates || []),
    explanation, lexical: lex,
    elephant: qty > ELEPHANT_QTY,
  };
});
const steps = (agent.intermediateSteps || []).map(s => ({
  tool: s.action?.tool, input: s.action?.toolInput, observation: String(s.observation ?? '').slice(0, 300),
}));
return [{ json: {
  order_id: $('StartProductAgent').first().json.order_id,
  agent: 'ProductAgent',
  lines,
  all_verified: lines.length > 0 && lines.every(l => l.status === 'verified'),
  elephant_lines: lines.filter(l => l.elephant).map(l => `${l.product_code || l.input_text}: ${l.quantity}`),
  summary: out.summary || '',
  tool_calls: steps.length,
  trace: steps,
}}];
""".replace("ELEPHANT_QTY", str(ELEPHANT_QTY))


def build_product_agent():
    R1, R2 = 0, 640
    nodes = [
        node("StartProductAgent", "n8n-nodes-base.executeWorkflowTrigger", 1.2, (0, R1),
             {"inputSource": "workflowInputs",
              "workflowInputs": {"values": [{"name": "order_id", "type": "string"}, {"name": "lines", "type": "array"}]}},
             notes="Inputs: order_id, lines = [{line_no, product_text, product_code_hint, quantity, unit}]"),
        node("ResolveProductsWithAgent", "@n8n/n8n-nodes-langchain.agent", 3.1, (240, R1),
             {"promptType": "define",
              "text": "=Order {{ $json.order_id }}. Resolve each of these lines against the catalogue:\n"
                      "{{ JSON.stringify(($json.lines || []).map((l, i) => ({ line_no: l.line_no || i + 1, product_text: l.product_text, "
                      "product_code_hint: l.product_code_hint || '', quantity: l.quantity, unit: l.unit || '' })), null, 2) }}",
              "hasOutputParser": True,
              "options": {"systemMessage": PRODUCT_SYSTEM, "maxIterations": 20, "returnIntermediateSteps": True}},
             retryOnFail=True, maxTries=2, waitBetweenTries=3000, onError="continueErrorOutput"),
        llm("ProductAgentModel", (0, R1 + 272)),
        pg_tool("lookup_product_by_code", (208, R1 + 272),
                "Look up one catalogue product by its product code. Use when the customer wrote a code such as ARCAL-1-B50 or OXY-IND-B50. "
                "Returns product_code, name, unit, category, is_cylinder, or no rows when the code does not exist.",
                "select product_code, name, unit, category, is_cylinder from al_poc.product_catalog\n"
                "where upper(product_code) = upper($1) or upper(replace(product_code, '-', '')) = upper(replace($1, '-', ''));",
                "={{ $fromAI('product_code', 'Product code exactly as the customer wrote it, e.g. ARCAL-1-B50', 'string') }}"),
        pg_tool("search_product_by_name", (416, R1 + 272),
                "Search the catalogue by product name or alias fragment. Use a short fragment: the gas or product name plus the cylinder size, "
                "e.g. 'arcal 1', 'oxygen B50', 'aligal 2'. Returns up to 10 rows with product_code, name, aliases, unit, category.",
                "with q as (select w from unnest(string_to_array(lower(translate($1, 'éèêëàâäôöûüç', 'eeeeaaaoouuc')), ' ')) w where length(w) >= 2)\n"
                "select p.product_code, p.name, p.aliases, p.unit, p.category from al_poc.product_catalog p\n"
                "where (select bool_and(lower(p.name) like '%' || w || '%' or exists (select 1 from unnest(p.aliases) a where lower(a) like '%' || w || '%')) from q)\n"
                "order by p.product_code limit 10;",
                "={{ $fromAI('search_term', 'Two or three words from the customer line, in the customer language, e.g. \"arcal 1\", \"detendeur oxygene\", \"kohlensaeure b20\". Every word must match, so drop quantities and filler words.', 'string') }}"),
        node("ProductOutputParser", "@n8n/n8n-nodes-langchain.outputParserStructured", 1.3, (624, R1 + 272),
             {"schemaType": "manual", "inputSchema": json.dumps(PRODUCT_SCHEMA, indent=2), "autoFix": True}),
        llm("ProductParserFixerModel", (624, R1 + 480), temperature=0.0, max_tokens=4096,
            notes="Second model call only when the agent's JSON does not match the schema."),

        pg("VerifyProductMatches", (208, R2),
           "select product_code, name, aliases, unit from al_poc.product_catalog\nwhere product_code = any(string_to_array($1, ','));",
           "={{ [ (($('ResolveProductsWithAgent').first().json.output || {}).lines || []).map(l => String(l.product_code || '').trim()).filter(Boolean).join(',') ] }}",
           alwaysOutputData=True,
           notes="Deterministic check: every code the agent chose must exist in the catalogue. A hallucinated code is dropped."),
        step_log("LogProductAgentStep", (0, R2), "product_agent", "ProductAgent",
                 "'Product Agent returned ' + ((($('ResolveProductsWithAgent').first().json.output || {}).lines || []).length) + ' lines'",
                 "JSON.stringify({ output: $('ResolveProductsWithAgent').first().json.output, "
                 "steps: ($('ResolveProductsWithAgent').first().json.intermediateSteps || []).map(s => ({ tool: s.action?.tool, input: s.action?.toolInput })) })",
                 order_expr="$('StartProductAgent').first().json.order_id"),
        code("FinalizeProductValidation", (416, R2), PRODUCT_FINALIZE),
        code("ReportProductAgentFailure", (208, R1 + 480), r"""
// The agent itself failed (model down, max iterations). Return a shape the caller can route to review.
const err = $input.first().json;
return [{ json: {
  order_id: $('StartProductAgent').first().json.order_id, agent: 'ProductAgent', lines: [], all_verified: false,
  elephant_lines: [], summary: 'Product Agent failed: ' + String(err.error?.message || err.error || 'unknown error'),
  tool_calls: 0, trace: [], agent_error: true,
}}];
"""),
    ]
    w = Wiring()
    w("StartProductAgent", "ResolveProductsWithAgent")
    w("ProductAgentModel", "ResolveProductsWithAgent", 0, "ai_languageModel")
    w("lookup_product_by_code", "ResolveProductsWithAgent", 0, "ai_tool")
    w("search_product_by_name", "ResolveProductsWithAgent", 0, "ai_tool")
    w("ProductOutputParser", "ResolveProductsWithAgent", 0, "ai_outputParser")
    w("ProductParserFixerModel", "ProductOutputParser", 0, "ai_languageModel")
    w("ResolveProductsWithAgent", "LogProductAgentStep", 0)
    w("ResolveProductsWithAgent", "ReportProductAgentFailure", 1)
    w("LogProductAgentStep", "VerifyProductMatches", 0)
    w("LogProductAgentStep", "VerifyProductMatches", 1)
    w("VerifyProductMatches", "FinalizeProductValidation", 0)
    w("VerifyProductMatches", "FinalizeProductValidation", 1)
    return workflow(WF_PRODUCT, nodes, w.c, ["al-poc", "pg-order-intake", "agent"])


# =============================================================================== shipping agent
SHIPPING_SCHEMA = {
    "type": "object",
    "properties": {
        "sold_to_code": {"type": "string", "description": "Account sold_to_code returned by find_account_by_email, or empty"},
        "sold_to_name": {"type": "string"},
        "ship_to_code": {"type": "string", "description": "ship_to_code returned by list_ship_to_for_account, or empty"},
        "ship_to_name": {"type": "string"},
        "match": {"type": "string", "enum": ["exact", "resolved", "ambiguous", "not_found"]},
        "candidates": {"type": "array", "items": {"type": "string"}},
        "sales_org": {"type": "string"},
        "plant": {"type": "string"},
        "cutoff_local_time": {"type": "string"},
        "tz": {"type": "string"},
        "explanation": {"type": "string"},
    },
    "required": ["sold_to_code", "ship_to_code", "match", "explanation"],
}

SHIPPING_SYSTEM = (
    "You are the Shipping Agent for Air Liquide packaged-gas order intake. Identify the customer account (Sold-To) "
    "and the delivery address (Ship-To) for one order, using only the tools. Steps: (1) call find_account_by_email "
    "with the sender email; if it returns no account, answer match=not_found. (2) call list_ship_to_for_account with "
    "the sold_to_code and compare the customer's delivery address and Ship-To code against the rows: street, city, "
    "postal code, or an explicit Ship-To code. (3) call get_cutoff with the account's sales_org and plant. "
    "'exact' means the customer gave a Ship-To code or an address that matches one row unambiguously; 'resolved' means "
    "one row matches on city plus street or name; 'ambiguous' means two or more rows could match (list them in "
    "candidates); 'not_found' means no row matches. Never invent codes: sold_to_code and ship_to_code must be values "
    "a tool returned, or empty strings. Return the JSON the output format requires and nothing else."
)

SHIPPING_FINALIZE = r"""
// Deterministic post-check: the Ship-To must be a partner of the Sold-To; cut-off evaluated in the plant's time zone.
const agent = $('ResolveShipToWithAgent').first().json;
const out = agent.output || {};
const row = ($('VerifyShipTo').first().json || {});
const verified = !!row.ship_to_code;
const tz = out.tz || row.tz || 'Europe/Paris';
const cutoff = String(out.cutoff_local_time || row.cutoff_local_time || '').slice(0, 5);
let past_cutoff = false, earliest_dispatch_date = null, now_local = null;
try {
  const now = DateTime.now().setZone(tz);
  now_local = now.toFormat('yyyy-LL-dd HH:mm');
  if (cutoff) {
    const [h, m] = cutoff.split(':').map(Number);
    const c = now.set({ hour: h, minute: m, second: 0, millisecond: 0 });
    past_cutoff = now > c;
    let d = now.plus({ days: past_cutoff ? 2 : 1 });
    while (d.weekday > 5) d = d.plus({ days: 1 });
    earliest_dispatch_date = d.toFormat('yyyy-LL-dd');
  }
} catch (e) { /* leave cut-off fields empty */ }
const steps = (agent.intermediateSteps || []).map(s => ({
  tool: s.action?.tool, input: s.action?.toolInput, observation: String(s.observation ?? '').slice(0, 300),
}));
return [{ json: {
  order_id: $('StartShippingAgent').first().json.order_id,
  agent: 'ShippingAgent',
  sold_to_code: row.sold_to_code || out.sold_to_code || '', sold_to_name: out.sold_to_name || '',
  ship_to_code: verified ? row.ship_to_code : '', ship_to_name: verified ? row.name : (out.ship_to_name || ''),
  ship_to_city: verified ? row.city : '',
  match: out.match || 'not_found', status: verified && ['exact', 'resolved'].includes(out.match) ? 'verified' : (out.match || 'not_found'),
  candidates: out.candidates || [],
  sales_org: out.sales_org || row.sales_org || '', plant: out.plant || row.plant || '',
  cutoff_local_time: cutoff, tz, now_local, past_cutoff, earliest_dispatch_date,
  explanation: out.explanation || '',
  tool_calls: steps.length, trace: steps,
}}];
"""


def build_shipping_agent():
    R1, R2 = 0, 640
    nodes = [
        node("StartShippingAgent", "n8n-nodes-base.executeWorkflowTrigger", 1.2, (0, R1),
             {"inputSource": "workflowInputs",
              "workflowInputs": {"values": [{"name": "order_id", "type": "string"}, {"name": "sender_email", "type": "string"},
                                            {"name": "customer_name", "type": "string"}, {"name": "sold_to_hint", "type": "string"},
                                            {"name": "ship_to", "type": "object"}, {"name": "requested_delivery_date", "type": "string"}]}},
             notes="Inputs: order_id, sender_email, customer_name, sold_to_hint, ship_to = {code, name, street, city, postal_code, country}, requested_delivery_date"),
        node("ResolveShipToWithAgent", "@n8n/n8n-nodes-langchain.agent", 3.1, (240, R1),
             {"promptType": "define",
              "text": "=Order {{ $json.order_id }}.\nSender email: {{ $json.sender_email }}\nCustomer name in the email: {{ $json.customer_name }}\n"
                      "Customer number mentioned (may be empty): {{ $json.sold_to_hint }}\n"
                      "Delivery address as written: {{ JSON.stringify($json.ship_to || {}) }}\n"
                      "Requested delivery date: {{ $json.requested_delivery_date || 'not given' }}\nToday is {{ $now.toFormat('yyyy-LL-dd') }}.",
              "hasOutputParser": True,
              "options": {"systemMessage": SHIPPING_SYSTEM, "maxIterations": 20, "returnIntermediateSteps": True}},
             retryOnFail=True, maxTries=2, waitBetweenTries=3000, onError="continueErrorOutput"),
        llm("ShippingAgentModel", (0, R1 + 272)),
        pg_tool("find_account_by_email", (208, R1 + 272),
                "Find the customer account (Sold-To) from the sender's email address. Matches the contact email or the email domain. "
                "Returns sold_to_code, name, email_domain, contact_email, sales_org, plant; no rows when unknown.",
                "select sold_to_code, name, email_domain, contact_email, sales_org, plant from al_poc.accounts\n"
                "where lower(contact_email) = lower($1) or lower(email_domain) = lower(split_part($1, '@', 2));",
                "={{ $fromAI('email', 'Sender email address exactly as given', 'string') }}"),
        pg_tool("list_ship_to_for_account", (416, R1 + 272),
                "List the delivery addresses (Ship-To partner roles) allowed for a Sold-To account. "
                "Returns ship_to_code, name, street, city, postal_code, country for each.",
                "select ship_to_code, name, street, city, postal_code, country from al_poc.partner_roles\n"
                "where sold_to_code = $1 and role = 'SH' order by ship_to_code;",
                "={{ $fromAI('sold_to_code', 'The sold_to_code returned by find_account_by_email', 'string') }}"),
        pg_tool("get_cutoff", (624, R1 + 272),
                "Get the daily order cut-off time for a sales organisation and plant (the Omnichannel Backend rule). "
                "Returns cutoff_local_time and tz.",
                "select sales_org, plant, cutoff_local_time::text as cutoff_local_time, tz from al_poc.cut_offs\n"
                "where sales_org = $1 and plant = $2;",
                "={{ $fromAI('sales_org', 'sales_org of the account, e.g. FR01', 'string') }},{{ $fromAI('plant', 'plant of the account, e.g. FR07', 'string') }}"),
        node("ShippingOutputParser", "@n8n/n8n-nodes-langchain.outputParserStructured", 1.3, (864, R1 + 272),
             {"schemaType": "manual", "inputSchema": json.dumps(SHIPPING_SCHEMA, indent=2), "autoFix": True}),
        llm("ShippingParserFixerModel", (864, R1 + 480), temperature=0.0,
            notes="Second model call only when the agent's JSON does not match the schema."),

        pg("VerifyShipTo", (208, R2),
           "select p.sold_to_code, p.ship_to_code, p.name, p.city, a.sales_org, a.plant, c.cutoff_local_time::text as cutoff_local_time, c.tz\n"
           "from al_poc.partner_roles p\njoin al_poc.accounts a on a.sold_to_code = p.sold_to_code\n"
           "left join al_poc.cut_offs c on c.sales_org = a.sales_org and c.plant = a.plant\n"
           "where p.sold_to_code = $1 and p.ship_to_code = $2 and p.role = 'SH';",
           "={{ (o => [String(o.sold_to_code || ''), String(o.ship_to_code || '')])($('ResolveShipToWithAgent').first().json.output || {}) }}",
           alwaysOutputData=True,
           notes="Deterministic check: the chosen Ship-To must be a partner role of the chosen Sold-To."),
        step_log("LogShippingAgentStep", (0, R2), "shipping_agent", "ShippingAgent",
                 "'Shipping Agent match=' + (($('ResolveShipToWithAgent').first().json.output || {}).match || 'none')",
                 "JSON.stringify({ output: $('ResolveShipToWithAgent').first().json.output, "
                 "steps: ($('ResolveShipToWithAgent').first().json.intermediateSteps || []).map(s => ({ tool: s.action?.tool, input: s.action?.toolInput })) })",
                 order_expr="$('StartShippingAgent').first().json.order_id"),
        code("FinalizeShippingValidation", (416, R2), SHIPPING_FINALIZE),
        code("ReportShippingAgentFailure", (208, R1 + 480), r"""
const err = $input.first().json;
return [{ json: {
  order_id: $('StartShippingAgent').first().json.order_id, agent: 'ShippingAgent', sold_to_code: '', ship_to_code: '',
  match: 'not_found', status: 'agent_error', candidates: [], past_cutoff: false, earliest_dispatch_date: null,
  explanation: 'Shipping Agent failed: ' + String(err.error?.message || err.error || 'unknown error'), tool_calls: 0, trace: [], agent_error: true,
}}];
"""),
    ]
    w = Wiring()
    w("StartShippingAgent", "ResolveShipToWithAgent")
    w("ShippingAgentModel", "ResolveShipToWithAgent", 0, "ai_languageModel")
    w("find_account_by_email", "ResolveShipToWithAgent", 0, "ai_tool")
    w("list_ship_to_for_account", "ResolveShipToWithAgent", 0, "ai_tool")
    w("get_cutoff", "ResolveShipToWithAgent", 0, "ai_tool")
    w("ShippingOutputParser", "ResolveShipToWithAgent", 0, "ai_outputParser")
    w("ShippingParserFixerModel", "ShippingOutputParser", 0, "ai_languageModel")
    w("ResolveShipToWithAgent", "LogShippingAgentStep", 0)
    w("ResolveShipToWithAgent", "ReportShippingAgentFailure", 1)
    w("LogShippingAgentStep", "VerifyShipTo", 0)
    w("LogShippingAgentStep", "VerifyShipTo", 1)
    w("VerifyShipTo", "FinalizeShippingValidation", 0)
    w("VerifyShipTo", "FinalizeShippingValidation", 1)
    return workflow(WF_SHIPPING, nodes, w.c, ["al-poc", "pg-order-intake", "agent"])


# =============================================================================== main intake
EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["order", "inquiry", "return", "complaint", "other"],
                   "description": "order = the customer wants products delivered; inquiry = question or price request; return = only empty cylinders going back"},
        "customer_name": {"type": "string", "description": "Company name of the customer"},
        "customer_email": {"type": "string", "description": "The sender's email address"},
        "sold_to_hint": {"type": "string", "description": "Customer or account number if written in the email, else empty"},
        "ship_to": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Ship-To code if written, else empty"},
                "name": {"type": "string"}, "street": {"type": "string"}, "city": {"type": "string"},
                "postal_code": {"type": "string"}, "country": {"type": "string", "description": "ISO 3166 alpha-2 if known"},
            },
        },
        "requested_delivery_date": {"type": "string", "description": "YYYY-MM-DD, or empty. 'tomorrow' is resolved against today's date."},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line_no": {"type": "integer"},
                    "product_text": {"type": "string", "description": "Product as written by the customer"},
                    "product_code_hint": {"type": "string", "description": "A code if the customer wrote one, else empty"},
                    "quantity": {"type": "number"},
                    "unit": {"type": "string", "description": "cylinder, bottle, EA, as written; empty if none"},
                },
                "required": ["line_no", "product_text", "quantity"],
            },
        },
        "empty_bottle_returns": {
            "type": "array",
            "items": {"type": "object", "properties": {"product_text": {"type": "string"}, "quantity": {"type": "number"}}},
            "description": "Empty cylinders the customer wants collected",
        },
        "notes": {"type": "string"},
        "reasoning": {"type": "string", "description": "One or two sentences on why you chose this intent and these lines"},
        "confidence": {"type": "number", "description": "0 to 1: how sure you are that intent, lines, quantities and address are read correctly"},
    },
    "required": ["intent", "customer_email", "ship_to", "lines", "empty_bottle_returns", "reasoning", "confidence"],
}

EXTRACTION_SYSTEM = (
    "You are the order-intake extraction step for Air Liquide packaged gases. Read the customer email and the "
    "attachment text (French, German or English) and return JSON matching the schema. Copy product wording and codes "
    "exactly as written; never translate a product code. Quantities are plain numbers. Dates are YYYY-MM-DD; today is "
    "{{ $now.toFormat('yyyy-LL-dd') }}. If something is absent use an empty string, 0 or an empty array; never invent. "
    "Empty cylinders the customer sends back go in empty_bottle_returns, not in lines. Set confidence low when the "
    "text is ambiguous or the attachment is unreadable."
)

DECODE_NOTIFICATION = r"""
// Pub/Sub push envelope -> {email_id}. The notification is only a pointer; the payload is pulled next.
const j = $input.first().json;
const body = j.body || j;
let email_id = body.email_id;
let message_id = body.message?.messageId || null;
if (!email_id && body.message?.data) {
  try { email_id = JSON.parse(Buffer.from(body.message.data, 'base64').toString('utf8')).email_id; } catch (e) { email_id = null; }
}
if (!email_id) throw new Error('notification carries no email_id');
return [{ json: { email_id, message_id, order_id: 'ORD-' + email_id, notified_at: new Date().toISOString() } }];
"""

PREPARE_DOCUMENT = r"""
// One item per email: the envelope plus the first attachment as binary, tagged by kind.
// PoC scope: one attachment per email. Multi-attachment emails loop over this stage in the next iteration.
const mail = $('PullEmailFromMailbox').first().json;
const order = $('OpenOrderRecord').first().json;
const atts = Array.isArray(mail.attachments) ? mail.attachments : [];
const a = atts[0];
const kindOf = (name, mime) => {
  const n = String(name || '').toLowerCase(), m = String(mime || '').toLowerCase();
  if (n.endsWith('.pdf') || m.includes('pdf')) return 'pdf';
  if (n.endsWith('.xlsx') || n.endsWith('.xls') || m.includes('spreadsheet') || m.includes('excel')) return 'xlsx';
  if (n.endsWith('.csv') || m.includes('csv')) return 'csv';
  if (n.endsWith('.docx') || m.includes('wordprocessingml')) return 'docx';
  return 'other';
};
const json = {
  order_id: order.order_id, email_id: mail.email_id, from: mail.from_addr, subject: mail.subject,
  body_text: mail.body_text, received_at: mail.received_at,
  attachment_count: atts.length, attachment_name: a ? a.name : '', attachment_kind: a ? kindOf(a.name, a.mime) : 'none',
};
const binary = {};
if (a) binary.attachment = { data: a.base64, mimeType: a.mime || 'application/octet-stream', fileName: a.name };
return [{ json, binary }];
"""

TABULAR_TO_TEXT = r"""
// Rows from Extract from File (xlsx or csv) -> one pipe-separated text block.
const rows = $input.all().map(i => i.json);
const text = rows.map(r => Object.values(r).map(v => String(v ?? '').trim()).filter(Boolean).join(' | ')).filter(Boolean).join('\n');
return [{ json: { table_text: text, row_count: rows.length } }];
"""

ASSEMBLE_DOCUMENT = r"""
// Email body plus whatever the attachment branch produced -> one document for the extractor.
const env = $('PrepareDocument').first().json;
const j = $input.first().json || {};
const attText = j.text || j.table_text || '';
const unsupported = ['docx', 'other'].includes(env.attachment_kind);
const document_text = [
  `EMAIL FROM: ${env.from}`, `SUBJECT: ${env.subject}`, `RECEIVED: ${env.received_at}`, '',
  '--- EMAIL BODY ---', env.body_text || '(empty)',
  attText ? `\n--- ATTACHMENT (${env.attachment_name}) ---\n${attText}` : '',
  unsupported ? `\n--- ATTACHMENT (${env.attachment_name}) could not be read: ${env.attachment_kind} is not parsed in the PoC ---` : '',
].join('\n');
return [{ json: { ...env, document_text, attachment_text_chars: attText.length, attachment_unsupported: unsupported } }];
"""

APPLY_BUSINESS_RULES = r"""
// Deterministic gate. Critical rules must all be 1.0; review rules decide auto vs Draft Order.
const AUTO_CONFIDENCE_MIN = __CONF__;
const ELEPHANT_QTY = __ELEPHANT__;
const env = $('AssembleDocument').first().json;
const ex = $('ExtractOrderWithAi').first().json.output || {};
const prod = $('RunProductAgent').first().json || {};
const ship = $('RunShippingAgent').first().json || {};
const MSG = {
  product: 'The product code identified by the AI is not found in the Salesforce product catalog',
  ambiguous_product: 'The product description matches more than one catalog reference',
  ship_to: 'The delivery address does not match a Ship-To partner of this Sold-To account',
  ambiguous_ship_to: 'Several Ship-To addresses match the delivery address given by the customer',
  account: 'The sender could not be matched to a customer account (Sold-To)',
  elephant: `Elephant Order: a line quantity exceeds ${ELEPHANT_QTY}`,
  returns: 'Empty cylinder return requested: local handling required',
  confidence: `AI confidence below ${AUTO_CONFIDENCE_MIN}`,
  quantity: 'A line has no usable quantity',
  intent: 'The email was not classified as an order',
  agent: 'A validation agent failed to answer',
};
const rules = [];
const add = (id, name, pass, detail, critical, message) => rules.push({ id, name, pass: !!pass, detail, critical, message: pass ? null : message });

const lines = prod.lines || [];
add('C1', 'Intent is order', ex.intent === 'order', `intent=${ex.intent}`, true, MSG.intent);
add('C2', 'Sender matched to a Sold-To account', !!ship.sold_to_code, ship.sold_to_code ? `sold_to ${ship.sold_to_code}` : (ship.explanation || 'no account'), true, MSG.account);
add('C3', 'Every product line verified in the catalogue', lines.length > 0 && lines.every(l => l.status === 'verified'),
  lines.map(l => `${l.input_text} -> ${l.product_code || l.status}`).join('; ') || 'no lines', true,
  lines.some(l => l.status === 'ambiguous') ? MSG.ambiguous_product : MSG.product);
add('C4', 'Ship-To verified as a partner of the Sold-To', ship.status === 'verified', ship.ship_to_code ? `ship_to ${ship.ship_to_code} (${ship.match})` : (ship.explanation || 'none'), true,
  ship.match === 'ambiguous' ? MSG.ambiguous_ship_to : MSG.ship_to);
add('C5', 'Quantities are positive integers', lines.length > 0 && lines.every(l => Number.isInteger(Number(l.quantity)) && Number(l.quantity) > 0),
  lines.map(l => `${l.product_code || l.input_text}: ${l.quantity}`).join('; '), true, MSG.quantity);
add('C6', 'Validation agents answered', !prod.agent_error && !ship.agent_error && !prod.error && !ship.error,
  (prod.summary || prod.error?.message || '') + ' ' + (ship.explanation || ship.error?.message || ''), true, MSG.agent);
add('R7', `No Elephant Order (qty <= ${ELEPHANT_QTY})`, !(prod.elephant_lines || []).length, (prod.elephant_lines || []).join('; ') || 'OK', false, MSG.elephant);
add('R8', 'No empty cylinder returns', !(ex.empty_bottle_returns || []).length, (ex.empty_bottle_returns || []).map(r => `${r.quantity} x ${r.product_text}`).join('; ') || 'none', false, MSG.returns);
add('R9', `AI confidence >= ${AUTO_CONFIDENCE_MIN}`, Number(ex.confidence) >= AUTO_CONFIDENCE_MIN, `confidence=${ex.confidence}`, false, MSG.confidence);
const cutoffNote = ship.cutoff_local_time
  ? `${ship.past_cutoff ? 'Past' : 'Before'} the ${ship.cutoff_local_time} ${ship.tz} cut-off (local ${ship.now_local}); earliest dispatch ${ship.earliest_dispatch_date}`
  : 'no cut-off rule found';
rules.push({ id: 'I10', name: 'Cut-off (informational)', pass: true, detail: cutoffNote, critical: false, message: null });

const critical_match = rules.filter(r => r.critical).every(r => r.pass);
const review_flags = rules.filter(r => !r.critical && !r.pass);
const decision = critical_match && review_flags.length === 0 ? 'auto' : 'draft';
const reasons = rules.filter(r => !r.pass).map(r => r.message);
return [{ json: {
  order_id: env.order_id, email_id: env.email_id, decision, critical_match, confidence: Number(ex.confidence) || 0,
  rules, reasons, cutoff: { past_cutoff: !!ship.past_cutoff, earliest_dispatch_date: ship.earliest_dispatch_date || null, note: cutoffNote },
  order: {
    sold_to_code: ship.sold_to_code || '', sold_to_name: ship.sold_to_name || ex.customer_name || '',
    ship_to_code: ship.ship_to_code || '', ship_to_name: ship.ship_to_name || '',
    requested_delivery_date: ex.requested_delivery_date || '',
    lines: lines.map(l => ({ line_no: l.line_no, product_code: l.product_code, product_name: l.product_name, quantity: l.quantity, unit: l.unit, status: l.status, input_text: l.input_text })),
    empty_bottle_returns: ex.empty_bottle_returns || [],
    customer_email: ex.customer_email || env.from, notes: ex.notes || '',
  },
  product_validation: prod, shipping_validation: ship,
}}];
""".replace("__CONF__", str(AUTO_CONFIDENCE_MIN)).replace("__ELEPHANT__", str(ELEPHANT_QTY))

BUILD_SALES_ORDER_PAYLOAD = r"""
// The sales-order call for the ESB. Same shape whether the order came straight through or via the order desk.
const v = $('ApplyBusinessRules').first().json;
const o = v.order;
const approvedBy = v.decision === 'auto' ? 'auto' : 'order-desk';
return [{ json: {
  order_id: v.order_id,
  payload: {
    order_id: v.order_id, idempotency_key: v.order_id, source: 'n8n-pg-order-intake',
    sold_to: o.sold_to_code, ship_to: o.ship_to_code,
    requested_delivery_date: o.requested_delivery_date || v.cutoff.earliest_dispatch_date || '',
    lines: o.lines.map(l => ({ item: l.line_no, material: l.product_code, quantity: l.quantity, unit: l.unit })),
    customer_reference: $('AssembleDocument').first().json.subject,
    approved_by: approvedBy, confidence: v.confidence,
  },
}}];
"""

COMPUTE_BACKOFF = r"""
// Exponential backoff for the ESB call. attempt n waits base * 2^(n-1) seconds; after the cap the order is escalated.
const MAX = __MAX__, BASE = __BASE__;
const attempt = $runIndex + 1;                      // how many times this node has run in this execution
const err = $input.first().json.error || {};
const status = Number(err.httpCode || err.status || (String(err.message || '').match(/\b(5\d\d)\b/) || [])[1] || 0);
const delay_seconds = BASE * Math.pow(2, attempt - 1);
return [{ json: {
  order_id: $('BuildSalesOrderPayload').first().json.order_id,
  attempt, max_attempts: MAX, retry: attempt <= MAX, delay_seconds,
  http_status: status, error_message: String(err.message || err.description || err || '').slice(0, 300),
}}];
""".replace("__MAX__", str(ESB_MAX_ATTEMPTS)).replace("__BASE__", str(ESB_BACKOFF_BASE_S))

DRAFT_EXPLANATION_PROMPT = (
    "=You write the note that accompanies a Draft Order in Salesforce so the order desk can resolve it fast.\n"
    "Order {{ $json.order_id }} from {{ $json.order.sold_to_name || $json.order.customer_email }}.\n"
    "Failed checks (fixed wording, keep each one verbatim):\n{{ $json.reasons.map(r => '- ' + r).join('\\n') }}\n"
    "Rule details:\n{{ $json.rules.filter(r => !r.pass).map(r => '- ' + r.id + ' ' + r.name + ': ' + r.detail).join('\\n') }}\n"
    "Lines as resolved:\n{{ $json.order.lines.map(l => '- ' + (l.product_code || '(no code)') + ' x ' + l.quantity + ' <- \"' + l.input_text + '\" [' + l.status + ']').join('\\n') }}\n"
    "Product agent summary: {{ $json.product_validation.summary }}\nShipping agent: {{ $json.shipping_validation.explanation }}\n"
    "{{ $json.cutoff.note }}\n\n"
    "Write 3 to 5 plain sentences in English: what was understood, exactly what blocks automatic posting (quote the fixed "
    "wording), and what the order desk should check or ask the customer. No greeting, no markdown."
)

ORDER_DESK_EMAIL_HTML = r"""={{ (() => {
  const v = $('ApplyBusinessRules').first().json, o = v.order, d = $('CreateDraftOrder').first().json;
  const esc = s => String(s ?? '').replace(/[<>&]/g, m => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[m]));
  const rules = v.rules.map(r => `<tr style="color:${r.pass ? '#1a7f37' : '#b42318'}"><td>${r.id}</td><td>${esc(r.name)}</td><td>${r.pass ? 'pass' : 'FAIL'}${r.critical ? ' (critical)' : ''}</td><td>${esc(r.detail)}</td></tr>`).join('');
  const lines = o.lines.map(l => `<tr><td>${esc(l.line_no)}</td><td>${esc(l.input_text)}</td><td>${esc(l.product_code || '-')}</td><td>${esc(l.product_name)}</td><td>${esc(l.quantity)} ${esc(l.unit)}</td><td>${esc(l.status)}</td></tr>`).join('');
  const resume = $execution.resumeUrl, base = resume + (resume.includes('?') ? '&' : '?');
  return `<html><body style="font-family:Arial,sans-serif;font-size:13px">
<h2>Draft Order #${d.id} needs the order desk</h2>
<p><b>${esc(o.sold_to_name)}</b> (${esc(o.sold_to_code || 'no Sold-To')}), Ship-To ${esc(o.ship_to_code || 'unresolved')}, requested ${esc(o.requested_delivery_date || 'asap')}. Order ${esc(v.order_id)}.</p>
<p style="background:#fff8e1;padding:10px;border-left:4px solid #f0a000">${esc($('WriteDraftExplanation').first().json.text)}</p>
<p><a href="${base}decision=approve" style="background:#1a7f37;color:#fff;padding:10px 18px;text-decoration:none;border-radius:4px">Approve and create the sales order</a>
&nbsp;&nbsp;<a href="${base}decision=reject" style="background:#b42318;color:#fff;padding:10px 18px;text-decoration:none;border-radius:4px">Reject</a></p>
<h3>Lines</h3><table border="1" cellpadding="4" cellspacing="0"><tr><th>#</th><th>As written</th><th>Code</th><th>Catalogue</th><th>Qty</th><th>Status</th></tr>${lines}</table>
<h3>Checks</h3><table border="1" cellpadding="4" cellspacing="0"><tr><th>#</th><th>Rule</th><th>Result</th><th>Detail</th></tr>${rules}</table>
<p>Returns: ${esc(o.empty_bottle_returns.map(r => r.quantity + ' x ' + r.product_text).join('; ') || 'none')}. ${esc(v.cutoff.note)}</p>
<p style="color:#666">Execution ${$execution.id}. In production this is a pre-filled Draft Order record in Salesforce; the links resume the waiting workflow.</p>
</body></html>`;
})() }}"""


def build_main():
    RA, RB, RC, RD, RE, RF, RG = 0, 592, 1184, 1920, 2512, 3200, 3952
    sub_product_inputs = sub_input_schema([("order_id", "string"), ("lines", "array")])
    sub_shipping_inputs = sub_input_schema([("order_id", "string"), ("sender_email", "string"), ("customer_name", "string"),
                                            ("sold_to_hint", "string"), ("ship_to", "object"), ("requested_delivery_date", "string")])

    def switch_rule(value, key):
        return {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
                               "conditions": [{"id": nid("route-" + key), "leftValue": "={{ $json.attachment_kind }}", "rightValue": value,
                                               "operator": {"type": "string", "operation": "equals"}}],
                               "combinator": "and"},
                "renameOutput": True, "outputKey": key}

    nodes = [
        # ---- row 1: notify, pull, parse
        node("ReceivePubSubNotification", "n8n-nodes-base.webhook", 2.1, (0, RA),
             {"httpMethod": "POST", "path": "al-pg-order-notify", "responseMode": "onReceived", "options": {}},
             webhookId=nid("webhook-al-pg-order-notify"),
             notes="Google Pub/Sub push subscription stand-in. Acknowledged immediately; the email is pulled afterwards (notify-then-pull)."),
        code("DecodeNotification", (208, RA), DECODE_NOTIFICATION),
        pg("PullEmailFromMailbox", (416, RA),
           "select email_id, from_addr, subject, body_text, received_at, attachments from al_poc.mailbox where email_id = $1;",
           "={{ [$json.email_id] }}",
           notes="Gmail stand-in. Customer instance: Gmail API users.messages.get plus attachments.get with the service account."),
        pg("OpenOrderRecord", (624, RA),
           "insert into al_poc.order_intake (order_id, email_id, execution_id, status)\nvalues ($1, $2, $3, 'received')\n"
           "on conflict (order_id) do update set execution_id = excluded.execution_id, status = 'received', started_at = now(),\n"
           "  finished_at = null, decision = null, sap_order_number = null, draft_order_id = null\nreturning order_id, email_id;",
           "={{ ['ORD-' + $json.email_id, $json.email_id, $execution.id] }}"),
        code("PrepareDocument", (0, RB), PREPARE_DOCUMENT),
        node("RouteByAttachmentType", "n8n-nodes-base.switch", 3.4, (208, RB),
             {"mode": "rules", "rules": {"values": [switch_rule("pdf", "pdf"), switch_rule("xlsx", "xlsx"), switch_rule("csv", "csv")]},
              "options": {"fallbackOutput": "extra", "renameFallbackOutput": "none"}}),
        node("ExtractPdfText", "n8n-nodes-base.extractFromFile", 1.1, (416, RB),
             {"operation": "pdf", "binaryPropertyName": "attachment", "options": {"joinPages": True}},
             onError="continueErrorOutput"),
        node("ExtractXlsxRows", "n8n-nodes-base.extractFromFile", 1.1, (416, RB + 208),
             {"operation": "xlsx", "binaryPropertyName": "attachment", "options": {"headerRow": False}},
             onError="continueErrorOutput"),
        node("ExtractCsvRows", "n8n-nodes-base.extractFromFile", 1.1, (416, RB + 416),
             {"operation": "csv", "binaryPropertyName": "attachment", "options": {"headerRow": False}},
             onError="continueErrorOutput"),
        code("TabularToText", (624, RB + 208), TABULAR_TO_TEXT),
        code("AssembleDocument", (832, RB), ASSEMBLE_DOCUMENT),

        # ---- row 2: extract and classify
        node("ExtractOrderWithAi", "@n8n/n8n-nodes-langchain.informationExtractor", 1.2, (0, RC),
             {"text": "={{ $json.document_text }}", "schemaType": "manual",
              "inputSchema": json.dumps(EXTRACTION_SCHEMA, indent=2),
              "options": {"systemPromptTemplate": EXTRACTION_SYSTEM}},
             retryOnFail=True, maxTries=2, waitBetweenTries=2000, onError="continueErrorOutput"),
        llm("ExtractorModel", (0, RC + 272)),
        step_log("LogExtractionStep", (240, RC), "extraction", "Extractor",
                 "'Extracted intent=' + (($json.output || {}).intent || 'none') + ' lines=' + ((($json.output || {}).lines || []).length) + ' confidence=' + (($json.output || {}).confidence ?? 'n/a')",
                 "JSON.stringify($json.output || {})", order_expr="$('AssembleDocument').first().json.order_id"),
        pg("SaveExtraction", (448, RC),
           "update al_poc.order_intake set intent = $2, extracted = $3::jsonb, confidence = $4, status = 'extracted'\nwhere order_id = $1\nreturning order_id, intent;",
           "={{ (ex => [$('AssembleDocument').first().json.order_id, ex.intent || 'other', JSON.stringify(ex), Number(ex.confidence) || 0])($('ExtractOrderWithAi').first().json.output || {}) }}"),
        if_node("IsOrderIntent", (656, RC), "={{ $json.intent }}", "string", "equals", "order"),
        pg("MarkNotAnOrder", (864, RC + 272),
           "update al_poc.order_intake set status = 'not_an_order', decision = 'skip', finished_at = now()\nwhere order_id = $1\nreturning order_id, status, intent;",
           "={{ [$json.order_id] }}",
           notes="Inquiries, complaints and pure returns stop here. Production: hand to the order desk queue as a non-order case."),
        node("RunProductAgent", "n8n-nodes-base.executeWorkflow", 1.3, (864, RC),
             {"workflowId": {"__rl": True, "mode": "list", "value": "", "__workflowName": WF_PRODUCT},
              "workflowInputs": {"mappingMode": "defineBelow",
                                 "value": {"order_id": "={{ $json.order_id }}",
                                           "lines": "={{ ($('ExtractOrderWithAi').first().json.output || {}).lines || [] }}"},
                                 "matchingColumns": [], "schema": sub_product_inputs,
                                 "attemptToConvertTypes": False, "convertFieldsToString": False},
              "mode": "once", "options": {"waitForSubWorkflow": True}},
             onError="continueErrorOutput"),
        node("RunShippingAgent", "n8n-nodes-base.executeWorkflow", 1.3, (1072, RC),
             {"workflowId": {"__rl": True, "mode": "list", "value": "", "__workflowName": WF_SHIPPING},
              "workflowInputs": {"mappingMode": "defineBelow",
                                 "value": {"order_id": "={{ $('SaveExtraction').first().json.order_id }}",
                                           "sender_email": "={{ $('AssembleDocument').first().json.from }}",
                                           "customer_name": "={{ ($('ExtractOrderWithAi').first().json.output || {}).customer_name || '' }}",
                                           "sold_to_hint": "={{ ($('ExtractOrderWithAi').first().json.output || {}).sold_to_hint || '' }}",
                                           "ship_to": "={{ ($('ExtractOrderWithAi').first().json.output || {}).ship_to || {} }}",
                                           "requested_delivery_date": "={{ ($('ExtractOrderWithAi').first().json.output || {}).requested_delivery_date || '' }}"},
                                 "matchingColumns": [], "schema": sub_shipping_inputs,
                                 "attemptToConvertTypes": False, "convertFieldsToString": False},
              "mode": "once", "options": {"waitForSubWorkflow": True}},
             onError="continueErrorOutput"),

        # ---- row 3: deterministic gate, HITL
        code("ApplyBusinessRules", (0, RD), APPLY_BUSINESS_RULES),
        pg("SaveValidation", (208, RD),
           "update al_poc.order_intake set product_validation = $2::jsonb, shipping_validation = $3::jsonb, rules = $4::jsonb,\n"
           "  critical_match = $5, decision = $6, status = 'validated'\nwhere order_id = $1\nreturning order_id, decision;",
           "={{ [$json.order_id, JSON.stringify($json.product_validation), JSON.stringify($json.shipping_validation), JSON.stringify($json.rules), $json.critical_match, $json.decision] }}"),
        step_log("LogDecisionStep", (416, RD), "decision", "Gate",
                 "'Decision ' + $json.decision + ' (critical_match=' + $('ApplyBusinessRules').first().json.critical_match + ')'",
                 "JSON.stringify({ rules: $('ApplyBusinessRules').first().json.rules, reasons: $('ApplyBusinessRules').first().json.reasons })"),
        if_node("IsAutoDecision", (0, RE), "={{ $('ApplyBusinessRules').first().json.decision }}", "string", "equals", "auto"),
        node("WriteDraftExplanation", "@n8n/n8n-nodes-langchain.chainLlm", 1.9, (208, RE),
             {"promptType": "define", "text": DRAFT_EXPLANATION_PROMPT.replace("$json", "$('ApplyBusinessRules').first().json"), "batching": {}},
             retryOnFail=True, maxTries=2, waitBetweenTries=2000, onError="continueRegularOutput",
             notes="Generative explainability: the AI writes the note on the Draft Order. The fixed reason wording comes from the gate, not the model."),
        llm("ExplanationModel", (208, RE + 272), temperature=0.3, max_tokens=800),
        pg("CreateDraftOrder", (448, RE),
           "insert into al_poc.draft_orders (order_id, sold_to_code, ship_to_code, lines, reasons, ai_explanation)\n"
           "values ($1, $2, $3, $4::jsonb, string_to_array($5, '||'), $6)\nreturning id, order_id, status;",
           "={{ (v => [v.order_id, v.order.sold_to_code, v.order.ship_to_code, JSON.stringify(v.order.lines), v.reasons.join('||'), String($json.text || '')])($('ApplyBusinessRules').first().json) }}",
           notes="Salesforce stand-in for the pre-filled Draft Order in the manual queue."),
        pg("LinkDraftToOrder", (656, RE),
           "update al_poc.order_intake set draft_order_id = $2, status = 'pending_review' where order_id = $1 returning order_id, draft_order_id;",
           "={{ [$json.order_id, $json.id] }}"),
        node("NotifyOrderDesk", "n8n-nodes-base.emailSend", 2.1, (864, RE),
             {"fromEmail": MAIL_FROM, "toEmail": MAIL_ORDER_DESK,
              "subject": "=[Draft Order] {{ $('ApplyBusinessRules').first().json.order.sold_to_name || $('AssembleDocument').first().json.from }}: {{ $('ApplyBusinessRules').first().json.reasons[0] }}",
              "emailFormat": "html", "html": ORDER_DESK_EMAIL_HTML, "options": {"appendAttribution": False}},
             credentials=CRED_SMTP, retryOnFail=True, maxTries=3, waitBetweenTries=3000),
        node("WaitForOrderDesk", "n8n-nodes-base.wait", 1.1, (1072, RE),
             {"resume": "webhook", "httpMethod": "GET", "options": {}},
             webhookId=nid("wait-order-desk"),
             notes="The execution sleeps here for hours or days at no worker cost. Production: resumed by the Salesforce Draft Order status change."),
        if_node("OrderDeskApproved", (1280, RE), "={{ $json.query.decision }}", "string", "equals", "approve"),
        pg("MarkRejected", (1280, RE + 272),
           "with d as (update al_poc.draft_orders set status = 'rejected', resolved_at = now() where order_id = $1)\n"
           "update al_poc.order_intake set status = 'rejected', finished_at = now() where order_id = $1 returning order_id, status;",
           "={{ [$('ApplyBusinessRules').first().json.order_id] }}"),

        # ---- row 4: post to SAP through the ESB with exponential backoff
        code("BuildSalesOrderPayload", (0, RF), BUILD_SALES_ORDER_PAYLOAD),
        node("PostSalesOrderToEsb", "n8n-nodes-base.httpRequest", 4.2, (208, RF),
             {"method": "POST", "url": ESB_URL, "sendBody": True, "specifyBody": "json",
              "jsonBody": "={{ JSON.stringify($('BuildSalesOrderPayload').first().json.payload) }}",
              "options": {"timeout": 15000}},
             onError="continueErrorOutput",
             notes="TIBCO ESB stand-in. Customer instance: the ESB sales-order endpoint with the SAP service account. "
                   "No node-level retry on purpose: the loop below is the exponential backoff."),
        pg("RecordSalesOrder", (416, RF),
           "with d as (update al_poc.draft_orders set status = 'approved', resolved_at = now() where order_id = $1 and status = 'open')\n"
           "update al_poc.order_intake set sap_order_number = $2, status = 'posted', finished_at = now()\nwhere order_id = $1\n"
           "returning order_id, sap_order_number, status, extract(epoch from (finished_at - started_at))::int as seconds;",
           "={{ [$('BuildSalesOrderPayload').first().json.order_id, $json.sap_order_number] }}"),
        step_log("LogPostedStep", (624, RF), "posted", "ESB",
                 "'SAP sales order ' + $json.sap_order_number + ' created in ' + $json.seconds + ' s'",
                 "JSON.stringify({ sap_order_number: $json.sap_order_number, seconds: $json.seconds, attempts: 1 + ($('ComputeBackoff').isExecuted ? $('ComputeBackoff').all().length : 0) })"),
        code("ComputeBackoff", (416, RF + 272), COMPUTE_BACKOFF),
        step_log("LogRetryStep", (624, RF + 272), "esb_retry", "ESB",
                 "'ESB call failed (HTTP ' + $json.http_status + '), attempt ' + $json.attempt + ' of ' + $json.max_attempts + ($json.retry ? ', retry in ' + $json.delay_seconds + ' s' : ', escalating')",
                 "JSON.stringify({ attempt: $json.attempt, delay_seconds: $json.delay_seconds, http_status: $json.http_status, error: $json.error_message })"),
        if_node("HasRetriesLeft", (832, RF + 272), "={{ $('ComputeBackoff').last().json.retry }}", "boolean", "true", single=True),
        node("WaitBackoff", "n8n-nodes-base.wait", 1.1, (1040, RF + 272),
             {"resume": "timeInterval", "amount": "={{ $('ComputeBackoff').last().json.delay_seconds }}", "unit": "seconds"},
             webhookId=nid("wait-backoff")),
        pg("EscalateEsbFailure", (1040, RF + 544),
           "insert into al_poc.draft_orders (order_id, sold_to_code, ship_to_code, lines, reasons, ai_explanation)\n"
           "values ($1, $2, $3, $4::jsonb, array['ESB unavailable after ' || $5 || ' attempts'], $6)\nreturning id;",
           "={{ (v => [v.order_id, v.order.sold_to_code, v.order.ship_to_code, JSON.stringify(v.order.lines), String($('ComputeBackoff').last().json.max_attempts), "
           "'The order was validated but the ESB did not accept it after exponential backoff retries. Last error: ' + $('ComputeBackoff').last().json.error_message])($('ApplyBusinessRules').first().json) }}"),
        pg("MarkFailed", (1248, RF + 544),
           "update al_poc.order_intake set status = 'failed', draft_order_id = $2, finished_at = now() where order_id = $1 returning order_id, status;",
           "={{ [$('ApplyBusinessRules').first().json.order_id, $json.id] }}"),

        # ---- row 5: failures
        node("NotifyOpsFailure", "n8n-nodes-base.emailSend", 2.1, (0, RG),
             {"fromEmail": MAIL_FROM, "toEmail": MAIL_OPS,
              "subject": "=[PG Order Intake] failure in execution {{ $execution.id }}",
              "emailFormat": "text",
              "text": "=A step failed and the order was not posted.\n\nError: {{ $json.error?.message || $json.error || JSON.stringify($json).slice(0, 800) }}\n"
                      "Order: {{ $('DecodeNotification').first().json.order_id }}\nExecution: {{ $execution.id }}\n",
              "options": {"appendAttribution": False}},
             credentials=CRED_SMTP, retryOnFail=True, maxTries=3, waitBetweenTries=3000),
    ]
    w = Wiring()
    w("ReceivePubSubNotification", "DecodeNotification")
    w("DecodeNotification", "PullEmailFromMailbox")
    w("PullEmailFromMailbox", "OpenOrderRecord", 0)
    w("PullEmailFromMailbox", "NotifyOpsFailure", 1)
    w("OpenOrderRecord", "PrepareDocument", 0)
    w("OpenOrderRecord", "NotifyOpsFailure", 1)
    w("PrepareDocument", "RouteByAttachmentType")
    w("RouteByAttachmentType", "ExtractPdfText", 0)
    w("RouteByAttachmentType", "ExtractXlsxRows", 1)
    w("RouteByAttachmentType", "ExtractCsvRows", 2)
    w("RouteByAttachmentType", "AssembleDocument", 3)
    w("ExtractPdfText", "AssembleDocument", 0)
    w("ExtractPdfText", "AssembleDocument", 1)
    w("ExtractXlsxRows", "TabularToText", 0)
    w("ExtractXlsxRows", "AssembleDocument", 1)
    w("ExtractCsvRows", "TabularToText", 0)
    w("ExtractCsvRows", "AssembleDocument", 1)
    w("TabularToText", "AssembleDocument")
    w("AssembleDocument", "ExtractOrderWithAi")
    w("ExtractorModel", "ExtractOrderWithAi", 0, "ai_languageModel")
    w("ExtractOrderWithAi", "LogExtractionStep", 0)
    w("ExtractOrderWithAi", "NotifyOpsFailure", 1)
    w("LogExtractionStep", "SaveExtraction", 0)
    w("LogExtractionStep", "SaveExtraction", 1)
    w("SaveExtraction", "IsOrderIntent", 0)
    w("SaveExtraction", "NotifyOpsFailure", 1)
    w("IsOrderIntent", "RunProductAgent", 0)
    w("IsOrderIntent", "MarkNotAnOrder", 1)
    w("RunProductAgent", "RunShippingAgent", 0)
    w("RunProductAgent", "RunShippingAgent", 1)
    w("RunShippingAgent", "ApplyBusinessRules", 0)
    w("RunShippingAgent", "ApplyBusinessRules", 1)
    w("ApplyBusinessRules", "SaveValidation")
    w("SaveValidation", "LogDecisionStep", 0)
    w("SaveValidation", "NotifyOpsFailure", 1)
    w("LogDecisionStep", "IsAutoDecision", 0)
    w("LogDecisionStep", "IsAutoDecision", 1)
    w("IsAutoDecision", "BuildSalesOrderPayload", 0)
    w("IsAutoDecision", "WriteDraftExplanation", 1)
    w("ExplanationModel", "WriteDraftExplanation", 0, "ai_languageModel")
    w("WriteDraftExplanation", "CreateDraftOrder")
    w("CreateDraftOrder", "LinkDraftToOrder", 0)
    w("CreateDraftOrder", "NotifyOpsFailure", 1)
    w("LinkDraftToOrder", "NotifyOrderDesk", 0)
    w("LinkDraftToOrder", "NotifyOpsFailure", 1)
    w("NotifyOrderDesk", "WaitForOrderDesk")
    w("WaitForOrderDesk", "OrderDeskApproved")
    w("OrderDeskApproved", "BuildSalesOrderPayload", 0)
    w("OrderDeskApproved", "MarkRejected", 1)
    w("BuildSalesOrderPayload", "PostSalesOrderToEsb")
    w("PostSalesOrderToEsb", "RecordSalesOrder", 0)
    w("PostSalesOrderToEsb", "ComputeBackoff", 1)
    w("RecordSalesOrder", "LogPostedStep", 0)
    w("RecordSalesOrder", "NotifyOpsFailure", 1)
    w("ComputeBackoff", "LogRetryStep")
    w("LogRetryStep", "HasRetriesLeft", 0)
    w("LogRetryStep", "HasRetriesLeft", 1)
    w("HasRetriesLeft", "WaitBackoff", 0)
    w("HasRetriesLeft", "EscalateEsbFailure", 1)
    w("WaitBackoff", "PostSalesOrderToEsb")
    w("EscalateEsbFailure", "MarkFailed", 0)
    w("EscalateEsbFailure", "NotifyOpsFailure", 1)
    w("MarkFailed", "NotifyOpsFailure", 0)
    w("MarkFailed", "NotifyOpsFailure", 1)
    return workflow(WF_MAIN, nodes, w.c, ["al-poc", "pg-order-intake"])


# =============================================================================== metrics dashboard
RENDER_DASHBOARD = r"""
const m = $('ReadMetrics').first().json || {};
const rows = $input.all().map(i => i.json);
const esc = s => String(s ?? '').replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));
const tile = (label, value, unit = '') => `<div class="tile"><div class="v">${esc(value ?? '-')}<span class="u">${unit}</span></div><div class="l">${label}</div></div>`;
const tr = rows.map(r => `<tr><td>${esc(r.order_id)}</td><td>${esc(r.intent)}</td><td class="${esc(r.decision)}">${esc(r.decision || '-')}</td><td>${esc(r.status)}</td><td>${esc(r.confidence)}</td><td>${esc(r.seconds ?? '-')}</td><td>${esc(r.sap_order_number || '')}</td><td>${esc(r.steps)}</td><td>${esc(String(r.started_at).slice(0, 19).replace('T', ' '))}</td></tr>`).join('');
const html = `<!doctype html><html><head><meta charset="utf-8"><title>PG Order Intake, PoC metrics</title>
<meta http-equiv="refresh" content="15">
<style>body{font-family:Arial,sans-serif;margin:24px;color:#1d1d1f;background:#fafafa}h1{font-size:20px}.tiles{display:flex;gap:16px;flex-wrap:wrap;margin:16px 0 24px}
.tile{background:#fff;border:1px solid #e5e5e5;border-radius:8px;padding:14px 18px;min-width:150px}.v{font-size:28px;font-weight:bold}.u{font-size:14px;color:#777;margin-left:4px}.l{font-size:12px;color:#666;margin-top:4px}
table{border-collapse:collapse;background:#fff;font-size:13px}th,td{border:1px solid #e5e5e5;padding:6px 10px;text-align:left}th{background:#f3f3f3}
td.auto{color:#1a7f37;font-weight:bold}td.draft{color:#b45309;font-weight:bold}td.skip{color:#777}.note{color:#666;font-size:12px}</style></head><body>
<h1>PG Order Intake, PoC metrics</h1>
<div class="tiles">${tile('orders received', m.orders_total)}${tile('automation rate', m.automation_rate_pct, '%')}${tile('auto posted', m.auto_posted)}${tile('sent to order desk', m.sent_to_review)}${tile('failed', m.failed)}${tile('avg end-to-end (auto)', m.avg_auto_seconds, 's')}${tile('avg AI confidence', m.avg_confidence)}</div>
<table><tr><th>Order</th><th>Intent</th><th>Decision</th><th>Status</th><th>Confidence</th><th>Seconds</th><th>SAP order</th><th>Logged steps</th><th>Started</th></tr>${tr || '<tr><td colspan=9>no orders yet</td></tr>'}</table>
<p class="note">Refreshes every 15 s from al_poc.order_intake and step_log. Targets from the RFP: under 5 minutes per order, over 80% automation on simple orders, critical fields at 1.0, reasoning confidence at or above 0.7.</p>
</body></html>`;
return [{ json: { html } }];
"""


def build_metrics():
    R = 0
    nodes = [
        node("ReceiveDashboardRequest", "n8n-nodes-base.webhook", 2.1, (0, R),
             {"httpMethod": "GET", "path": "al-pg-order-metrics", "responseMode": "responseNode", "options": {}},
             webhookId=nid("webhook-al-pg-order-metrics")),
        pg("ReadMetrics", (208, R), "select * from al_poc.metrics;", "", alwaysOutputData=True),
        pg("ReadRecentOrders", (416, R),
           "select o.order_id, o.intent, o.decision, o.status, o.confidence, o.sap_order_number, o.started_at,\n"
           "  extract(epoch from (o.finished_at - o.started_at))::int as seconds,\n"
           "  (select count(*) from al_poc.step_log s where s.order_id = o.order_id) as steps\n"
           "from al_poc.order_intake o order by o.started_at desc limit 25;", "", alwaysOutputData=True),
        code("RenderDashboard", (624, R), RENDER_DASHBOARD),
        node("RespondHtml", "n8n-nodes-base.respondToWebhook", 1.5, (832, R),
             {"respondWith": "text", "responseBody": "={{ $json.html }}",
              "options": {"responseHeaders": {"entries": [{"name": "Content-Type", "value": "text/html; charset=utf-8"}]}}}),
    ]
    w = Wiring()
    w("ReceiveDashboardRequest", "ReadMetrics")
    w("ReadMetrics", "ReadRecentOrders", 0)
    w("ReadMetrics", "ReadRecentOrders", 1)
    w("ReadRecentOrders", "RenderDashboard", 0)
    w("ReadRecentOrders", "RenderDashboard", 1)
    w("RenderDashboard", "RespondHtml")
    return workflow(WF_METRICS, nodes, w.c, ["al-poc", "pg-order-intake", "monitoring"])


if __name__ == "__main__":
    write(build_mock_esb(), "al-mock-tibco-esb.json")
    write(build_product_agent(), "al-product-agent.json")
    write(build_shipping_agent(), "al-shipping-agent.json")
    write(build_main(), "al-pg-order-intake.json")
    write(build_metrics(), "al-metrics-dashboard.json")

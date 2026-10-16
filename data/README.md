# Lab data

`schema.sql` and `seed.sql` build the `al_poc` database on pg-lab (stand-ins for Gmail,
Salesforce, OCB, SAP and the trace tables). `fixtures/` are synthetic emails: invented
customers and `.example` domains, nothing from Air Liquide. Regenerate the XLSX and PDF
with `python3 scripts/make-fixture-files.py`.

| Fixture | Exercises | Expected |
|---|---|---|
| f1-durand-clean-order | French body, two lines, address in the body | auto, SAP order |
| f2-kessler-xlsx-order | German body, XLSX attachment, past the 09:00 Berlin cut-off | auto, SAP order, cut-off note |
| f3-stmartin-pdf-elephant-unknown | PDF attachment, qty 100 (Elephant), product not in catalogue, 40 empties returned | Draft Order with three reasons; Approve link posts |
| f4-durand-inquiry | price request, not an order | not_an_order |
| f5-durand-esb-retry (`--chaos 2`) | ESB answers 503 twice | backoff 4 s and 8 s, then SAP order |

Air Liquide's anonymised dataset arrives during the PoC and goes under `fixtures/al/`,
gitignored like the rest of `data/`.

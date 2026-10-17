# PoC prerequisites for Air Liquide (draft answer to deck section 2)

Status: draft 2026-10-16, to be reviewed by Morgan Lupton before it goes to Romain Fassi.
Based on the n8n self-hosting documentation and the PoC architecture deck (one EC2,
full on-premise, AWS private cloud). Confirm each line against the live docs before sending.

## Component and flows

| Component | Purpose | Flow |
|---|---|---|
| n8n (single container, Docker or Podman) | Editor, API, executions | Inbound HTTPS 443 from the Citrix network and from Google Pub/Sub push (or none if we poll Gmail) |
| PostgreSQL 14+ (container on the same host for the PoC) | Executions, credentials, waiting workflows | Local only |
| Outbound HTTPS | Gmail API, Pub/Sub, Gemini API, Salesforce API, TIBCO ESB endpoint, npm registry at install time only | Allow-list per destination; no other egress |
| SMTP (optional) | Order-desk and ops notifications if not done inside Salesforce | Outbound 587 to the AL relay |

n8n initiates every connection except the editor and the Pub/Sub push. If an inbound
route to the EC2 is not possible, we poll Gmail with the Gmail trigger (every minute) and the
notify-then-pull pattern is kept inside the workflow (notification item, then fetch).

## Runner server sizing (PoC)

| Resource | Recommendation |
|---|---|
| vCPU | 4 |
| RAM | 8 GB (16 GB if PostgreSQL shares the host and we keep full execution data) |
| Disk | 100 GB SSD (execution data with attachments grows; prune at 14 days) |
| Network | 1 Gbps internal; latency to Salesforce and the ESB under 100 ms |

Production sizing is in the proposal (queue mode, workers scaled horizontally).

## Operating system

Linux (Ubuntu 22.04 or 24.04 LTS, or Amazon Linux 2023) with Docker Engine or Podman.
Windows Server 2022 is possible only through Docker Desktop or WSL2 and is not recommended
for a server; if Windows is mandatory, we need WSL2 enabled and Docker Engine inside it.

## Software to approve before install

The Administrator Charter forbids software outside Air Liquide's approved standards
without the manager's sign-off. One approval request covering:

| Package | Version | Why |
|---|---|---|
| Docker Engine or Podman | current stable | runs the containers |
| n8n image `n8nio/n8n` | the release Morgan installs (lab: 2.35.5) | the platform |
| PostgreSQL image | 16 | n8n's database and the application tables |
| Reverse proxy (Caddy or nginx) with a certificate from Air Liquide's CA | current stable | TLS for the editor and the Pub/Sub push |
| Nothing else | | Code nodes run in n8n's task runner; no extra packages on the host |

## Account provisioning (Citrix access to the EC2)

Three accounts, technical role: Michael Frostbutter (FDE, build), Morgan Lupton (SE),
Madison Seeley (n8n, per the 2026-10-16 sync). Plus one non-technical viewer for Manuel
Craciun if demos are run from the instance. Names and emails to confirm with Manuel.

## Access level

- sudo (or Docker group membership) on the EC2 for the install and upgrades.
- Read-write to the n8n data directory and the PostgreSQL volume.
- n8n owner account for the instance; AL evaluators get member or viewer accounts on a
  shared project so they can open the platform without editing it.
- Service accounts and API users (Gmail, Gemini, Salesforce, SAP via TIBCO) are created by
  AL and handed over as credentials entered in the n8n credential store, never in files.

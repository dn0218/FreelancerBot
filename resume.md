# Danny Hii Lu Hui — Profile

## Contact

- Freelancer username: dn0218
- Location: Kuala Lumpur, Malaysia (UTC+8)
- Working languages: English (fluent), Mandarin (native), Bahasa Melayu (intermediate), Cantonese (basic), Spanish (basic)

## Skills

python, web scraping, playwright, selenium, beautifulsoup, requests, llm integration, rag, openai api, glm, ollama, telegram bot, pdf extraction, data analysis, pandas, numpy, fastapi, flask, api integration, paramiko, linux, rhel, oracle linux, selinux, systemd, ansible, podman, k3s, docker, cloudflare, cloudflare tunnel, cloudflare access, zero trust, dns automation, dns-over-https, aws ec2, route53, boto3, oracle database, oracle data pump, mysql, postgresql, sqlalchemy, bash, powershell, pyinstaller, git, wireshark, asterisk, freepbx, voip, sip, networking, ccna, cybersecurity, automation

## Selected experience

<!--
Each entry is cited by the proposal drafter. Keep the "Built" line concrete
and the "Outcome" line evidence-based. No unverifiable claims.
-->

### 1. Dunning Simulation Tool v4 — Enabling Technology Malaysia (Jul 2025 – present)
- Context: enterprise telco billing support for YTL Communications corporate partner
- Built: Python + Oracle + SSH + tkinter tool, packaged as Windows .exe via PyInstaller
- Function: resolves account rule groups from Oracle, restarts remote jobservers via SSH, triggers DebitAction jobs over HTTP, verifies execution and restores state
- Outcome: replaced manual SQL workflows used by the production support team

### 2. Seven-Site Autonomous Delivery Pipeline — personal (github.com/dn0218)
- Built: 4-module pipeline — Google Places API lead scraping with Playwright fallback, GLM-4/5 bilingual website generation, AWS EC2 deployment, Cloudflare DNS + SSL automation
- Self-healing: idempotent DNS sync with dynamic-IP drift repair, atomic tmp-mv SFTP uploads, probe diagnostics with retry
- Outcome: 7 restaurant websites generated and deployed live on custom subdomains, 7/7 delivered

### 3. DNS Automation SDK — personal (github.com/dn0218)
- Built: standalone Python SDK wrapping Cloudflare API v4 and AWS Route53 (boto3)
- Features: idempotent A/CNAME upsert and delete with explicit action semantics, exponential backoff honouring Retry-After, 3-channel verification (DoH + authoritative API + HTTP probe)
- Ships: CLI + Python SDK + offline mock tests with zero credentials required

### 4. Multi-Tenant AI Diet Coach — personal, public production
- Built: LLM agent published via Cloudflare Tunnel + Access (SSO), Flask read-only proxy holding zero credentials, BYOK key passthrough
- Data chain: 4-tier food database (tenant-private, base, extended 171 foods, USDA FDC API) with caching and rate-limit handling
- Features: RAG knowledge injection, session compression (20+ msgs -> summary + last 10), SSE streaming
- Verified: e2e test suites 8/8, 4/4, 6/6

### 5. MT5 AI Trading Bot — personal
- Built: Python 3.12 + MetaTrader 5 SDK, Smart Money Concepts structure detection, LLM veto layer with schema-enforced ALLOW or REFUSE
- Risk controls: FTMO-style drawdown circuit breakers, fractional-Kelly position sizing, staged take-profit
- Operations: Telegram alerts, JSONL audit log, DRY_RUN=true default, fully mocked pytest suite

### 6. InvestClaw — personal
- Built: cron-scheduled Python data ingestion with source and timestamp recorded on every value
- Pipeline: four-dimension panic detector + megacap capex tracker + weighted scoring with renormalization on missing data
- Output: five-persona LLM committee with one veto; Telegram signal cards; signal-only, no order API

### 7. Homelab Services — personal
- Running: rootless Podman + Quadlet services, Zabbix monitoring, Ansible playbooks, LVM and XFS storage on RHEL-family systems
- Served 24/7 via Cloudflare Tunnel + Access

## Work history

- Linux Administrator, Wevo System Sdn Bhd: RHEL troubleshooting, Asterisk VoIP diagnosis, Zabbix monitoring, Traefik reverse proxy with OIDC, Entra ID app configuration
- Intern, Yokogawa Electric (Malaysia): DCS testing, Cisco Catalyst 9600 configuration, architecture diagrams
- Power and Cooling System Installer, H&P Electrical Service (Perak project): Cat6 UTP and fibre-optic cabling

## Education

- BSc (Hons) Computer Science — Cybersecurity, Asia Pacific University, 2022-2025, CGPA 3.37
- Foundation in Information Technology, Swinburne University Sarawak, 2020-2021, CGPA 3.63

## Certifications

CCNA Switching Routing and Wireless Essentials (Cisco), Fortinet NSE 1-3, Ethical Hacking Essentials (EC-Council), SQL Injection Attacks (EC-Council), Cybersecurity Fundamentals (IBM SkillsBuild), NIOSH-Tenaga Safety Passport. In progress: RHCSA, Google Cloud Computing Foundations.

## Rates and preferences

- Typical budget range: USD 150-500 per project
- Hourly vs fixed: fixed price preferred for well-scoped work; hourly for ongoing retainers
- Availability: evenings and weekends, 15-20 hours per week
- Will NOT do: adult content, gambling, captcha bypass, guaranteed-return trading systems, high-volume cold email

## Approval and compliance notes

- Timeline commitments allowed: only "X days" for well-scoped fixed work; default to "Timeline: to be confirmed after scope review"
- Every proposal must cite a real portfolio item from Selected experience or the GitHub cache
- Never inflate metrics, client names, or delivery claims
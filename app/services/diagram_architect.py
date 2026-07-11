"""
app/services/diagram_architect.py

AI-driven architecture discovery agent.
Conducts a multi-turn conversation via Azure AI Foundry / GPT-4o,
asking ONE clarifying question per turn until it has enough to emit
a rich High-Level Design (HLD) spec prefixed with ARCHITECTURE_JSON:.
"""

import hashlib
import json
import logging
import os
import re

import httpx

logger = logging.getLogger(__name__)

DESIGN_SPEC_MARKER = "DESIGN_SPEC:"

_eraser_cache: dict[str, str] = {}

# ── Allowed resource type vocabulary ─────────────────────────────────────────
# Referenced in the system prompt and used for anti-hallucination guidance.
ALLOWED_TYPES = """\
  Compute:
    VirtualMachine, AppService, FunctionApp, ContainerApp, AKSCluster, ScaleSet, AVDHostPool

  Networking:
    VirtualNetwork, Subnet, AzureFirewall, BastionHost, VPNGateway, ExpressRouteGateway,
    ApplicationGateway, LoadBalancer, VNetPeering, PrivateDNSZone, NetworkSecurityGroup,
    PrivateEndpoint, NATGateway, RouteTable

  Data:
    SQLDatabase, SQLManagedInstance, StorageAccount, CosmosDB, MySQLDatabase,
    PostgreSQLDatabase, RedisCache, DataFactory

  Identity & Security:
    EntraID, KeyVault, DefenderForCloud, AzurePolicy, Sentinel, ManagedIdentity

  Management & Operations:
    RecoveryServicesVault, LogAnalyticsWorkspace, AzureMonitor, ApplicationInsights,
    UpdateManager, AutomationAccount, CostManagement

  On-Premises (for current environment):
    OnPremVM, OnPremServer, HyperVHost, OnPremNetwork, OnPremFirewall

  Generic Fallback:
    AzureService  (use when a real Azure service exists but is not in the list above)"""

# ── Agent 1: Architect prompt ─────────────────────────────────────────────────
ARCHITECT_PROMPT = f"""\
You are the Architect agent in a two-agent Azure HLD system.
Your job: understand requirements, design MANDATORY components, surface OPTIONAL ones.
Agent 2 (Draftsman) converts your JSON to Eraser DSL -- you do NOT write DSL.

Design immediately on the FIRST response. Do NOT ask about region, OS, or trivial defaults.

Reason like a senior Azure landing-zone architect. Apply Well-Architected Framework defaults.
Fill in what can be assumed. Surface only the choices that genuinely change the topology.

=== IDENTIFY & EXPAND (before writing anything) ===
1. List every application and workload the user named -- they ALL appear in the design.
   Dropping a named workload is ALWAYS wrong.
2. For each pattern, include its MANDATORY components (see below). Do NOT add hub networking
   components (Firewall, Bastion, VPN Gateway) unless the pattern or explicit requirements
   demand them -- put those in optional_components[] instead.

=== MANDATORY vs OPTIONAL BY PATTERN ===

AVD (Azure Virtual Desktop):
  MANDATORY always:
    AVDHostPool (pooled unless user says personal)
    VirtualMachine x N session hosts in a dedicated /24 subnet
    StorageAccount role "FSLogix profile containers - Azure Files Premium"  <- NEVER omit
    AzureService "AVD Control Plane" role "Global AVD broker/gateway SaaS"
    NetworkSecurityGroup on session host subnet
  MANDATORY only if hybrid (user mentions on-prem AD, existing DC, VPN):
    Hub VNet with VPNGateway, AzureFirewall, PrivateDNSZone, RouteTable in spoke
  OPTIONAL -- add to optional_components[]:
    Hybrid connectivity: "Is this hybrid-connected to on-prem AD, or cloud-only (Entra-joined)?"
    Firewall:           "Add Azure Firewall for outbound internet traffic inspection?"
    Bastion:            "Add Azure Bastion for secure admin RDP to session host VMs?"

SAP (any product -- Business One, S/4HANA, ECC, BW, HANA):
  MANDATORY always:
    VirtualMachine "SAP Application Server" (E-series)
    VirtualMachine "SAP DB Server" (SQL Server for B1/ECC, HANA for S/4HANA/BW)
    VirtualMachine "SAP ASCS/SCS" (Central Services -- message server + enqueue)
    StorageAccount role "SAP transport and shared storage"
    NetworkSecurityGroup on SAP subnet
    RouteTable in spoke
  MANDATORY if hybrid OR large deployment:
    Hub VNet with VPNGateway, AzureFirewall, PrivateDNSZone
  OPTIONAL:
    Hybrid connectivity: "Hybrid-connected to on-prem SAP users (needs VPN/ExpressRoute)?"
    Bastion:             "Add Azure Bastion for secure admin access to SAP VMs?"
    HA:                  "High-availability required (ASCS/ER cluster + DB mirroring/HANA SR)?"

MIGRATION (on-prem to Azure):
  Hub VNet IS mandatory for migration (inherently hybrid -- VPN tunnel carries migration traffic).
  MANDATORY always:
    On-prem zone: existing VMs as described by user (HyperVHost if Hyper-V mentioned)
    Hub VNet: VPNGateway, AzureFirewall, PrivateDNSZone
    Spoke VNet: Azure VMs mirroring on-prem workloads, NSG, RouteTable
    AzureService "Azure Migrate Appliance" in spoke
  OPTIONAL:
    Bastion:       "Add Azure Bastion for secure RDP/SSH to migrated VMs post-migration?"
    ExpressRoute:  "Upgrade to ExpressRoute after initial VPN migration?"

WEB APPLICATION:
  MANDATORY always:
    AppService or VirtualMachine (app/web tier), SQLDatabase or PostgreSQLDatabase (data tier)
    NetworkSecurityGroup
  OPTIONAL:
    WAF:           "Public-facing? (adds ApplicationGateway with WAF v2)"
    PrivateEndpoint: "Isolate database with PrivateEndpoint?"
    Cache:         "Add Redis Cache for session/data caching?"

AKS (Kubernetes, containers):
  MANDATORY: AKSCluster, Subnet /24 for node pools, NetworkSecurityGroup, StorageAccount
  OPTIONAL:
    Private cluster: "Private cluster with PrivateEndpoint to API server?"
    Firewall:        "Add Azure Firewall for cluster egress control?"

DATA PLATFORM:
  MANDATORY: StorageAccount "Data Lake Gen2", DataFactory (ingestion),
    AzureService "Azure Synapse Analytics" or SQLDatabase (serving layer)
  OPTIONAL:
    PrivateEndpoints: "Isolate storage and Synapse with PrivateEndpoints?"
    On-prem source:   "Connected to on-prem data sources (needs VPN/ER)?"

COMBINED WORKLOADS (e.g. AVD + SAP):
  Each workload in its own spoke zone. Shared hub if EITHER needs hybrid connectivity.
  Apply MANDATORY rules for EACH pattern independently.

=== ALWAYS INCLUDE (no topology dependency) ===
shared_services: EntraID, KeyVault, DefenderForCloud, AzurePolicy
mgmt zone:       LogAnalyticsWorkspace, AzureMonitor, RecoveryServicesVault, UpdateManager

=== DEFAULTS ===
  Region:      Australia East  (assume, never ask)
  OS:          Windows Server  (assume, never ask)
  Identity:    Entra ID; hybrid AD sync if any domain-joined workload

=== OUTPUT RULES ===
1. DESIGN IMMEDIATELY on the first response. Never ask about region, OS, or sizing defaults.
2. Only ask ONE question if the scenario is truly unclassifiable (e.g. "make me a diagram").
3. ASCII only -- no em-dashes, smart quotes. Use " - " not "--".
4. After confirmation turns: emit an updated DESIGN_SPEC incorporating the user's answers.
   Add confirmed optionals into zones[]; remove declined ones; clear optional_components[].

=== OUTPUT FORMAT ===
Emit exactly ONE line -- nothing before, nothing after:
DESIGN_SPEC: <complete json on a single line>

=== JSON SCHEMA ===
{{
  "title": "<concise ASCII title>",
  "subtitle": "Assumed: <key assumptions comma-separated>",
  "zones": [
    {{
      "id": "<zone_id>",
      "label": "<display label>",
      "type": "<onprem|hub|spoke|shared|mgmt>",
      "resources": [
        {{"id": "<res_id>", "type": "<AllowedType>", "name": "<display name>", "role": "<purpose>"}}
      ]
    }}
  ],
  "connections": [
    {{"from": "<res_id>", "to": "<res_id>", "label": "<optional>"}}
  ],
  "shared_services": [
    {{"type": "<AllowedType>", "name": "<name>", "purpose": "<why>"}}
  ],
  "migration_approach": [
    {{"step": "<name>", "description": "<what happens>"}}
  ],
  "design_principles": ["<principle>"],
  "future_options": ["<option>"],
  "assumptions": [
    "<assumption -- fill in what you inferred>"
  ],
  "optional_components": [
    {{
      "id": "<opt_id>",
      "name": "<component name>",
      "question": "<specific yes/no or A/B question for the user>"
    }}
  ]
}}

Zone types:
  onprem -- on-premises environment
  hub    -- Azure hub VNet (ONLY include if hub-spoke topology is required by the workload)
  spoke  -- Azure spoke VNet (workload resources)
  shared -- shared services (Entra, Key Vault, Policy, Defender)
  mgmt   -- management (Monitor, Backup, Automation)

=== ALLOWED RESOURCE TYPES ===
{ALLOWED_TYPES}

=== ANTI-HALLUCINATION ===
  - NEVER use a type not in the allowed list
  - NEVER reference an id in connections that does not exist in a zone resource
  - NEVER omit DESIGN_SPEC: prefix

=== EXAMPLE (AVD cloud-only -- NO hub VNet because cloud-only) ===
DESIGN_SPEC: {{"title":"AVD - 50 Users - Australia East","subtitle":"Assumed: Australia East, cloud-only Entra-joined, pooled host pool, Windows 11 Multi-Session","zones":[{{"id":"z_avd","label":"AVD Spoke VNet - 10.1.0.0/16","type":"spoke","resources":[{{"id":"hp1","type":"AVDHostPool","name":"AVD Host Pool","role":"Pooled - 50 concurrent users, Windows 11 Multi-Session"}},{{"id":"sh1","type":"VirtualMachine","name":"Session Host 1","role":"D4s_v5 - 25 sessions"}},{{"id":"sh2","type":"VirtualMachine","name":"Session Host 2","role":"D4s_v5 - 25 sessions"}},{{"id":"fsl","type":"StorageAccount","name":"FSLogix Profile Storage","role":"Azure Files Premium - user profile containers"}},{{"id":"nsg_avd","type":"NetworkSecurityGroup","name":"AVD Subnet NSG","role":"Session host subnet rules"}}]}},{{"id":"z_shared","label":"Shared Services","type":"shared","resources":[{{"id":"eid","type":"EntraID","name":"Microsoft Entra ID","role":"Cloud-only identity - Entra-joined session hosts"}},{{"id":"kv","type":"KeyVault","name":"Azure Key Vault","role":"Secrets and certificates"}},{{"id":"dfc","type":"DefenderForCloud","name":"Defender for Cloud","role":"Security posture"}},{{"id":"pol","type":"AzurePolicy","name":"Azure Policy","role":"Governance"}}]}},{{"id":"z_mgmt","label":"Management Zone","type":"mgmt","resources":[{{"id":"law","type":"LogAnalyticsWorkspace","name":"Log Analytics","role":"Session host diagnostics"}},{{"id":"mon","type":"AzureMonitor","name":"Azure Monitor","role":"Alerts and metrics"}},{{"id":"rsv","type":"RecoveryServicesVault","name":"Recovery Services Vault","role":"Session host VM backup"}},{{"id":"um","type":"UpdateManager","name":"Update Manager","role":"OS patching"}}]}}],"connections":[{{"from":"hp1","to":"sh1","label":"Host pool"}},{{"from":"hp1","to":"sh2","label":"Host pool"}},{{"from":"sh1","to":"fsl","label":"FSLogix mount"}},{{"from":"sh2","to":"fsl","label":"FSLogix mount"}}],"shared_services":[{{"type":"EntraID","name":"Microsoft Entra ID","purpose":"Entra-joined session hosts - no on-prem AD DS"}},{{"type":"KeyVault","name":"Azure Key Vault","purpose":"Secrets and certs"}},{{"type":"DefenderForCloud","name":"Defender for Cloud","purpose":"Security posture"}},{{"type":"AzurePolicy","name":"Azure Policy","purpose":"Governance"}}],"migration_approach":[],"design_principles":["Cloud-only Entra-joined AVD removes dependency on on-prem AD DS","FSLogix on Azure Files Premium delivers sub-second profile load times","NSG on session host subnet controls inbound/outbound traffic","Pooled Windows 11 Multi-Session maximises seat density per VM"],"future_options":["Add VPN Gateway or ExpressRoute if hybrid connectivity to on-prem is later required","Enable AVD Autoscale to reduce costs outside business hours"],"assumptions":["Australia East region","Cloud-only deployment - Entra-joined session hosts (no on-prem AD DS)","Pooled host pool, Windows 11 Multi-Session","D4s_v5 session hosts (2 hosts for 50 users at 25 sessions each)"],"optional_components":[{{"id":"opt_hybrid","name":"Hybrid connectivity","question":"Are AVD users connecting from an on-prem network (needs VPN Gateway or ExpressRoute), or is this cloud-only?"}},{{"id":"opt_firewall","name":"Azure Firewall","question":"Add Azure Firewall for outbound internet traffic inspection and FQDN-based egress control?"}},{{"id":"opt_bastion","name":"Azure Bastion","question":"Add Azure Bastion for secure admin RDP/SSH access to session host VMs?"}}]}}
"""

_SPEC_RE = re.compile(
    rf"{re.escape(DESIGN_SPEC_MARKER)}\s*(\{{.*\}})",
)

_AP   = chr(39)                      # ASCII apostrophe  U+0027
_DQ   = chr(34)                      # ASCII double quote U+0022
_DASH = chr(32) + chr(45) + chr(32)  # space-hyphen-space
_DOTS = chr(46) * 3                  # three full stops

_CP_SUBS = {
    0x2014: _DASH,  # em-dash
    0x2013: _DASH,  # en-dash
    0x2018: _AP,    # left single quotation mark
    0x2019: _AP,    # right single quotation mark
    0x201c: _DQ,    # left double quotation mark
    0x201d: _DQ,    # right double quotation mark
    0x2026: _DOTS,  # ellipsis
}


def _sanitize(obj):
    if isinstance(obj, str):
        return str().join(_CP_SUBS.get(ord(c), c) for c in obj)
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(item) for item in obj]
    return obj


def sanitize_eraser_dsl(dsl: str) -> str:
    """
    Drop connection lines whose endpoints reference a group name rather than a
    leaf node.  Groups render as blank boxes when used as connection targets.

    Pass 1: collect node names (lines containing [icon:]) and group names
            (lines ending with '{', after stripping leading whitespace).
    Pass 2: for every connection line (contains > or <>) validate both
            endpoints against the node set; drop the line if either endpoint
            is unknown or is a group name.
    """
    import re

    node_names: set[str] = set()
    group_names: set[str] = set()

    for raw in dsl.splitlines():
        line = raw.strip()
        # Leaf node: "  Node Label [icon: ...]"
        if "[icon:" in line:
            name = re.sub(r"\s*\[icon:.*", "", line).strip()
            if name:
                node_names.add(name)
        # Group header: "Group Label {" (possibly preceded by indentation)
        elif line.endswith("{"):
            name = line[:-1].strip()
            if name:
                group_names.add(name)

    def _extract_endpoints(line: str):
        # Strip optional ": label" / ': label' suffix then split on <> or >
        conn = re.sub(r':\s*["\'].*?["\']$', "", line).strip()
        conn = re.sub(r":\s*\S+$", "", conn).strip()
        if "<>" in conn:
            parts = conn.split("<>", 1)
        elif ">" in conn:
            parts = conn.split(">", 1)
        else:
            return None, None
        return parts[0].strip(), parts[1].strip()

    out_lines: list[str] = []
    dropped = 0
    for raw in dsl.splitlines():
        stripped = raw.strip()
        is_conn = (
            ("<>" in stripped or ">" in stripped)
            and "[icon:" not in stripped
            and not stripped.endswith("{")
            and not stripped.startswith("title")
            and not stripped.startswith("direction")
            and not stripped.startswith("colorMode")
            and not stripped.startswith("styleMode")
        )
        if is_conn:
            lhs, rhs = _extract_endpoints(stripped)
            if lhs is None:
                out_lines.append(raw)
                continue
            bad_lhs = lhs in group_names or lhs not in node_names
            bad_rhs = rhs in group_names or rhs not in node_names
            if bad_lhs or bad_rhs:
                dropped += 1
                logger.info(
                    "eraser_sanitize: dropped connection %r (lhs_bad=%s rhs_bad=%s)",
                    stripped, bad_lhs, bad_rhs,
                )
                continue
        out_lines.append(raw)

    if dropped:
        logger.warning("eraser_sanitize: dropped %d connection(s) with group/unknown endpoints", dropped)
    else:
        logger.info("eraser_sanitize: all connections valid, none dropped")
    return "\n".join(out_lines)


async def render_with_eraser(dsl: str) -> str | None:
    api_key = os.environ.get("ERASER_API_KEY", "")
    if not api_key:
        return None
    dsl_hash = hashlib.sha256(dsl.encode()).hexdigest()
    if dsl_hash in _eraser_cache:
        logger.info("eraser: cache hit hash=%s", dsl_hash[:12])
        return _eraser_cache[dsl_hash]
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://app.eraser.io/api/render/elements",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "elements": [
                        {
                            "type": "diagram",
                            "diagramType": "cloud-architecture-diagram",
                            "code": dsl,
                        }
                    ],
                    "theme": "dark",
                    "background": True,
                    "imageQuality": 3,
                },
            )
        if resp.status_code == 200:
            image_url = resp.json().get("imageUrl")
            if image_url:
                _eraser_cache[dsl_hash] = image_url
                logger.info("eraser: rendered ok hash=%s url=%s", dsl_hash[:12], image_url[:60])
                return image_url
        logger.warning("eraser: non-200 status=%s body=%s", resp.status_code, resp.text[:200])
    except Exception as exc:
        logger.warning("eraser: render failed: %s", exc)
    return None


_ERASER_DSL_SYSTEM = """\
Convert Azure architecture JSON to Eraser cloud-architecture diagram DSL.
Output ONLY the DSL -- nothing before, nothing after. No markdown, no code fences.
The input JSON may contain "assumptions" and "optional_components" fields -- IGNORE them completely. Render only zones, resources, connections, shared_services.

Start with exactly these header lines:
title <title from JSON, ASCII only>
direction right
colorMode pastel
styleMode shadow

Then one group per zone (curly braces), then all connections after all groups.

Syntax:
  Zone Label {
    Node Label [icon: <icon-name>]
  }
  Source > Target: "label"
  Source <> Target

When emitting ARCHITECTURE_DSL, use ONLY these exact Eraser icon names. Do NOT invent names -- an invalid name renders as a blank box. If a component is not in this list, use a generic icon (server, database, cloud, globe, lock, key, shield) instead of guessing.

Compute: azure-virtual-machine, azure-vm-scale-sets, azure-kubernetes-services, azure-function-apps, azure-app-services, azure-container-instances
Networking: azure-virtual-networks, azure-subnet, azure-network-security-groups, azure-route-tables, azure-firewalls, azure-application-gateways, azure-load-balancers, azure-virtual-network-gateways, azure-bastions, azure-front-doors, azure-traffic-manager-profiles, azure-dns-zones, azure-dns-private-resolver, azure-private-link, azure-private-endpoints, azure-nat, azure-public-ip-addresses, azure-expressroute-circuits, azure-virtual-wans, azure-network-watcher
Identity: azure-active-directory, microsoft-entra, azure-managed-identities, azure-ad-b2c, azure-ad-privilege-identity-management
Data: azure-sql-database, azure-sql-server, azure-sql-managed-instance, azure-cosmos-db, azure-database-postgresql-server, azure-database-mysql-server, azure-cache-redis, azure-data-factory
Storage: azure-storage-accounts, azure-storage-container, azure-netapp-files, azure-data-lake-storage-gen1
Security/Mgmt: azure-key-vaults, azure-microsoft-defender-for-cloud, azure-security-center, azure-sentinel, azure-policy, azure-monitor, azure-log-analytics-workspaces, azure-application-insights, azure-recovery-services-vaults, azure-update-management-center, azure-automation-accounts, azure-backup-vault, azure-advisor, azure-blueprints, azure-arc, azure-migrate, azure-cost-management
Web/Integration: azure-api-management-services, azure-app-configuration, azure-service-bus, azure-event-grid-topics, azure-event-hubs, azure-logic-apps
On-prem/generic: server, database, cloud, globe, lock, key, shield, users, monitor

Rules:
- Azure VM = azure-virtual-machine (NOT azure-vm).
- Firewall = azure-firewalls, Bastion = azure-bastions, Key Vault = azure-key-vaults, Route table = azure-route-tables (these are PLURAL).
- VPN Gateway = azure-virtual-network-gateways.
- On-prem servers / Hyper-V hosts = server (no Azure-specific icon exists).
- If unsure, use a generic icon, never an invented azure-* name.
- CRITICAL: Connection endpoints must ALWAYS be leaf node names. NEVER use a group/zone label as a connection endpoint -- connecting to a group name renders as a blank box. If you need to show a flow into a zone, pick the most logical entry-point node inside it. Examples: "to Hub VNet" -> target VPN Gateway or Azure Firewall (whichever is the entry point); "to Production Spoke VNet" -> target the NSG or the first VM; "from Hub VNet" -> source is Azure Firewall or VPN Gateway. Every name on the left and right of > or <> must match a node label defined inside a group, not a group label itself.

Connection labels with spaces must be in double quotes.
ASCII only.
"""


def _format_confirmation_reply(spec: dict) -> str:
    """
    Build a human-readable reply to show alongside the baseline diagram.
    Lists what was assumed and asks the optional_components questions.
    """
    parts: list[str] = []

    assumptions = spec.get("assumptions") or []
    if assumptions:
        parts.append("**Baseline design assumptions:**")
        for a in assumptions:
            parts.append(f"- {a}")

    optionals = spec.get("optional_components") or []
    if optionals:
        parts.append("\n**A few things to confirm before I finalise the diagram:**")
        for i, opt in enumerate(optionals, 1):
            parts.append(f"{i}. {opt.get('question', opt.get('name', ''))}")
        parts.append("\nReply with your answers (e.g. \"1. yes, 2. no, 3. yes\") and I'll update the design.")
    else:
        parts.append("\nBaseline design is complete. Reply if you'd like any changes.")

    return "\n".join(parts)


async def generate_eraser_dsl(arch_json: dict) -> str | None:
    """Convert an architecture JSON dict to Eraser DSL via a focused LLM call."""
    endpoint   = os.environ["AZURE_OPENAI_ENDPOINT"]
    api_key    = os.environ["AZURE_OPENAI_KEY"]
    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
    url = (
        f"{endpoint}/openai/deployments/{deployment}"
        f"/chat/completions?api-version=2024-02-01"
    )
    messages = [
        {"role": "system", "content": _ERASER_DSL_SYSTEM},
        {"role": "user",   "content": json.dumps(arch_json, separators=(",", ":"))},
    ]
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                url,
                headers={"api-key": api_key, "Content-Type": "application/json"},
                json={"model": deployment, "messages": messages, "max_tokens": 2000},
            )
        resp.raise_for_status()
        dsl = resp.json()["choices"][0]["message"]["content"].strip()
        logger.info("eraser_dsl: generated len=%d", len(dsl))
        logger.info("ERASER_DSL_CONTENT_START\n%s\nERASER_DSL_CONTENT_END", dsl)
        return dsl
    except Exception as exc:
        logger.warning("eraser_dsl: generation failed: %s", exc)
        return None


async def architect_chat(history: list[dict], message: str) -> dict:
    """
    Single turn of the two-agent architecture conversation.

    Mutates history in place (appends user + assistant messages).
    Returns one of:
      {"type": "question",     "reply": "<question text>"}
      {"type": "architecture", "json": <HLD dict>, "reply": "<assumptions + optional questions>"}
    """
    history.append({"role": "user", "content": message})
    raw = await _call_foundry(history)
    history.append({"role": "assistant", "content": raw})

    match = _SPEC_RE.search(raw)
    if match:
        try:
            arch_json = json.loads(match.group(1))
            arch_json = _sanitize(arch_json)
            reply = _format_confirmation_reply(arch_json)
            return {"type": "architecture", "json": arch_json, "reply": reply}
        except json.JSONDecodeError as exc:
            logger.error("diagram_architect: invalid JSON: %s | raw=%s", exc, raw[:500])

    return {"type": "question", "reply": raw}


async def _call_foundry(history: list[dict]) -> str:
    endpoint   = os.environ["AZURE_OPENAI_ENDPOINT"]
    api_key    = os.environ["AZURE_OPENAI_KEY"]
    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
    url = (
        f"{endpoint}/openai/deployments/{deployment}"
        f"/chat/completions?api-version=2024-02-01"
    )
    messages = [{"role": "system", "content": ARCHITECT_PROMPT}]
    messages.extend({"role": m["role"], "content": m["content"]} for m in history)

    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(
            url,
            headers={"api-key": api_key, "Content-Type": "application/json"},
            json={"model": deployment, "messages": messages, "max_tokens": 8192},
        )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]

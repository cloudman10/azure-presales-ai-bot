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

ARCH_MARKER = "ARCHITECTURE_JSON:"

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

# ── System prompt ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = f"""\
You are an expert Azure solutions architect running a structured discovery conversation.
Your goal: produce a rich, architecturally sound High-Level Design (HLD) specification
as a single-line JSON on the FIRST response. Design immediately using the information
given and sensible defaults -- do not interview the user.

Reason like a senior Azure architect. Recommend hub-spoke landing zones, Zero Trust,
and Well-Architected Framework principles. Fill in best-practice defaults rather than
asking about every individual service.

=== SCENARIO DETECTION ===
Detect the scenario type from the user's first message and IMMEDIATELY design the architecture:

  MIGRATION (on-prem to Azure)
    Infer: VM roles from description, default region Australia East, default VPN Gateway connectivity.
    Assume Windows Server unless stated. Apply hub-spoke landing zone pattern automatically.

  WEB APPLICATION
    Infer: tier count from description (assume 3-tier if not stated), default region Australia East,
    public-facing unless stated private, default SQL Database as data tier.

  AZURE VIRTUAL DESKTOP (AVD)
    Infer: pooled host pool unless stated personal, Entra hybrid identity, default region Australia East.

  LANDING ZONE / GREENFIELD
    Infer: single subscription unless stated multi, no compliance requirements unless stated,
    default hub-spoke with VPN Gateway.

  GENERIC
    Apply hub-spoke pattern, default region Australia East.

=== DEFAULTS (apply silently unless the user says otherwise) ===
  Azure region:    Australia East
  Connectivity:    VPN Gateway (note ExpressRoute as a future option in design_principles)
  OS:              Windows Server
  Identity:        Hybrid with Entra Connect AD sync
  Landing zone:    Hub-spoke, standard Well-Architected pattern
  Compliance:      None assumed unless stated

=== CONVERSATION RULES ===
1. DESIGN IMMEDIATELY. Emit ARCHITECTURE_JSON on the FIRST response using what the user gave
   plus defaults. Do NOT ask a question first.
2. State assumptions in the subtitle field of the JSON (e.g. "Assumed: Australia East, VPN Gateway").
3. Use ONLY ASCII characters -- no em-dashes, smart quotes, or non-ASCII.
   Write " - " (hyphen with spaces) not "--" or em-dash.
4. NEVER ask about: Azure region, connectivity method, OS, or any detail you can infer or default.
5. Only ask ONE clarifying question if the user's message is so vague you cannot determine even the
   basic scenario type (e.g. "make me a diagram" with zero context about workloads).
6. If the user says "generate now" or "don't ask questions" -- always emit JSON immediately.

=== WHEN TO EMIT ===
Emit ARCHITECTURE_JSON on the FIRST response whenever the scenario type and at least one
workload or service is inferable. Apply defaults for everything else.
Do NOT wait for region, connectivity, compliance, or OS details -- assume and proceed.

=== OUTPUT FORMAT ===
Emit EXACTLY this block on the first response -- nothing before, nothing after:
ARCHITECTURE_JSON: <complete json on a single line>

=== JSON SCHEMA ===
{{
  "title": "<concise title, ASCII only, hyphen not em-dash>",
  "subtitle": "<one-line scenario summary>",
  "zones": [
    {{
      "id": "<zone_id>",
      "label": "<display label>",
      "type": "<onprem|hub|spoke|shared|mgmt>",
      "resources": [
        {{"id": "<res_id>", "type": "<AllowedType>", "name": "<display name>", "role": "<optional role/purpose>"}}
      ]
    }}
  ],
  "connections": [
    {{"from": "<res_id or zone_id>", "to": "<res_id or zone_id>", "label": "<optional label>"}}
  ],
  "shared_services": [
    {{"type": "<AllowedType>", "name": "<display name>", "purpose": "<why included>"}}
  ],
  "migration_approach": [
    {{"step": "<step name>", "description": "<what happens in this step>"}}
  ],
  "design_principles": ["<principle 1>", "<principle 2>"],
  "future_options": ["<modernization path 1>", "<modernization path 2>"]
}}

Zone type definitions:
  onprem  -- current on-premises environment (servers, network, Hyper-V hosts)
  hub     -- Azure hub VNet (firewall, bastion, VPN/ER gateways, DNS)
  spoke   -- Azure spoke VNet (workload VMs, app tiers, databases)
  shared  -- Shared services (Entra ID, Key Vault, Policy, Defender -- not network-bound)
  mgmt    -- Management zone (Monitor, Backup, Update Manager, Automation)

=== ALLOWED RESOURCE TYPES ===
Use ONLY the types listed below. Never invent type names.
If a real Azure service is not listed, use AzureService with a descriptive name and role.

{ALLOWED_TYPES}

=== ARCHITECTURE BEST PRACTICES (apply these as defaults) ===
Hub VNet always contains:
  - AzureFirewall (centralised N/S and E/W inspection)
  - BastionHost (secure RDP/SSH -- no public IPs on workload VMs)
  - VPNGateway or ExpressRouteGateway (on-prem connectivity when applicable)
  - PrivateDNSZone (private name resolution)

Spoke VNets always contain:
  - NetworkSecurityGroup on each subnet
  - RouteTable (UDR to force traffic via hub firewall)

Shared services zone always includes:
  - EntraID (identity, hybrid with on-prem AD when applicable)
  - KeyVault (secrets, certificates, disk encryption keys)
  - DefenderForCloud (security posture management)
  - AzurePolicy (governance: tagging, allowed regions, SKU enforcement)

Management zone always includes:
  - RecoveryServicesVault (backup and site recovery)
  - LogAnalyticsWorkspace (central log aggregation)
  - AzureMonitor (metrics, alerts, dashboards)
  - UpdateManager (centralised OS patching)

=== ANTI-HALLUCINATION RULES ===
  - NEVER use a type not in the allowed list
  - NEVER reference an id in connections that does not exist in zones[].id or zones[].resources[].id
  - NEVER describe Azure features that do not exist
  - NEVER omit the ARCHITECTURE_JSON: prefix -- the line must start with it exactly

=== EXAMPLE OUTPUT (migration scenario) ===
ARCHITECTURE_JSON: {{"title":"ERP Migration - Australia East","subtitle":"Lift-and-Shift of 5 Hyper-V VMs to Azure Hub-Spoke Landing Zone","zones":[{{"id":"z_onprem","label":"On-Premises","type":"onprem","resources":[{{"id":"hv1","type":"HyperVHost","name":"Hyper-V Cluster","role":"Hosts 5 VMs"}},{{"id":"op_sql","type":"OnPremVM","name":"SQL Server VM","role":"ERP database"}},{{"id":"op_app","type":"OnPremVM","name":"App Server","role":"ERP application tier"}},{{"id":"op_web","type":"OnPremVM","name":"Web Server","role":"IIS frontend"}},{{"id":"op_dc","type":"OnPremVM","name":"Domain Controller","role":"Active Directory"}},{{"id":"op_file","type":"OnPremVM","name":"File Server","role":"SMB file shares"}}]}},{{"id":"z_hub","label":"Hub VNet - Australia East","type":"hub","resources":[{{"id":"vpngw","type":"VPNGateway","name":"VPN Gateway","role":"Site-to-site VPN to on-prem"}},{{"id":"fw","type":"AzureFirewall","name":"Azure Firewall","role":"N/S and E/W traffic inspection"}},{{"id":"bas","type":"BastionHost","name":"Azure Bastion","role":"Secure RDP/SSH - no public IPs"}},{{"id":"pdns","type":"PrivateDNSZone","name":"Private DNS Zone","role":"Private name resolution"}}]}},{{"id":"z_spoke","label":"Workload Spoke VNet","type":"spoke","resources":[{{"id":"az_sql","type":"VirtualMachine","name":"SQL Server VM","role":"Migrated ERP SQL Server"}},{{"id":"az_app","type":"VirtualMachine","name":"App Server VM","role":"Migrated ERP application tier"}},{{"id":"az_web","type":"VirtualMachine","name":"Web Server VM","role":"Migrated IIS frontend"}},{{"id":"az_dc","type":"VirtualMachine","name":"Domain Controller","role":"Migrated AD DS"}},{{"id":"az_file","type":"VirtualMachine","name":"File Server VM","role":"SMB shares - candidate for Azure Files"}},{{"id":"nsg1","type":"NetworkSecurityGroup","name":"Spoke NSG","role":"Subnet ingress/egress rules"}},{{"id":"rt1","type":"RouteTable","name":"UDR Route Table","role":"Force traffic via Azure Firewall"}}]}},{{"id":"z_shared","label":"Shared Services","type":"shared","resources":[{{"id":"eid","type":"EntraID","name":"Microsoft Entra ID","role":"Hybrid identity via Entra Connect AD sync"}},{{"id":"kv","type":"KeyVault","name":"Azure Key Vault","role":"Secrets, certs, disk encryption keys"}},{{"id":"dfc","type":"DefenderForCloud","name":"Defender for Cloud","role":"Security posture and threat protection"}},{{"id":"pol","type":"AzurePolicy","name":"Azure Policy","role":"Governance - tagging, regions, SKU controls"}}]}},{{"id":"z_mgmt","label":"Management Zone","type":"mgmt","resources":[{{"id":"rsv","type":"RecoveryServicesVault","name":"Recovery Services Vault","role":"VM backup and site recovery"}},{{"id":"law","type":"LogAnalyticsWorkspace","name":"Log Analytics Workspace","role":"Central log aggregation"}},{{"id":"mon","type":"AzureMonitor","name":"Azure Monitor","role":"Metrics, alerts, dashboards"}},{{"id":"um","type":"UpdateManager","name":"Update Manager","role":"Centralised OS patching"}}]}}],"connections":[{{"from":"z_onprem","to":"vpngw","label":"Site-to-site VPN"}},{{"from":"vpngw","to":"z_hub","label":""}},{{"from":"fw","to":"z_spoke","label":"Inspected traffic"}},{{"from":"bas","to":"az_sql","label":"Secure RDP"}},{{"from":"az_web","to":"az_app","label":"App tier call"}},{{"from":"az_app","to":"az_sql","label":"SQL connection"}},{{"from":"az_dc","to":"az_app","label":"AD authentication"}},{{"from":"law","to":"z_spoke","label":"Log collection"}}],"shared_services":[{{"type":"EntraID","name":"Microsoft Entra ID","purpose":"Hybrid identity with on-prem AD sync via Entra Connect"}},{{"type":"KeyVault","name":"Azure Key Vault","purpose":"Secrets, certificates, and disk encryption keys for all workload VMs"}},{{"type":"DefenderForCloud","name":"Defender for Cloud","purpose":"Security posture management and threat protection across all resources"}},{{"type":"AzurePolicy","name":"Azure Policy","purpose":"Governance: enforce tagging standards, allowed regions, and approved VM SKUs"}}],"migration_approach":[{{"step":"1 - Assess","description":"Deploy Azure Migrate appliance on-prem. Discover and assess all 5 VMs for Azure readiness, right-sizing, and cost estimation."}},{{"step":"2 - Prepare Landing Zone","description":"Deploy hub-spoke VNet topology, VPN Gateway, Azure Firewall, Bastion, NSGs, and route tables in Australia East."}},{{"step":"3 - Identity Sync","description":"Deploy Entra Connect to sync on-prem AD to Microsoft Entra ID. Configure hybrid identity before migrating any workloads."}},{{"step":"4 - Replicate","description":"Use Azure Migrate to continuously replicate VM disks to Azure. Start with Domain Controller and SQL Server."}},{{"step":"5 - Test Migration","description":"Perform test migration of each VM into an isolated test VNet. Validate application connectivity, SQL access, and ERP functionality."}},{{"step":"6 - Cutover","description":"Schedule a maintenance window. Perform final delta replication and cutover. Update DNS. Decommission on-prem VMs after validation period."}},{{"step":"7 - Optimise","description":"Right-size VMs based on actual Azure usage metrics. Review Reserved Instance opportunities. Enable auto-shutdown for non-production."}}],"design_principles":["Hub-spoke topology enforces network segmentation and centralises security controls in the hub","All VM access via Azure Bastion - no public IP addresses on any workload VM","Azure Firewall as the single egress point with FQDN filtering and threat intelligence","Hybrid identity maintained via Entra Connect during and after migration","Immutable backups via Recovery Services Vault with soft-delete enabled","Infrastructure-as-Code (Bicep) for all landing zone components for repeatability"],"future_options":["Modernise SQL Server to Azure SQL Managed Instance for automated patching and built-in HA/DR","Replace IIS web tier with Azure App Service or containerise with Container Apps","Migrate SMB file shares to Azure Files with AD integration","Adopt ExpressRoute for higher bandwidth and lower latency than VPN Gateway","Enable Defender for Servers Plan 2 for advanced threat protection, just-in-time VM access, and file integrity monitoring"]}}
"""

_JSON_RE = re.compile(
    rf"{re.escape(ARCH_MARKER)}\s*(\{{.*\}})",
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


async def chat(history: list[dict], message: str) -> dict:
    """
    Single turn of the architecture discovery conversation.

    Mutates history in place (appends user + assistant messages).
    Returns one of:
      {"type": "question", "reply": "<question text>"}
      {"type": "architecture", "json": <rich HLD dict>}
    """
    history.append({"role": "user", "content": message})
    raw = await _call_foundry(history)
    history.append({"role": "assistant", "content": raw})

    match = _JSON_RE.search(raw)
    if match:
        try:
            arch_json = json.loads(match.group(1))
            arch_json = _sanitize(arch_json)
            return {"type": "architecture", "json": arch_json}
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
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend({"role": m["role"], "content": m["content"]} for m in history)

    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(
            url,
            headers={"api-key": api_key, "Content-Type": "application/json"},
            json={"model": deployment, "messages": messages, "max_tokens": 8192},
        )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]

"""
app/services/diagram_architect.py

Two-agent Azure HLD system:
  Agent 1 (ARCHITECT)  — multi-turn GPT-4o discovery, emits DESIGN_SPEC JSON
  Agent 2 (DRAFTSMAN)  — converts DESIGN_SPEC JSON to Eraser cloud-architecture DSL

RAG grounding (optional, controlled by RAG_ENABLED env var):
  On the first turn of each session, retrieve relevant Azure Architecture Center
  reference excerpts from the "arch-center-spike" Azure AI Search index and inject
  them into the ARCHITECT system context before the design step.
  Populate the index by running: python scripts/ingest_arch_center.py
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

  Actors:
    EndUser  (generic actor representing the person or external system accessing the solution;
              use as the "from" endpoint of user-access-path connections)

  Generic Fallback:
    AzureService  (use when a real Azure service exists but is not in the list above)"""

# ── Agent 1: Architect prompt ─────────────────────────────────────────────────
ARCHITECT_PROMPT = f"""\
You are the Architect agent in a two-agent Azure HLD system.
Your job: understand requirements, design MANDATORY components, surface OPTIONAL ones.
Agent 2 (Draftsman) converts your JSON to Eraser DSL -- you do NOT write DSL.

CRITICAL OUTPUT RULE: Your ONLY valid output starts with the EXACT prefix "DESIGN_SPEC: "
followed by complete JSON on one line. Never use "ARCHITECTURE_JSON:", "JSON:", or any
other prefix. The downstream parser looks for "DESIGN_SPEC:" only -- any other label means
the diagram will NOT be generated and the user sees nothing.

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
identity zone (type "identity"): EntraID, KeyVault, DefenderForCloud, AzurePolicy
  - Place in a zone with type "identity" so their ids can appear as connection endpoints.
  - Do NOT list these in shared_services[] -- resources there have no id and cannot be
    referenced in connections[]. Zone placement is required for auth-flow connections.
mgmt zone (type "mgmt"):        LogAnalyticsWorkspace, AzureMonitor, RecoveryServicesVault, UpdateManager
  - Always include these. Do NOT connect them with arrows -- they appear as a context panel, not a data-flow target.

=== DEFAULTS ===
  Region:      Australia East  (assume, never ask)
  OS:          Windows Server  (assume, never ask)
  Identity:    Entra ID; hybrid AD sync if any domain-joined workload

=== CONNECTIONS (PRIMARY DATA FLOWS ONLY) ===
connections[] represents PRIMARY data flows ONLY -- NOT passive/background relationships.

NEVER add connections to or from:
  Management & Security resources: LogAnalyticsWorkspace, AzureMonitor, RecoveryServicesVault,
  UpdateManager, DefenderForCloud, KeyVault, AzurePolicy, Sentinel, CostManagement,
  NetworkSecurityGroup, ManagedIdentity, AutomationAccount, ApplicationInsights.
These appear as context panels (sidebar / footer band), never wired with arrows.

Exception: EntraID IS a valid connection target for auth flows (it is an active participant
in the session/request path, not passive monitoring).

Include ONLY the following primary flows that apply to the pattern:

  1. USER ACCESS PATH
     Add an EndUser resource in the workload spoke zone, then connect to the entry point:
       AVD:      EndUser -> AVDHostPool              label "HTTPS 443 (reverse-connect)"
       Web app:  EndUser -> AppService or AppGateway label "HTTPS 443"
       SAP:      EndUser -> SAP App Server VM        label "SAP GUI 3200"
       AKS:      EndUser -> LoadBalancer or AppGateway label "HTTPS 443"

  2. AUTH FLOW
     Connect session hosts, app VMs, or services to EntraID in the identity zone:
       session_host_id / app_vm_id -> entra_id      label "HTTPS 443 (Entra auth)"
     For hybrid on-prem AD:
       session_host_id -> on_prem_dc_id             label "LDAP 389"
       on_prem_dc_id   -> entra_id                  label "Entra Connect sync"

  3. APP-TO-DATA FLOW (always include protocol label)
       app_id -> SQLDatabase or SQLManagedInstance  label "TDS 1433"
       app_id -> PostgreSQLDatabase                 label "TLS 5432"
       app_id -> MySQLDatabase                      label "TLS 3306"
       app_id -> CosmosDB                           label "HTTPS 443"
       app_id -> RedisCache                         label "TLS 6380"
       session_host_id -> StorageAccount (FSLogix)  label "SMB 445"
       SAP App VM -> SAP DB VM (SQL Server)         label "TDS 1433"
       SAP App VM -> SAP DB VM (HANA)               label "SQL 30013"

  4. ZONE-TO-ZONE ROUTING (hybrid and hub-spoke)
       on_prem_resource_id -> vpn_gateway_id        label "IPSec VPN"
       firewall_id -> spoke_entry_resource_id       label "inspected"

LABEL RULE: Include a label ONLY when a genuine protocol/port applies.
  Omit the label for structural/membership-only connections.
  NEVER guess a port -- omit the label rather than hallucinate one.

=== OUTPUT RULES ===
1. DESIGN IMMEDIATELY on the first response. Never ask about region, OS, or sizing defaults.
2. Only ask ONE question if the scenario is truly unclassifiable (e.g. "make me a diagram").
3. ASCII only -- no em-dashes, smart quotes. Use " - " not "--".
4. After confirmation turns: emit an updated DESIGN_SPEC incorporating the user's answers.
   Add confirmed optionals into zones[]; remove declined ones; clear optional_components[].

=== OUTPUT FORMAT ===
CRITICAL: Emit exactly ONE line -- nothing before, nothing after:
DESIGN_SPEC: <complete json on a single line>
Use "DESIGN_SPEC:" (not "ARCHITECTURE_JSON:", not "JSON:", not any other prefix).
This is the ONLY marker the parser recognises.

=== JSON SCHEMA ===
{{
  "title": "<concise ASCII title>",
  "subtitle": "Assumed: <key assumptions comma-separated>",
  "zones": [
    {{
      "id": "<zone_id>",
      "label": "<display label>",
      "type": "<onprem|hub|spoke|identity|mgmt>",
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
  onprem   -- on-premises environment
  hub      -- Azure hub VNet (ONLY include if hub-spoke topology is required by the workload)
  spoke    -- Azure spoke VNet (workload resources)
  identity -- identity and security zone (EntraID, KeyVault, Defender, Policy)
              Resources in this zone CAN be connection endpoints -- use for auth flows.
  mgmt     -- management zone (Monitor, Backup, Automation)
  shared   -- DEPRECATED: prefer "identity" for identity resources. Do not use "shared".

=== ALLOWED RESOURCE TYPES ===
{ALLOWED_TYPES}

=== ANTI-HALLUCINATION ===
  - NEVER use a type not in the allowed list
  - NEVER reference an id in connections[] that does not appear in zones[].resources[].id
    (shared_services[] items have no id and CANNOT be connection endpoints;
     only ids from zones[].resources[] are valid in connections[])
  - NEVER omit DESIGN_SPEC: prefix

=== EXAMPLE (AVD cloud-only -- NO hub VNet because cloud-only) ===
Key points shown in this example:
  - EndUser in the spoke zone; connected to AVDHostPool with HTTPS 443 label (user access path)
  - identity zone (type "identity") for EntraID/KV/Dfc/Policy -- their ids appear in connections[]
  - Session hosts connect to EntraID (cross-zone, labelled auth flow) and FSLogix (labelled data flow)
  - shared_services[] is EMPTY -- identity resources live in the identity zone instead
  - mgmt zone resources (law, mon, rsv, um) have NO connections -- they are context panels only
  - connections[] has EXACTLY 5 entries: 1 user-access + 2 auth + 2 data -- no diagnostics/mgmt arrows
DESIGN_SPEC: {{"title":"AVD - 50 Users - Australia East","subtitle":"Assumed: Australia East, cloud-only Entra-joined, pooled host pool, Windows 11 Multi-Session","zones":[{{"id":"z_avd","label":"AVD Spoke VNet - 10.1.0.0/16","type":"spoke","resources":[{{"id":"eu1","type":"EndUser","name":"End Users (50)","role":"Remote desktop via HTTPS reverse-connect"}},{{"id":"hp1","type":"AVDHostPool","name":"AVD Host Pool","role":"Pooled - 50 concurrent users, Windows 11 Multi-Session"}},{{"id":"sh1","type":"VirtualMachine","name":"Session Host 1","role":"D4s_v5 - 25 sessions"}},{{"id":"sh2","type":"VirtualMachine","name":"Session Host 2","role":"D4s_v5 - 25 sessions"}},{{"id":"fsl","type":"StorageAccount","name":"FSLogix Profile Storage","role":"Azure Files Premium - user profile containers"}},{{"id":"nsg_avd","type":"NetworkSecurityGroup","name":"AVD Subnet NSG","role":"Session host subnet rules"}}]}},{{"id":"z_identity","label":"Identity & Security","type":"identity","resources":[{{"id":"eid","type":"EntraID","name":"Microsoft Entra ID","role":"Cloud-only identity - Entra-joined session hosts"}},{{"id":"kv","type":"KeyVault","name":"Azure Key Vault","role":"Secrets and certificates"}},{{"id":"dfc","type":"DefenderForCloud","name":"Defender for Cloud","role":"Security posture"}},{{"id":"pol","type":"AzurePolicy","name":"Azure Policy","role":"Governance"}}]}},{{"id":"z_mgmt","label":"Management Zone","type":"mgmt","resources":[{{"id":"law","type":"LogAnalyticsWorkspace","name":"Log Analytics","role":"Session host diagnostics"}},{{"id":"mon","type":"AzureMonitor","name":"Azure Monitor","role":"Alerts and metrics"}},{{"id":"rsv","type":"RecoveryServicesVault","name":"Recovery Services Vault","role":"Session host VM backup"}},{{"id":"um","type":"UpdateManager","name":"Update Manager","role":"OS patching"}}]}}],"connections":[{{"from":"eu1","to":"hp1","label":"HTTPS 443 (reverse-connect)"}},{{"from":"sh1","to":"eid","label":"HTTPS 443 (Entra auth)"}},{{"from":"sh2","to":"eid","label":"HTTPS 443 (Entra auth)"}},{{"from":"sh1","to":"fsl","label":"SMB 445"}},{{"from":"sh2","to":"fsl","label":"SMB 445"}}],"shared_services":[],"migration_approach":[],"design_principles":["Cloud-only Entra-joined AVD removes dependency on on-prem AD DS","FSLogix on Azure Files Premium delivers sub-second profile load times","NSG on session host subnet controls inbound/outbound traffic","Pooled Windows 11 Multi-Session maximises seat density per VM"],"future_options":["Add VPN Gateway or ExpressRoute if hybrid connectivity to on-prem is later required","Enable AVD Autoscale to reduce costs outside business hours"],"assumptions":["Australia East region","Cloud-only deployment - Entra-joined session hosts (no on-prem AD DS)","Pooled host pool, Windows 11 Multi-Session","D4s_v5 session hosts (2 hosts for 50 users at 25 sessions each)"],"optional_components":[{{"id":"opt_hybrid","name":"Hybrid connectivity","question":"Are AVD users connecting from an on-prem network (needs VPN Gateway or ExpressRoute), or is this cloud-only?"}},{{"id":"opt_firewall","name":"Azure Firewall","question":"Add Azure Firewall for outbound internet traffic inspection and FQDN-based egress control?"}},{{"id":"opt_bastion","name":"Azure Bastion","question":"Add Azure Bastion for secure admin RDP/SSH access to session host VMs?"}}]}}
"""

# Accept either marker so a single-token hallucination doesn't break the whole flow.
_SPEC_RE = re.compile(
    r"(?:DESIGN_SPEC:|ARCHITECTURE_JSON:)\s*(\{.*\})",
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
    Three-pass guard before sending DSL to Eraser:

    Pre-pass: auto-quote any group-header or node name that contains "/" (CIDR),
              "(", ")", ":", or ",".  An unquoted "/" breaks Eraser's parser and
              causes the entire group's contents to render as blank boxes.

    Pass 1: collect leaf node names ([icon:] lines) and group names (lines ending
            with '{') from the (already-quoted) DSL.

    Pass 2: walk lines tracking group-nesting depth.
            - At depth > 0: drop connection-syntax lines (they render as blank nodes).
            - At depth 0: validate connection endpoints; drop if either references a
              group name or an unknown node.
    """
    import re

    # Characters that break Eraser's parser when unquoted in a label.
    _SPECIAL = re.compile(r'[/(),:]')

    def _quote_if_needed(raw: str) -> str:
        stripped = raw.strip()
        indent = raw[: len(raw) - len(stripped)]

        if stripped.endswith("{"):
            name_part = stripped[:-1].strip()
            if not name_part.startswith('"') and _SPECIAL.search(name_part):
                return f'{indent}"{name_part}" {{'

        elif "[icon:" in stripped:
            name_part = re.sub(r"\s*\[icon:.*", "", stripped).strip()
            if not name_part.startswith('"') and _SPECIAL.search(name_part):
                icon_part = stripped[stripped.index("[icon:"):]
                return f'{indent}"{name_part}" {icon_part}'

        return raw

    # Pre-pass: rewrite lines whose names contain special chars.
    fixed_lines = [_quote_if_needed(r) for r in dsl.splitlines()]
    auto_quoted = sum(1 for a, b in zip(dsl.splitlines(), fixed_lines) if a != b)
    if auto_quoted:
        logger.info("eraser_sanitize: auto-quoted %d line(s) with special-char names", auto_quoted)
        dsl = "\n".join(fixed_lines)

    node_names: set[str] = set()
    group_names: set[str] = set()

    for raw in dsl.splitlines():
        line = raw.strip()
        if "[icon:" in line:
            name = re.sub(r"\s*\[icon:.*", "", line).strip()
            if name:
                node_names.add(name)
        elif line.endswith("{"):
            name = line[:-1].strip()
            if name:
                group_names.add(name)

    def _extract_endpoints(line: str):
        conn = re.sub(r':\s*["\'].*?["\']$', "", line).strip()
        conn = re.sub(r":\s*\S+$", "", conn).strip()
        if "<>" in conn:
            parts = conn.split("<>", 1)
        elif ">" in conn:
            parts = conn.split(">", 1)
        else:
            return None, None
        return parts[0].strip(), parts[1].strip()

    def _is_connection_like(s: str) -> bool:
        return (
            ("<>" in s or ">" in s)
            and "[icon:" not in s
            and not s.endswith("{")
            and not s.startswith("title")
            and not s.startswith("direction")
            and not s.startswith("colorMode")
            and not s.startswith("styleMode")
        )

    out_lines: list[str] = []
    dropped = 0
    depth = 0  # nesting depth inside group blocks

    for raw in dsl.splitlines():
        stripped = raw.strip()

        if stripped.endswith("{"):
            depth += 1
            out_lines.append(raw)
            continue

        if stripped == "}":
            depth = max(0, depth - 1)
            out_lines.append(raw)
            continue

        if _is_connection_like(stripped):
            if depth > 0:
                # Connection inside a group block — Eraser renders it as a blank node.
                dropped += 1
                logger.info(
                    "eraser_sanitize: dropped connection inside group (depth=%d): %r",
                    depth, stripped,
                )
                continue
            lhs, rhs = _extract_endpoints(stripped)
            if lhs is not None:
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
        logger.warning(
            "eraser_sanitize: dropped %d line(s) (inside-group or bad endpoints)", dropped
        )
    else:
        logger.info("eraser_sanitize: all connections valid, none dropped")
    return "\n".join(out_lines)


async def render_with_eraser(dsl: str) -> str | None:
    api_key = os.environ.get("ERASER_API_KEY", "")
    # TEMP DIAG
    logger.warning("eraser: ENTERED render_with_eraser key_set=%s dsl_len=%d", bool(api_key), len(dsl))
    if not api_key:
        logger.warning("eraser: ERASER_API_KEY not set -- skipping render")
        return None
    dsl_hash = hashlib.sha256(dsl.encode()).hexdigest()
    if dsl_hash in _eraser_cache:
        logger.info("eraser: cache hit hash=%s", dsl_hash[:12])
        return _eraser_cache[dsl_hash]
    try:
        logger.warning("eraser: CALLING POST https://app.eraser.io/api/render/elements auth=Bearer ***%s", api_key[-4:])
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
        logger.warning("eraser: HTTP status=%s body_100=%r", resp.status_code, resp.text[:100])
        if resp.status_code == 200:
            image_url = resp.json().get("imageUrl")
            if image_url:
                _eraser_cache[dsl_hash] = image_url
                logger.info("eraser: rendered ok hash=%s url=%s", dsl_hash[:12], image_url[:60])
                return image_url
            logger.warning("eraser: 200 but no imageUrl in response keys=%s", list(resp.json().keys()))
        else:
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

QUOTING RULE -- CRITICAL: Any group or node name that contains "/" (e.g. CIDR blocks like
10.0.0.0/16), "(", ")", ":", or "," MUST be wrapped in double quotes. An unquoted "/"
in a group header breaks Eraser's parser and causes ALL nodes inside that group to render
as blank boxes. Names without these characters do NOT need quotes.

Correct (CIDR and parentheses quoted):
  "Hub VNet - 10.0.0.0/16" {
    "App Subnet - 10.0.1.0/24" [icon: azure-subnet]
    VPN Gateway [icon: azure-virtual-network-gateways]
  }
  "On-Premises (Primary Site)" {
    DC Server [icon: server]
  }

No quotes needed for plain names:
  Shared Services {
    Azure Key Vault [icon: azure-key-vaults]
  }

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
    # Strip Architect-only fields — Draftsman must never see them or it may
    # render optional/assumption text as floating nodes or stray connections.
    draftsman_input = {
        k: v for k, v in arch_json.items()
        if k not in ("assumptions", "optional_components")
    }
    messages = [
        {"role": "system", "content": _ERASER_DSL_SYSTEM},
        {"role": "user",   "content": json.dumps(draftsman_input, separators=(",", ":"))},
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


# ── Query expansion ────────────────────────────────────────────────────────────
# Maps detected workload type → additional technical search terms.
# Appended to the user's natural-language scenario before hybrid retrieval so
# that BM25 and vector both hit workload-specific sub-pattern chunks that the
# user's words alone (e.g. "AKS for microservices") would not rank highly.

_QUERY_EXPANSION_MAP: dict[str, list[str]] = {
    "aks": [
        "system node pool", "user node pool", "Azure CNI network plugin",
        "ingress controller NGINX", "Azure Container Registry private endpoint",
        "Key Vault CSI Secrets Store driver", "Container Insights monitoring",
        "private cluster API server endpoint", "cluster autoscaler workload identity",
    ],
    "avd": [
        "reverse connect transport HTTPS 443", "FSLogix profile Azure Files Premium",
        "session host Entra ID joined", "host pool RDP Shortpath",
        "Teams media optimization AVD", "Azure Virtual Desktop gateway broker",
    ],
    "sap": [
        "ASCS ERS SAP central services", "HANA system replication",
        "application server E-series M-series VM SAP certified",
        "Azure NetApp Files transport shared storage NFS",
        "pacemaker HA cluster availability zones SIOS",
    ],
    "openai": [
        "Azure OpenAI private endpoint VNet integration",
        "managed identity model deployment token rate limit",
        "content filtering responsible AI Azure AI Foundry",
        "API Management semantic cache private DNS zone",
        "data residency Australia compliance Log Analytics diagnostics",
    ],
    "hybrid": [
        "ExpressRoute private peering BGP circuit", "VPN Gateway active-active IKEv2",
        "redundant MPLS circuit hub VNet Azure Firewall",
        "forced tunneling UDR on-premises connectivity", "private DNS zone",
    ],
    "migration": [
        "Azure Migrate appliance replication dependency mapping",
        "VPN tunnel cutover lift and shift",
        "Hyper-V VMware agentless replication",
    ],
    "web": [
        "Application Gateway WAF v2 TLS termination", "private endpoint database",
        "Redis Cache session managed identity App Service",
        "Azure Front Door CDN global load balancing",
    ],
    "data": [
        "Data Lake Gen2 Azure Data Factory pipeline",
        "Synapse Analytics dedicated pool private endpoint",
        "managed VNet integration", "Unity Catalog governance",
    ],
}

_WORKLOAD_DETECT: list[tuple[str, list[str]]] = [
    ("aks",       ["aks", "kubernetes", "k8s", "microservice", "container", "helm", "kubectl"]),
    ("avd",       ["avd", "virtual desktop", "remote desktop", "wvd", "session host", "azure virtual desktop"]),
    ("sap",       ["sap", "s/4hana", "s4hana", "hana", "ascs", "business one", "ecc", "bw/4"]),
    ("openai",    ["openai", "gpt", "llm", "azure ai", "ai landing", "language model", "generative ai", "ai hub", "foundry"]),
    ("hybrid",    ["on-prem", "on-premises", "expressroute", "mpls", "datacenter", "site-to-site"]),
    ("migration", ["migrat", "lift and shift", "azure migrate", "rehost", "replatform"]),
    ("web",       ["web app", "app service", "web application", "waf", "front door"]),
    ("data",      ["data lake", "data platform", "synapse", "data factory", "analytics warehouse"]),
]


def _expand_query(scenario: str) -> str:
    """Append workload-specific technical terms to a natural-language query."""
    q_lower = scenario.lower()
    for workload, keywords in _WORKLOAD_DETECT:
        if any(kw in q_lower for kw in keywords):
            terms = _QUERY_EXPANSION_MAP.get(workload, [])
            if terms:
                expanded = f"{scenario} {' '.join(terms[:8])}"
                logger.info("rag: query expanded for workload=%s", workload)
                return expanded
    return scenario


async def _embed_query(text: str) -> list[float] | None:
    """Embed a query string using text-embedding-3-small; returns None if unavailable."""
    dep = os.environ.get("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "")
    if not dep:
        return None
    try:
        endpoint = os.environ["AZURE_OPENAI_ENDPOINT"]
        api_key  = os.environ["AZURE_OPENAI_KEY"]
        url = f"{endpoint}/openai/deployments/{dep}/embeddings?api-version=2024-02-01"
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                url,
                headers={"api-key": api_key, "Content-Type": "application/json"},
                json={"input": text},
            )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]
    except Exception as exc:
        logger.warning("rag: embed query failed: %s", exc)
        return None


async def retrieve_arch_context(scenario: str, top_k: int = 5) -> str:
    """
    Hybrid search (BM25 keyword + vector) in the arch-center-spike Azure AI Search
    index. Returns formatted reference chunks to inject into the ARCHITECT prompt.

    Diversity: prefers one chunk per unique source URL, filling remaining slots from
    the best remaining chunks.  Falls back to keyword-only if embedding unavailable.

    Requires:
      - AZURE_SEARCH_ENDPOINT, AZURE_SEARCH_API_KEY env vars set
      - AZURE_OPENAI_EMBEDDING_DEPLOYMENT set (e.g. "text-embedding-3-small")
      - "arch-center-spike" index populated via scripts/ingest_arch_center.py
    """
    endpoint   = os.environ.get("AZURE_SEARCH_ENDPOINT", "")
    search_key = os.environ.get("AZURE_SEARCH_API_KEY", "")
    if not endpoint or not search_key:
        logger.warning("rag: AZURE_SEARCH_ENDPOINT or AZURE_SEARCH_API_KEY not set — skipping")
        return ""
    try:
        import asyncio
        from azure.search.documents import SearchClient
        from azure.core.credentials import AzureKeyCredential

        # Expand with workload-specific technical terms, then cap for search API
        query     = _expand_query(scenario[:300])[:700]
        embedding = await _embed_query(query)
        use_vector = embedding is not None

        def _sync_search() -> list[dict]:
            client = SearchClient(
                endpoint=endpoint,
                index_name="arch-center-spike",
                credential=AzureKeyCredential(search_key),
            )
            kwargs: dict = {
                "search_text": query,
                "select": ["pattern_name", "workload_type", "content", "source_url"],
                "top": top_k * 3,  # fetch extra for diversity dedup
            }
            if use_vector:
                from azure.search.documents.models import VectorizedQuery
                kwargs["vector_queries"] = [
                    VectorizedQuery(
                        vector=embedding,
                        k_nearest_neighbors=top_k,
                        fields="embedding",
                    )
                ]
            results = client.search(**kwargs)
            return [dict(r) for r in results]

        raw = await asyncio.to_thread(_sync_search)

        # Diversity: one chunk per source URL first, then fill remaining slots
        seen: set[str] = set()
        primary: list[dict]   = []
        secondary: list[dict] = []
        for r in raw:
            src = r.get("source_url", "")
            if src not in seen:
                seen.add(src)
                primary.append(r)
            else:
                secondary.append(r)
        diverse = (primary + secondary)[:top_k]

        if not diverse:
            logger.info("rag: no results for query %r", query[:80])
            return ""

        search_type = "hybrid" if use_vector else "keyword"
        logger.info(
            "rag: retrieved %d chunk(s) via %s for query %r",
            len(diverse), search_type, query[:80],
        )
        formatted = [
            f"[{r['pattern_name']} | {r['source_url']}]\n{r['content']}"
            for r in diverse
        ]
        return "\n\n---\n\n".join(formatted)

    except Exception as exc:
        logger.warning("rag: retrieval failed: %s", exc)
        return ""


async def architect_chat(history: list[dict], message: str) -> dict:
    """
    Single turn of the two-agent architecture conversation.

    Mutates history in place (appends user + assistant messages).
    Returns one of:
      {"type": "question",     "reply": "<question text>"}
      {"type": "architecture", "json": <HLD dict>, "reply": "<assumptions + optional questions>"}

    RAG grounding: if RAG_ENABLED env var is "true"/"1"/"yes", retrieves relevant
    Azure Architecture Center reference excerpts on the FIRST turn and injects them
    into the ARCHITECT system context before the design step.
    """
    history.append({"role": "user", "content": message})

    # RAG: inject reference context only on the first user turn
    rag_context = ""
    rag_enabled = os.environ.get("RAG_ENABLED", "").lower() in ("true", "1", "yes")
    if rag_enabled and len(history) == 1:
        rag_context = await retrieve_arch_context(message)
        if rag_context:
            logger.info("rag: injecting %d chars of reference context", len(rag_context))
        else:
            logger.info("rag: enabled but no context retrieved (index empty or query miss)")

    raw = await _call_foundry(history, rag_context=rag_context)
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


async def _call_foundry(history: list[dict], rag_context: str = "") -> str:
    endpoint   = os.environ["AZURE_OPENAI_ENDPOINT"]
    api_key    = os.environ["AZURE_OPENAI_KEY"]
    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
    url = (
        f"{endpoint}/openai/deployments/{deployment}"
        f"/chat/completions?api-version=2024-02-01"
    )
    messages = [{"role": "system", "content": ARCHITECT_PROMPT}]
    if rag_context:
        messages.append({
            "role": "system",
            "content": (
                "=== AZURE REFERENCE ARCHITECTURE GUIDANCE ===\n"
                "The following authoritative excerpts were retrieved from the Azure Architecture Center "
                "and Azure documentation for this specific scenario.\n\n"
                "GROUNDING RULES -- follow these exactly:\n"
                "1. Use these references to determine which components are correct, mandatory, or "
                "optional for this specific workload type.\n"
                "2. Use the correct access and connectivity model from the reference (e.g. AVD uses "
                "reverse-connect transport over HTTPS/443 -- no VPN needed for cloud-only deployments; "
                "a VPN Gateway is only required when on-premises AD DS or on-prem data sources are needed).\n"
                "3. CRITICAL -- design only for what the user asked: do NOT copy topology, zones, or "
                "components from the reference that the user did NOT request. If the reference shows an "
                "on-premises environment but the user said cloud-only, omit on-premises entirely. If the "
                "reference includes ExpressRoute but the user has no on-prem connectivity requirement, "
                "omit it. Adapt the reference pattern to the user's actual stated requirements -- "
                "never copy a reference architecture wholesale.\n\n"
                f"{rag_context}\n"
                "=== END REFERENCE GUIDANCE ==="
            ),
        })
    messages.extend({"role": m["role"], "content": m["content"]} for m in history)

    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(
            url,
            headers={"api-key": api_key, "Content-Type": "application/json"},
            json={"model": deployment, "messages": messages, "max_tokens": 8192},
        )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]

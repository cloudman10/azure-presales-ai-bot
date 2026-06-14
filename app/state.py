# Shared in-memory session store — in-process only, per-worker, cleared on restart.
# Keyed by session_id; each agent appends its own namespaced sub-keys:
#   sessions[sid]                 — conversation history  (list[dict])
#   sessions[f"{sid}_advisor_state"]  — SKU advisor state dict
#   sessions[f"{sid}_advisor_picks"]  — SKU advisor picks dict
#   sessions[f"{sid}_basket"]         — quote basket       (list[dict])
sessions: dict = {}

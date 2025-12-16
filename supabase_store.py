import streamlit as st
from supabase import create_client
import json
import hashlib
from datetime import datetime, timezone


@st.cache_resource
def get_supabase():
    """
    Cached supabase client.
    IMPORTANT: may throw if URL is invalid or network/DNS is blocked.
    """
    url = st.secrets.get("SUPABASE_URL", "").strip()
    key = st.secrets.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()  # server-side only
    if not url or not key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in secrets.")
    return create_client(url, key)


def state_hash(state: dict) -> str:
    """Deterministic hash to avoid unnecessary writes."""
    blob = json.dumps(state, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def db_load_state(plan_id: str) -> dict | None:
    """
    Load shared state from Supabase.
    Safe: returns None on any failure (DNS/firewall/misconfig), never raises.
    """
    try:
        sb = get_supabase()
        res = (
            sb.table("planner_state")
            .select("state, updated_at")
            .eq("plan_id", plan_id)
            .limit(1)
            .execute()
        )
        if not getattr(res, "data", None):
            return None
        row = res.data[0]
        out = row.get("state") or {}
        out["_updated_at"] = row.get("updated_at")
        return out
    except Exception:
        return None


def db_upsert_state(plan_id: str, state: dict) -> bool:
    """
    Upsert shared state into Supabase.
    Safe: returns False on failure, never raises.
    """
    try:
        sb = get_supabase()
        payload = {
            "plan_id": plan_id,
            "state": state,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        sb.table("planner_state").upsert(payload).execute()
        return True
    except Exception:
        return False

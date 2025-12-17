import streamlit as st
from supabase import create_client
import json
import hashlib
from datetime import datetime, timezone


@st.cache_resource
def get_supabase():
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_SERVICE_ROLE_KEY"]  # server-side only
    return create_client(url, key)


def state_hash(state: dict) -> str:
    """Deterministic hash to avoid unnecessary writes."""
    payload = json.dumps(state, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def db_load_state(plan_id: str) -> dict | None:
    sb = get_supabase()
    res = (
        sb.table("planner_state")
        .select("state, updated_at")
        .eq("plan_id", plan_id)
        .limit(1)
        .execute()
    )
    if not res.data:
        return None
    row = res.data[0]
    out = row["state"] or {}
    out["_updated_at"] = row.get("updated_at")
    return out


def db_upsert_state(plan_id: str, state: dict) -> None:
    sb = get_supabase()
    payload = {
        "plan_id": plan_id,
        "state": state,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    sb.table("planner_state").upsert(payload).execute()


def db_insert_change(plan_id: str, editor: str | None, action: str, state_h: str, details: dict | None = None) -> None:
    sb = get_supabase()
    payload = {
        "plan_id": plan_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "editor": editor or "",
        "action": action,
        "state_hash": state_h,
        "details": details or {},
    }
    sb.table("planner_changes").insert(payload).execute()


def db_latest_change(plan_id: str) -> dict | None:
    sb = get_supabase()
    res = (
        sb.table("planner_changes")
        .select("ts, editor, action")
        .eq("plan_id", plan_id)
        .order("ts", desc=True)
        .limit(1)
        .execute()
    )
    if not res.data:
        return None
    return res.data[0]


def db_list_changes(plan_id: str, limit: int = 20) -> list[dict]:
    sb = get_supabase()
    res = (
        sb.table("planner_changes")
        .select("ts, editor, action")
        .eq("plan_id", plan_id)
        .order("ts", desc=True)
        .limit(limit)
        .execute()
    )
    return res.data or []

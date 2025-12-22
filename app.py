# ============================================================
# Activity Planner — MS Project / Notion feel (AgGrid)
#
# ✅ Tasks table:
#   - Editable in edit mode (AgGrid)
#   - Drag & drop row ordering INSIDE the table (handle on Task)
#   - Pinned columns (Task + Start)
#   - Notes single-line + tooltip + horizontal scroll
#   - Compact progress bar renderer inside Progress column
#   - Vertical gridlines only + heavier/sticky header feel
#   - "Apply edits" workflow (NO DB save until Apply)
#   - Row order is SAVED (Order) and reflected in chart after Apply
#
# ✅ Streams/colors:
#   - Chart tab expander is COLLAPSED by default
#   - Streams table is editable in edit mode (AgGrid color picker + rename)
#   - "Apply stream edits" button (NO rerun/save on every click)
# ============================================================

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import date, datetime, timezone
from typing import Any

import pandas as pd
import streamlit as st

from st_aggrid import AgGrid, GridOptionsBuilder, JsCode, GridUpdateMode

from supabase_store import db_load_state, db_upsert_state, state_hash
from excel_io import make_template_excel_bytes, export_excel_bytes, load_from_excel_bytes
from gantt_chart import build_gantt_figure

# ============================================================
# SCK CEN — palette
# ============================================================
SCK_PALETTE = [
    "#5B2C83",  # deep purple
    "#7E3FA0",
    "#9B59B6",
    "#2C3E50",
    "#34495E",
    "#16A085",
    "#2980B9",
    "#F39C12",
    "#D35400",
    "#C0392B",
]
SCK_GRID = "#E6E8F0"
HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

st.set_page_config(page_title="Activity Planner", layout="wide", page_icon="📅")

# ============================================================
# Access control
# ============================================================
plan_id = st.secrets.get("PLAN_ID", "default-plan")


def _qp(name: str, default: str = "") -> str:
    v = st.query_params.get(name, default)
    if isinstance(v, (list, tuple)):
        return str(v[0]) if v else default
    return str(v) if v is not None else default


mode = _qp("mode", "view")  # view|edit
token = _qp("token", "")
is_editor = (mode == "edit") and (token == st.secrets.get("EDIT_TOKEN", ""))

# ============================================================
# Helpers
# ============================================================
def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def uid() -> str:
    return uuid.uuid4().hex[:8].upper()


def is_hex_color(x: str) -> bool:
    return bool(HEX_RE.match(str(x).strip()))


def stable_color_from_name(name: str) -> str:
    h = int(hashlib.md5(name.encode("utf-8")).hexdigest()[:8], 16)
    return SCK_PALETTE[h % len(SCK_PALETTE)]


def stream_color_map(streams_df: pd.DataFrame) -> dict[str, str]:
    if streams_df is None or streams_df.empty:
        return {}
    return {str(r["Stream"]): str(r["Color"]) for _, r in streams_df.iterrows()}


def df_hash(df: pd.DataFrame) -> str:
    try:
        return hashlib.md5(pd.util.hash_pandas_object(df.fillna(""), index=True).values).hexdigest()
    except Exception:
        return hashlib.md5(df.to_csv(index=False).encode("utf-8")).hexdigest()


def ensure_ids(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if "ID" not in out.columns:
        out["ID"] = ""
    out["ID"] = out["ID"].astype(str)
    bad = out["ID"].str.strip().eq("") | out["ID"].str.lower().eq("none")
    if bad.any():
        out.loc[bad, "ID"] = [uid() for _ in range(int(bad.sum()))]
    return out


def ensure_order(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if "Order" not in out.columns:
        out["Order"] = list(range(1, len(out) + 1))
    out["Order"] = pd.to_numeric(out["Order"], errors="coerce").fillna(0).astype(int)
    if out["Order"].nunique(dropna=False) != len(out) or (out["Order"] <= 0).any():
        out["Order"] = list(range(1, len(out) + 1))
    return out


def normalize_streams(tasks_df: pd.DataFrame, streams_df: pd.DataFrame) -> pd.DataFrame:
    streams_df = streams_df.copy()
    if streams_df.empty:
        streams_df = pd.DataFrame(columns=["Stream", "Color"])

    streams_df["Stream"] = streams_df.get("Stream", "").astype(str).str.strip()
    streams_df["Color"] = streams_df.get("Color", "").astype(str).str.strip()

    task_streams = (
        tasks_df.get("Stream", pd.Series([], dtype=str))
        .astype(str)
        .str.strip()
        .replace({"": "Stream 1", "None": "Stream 1", "nan": "Stream 1"})
    ).unique().tolist()

    mapping: dict[str, str] = {}
    for _, r in streams_df.iterrows():
        s = str(r.get("Stream", "")).strip()
        c = str(r.get("Color", "")).strip()
        if s:
            mapping[s] = c if is_hex_color(c) else stable_color_from_name(s)

    for s in task_streams:
        if s not in mapping:
            mapping[s] = stable_color_from_name(s)

    out = pd.DataFrame([{"Stream": s, "Color": mapping[s]} for s in sorted(mapping.keys())])
    out["Color"] = out.apply(
        lambda r: r["Color"] if is_hex_color(r["Color"]) else stable_color_from_name(r["Stream"]),
        axis=1,
    )
    return out.reset_index(drop=True)


def normalize_tasks(tasks_df: pd.DataFrame, streams_df: pd.DataFrame) -> pd.DataFrame:
    """
    Stored state uses Start/End as YYYY-MM-DD strings (stable for Excel/JSON).
    Also persists Order (row ordering).
    """
    df = tasks_df.copy()

    if "Section" in df.columns:
        df = df.drop(columns=["Section"])

    if "ID" not in df.columns:
        df.insert(0, "ID", [uid() for _ in range(len(df))])
    if "Stream" not in df.columns:
        df["Stream"] = "Stream 1"
    if "Task" not in df.columns:
        df["Task"] = ""
    if "Notes" not in df.columns:
        df["Notes"] = ""
    if "Progress_pct" not in df.columns:
        df["Progress_pct"] = 0.0

    df = ensure_ids(df)
    df = ensure_order(df)

    df["Stream"] = df["Stream"].astype(str).fillna("").str.strip()
    df.loc[df["Stream"].eq("") | df["Stream"].str.lower().eq("none"), "Stream"] = "Stream 1"
    df["Task"] = df["Task"].astype(str).fillna("").str.strip()
    df["Notes"] = df["Notes"].astype(str).fillna("")

    start_dt = pd.to_datetime(df.get("Start"), errors="coerce")
    end_dt = pd.to_datetime(df.get("End"), errors="coerce")
    start_dt = start_dt.fillna(pd.Timestamp(date.today()))
    end_dt = end_dt.fillna(start_dt)

    valid = end_dt >= start_dt
    df["_valid"] = valid
    df["_validation_msg"] = ""
    df.loc[~valid, "_validation_msg"] = "End date must be on/after Start date."

    dur_days = (end_dt - start_dt).dt.days
    dur_days = dur_days.where(dur_days >= 0, 0).fillna(0).astype("int64")
    df["Duration_days"] = dur_days

    df["Progress_pct"] = (
        pd.to_numeric(df.get("Progress_pct", 0), errors="coerce").fillna(0).clip(0, 100).astype(float)
    )

    df["Start"] = start_dt.dt.strftime("%Y-%m-%d")
    df["End"] = end_dt.dt.strftime("%Y-%m-%d")

    cols = ["Order", "ID", "Stream", "Task", "Start", "End", "Duration_days", "Progress_pct", "Notes"]
    for c in cols:
        if c not in df.columns:
            df[c] = ""

    df = df.sort_values(["Order"], kind="stable").reset_index(drop=True)
    df["Order"] = list(range(1, len(df) + 1))
    return df[cols + ["_valid", "_validation_msg"]].reset_index(drop=True)


def seed_demo():
    tasks = pd.DataFrame(
        [
            {
                "Order": 1,
                "Stream": "Project A",
                "Task": "Kickoff & planning",
                "Start": "2025-11-01",
                "End": "2025-11-15",
                "Progress_pct": 20,
                "Notes": "Align scope, owners, deliverables.",
            },
            {
                "Order": 2,
                "Stream": "Project A",
                "Task": "Prototype",
                "Start": "2025-11-16",
                "End": "2025-12-20",
                "Progress_pct": 45,
                "Notes": "Iterate quickly; validate data model + UX.",
            },
            {
                "Order": 3,
                "Stream": "Project B",
                "Task": "Requirements",
                "Start": "2025-11-10",
                "End": "2025-12-05",
                "Progress_pct": 10,
                "Notes": "Constraints, interfaces, assumptions, risks.",
            },
            {
                "Order": 4,
                "Stream": "Project B",
                "Task": "Milestone: Review",
                "Start": "2026-01-15",
                "End": "2026-01-15",
                "Progress_pct": 0,
                "Notes": "0-day milestone",
            },
        ]
    )
    streams = pd.DataFrame(columns=["Stream", "Color"])
    streams = normalize_streams(tasks, streams)
    tasks = normalize_tasks(tasks, streams)
    meta = {"last_editor": None, "last_edit_at": None}
    history = []
    return tasks, streams, meta, history


def current_state_dict() -> dict[str, Any]:
    return {
        "tasks": st.session_state.tasks.to_dict(orient="records"),
        "streams": st.session_state.streams.to_dict(orient="records"),
        "_meta": st.session_state.get("meta", {}),
        "_history": st.session_state.get("history", []),
    }


def append_history(editor_name: str | None, change_hint: str):
    if "history" not in st.session_state:
        st.session_state.history = []
    entry = {"ts_utc": now_utc_iso(), "editor": editor_name or "unknown", "change": change_hint}
    st.session_state.history = [entry] + st.session_state.history
    st.session_state.history = st.session_state.history[:30]


def save_to_db_if_changed(change_hint: str = "Update"):
    if not is_editor:
        return
    if not st.session_state.get("autosave", True):
        return

    state = current_state_dict()
    h = state_hash(state)
    if st.session_state.get("last_saved_hash") == h:
        return

    ok = db_upsert_state(plan_id, state)
    if ok:
        st.session_state.last_saved_hash = h
        st.session_state.db_status = "Saved to Supabase."
    else:
        st.session_state.db_status = "Supabase unreachable — using local state (not saved)."


# ============================================================
# Load from DB on first run
# ============================================================
if "tasks" not in st.session_state or "streams" not in st.session_state:
    loaded = db_load_state(plan_id)
    if loaded and "tasks" in loaded and "streams" in loaded:
        st.session_state.tasks = pd.DataFrame(loaded["tasks"])
        st.session_state.streams = pd.DataFrame(loaded["streams"])
        st.session_state.meta = loaded.get("_meta", {}) or {}
        st.session_state.history = loaded.get("_history", []) or []
        st.session_state.last_loaded_at = loaded.get("_updated_at")
        st.session_state.last_saved_hash = state_hash(
            {
                "tasks": loaded["tasks"],
                "streams": loaded["streams"],
                "_meta": st.session_state.meta,
                "_history": st.session_state.history,
            }
        )
        st.session_state.db_status = "Loaded from Supabase."
    else:
        st.session_state.tasks, st.session_state.streams, st.session_state.meta, st.session_state.history = seed_demo()
        st.session_state.last_loaded_at = None
        st.session_state.last_saved_hash = None
        st.session_state.db_status = "Supabase empty/unreachable — using local demo state."

# Normalize
st.session_state.streams = normalize_streams(st.session_state.tasks, st.session_state.streams)
st.session_state.tasks = normalize_tasks(st.session_state.tasks, st.session_state.streams)
st.session_state.meta = st.session_state.get("meta", {}) or {}
st.session_state.history = st.session_state.get("history", []) or []

# ============================================================
# Header
# ============================================================
h1, h2 = st.columns([1, 7])
with h1:
    try:
        st.image("assets/logo.png", width=180)
    except Exception:
        pass
with h2:
    st.markdown(
        """
        <h2 style="margin-bottom:0;">Activity Planner</h2>
        <div style="opacity:0.75;">Developed by <b>Mahmoud A.</b></div>
        """,
        unsafe_allow_html=True,
    )

st.markdown(
    f"""<div style="height:6px;background:{SCK_PALETTE[0]};border-radius:6px;margin:8px 0 16px 0;"></div>""",
    unsafe_allow_html=True,
)

# ============================================================
# Sidebar
# ============================================================
with st.sidebar:
    st.subheader("Mode")
    st.write("**Editor**" if is_editor else "**Viewer** (read-only)")
    st.caption(st.session_state.get("db_status", ""))

    st.divider()
    if is_editor:
        editor_name = st.text_input(
            "Editor name (for history)",
            value=str(st.session_state.meta.get("last_editor") or "Mahmoud A."),
        ).strip()
    else:
        editor_name = None

    st.divider()
    st.subheader("Excel")
    st.download_button(
        "⬇️ Download Excel template",
        data=make_template_excel_bytes(),
        file_name="ActivityPlanner_Template.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        width="stretch",
    )

    st.download_button(
        "⬇️ Export current plan (Excel)",
        data=export_excel_bytes(st.session_state.tasks, st.session_state.streams),
        file_name="ActivityPlanner_Export.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        width="stretch",
    )

    if is_editor:
        uploaded = st.file_uploader("Load plan from Excel (.xlsx)", type=["xlsx"])
        if uploaded is not None:
            try:
                tasks_raw, streams_raw = load_from_excel_bytes(uploaded.getvalue())
                if "Order" not in tasks_raw.columns:
                    tasks_raw["Order"] = list(range(1, len(tasks_raw) + 1))
                st.session_state.streams = normalize_streams(tasks_raw, streams_raw)
                st.session_state.tasks = normalize_tasks(tasks_raw, st.session_state.streams)

                st.session_state.meta["last_editor"] = editor_name or "unknown"
                st.session_state.meta["last_edit_at"] = now_utc_iso()
                append_history(editor_name, "Loaded plan from Excel")
                save_to_db_if_changed("Loaded plan from Excel")

                # reset drafts
                for k in ["tasks_draft", "tasks_draft_source_hash", "tasks_draft_dirty",
                          "streams_draft", "streams_draft_source_hash", "streams_draft_dirty"]:
                    st.session_state.pop(k, None)

                st.success("Loaded Excel → saved to Supabase (if reachable).")
                st.rerun()
            except Exception as e:
                st.error(f"Failed to load Excel: {e}")

    st.divider()
    if is_editor:
        st.session_state.autosave = st.toggle("Auto-save to Supabase", value=True)
    else:
        st.session_state.autosave = False

    st.divider()
    st.subheader("Change tracking")
    st.caption(f"Last editor: **{st.session_state.meta.get('last_editor') or '—'}**")
    st.caption(f"Last edit (UTC): {st.session_state.meta.get('last_edit_at') or '—'}")

    with st.expander("Recent history", expanded=False):
        if st.session_state.history:
            for hrow in st.session_state.history[:10]:
                st.write(f"- `{hrow.get('ts_utc','')}` — **{hrow.get('editor','')}** — {hrow.get('change','')}")
        else:
            st.caption("No history yet.")

# ============================================================
# Filters
# ============================================================
streams_df = st.session_state.streams.copy()
cmap = stream_color_map(streams_df)

c1, c2 = st.columns([1, 1])
with c1:
    timeline_start = st.date_input("Timeline start", date(2025, 11, 1))
with c2:
    timeline_end = st.date_input("Timeline end", date(2026, 8, 31))

tab_table, tab_chart = st.tabs(["📋 Table", "📈 Chart"])

# ============================================================
# AgGrid JS renderers + editors
# ============================================================
progress_renderer = JsCode(
    """
class ProgressRenderer {
  init(params) {
    const v = Math.max(0, Math.min(100, Number(params.value || 0)));
    const outer = document.createElement('div');
    outer.style.width = '100%';
    outer.style.height = '14px';
    outer.style.borderRadius = '7px';
    outer.style.background = 'rgba(15, 23, 42, 0.10)';
    outer.style.position = 'relative';
    outer.style.overflow = 'hidden';

    const inner = document.createElement('div');
    inner.style.height = '100%';
    inner.style.width = v + '%';
    inner.style.borderRadius = '7px';
    inner.style.background = 'rgba(91, 44, 131, 0.95)';

    const label = document.createElement('div');
    label.style.position = 'absolute';
    label.style.right = '6px';
    label.style.top = '50%';
    label.style.transform = 'translateY(-50%)';
    label.style.fontSize = '11px';
    label.style.fontWeight = '800';
    label.style.color = 'rgba(15, 23, 42, 0.75)';
    label.innerText = v.toFixed(0) + '%';

    outer.appendChild(inner);
    outer.appendChild(label);
    this.eGui = outer;
  }
  getGui() { return this.eGui; }
}
"""
)

stream_pill_renderer = JsCode(
    """
class StreamPillRenderer {
  init(params) {
    const s = String(params.value || '');
    const c = params.data && params.data._streamColor ? String(params.data._streamColor) : '#5B2C83';

    const wrap = document.createElement('div');
    wrap.style.display = 'flex';
    wrap.style.alignItems = 'center';
    wrap.style.gap = '8px';
    wrap.style.minWidth = '0px';

    const sw = document.createElement('div');
    sw.style.width = '10px';
    sw.style.height = '10px';
    sw.style.borderRadius = '3px';
    sw.style.background = c;
    sw.style.border = '1px solid rgba(0,0,0,0.20)';

    const tx = document.createElement('div');
    tx.style.whiteSpace = 'nowrap';
    tx.style.overflow = 'hidden';
    tx.style.textOverflow = 'ellipsis';
    tx.style.fontWeight = '700';
    tx.innerText = s;

    wrap.appendChild(sw);
    wrap.appendChild(tx);
    this.eGui = wrap;
  }
  getGui() { return this.eGui; }
}
"""
)

notes_cell_style = JsCode(
    """
function(params) {
  return {'whiteSpace':'nowrap','overflow':'hidden','textOverflow':'ellipsis'};
}
"""
)

row_style = JsCode(
    """
function(params) {
  if (!params.data) return {};
  if (params.data._valid === false) return { 'backgroundColor': 'rgba(220, 38, 38, 0.08)' };
  return {};
}
"""
)

color_editor = JsCode(
    """
class ColorEditor {
  init(params) {
    this.params = params;
    this.input = document.createElement('input');
    this.input.type = 'color';
    this.input.value = params.value || '#5B2C83';
    this.input.style.width = '100%';
    this.input.style.height = '100%';
    this.input.style.border = '0';
    this.input.style.padding = '0';
    this.input.style.background = 'transparent';
    this.input.addEventListener('input', () => {
      this.params.api.stopEditing();
    });
  }
  getGui() { return this.input; }
  afterGuiAttached() { this.input.focus(); }
  getValue() { return this.input.value; }
}
"""
)

color_renderer = JsCode(
    """
class ColorCellRenderer {
  init(params) {
    const color = params.value || '#5B2C83';
    this.eGui = document.createElement('div');
    this.eGui.style.display = 'flex';
    this.eGui.style.alignItems = 'center';
    this.eGui.style.gap = '8px';
    const sw = document.createElement('div');
    sw.style.width = '14px';
    sw.style.height = '14px';
    sw.style.borderRadius = '4px';
    sw.style.border = '1px solid rgba(0,0,0,0.25)';
    sw.style.backgroundColor = color;
    const tx = document.createElement('span');
    tx.innerText = color.toUpperCase();
    tx.style.fontWeight = '700';
    this.eGui.appendChild(sw);
    this.eGui.appendChild(tx);
  }
  getGui() { return this.eGui; }
}
"""
)

# ============================================================
# TAB: TABLE (Tasks)
# ============================================================
with tab_table:
    st.subheader("Tasks")

    st.markdown(
        """
<style>
/* MS-Project-ish grid styling */
div.ag-theme-alpine {
  --ag-font-family: Inter, Segoe UI, Roboto, Arial, sans-serif;
  --ag-font-size: 13px;
  --ag-header-height: 38px;
  --ag-row-height: 34px;
  --ag-borders: none;
  --ag-row-border-color: rgba(15,23,42,0.06);
  --ag-header-foreground-color: rgba(15,23,42,0.92);
  --ag-header-background-color: rgba(15,23,42,0.02);
}
/* heavier header */
div.ag-theme-alpine .ag-header-cell-label { font-weight: 900; }
/* vertical gridlines only */
div.ag-theme-alpine .ag-cell { border-right: 1px solid rgba(15,23,42,0.08); }
div.ag-theme-alpine .ag-row { border-bottom: 1px solid rgba(15,23,42,0.06); }
/* reduce padding slightly */
div.ag-theme-alpine .ag-cell { padding-left: 10px; padding-right: 10px; }
/* make horizontal scrollbar visible */
div.ag-theme-alpine .ag-body-horizontal-scroll { height: 14px !important; }
</style>
""",
        unsafe_allow_html=True,
    )

    # Source tasks (always ordered)
    tasks_all = st.session_state.tasks.copy()
    tasks_all = tasks_all[tasks_all["Task"].astype(str).str.strip() != ""].copy()
    tasks_all = tasks_all.sort_values("Order", kind="stable").reset_index(drop=True)

    # Draft init/refresh
    source_hash = df_hash(tasks_all[["Order", "ID", "Stream", "Task", "Start", "End", "Progress_pct", "Notes"]].copy())
    if "tasks_draft" not in st.session_state:
        st.session_state.tasks_draft = tasks_all.copy()
        st.session_state.tasks_draft_source_hash = source_hash
        st.session_state.tasks_draft_dirty = False
    else:
        # refresh draft only if source changed AND no pending dirty draft
        if st.session_state.get("tasks_draft_source_hash") != source_hash and not st.session_state.get("tasks_draft_dirty", False):
            st.session_state.tasks_draft = tasks_all.copy()
            st.session_state.tasks_draft_source_hash = source_hash
            st.session_state.tasks_draft_dirty = False

    # Prepare grid dataframe (with stream color)
    draft = st.session_state.tasks_draft.copy()
    cmap_now = stream_color_map(st.session_state.streams)
    draft["_streamColor"] = draft["Stream"].astype(str).map(cmap_now).fillna("#5B2C83")

    grid_df = draft[
        ["Order", "ID", "Stream", "Task", "Start", "End", "Duration_days", "Progress_pct", "Notes", "_valid", "_validation_msg", "_streamColor"]
    ].copy()

    gb = GridOptionsBuilder.from_dataframe(grid_df)
    gb.configure_grid_options(
        headerHeight=38,
        rowHeight=34,
        animateRows=True,
        rowDragManaged=is_editor,
        suppressMoveWhenRowDragging=False,
        getRowStyle=row_style,
        tooltipShowDelay=200,
        suppressHorizontalScroll=False,
        ensureDomOrder=True,
        enableCellTextSelection=True,
    )

    gb.configure_column("Order", hide=True)
    gb.configure_column("ID", hide=True)
    gb.configure_column("_valid", hide=True)
    gb.configure_column("_validation_msg", hide=True)
    gb.configure_column("_streamColor", hide=True)

    # Pinned left
    gb.configure_column("Task", headerName="Task", editable=is_editor, pinned="left", rowDrag=is_editor, flex=2, minWidth=280)
    gb.configure_column("Start", headerName="Start", editable=is_editor, pinned="left", width=130)

    gb.configure_column(
        "Stream",
        headerName="Stream",
        editable=is_editor,
        cellEditor="agSelectCellEditor" if is_editor else None,
        cellEditorParams={"values": streams_df["Stream"].tolist()},
        cellRenderer=stream_pill_renderer,
        width=210,
        minWidth=180,
    )
    gb.configure_column("End", headerName="End", editable=is_editor, width=130, tooltipField="_validation_msg")
    gb.configure_column("Duration_days", headerName="Duration", editable=False, width=110)
    gb.configure_column("Progress_pct", headerName="Progress", editable=is_editor, width=170, cellRenderer=progress_renderer)
    gb.configure_column(
        "Notes",
        headerName="Notes",
        editable=is_editor,
        flex=3,
        minWidth=520,
        tooltipField="Notes",
        cellStyle=notes_cell_style,
    )

    grid_options = gb.build()

    # IMPORTANT:
    # - Use update_on for cell edits and drag reorder
    # - Also set update_mode=MODEL_CHANGED as a robust fallback (older st_aggrid versions)
    update_on = ["cellValueChanged", "rowDragEnd", "rowValueChanged"] if is_editor else []

    grid = AgGrid(
        grid_df,
        gridOptions=grid_options,
        update_on=update_on,
        update_mode=GridUpdateMode.MODEL_CHANGED if is_editor else GridUpdateMode.NO_UPDATE,
        allow_unsafe_jscode=True,
        theme="alpine",
        height=760,
        fit_columns_on_grid_load=False,
        key="tasks_aggrid",
    )

    # Compute dirty after grid returns (captures reorder + edits)
    if is_editor and grid and grid.get("data") is not None:
        new_draft = pd.DataFrame(grid["data"]).copy()

        # Re-apply order based on returned row sequence (THIS is the saved ordering)
        new_draft["Order"] = list(range(1, len(new_draft) + 1))

        # Drop UI-only cols and normalize
        new_draft = new_draft.drop(columns=["_streamColor"], errors="ignore")
        new_draft = normalize_tasks(new_draft, st.session_state.streams)

        st.session_state.tasks_draft = new_draft

        cols_cmp = ["Order", "ID", "Stream", "Task", "Start", "End", "Progress_pct", "Notes"]
        st.session_state.tasks_draft_dirty = (df_hash(tasks_all[cols_cmp]) != df_hash(new_draft[cols_cmp]))

    # Controls (AFTER dirty is known)
    if is_editor:
        cA, cB, cC, cD = st.columns([1.1, 1.1, 1.2, 3.6])

        with cA:
            add_clicked = st.button("➕ Add task", width="stretch")
        with cB:
            reset_clicked = st.button("↩ Reset draft", width="stretch")
        with cC:
            apply_clicked = st.button(
                "✅ Apply edits",
                type="primary",
                width="stretch",
                disabled=not st.session_state.get("tasks_draft_dirty", False),
            )
        with cD:
            if st.session_state.get("tasks_draft_dirty", False):
                st.warning("Unsaved draft changes. Apply to save and update the chart.", icon="⚠️")
            else:
                st.caption("No unsaved changes.")

        if add_clicked:
            d = st.session_state.tasks_draft.copy()
            next_order = int(d["Order"].max()) + 1 if len(d) else 1
            d = pd.concat(
                [
                    d,
                    pd.DataFrame(
                        [
                            {
                                "Order": next_order,
                                "ID": uid(),
                                "Stream": streams_df["Stream"].iloc[0] if len(streams_df) else "Stream 1",
                                "Task": "New task",
                                "Start": date.today().strftime("%Y-%m-%d"),
                                "End": date.today().strftime("%Y-%m-%d"),
                                "Duration_days": 0,
                                "Progress_pct": 0.0,
                                "Notes": "",
                            }
                        ]
                    ),
                ],
                ignore_index=True,
            )
            st.session_state.tasks_draft = normalize_tasks(d, st.session_state.streams)
            st.session_state.tasks_draft_dirty = True
            st.rerun()

        if reset_clicked:
            st.session_state.tasks_draft = tasks_all.copy()
            st.session_state.tasks_draft_source_hash = source_hash
            st.session_state.tasks_draft_dirty = False
            st.rerun()

        if apply_clicked:
            to_save = ensure_ids(st.session_state.tasks_draft.copy())
            to_save = normalize_tasks(to_save, st.session_state.streams)

            st.session_state.tasks = to_save.copy()
            st.session_state.meta["last_editor"] = editor_name or "unknown"
            st.session_state.meta["last_edit_at"] = now_utc_iso()
            append_history(editor_name, "Applied task edits (incl. ordering)")
            save_to_db_if_changed("Applied task edits (incl. ordering)")

            st.session_state.tasks_draft_source_hash = df_hash(
                st.session_state.tasks[["Order", "ID", "Stream", "Task", "Start", "End", "Progress_pct", "Notes"]]
            )
            st.session_state.tasks_draft_dirty = False

            st.success("Saved. Chart order updated.")
            st.rerun()

    # Full Notes expander
    with st.expander("📝 Full Notes", expanded=False):
        base_df = st.session_state.tasks.copy().sort_values("Order", kind="stable").reset_index(drop=True)
        if len(base_df) == 0:
            st.caption("No tasks.")
        else:
            pick_id = st.selectbox(
                "Select a task",
                options=base_df["ID"].astype(str).tolist(),
                format_func=lambda rid: (
                    f"{base_df.loc[base_df['ID'].astype(str).eq(rid)].iloc[0]['Stream']} — "
                    f"{base_df.loc[base_df['ID'].astype(str).eq(rid)].iloc[0]['Task']}"
                ),
            )
            row = base_df.loc[base_df["ID"].astype(str).eq(str(pick_id))].iloc[0]
            if is_editor:
                new_notes = st.text_area("Notes", value=str(row.get("Notes", "") or ""), height=220)
                if st.button("💾 Save Notes", type="primary"):
                    mask = st.session_state.tasks["ID"].astype(str).eq(str(pick_id))
                    st.session_state.tasks.loc[mask, "Notes"] = new_notes
                    st.session_state.tasks = normalize_tasks(st.session_state.tasks, st.session_state.streams)

                    st.session_state.meta["last_editor"] = editor_name or "unknown"
                    st.session_state.meta["last_edit_at"] = now_utc_iso()
                    append_history(editor_name, "Edited notes")
                    save_to_db_if_changed("Edited notes")

                    # keep draft in sync ONLY if draft is not dirty
                    if not st.session_state.get("tasks_draft_dirty", False):
                        st.session_state.tasks_draft = st.session_state.tasks.copy()
                        st.session_state.tasks_draft_source_hash = df_hash(
                            st.session_state.tasks[["Order", "ID", "Stream", "Task", "Start", "End", "Progress_pct", "Notes"]]
                        )

                    st.success("Notes saved.")
                    st.rerun()
            else:
                st.text_area("Notes", value=str(row.get("Notes", "") or ""), height=220, disabled=True)

# ============================================================
# TAB: CHART
# ============================================================
with tab_chart:
    st.subheader("Gantt chart")

    # Streams expander: collapsed by default (per your request)
    with st.expander("Streams (rename + colors)", expanded=False):
        st.caption("Edit streams here, then click Apply. (No auto-save while typing.)")

        # Build source streams state
        streams_src = st.session_state.streams.copy().reset_index(drop=True)
        streams_src = streams_src[["Stream", "Color"]].copy()
        streams_src["Stream"] = streams_src["Stream"].astype(str).str.strip()
        streams_src["Color"] = streams_src["Color"].astype(str).str.strip()

        streams_src_hash = df_hash(streams_src)

        # Draft init/refresh
        if "streams_draft" not in st.session_state:
            st.session_state.streams_draft = streams_src.copy()
            st.session_state.streams_draft_source_hash = streams_src_hash
            st.session_state.streams_draft_dirty = False
        else:
            if st.session_state.get("streams_draft_source_hash") != streams_src_hash and not st.session_state.get("streams_draft_dirty", False):
                st.session_state.streams_draft = streams_src.copy()
                st.session_state.streams_draft_source_hash = streams_src_hash
                st.session_state.streams_draft_dirty = False

        # Streams AgGrid
        sgrid_df = st.session_state.streams_draft.copy()

        sgb = GridOptionsBuilder.from_dataframe(sgrid_df)
        sgb.configure_grid_options(
            headerHeight=38,
            rowHeight=34,
            animateRows=True,
            suppressHorizontalScroll=False,
            ensureDomOrder=True,
        )
        sgb.configure_column("Stream", editable=is_editor, flex=2, minWidth=220)
        sgb.configure_column(
            "Color",
            editable=is_editor,
            cellEditor=color_editor,
            cellRenderer=color_renderer,
            width=220,
            minWidth=200,
        )

        sgrid = AgGrid(
            sgrid_df,
            gridOptions=sgb.build(),
            update_on=["cellValueChanged"] if is_editor else [],
            update_mode=GridUpdateMode.MODEL_CHANGED if is_editor else GridUpdateMode.NO_UPDATE,
            allow_unsafe_jscode=True,
            theme="alpine",
            height=260,
            fit_columns_on_grid_load=False,
            key="streams_aggrid",
        )

        if is_editor and sgrid and sgrid.get("data") is not None:
            draft_streams_new = pd.DataFrame(sgrid["data"]).copy()
            draft_streams_new["Stream"] = draft_streams_new["Stream"].astype(str).str.strip()
            draft_streams_new["Color"] = draft_streams_new["Color"].astype(str).str.strip()
            draft_streams_new = draft_streams_new[draft_streams_new["Stream"].ne("")].reset_index(drop=True)

            # Normalize (also ensures missing task streams get colors later when applied)
            draft_streams_new = normalize_streams(st.session_state.tasks, draft_streams_new)

            st.session_state.streams_draft = draft_streams_new
            st.session_state.streams_draft_dirty = (df_hash(streams_src) != df_hash(draft_streams_new[["Stream", "Color"]]))

        if is_editor:
            c1, c2, c3 = st.columns([1.2, 1.2, 3.6])
            with c1:
                reset_s = st.button("↩ Reset streams", width="stretch")
            with c2:
                apply_s = st.button(
                    "✅ Apply streams",
                    type="primary",
                    width="stretch",
                    disabled=not st.session_state.get("streams_draft_dirty", False),
                )
            with c3:
                if st.session_state.get("streams_draft_dirty", False):
                    st.warning("Unsaved stream changes. Apply to update chart colors.", icon="⚠️")
                else:
                    st.caption("No unsaved stream changes.")

            if reset_s:
                st.session_state.streams_draft = streams_src.copy()
                st.session_state.streams_draft_source_hash = streams_src_hash
                st.session_state.streams_draft_dirty = False
                st.rerun()

            if apply_s:
                # Apply streams
                st.session_state.streams = normalize_streams(st.session_state.tasks, st.session_state.streams_draft.copy())

                st.session_state.meta["last_editor"] = editor_name or "unknown"
                st.session_state.meta["last_edit_at"] = now_utc_iso()
                append_history(editor_name, "Applied stream edits (rename/colors)")
                save_to_db_if_changed("Applied stream edits (rename/colors)")

                st.session_state.streams_draft_source_hash = df_hash(st.session_state.streams[["Stream", "Color"]])
                st.session_state.streams_draft_dirty = False

                st.success("Streams saved. Chart colors updated.")
                st.rerun()

    st.markdown(
        """
        **Legend**
        - **Bars** = tasks (Start → End)
        - **🚩 Flag** = milestones
        - **Red vertical line** = today
        """.strip()
    )

    tasks = st.session_state.tasks.copy()
    tasks = tasks[tasks["Task"].astype(str).str.strip() != ""].copy()
    tasks = tasks.sort_values("Order", kind="stable").reset_index(drop=True)

    t0 = pd.Timestamp(timeline_start)
    t1 = pd.Timestamp(timeline_end)

    fig = build_gantt_figure(tasks, stream_color_map(st.session_state.streams), t0, t1, show_progress_text=True)
    fig.update_layout(template="plotly_white", paper_bgcolor="white", plot_bgcolor="white", font=dict(size=13))
    fig.update_xaxes(showgrid=True, gridcolor=SCK_GRID, side="top")
    fig.update_yaxes(showgrid=False)

    today_ts = pd.Timestamp(date.today())
    if t0 <= today_ts <= t1:
        fig.add_vline(x=today_ts, line_color="red", line_width=3)

    st.plotly_chart(fig, width="stretch")

import streamlit as st
import pandas as pd
from datetime import date, datetime, timezone
import re
import hashlib
import uuid
from io import BytesIO

from st_aggrid import AgGrid, GridOptionsBuilder, JsCode

from supabase_store import db_load_state, db_upsert_state, state_hash
from excel_io import make_template_excel_bytes, export_excel_bytes, load_from_excel_bytes
from gantt_chart import build_gantt_figure

# ============================================================
# SCK CEN — palette (tune if you have the official HEX guide)
# ============================================================
SCK_PALETTE = [
    "#5B2C83",  # deep purple
    "#7E3FA0",  # purple
    "#9B59B6",  # light purple
    "#2C3E50",  # dark blue-grey
    "#34495E",  # blue-grey
    "#16A085",  # teal
    "#2980B9",  # blue
    "#F39C12",  # amber
    "#D35400",  # orange
    "#C0392B",  # red
]
SCK_GRID = "#E6E8F0"

st.set_page_config(
    page_title="Activity Planner",
    layout="wide",
    page_icon="assets/logo.jpg",  # optional; keep file in assets/
)

# ============================================================
# Header (logo + title + byline)
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
        <div style="opacity:0.75;">Developed by <b>M. Abdelrahman</b></div>
        """,
        unsafe_allow_html=True,
    )

st.markdown(
    f"""<div style="height:6px;background:{SCK_PALETTE[0]};border-radius:6px;margin:8px 0 16px 0;"></div>""",
    unsafe_allow_html=True,
)

HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

# ============================================================
# Access control
# ============================================================
plan_id = st.secrets.get("PLAN_ID", "default-plan")
mode = st.query_params.get("mode", "view")  # view|edit
token = st.query_params.get("token", "")
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
    """Stable assignment from the fixed palette (no random HSV)."""
    h = int(hashlib.md5(name.encode("utf-8")).hexdigest()[:8], 16)
    return SCK_PALETTE[h % len(SCK_PALETTE)]

def stream_color_map(streams_df: pd.DataFrame) -> dict:
    if streams_df is None or streams_df.empty:
        return {}
    return {r["Stream"]: r["Color"] for _, r in streams_df.iterrows()}

def normalize_streams(tasks_df: pd.DataFrame, streams_df: pd.DataFrame) -> pd.DataFrame:
    streams_df = streams_df.copy()
    if streams_df.empty:
        streams_df = pd.DataFrame(columns=["Stream", "Color"])

    streams_df["Stream"] = streams_df.get("Stream", "").astype(str).str.strip()
    streams_df["Color"] = streams_df.get("Color", "").astype(str).str.strip()

    task_streams = (
        tasks_df.get("Stream", pd.Series([], dtype=str))
        .astype(str).str.strip()
        .replace({"": "Stream 1"})
    ).unique().tolist()

    mapping = {}
    for _, r in streams_df.iterrows():
        s = str(r["Stream"]).strip()
        c = str(r["Color"]).strip()
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
    df = tasks_df.copy()

    if "ID" not in df.columns:
        df.insert(0, "ID", [uid() for _ in range(len(df))])

    df["ID"] = df["ID"].astype(str)
    bad_id = df["ID"].str.strip().eq("") | df["ID"].str.lower().eq("none")
    if bad_id.any():
        df.loc[bad_id, "ID"] = [uid() for _ in range(bad_id.sum())]

    df["Stream"] = df.get("Stream", "Stream 1").astype(str).str.strip()
    df.loc[df["Stream"].eq("") | df["Stream"].str.lower().eq("none"), "Stream"] = "Stream 1"

    df["Task"] = df.get("Task", "").astype(str).fillna("").str.strip()
    df["Notes"] = df.get("Notes", "").astype(str).fillna("")

    start_dt = pd.to_datetime(df.get("Start"), errors="coerce").fillna(pd.Timestamp(date.today()))
    end_dt = pd.to_datetime(df.get("End"), errors="coerce").fillna(start_dt)

    valid = end_dt >= start_dt
    df["_valid"] = valid
    df["_validation_msg"] = ""
    df.loc[~valid, "_validation_msg"] = "Invalid dates: End must be ≥ Start."

    dur_days = (end_dt - start_dt).dt.days
    dur_days = dur_days.where(dur_days >= 0, 0).fillna(0)
    dur_days = dur_days.infer_objects(copy=False)  # reduces future warnings
    df["Duration_days"] = dur_days.astype("int64")

    df["Progress_pct"] = (
        pd.to_numeric(df.get("Progress_pct", 0), errors="coerce")
        .fillna(0)
        .clip(0, 100)
        .astype(float)
    )

    # Keep ISO strings for the AgGrid date editor
    df["Start"] = start_dt.dt.strftime("%Y-%m-%d")
    df["End"] = end_dt.dt.strftime("%Y-%m-%d")

    cols = [
        "ID", "Stream", "Task", "Start", "End", "Duration_days", "Progress_pct", "Notes",
        "_valid", "_validation_msg"
    ]
    for c in cols:
        if c not in df.columns:
            df[c] = ""
    return df[cols].reset_index(drop=True)

def seed_demo():
    tasks = pd.DataFrame(
        [
            {"Stream": "Project A", "Task": "Kickoff & planning", "Start": "2025-11-01", "End": "2025-11-15", "Progress_pct": 20, "Notes": ""},
            {"Stream": "Project A", "Task": "Prototype",          "Start": "2025-11-16", "End": "2025-12-20", "Progress_pct": 45, "Notes": ""},
            {"Stream": "Project B", "Task": "Requirements",       "Start": "2025-11-10", "End": "2025-12-05", "Progress_pct": 10, "Notes": ""},
            {"Stream": "Project B", "Task": "Milestone: Review",  "Start": "2026-01-15", "End": "2026-01-15", "Progress_pct": 0,  "Notes": "0-day milestone"},
        ]
    )
    streams = pd.DataFrame(columns=["Stream", "Color"])
    streams = normalize_streams(tasks, streams)
    tasks = normalize_tasks(tasks, streams)
    meta = {
        "last_editor": None,
        "last_edit_at": None,
    }
    history = []
    return tasks, streams, meta, history

def current_state_dict() -> dict:
    # persisted state (DB)
    return {
        "tasks": st.session_state.tasks.to_dict(orient="records"),
        "streams": st.session_state.streams.to_dict(orient="records"),
        "_meta": st.session_state.get("meta", {}),
        "_history": st.session_state.get("history", []),
    }

def append_history(editor_name: str | None, change_hint: str):
    if "history" not in st.session_state:
        st.session_state.history = []
    entry = {
        "ts_utc": now_utc_iso(),
        "editor": editor_name or "unknown",
        "change": change_hint,
    }
    st.session_state.history = [entry] + st.session_state.history
    st.session_state.history = st.session_state.history[:30]  # keep last 30

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
# PDF export
# ============================================================
def export_pdf(tasks_df: pd.DataFrame, streams_df: pd.DataFrame, fig, plan_id: str, editor_name: str | None):
    """
    Creates a PDF (ReportLab). Includes:
    - Header + metadata
    - Stream legend
    - Gantt chart (if image export available)
    - Task table
    """
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib.utils import ImageReader

    # Try to render Plotly figure to PNG (requires Plotly+Kaleido compatibility)
    chart_img_reader = None
    chart_err = None
    try:
        import plotly.io as pio
        img_bytes = pio.to_image(fig, format="png", width=1600, height=800, scale=2)
        chart_img_reader = ImageReader(BytesIO(img_bytes))
    except Exception as e:
        chart_err = str(e)

    buf = BytesIO()
    W, H = A4
    c = canvas.Canvas(buf, pagesize=A4)

    y = H - 2.0 * cm
    c.setFont("Helvetica-Bold", 16)
    c.drawString(2 * cm, y, "Activity Planner — Report")
    y -= 0.6 * cm

    c.setFont("Helvetica", 10)
    c.drawString(2 * cm, y, f"Plan ID: {plan_id}")
    y -= 0.45 * cm
    c.drawString(2 * cm, y, f"Generated (UTC): {now_utc_iso()}")
    y -= 0.45 * cm
    c.drawString(2 * cm, y, f"Last editor (app): {editor_name or '—'}")
    y -= 0.8 * cm

    # Stream legend
    c.setFont("Helvetica-Bold", 12)
    c.drawString(2 * cm, y, "Streams (Legend)")
    y -= 0.5 * cm

    c.setFont("Helvetica", 9)
    for _, r in streams_df.iterrows():
        if y < 3.0 * cm:
            c.showPage()
            y = H - 2.0 * cm
        s = str(r["Stream"])
        col = str(r["Color"])
        c.drawString(2.0 * cm, y, f"■ {s}   {col}")
        y -= 0.35 * cm

    y -= 0.4 * cm

    # Chart page
    if chart_img_reader is not None:
        if y < 13.5 * cm:
            c.showPage()
            y = H - 2.0 * cm
        c.setFont("Helvetica-Bold", 12)
        c.drawString(2 * cm, y, "Gantt Chart")
        y -= 0.5 * cm
        c.drawImage(chart_img_reader, 2 * cm, y - 12.0 * cm, width=W - 4 * cm, height=12.0 * cm, preserveAspectRatio=True, anchor='c')
        y -= 12.6 * cm
    else:
        c.setFont("Helvetica-Bold", 12)
        c.drawString(2 * cm, y, "Gantt Chart")
        y -= 0.5 * cm
        c.setFont("Helvetica", 9)
        c.drawString(2 * cm, y, "Chart image could not be embedded (Plotly/Kaleido mismatch or missing deps).")
        y -= 0.35 * cm
        if chart_err:
            c.drawString(2 * cm, y, f"Reason: {chart_err[:120]}")
            y -= 0.5 * cm

    # Tasks table (simple text table)
    if y < 3.5 * cm:
        c.showPage()
        y = H - 2.0 * cm

    c.setFont("Helvetica-Bold", 12)
    c.drawString(2 * cm, y, "Tasks")
    y -= 0.6 * cm

    cols = ["Stream", "Task", "Start", "End", "Duration_days", "Progress_pct"]
    c.setFont("Helvetica-Bold", 8)
    c.drawString(2 * cm, y, " | ".join(cols))
    y -= 0.35 * cm
    c.setFont("Helvetica", 8)

    for _, row in tasks_df.iterrows():
        if y < 2.5 * cm:
            c.showPage()
            y = H - 2.0 * cm
            c.setFont("Helvetica-Bold", 8)
            c.drawString(2 * cm, y, " | ".join(cols))
            y -= 0.35 * cm
            c.setFont("Helvetica", 8)

        vals = []
        for cc in cols:
            v = row.get(cc, "")
            if cc == "Progress_pct":
                try:
                    v = f"{float(v):.0f}%"
                except Exception:
                    pass
            vals.append(str(v))
        line = " | ".join(vals)
        c.drawString(2 * cm, y, line[:140])
        y -= 0.32 * cm

    c.showPage()

    # History page
    c.setFont("Helvetica-Bold", 12)
    c.drawString(2 * cm, H - 2.0 * cm, "Change History (latest first)")
    y = H - 2.8 * cm
    c.setFont("Helvetica", 9)
    for hrow in st.session_state.get("history", [])[:25]:
        if y < 2.5 * cm:
            c.showPage()
            y = H - 2.0 * cm
        c.drawString(2 * cm, y, f"{hrow.get('ts_utc','')} — {hrow.get('editor','')} — {hrow.get('change','')}")
        y -= 0.35 * cm

    c.save()
    buf.seek(0)
    return buf

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
        st.session_state.last_saved_hash = state_hash({
            "tasks": loaded["tasks"],
            "streams": loaded["streams"],
            "_meta": st.session_state.meta,
            "_history": st.session_state.history,
        })
        st.session_state.db_status = "Loaded from Supabase."
    else:
        st.session_state.tasks, st.session_state.streams, st.session_state.meta, st.session_state.history = seed_demo()
        st.session_state.last_loaded_at = None
        st.session_state.last_saved_hash = None
        st.session_state.db_status = "Supabase empty/unreachable — using local demo state."

# Always keep normalized
st.session_state.streams = normalize_streams(st.session_state.tasks, st.session_state.streams)
st.session_state.tasks = normalize_tasks(st.session_state.tasks, st.session_state.streams)
st.session_state.meta = st.session_state.get("meta", {}) or {}
st.session_state.history = st.session_state.get("history", []) or []

# AgGrid remount control
if "data_version" not in st.session_state:
    st.session_state.data_version = 0

# ============================================================
# Sidebar
# ============================================================
with st.sidebar:
    st.subheader("Mode")
    st.write("**Editor**" if is_editor else "**Viewer** (read-only)")
    st.caption(st.session_state.get("db_status", ""))

    if st.session_state.get("last_loaded_at"):
        st.caption(f"Loaded from DB: {st.session_state.last_loaded_at}")

    st.divider()

    # Editor identity (for history)
    if is_editor:
        editor_name = st.text_input(
            "Editor name (for history)",
            value=str(st.session_state.meta.get("last_editor") or "M. Abdelrahman"),
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
                file_bytes = uploaded.getvalue()
                tasks_raw, streams_raw = load_from_excel_bytes(file_bytes)

                streams_norm = normalize_streams(tasks_raw, streams_raw)
                tasks_norm = normalize_tasks(tasks_raw, streams_norm)

                st.session_state.tasks = tasks_norm
                st.session_state.streams = streams_norm

                st.session_state.meta["last_editor"] = editor_name or "unknown"
                st.session_state.meta["last_edit_at"] = now_utc_iso()
                append_history(editor_name, "Loaded plan from Excel")

                st.session_state.data_version += 1
                save_to_db_if_changed("Loaded plan from Excel")

                st.success("Loaded Excel → saved to Supabase (if reachable).")
                st.rerun()
            except Exception as e:
                st.error(f"Failed to load Excel: {e}")
    else:
        st.caption("Excel import is disabled in Viewer mode.")

    st.divider()
    if is_editor:
        st.session_state.autosave = st.toggle("Auto-save to Supabase", value=True)
    else:
        st.session_state.autosave = False

    # Last editor + history preview
    st.divider()
    last_editor = st.session_state.meta.get("last_editor")
    last_edit_at = st.session_state.meta.get("last_edit_at")
    st.subheader("Change tracking")
    st.caption(f"Last editor: **{last_editor or '—'}**")
    st.caption(f"Last edit (UTC): {last_edit_at or '—'}")

    with st.expander("Recent change history", expanded=False):
        if st.session_state.history:
            for hrow in st.session_state.history[:10]:
                st.write(f"- `{hrow.get('ts_utc','')}` — **{hrow.get('editor','')}** — {hrow.get('change','')}")
        else:
            st.caption("No history yet.")

    st.divider()
    try:
        st.image("assets/logo.jpg", width=220)
    except Exception:
        pass
    st.markdown(
        """
        <div style="font-size:12px; opacity:0.8; text-align:center; margin-top:8px;">
            Developed by <b>M. Abdelrahman</b><br>
            © SCK CEN
        </div>
        """,
        unsafe_allow_html=True,
    )

# ============================================================
# Filters (colored toggles)
# ============================================================
streams_df = st.session_state.streams.copy()
cmap = stream_color_map(streams_df)

st.markdown("### Filter by project / stream")
cols = st.columns(max(1, len(streams_df)))
selected_streams = []
for col, (_, r) in zip(cols, streams_df.iterrows()):
    s = r["Stream"]
    c = r["Color"]
    with col:
        on = st.toggle(s, value=True, key=f"filter_{s}_{st.session_state.data_version}")
        st.markdown(
            f"""<div style="height:4px;background:{c};border-radius:2px;margin-top:-6px;"></div>""",
            unsafe_allow_html=True,
        )
        if on:
            selected_streams.append(s)
stream_filter = selected_streams if selected_streams else streams_df["Stream"].tolist()

c1, c2 = st.columns([1, 1])
with c1:
    timeline_start = st.date_input("Timeline start", date(2025, 11, 1))
with c2:
    timeline_end = st.date_input("Timeline end", date(2026, 8, 31))

tab_table, tab_chart = st.tabs(["📋 Table", "📈 Chart"])

# ============================================================
# AgGrid JS (date picker + color picker + styling)
# ============================================================
date_editor = JsCode("""
class DateEditor {
  init(params) {
    this.params = params;
    this.input = document.createElement('input');
    this.input.type = 'date';
    this.input.value = params.value || '';
    this.input.style.width = '100%';
    this.input.style.height = '100%';
    this.input.style.border = '0';
    this.input.style.outline = 'none';
    this.input.style.background = 'transparent';
    this.input.addEventListener('input', () => {
      this.params.api.stopEditing();
    });
  }
  getGui() { return this.input; }
  afterGuiAttached() {
    this.input.focus();
    if (this.input.showPicker) this.input.showPicker();
  }
  getValue() { return this.input.value; }
}
""")

color_editor = JsCode("""
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
""")

color_renderer = JsCode("""
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
    sw.style.borderRadius = '3px';
    sw.style.border = '1px solid rgba(0,0,0,0.25)';
    sw.style.backgroundColor = color;
    const tx = document.createElement('span');
    tx.innerText = color.toUpperCase();
    tx.style.fontWeight = '600';
    this.eGui.appendChild(sw);
    this.eGui.appendChild(tx);
  }
  getGui() { return this.eGui; }
}
""")

row_style_invalid = JsCode("""
function(params) {
  if (params.data && params.data._valid === false) {
    return { 'backgroundColor': 'rgba(255, 0, 0, 0.08)' };
  }
  return {};
}
""")

end_cell_style_invalid = JsCode("""
function(params) {
  if (params.data && params.data._valid === false) {
    return { 'border': '2px solid red', 'backgroundColor': 'rgba(255,0,0,0.06)' };
  }
  return {};
}
""")

end_tooltip_invalid = JsCode("""
function(params) {
  if (params.data && params.data._valid === false) {
    return params.data._validation_msg || 'Invalid dates';
  }
  return '';
}
""")

# ============================================================
# TAB: TABLE
# ============================================================
with tab_table:
    # Streams section moved to collapsible
    with st.expander("Streams (colors)", expanded=False):
        gbS = GridOptionsBuilder.from_dataframe(streams_df)
        gbS.configure_grid_options(
            singleClickEdit=True,
            stopEditingWhenCellsLoseFocus=True,
            rowHeight=32,
            headerHeight=34,
        )
        gbS.configure_column("Stream", editable=is_editor, width=260)
        gbS.configure_column("Color", editable=is_editor, cellEditor=color_editor, cellRenderer=color_renderer, width=200)

        gridS = AgGrid(
            streams_df,
            gridOptions=gbS.build(),
            update_on=["cellValueChanged"],
            allow_unsafe_jscode=True,
            theme="alpine",
            height=220,
            fit_columns_on_grid_load=True,
            key=f"grid_streams_{st.session_state.data_version}",
        )

        if is_editor:
            streams_edited = pd.DataFrame(gridS["data"]).copy()
            streams_edited["Stream"] = streams_edited["Stream"].astype(str).str.strip()
            streams_edited["Color"] = streams_edited["Color"].astype(str).str.strip()
            streams_edited = streams_edited[streams_edited["Stream"].ne("")].copy()

            streams_norm = normalize_streams(st.session_state.tasks, streams_edited)
            if not streams_norm.equals(st.session_state.streams):
                st.session_state.streams = streams_norm

                st.session_state.meta["last_editor"] = editor_name or "unknown"
                st.session_state.meta["last_edit_at"] = now_utc_iso()
                append_history(editor_name, "Updated stream colors")

                st.session_state.data_version += 1
                save_to_db_if_changed("Updated stream colors")
                st.rerun()

    st.subheader("Tasks")

    # Add new row
    if is_editor and st.button("➕ Add new task"):
        new_task = pd.DataFrame(
            [
                {
                    "ID": uid(),
                    "Stream": st.session_state.streams["Stream"].iloc[0] if len(st.session_state.streams) else "Stream 1",
                    "Task": "New task",
                    "Start": date.today().strftime("%Y-%m-%d"),
                    "End": date.today().strftime("%Y-%m-%d"),
                    "Progress_pct": 0,
                    "Notes": "",
                }
            ]
        )
        base = st.session_state.tasks.drop(columns=["Duration_days", "_valid", "_validation_msg"], errors="ignore")
        st.session_state.tasks = normalize_tasks(pd.concat([base, new_task], ignore_index=True), st.session_state.streams)

        st.session_state.meta["last_editor"] = editor_name or "unknown"
        st.session_state.meta["last_edit_at"] = now_utc_iso()
        append_history(editor_name, "Added a new task")

        st.session_state.data_version += 1
        save_to_db_if_changed("Added a new task")
        st.rerun()

    tasks_all = st.session_state.tasks.copy()
    tasks_view = tasks_all[tasks_all["Stream"].isin(stream_filter)].copy()

    gbT = GridOptionsBuilder.from_dataframe(tasks_view)
    gbT.configure_grid_options(
        singleClickEdit=True,
        stopEditingWhenCellsLoseFocus=True,
        rowHeight=32,
        headerHeight=34,
        getRowStyle=row_style_invalid,
        tooltipShowDelay=200,
    )

    gbT.configure_column("ID", hide=True)
    gbT.configure_column("_valid", hide=True)
    gbT.configure_column("_validation_msg", hide=True)

    gbT.configure_column(
        "Stream",
        editable=is_editor,
        cellEditor="agSelectCellEditor",
        cellEditorParams={"values": st.session_state.streams["Stream"].tolist()},
        width=200,
    )
    gbT.configure_column("Task", editable=is_editor, width=560)
    gbT.configure_column("Start", headerName="Start 📅", editable=is_editor, cellEditor=date_editor, width=160)
    gbT.configure_column(
        "End",
        headerName="End 📅",
        editable=is_editor,
        cellEditor=date_editor,
        width=160,
        cellStyle=end_cell_style_invalid,
        tooltipValueGetter=end_tooltip_invalid,
    )
    gbT.configure_column("Duration_days", headerName="Duration (days)", editable=False, width=150)
    gbT.configure_column("Progress_pct", headerName="Progress %", editable=is_editor, width=130)
    gbT.configure_column("Notes", editable=is_editor, width=320)

    gridT = AgGrid(
        tasks_view,
        gridOptions=gbT.build(),
        update_on=["cellValueChanged"],
        allow_unsafe_jscode=True,
        theme="alpine",
        height=560,
        fit_columns_on_grid_load=True,
        key=f"grid_tasks_{st.session_state.data_version}",
    )

    # Apply edits back to full dataframe (keep stable IDs)
    if is_editor:
        edited_view = pd.DataFrame(gridT["data"]).copy()
        edited_view = edited_view.drop(columns=["Duration_days", "_valid", "_validation_msg"], errors="ignore")

        base = tasks_all.set_index("ID")
        upd = edited_view.set_index("ID")

        base.update(upd)
        new_ids = upd.index.difference(base.index)
        if len(new_ids) > 0:
            base = pd.concat([base, upd.loc[new_ids]])

        tasks_norm = normalize_tasks(base.reset_index(), st.session_state.streams)
        if not tasks_norm.equals(st.session_state.tasks):
            st.session_state.tasks = tasks_norm

            st.session_state.meta["last_editor"] = editor_name or "unknown"
            st.session_state.meta["last_edit_at"] = now_utc_iso()
            append_history(editor_name, "Edited tasks table")

            st.session_state.data_version += 1
            save_to_db_if_changed("Edited tasks table")
            st.rerun()

    invalid_count = int((~st.session_state.tasks["_valid"]).sum())
    if invalid_count > 0:
        st.error(f"{invalid_count} row(s) have invalid dates (End < Start). Fix them in the table (End cell highlighted).")

    # Export / report actions
    st.divider()
    colx1, colx2 = st.columns([1, 1])

    with colx1:
        if st.button("📄 Export PDF report"):
            # Build fig for the current filter range to embed
            tasks_pdf = st.session_state.tasks.copy()
            tasks_pdf = tasks_pdf[tasks_pdf["Stream"].isin(stream_filter)].copy()
            t0 = pd.Timestamp(timeline_start)
            t1 = pd.Timestamp(timeline_end)
            fig_pdf = build_gantt_figure(
                tasks_pdf,
                stream_color_map(st.session_state.streams),
                t0,
                t1,
                show_progress_text=True,
            )
            today = pd.Timestamp(date.today())
            if t0 <= today <= t1:
                fig_pdf.add_vline(x=today, line_color="red", line_width=3)

            pdf_buf = export_pdf(
                tasks_df=tasks_pdf,
                streams_df=st.session_state.streams.copy(),
                fig=fig_pdf,
                plan_id=plan_id,
                editor_name=editor_name if is_editor else st.session_state.meta.get("last_editor"),
            )

            st.download_button(
                "⬇️ Download PDF",
                data=pdf_buf.getvalue(),
                file_name="ActivityPlanner_Report.pdf",
                mime="application/pdf",
                width="stretch",
            )

    with colx2:
        if is_editor and not st.session_state.get("autosave", True):
            if st.button("💾 Save to Supabase now"):
                st.session_state.meta["last_editor"] = editor_name or "unknown"
                st.session_state.meta["last_edit_at"] = now_utc_iso()
                append_history(editor_name, "Manual save")

                # Force save by resetting last hash so it will write
                st.session_state.last_saved_hash = None
                save_to_db_if_changed("Manual save")
                st.success("Saved (if Supabase reachable).")

# ============================================================
# TAB: CHART
# ============================================================
with tab_chart:
    st.subheader("Gantt chart")

    # Legend for milestones
    st.markdown(
        """
        **Legend**  
        - **Bars** = tasks (Start → End)  
        - **◆ Diamonds** = milestones (0-day: Start = End)  
        - **Red vertical line** = today  
        """.strip()
    )

    tasks = st.session_state.tasks.copy()
    tasks = tasks[tasks["Stream"].isin(stream_filter)].copy()

    t0 = pd.Timestamp(timeline_start)
    t1 = pd.Timestamp(timeline_end)

    fig = build_gantt_figure(
        tasks,
        stream_color_map(st.session_state.streams),
        t0,
        t1,
        show_progress_text=True,
    )

    fig.update_layout(
        template="plotly_white",
        paper_bgcolor="white",
        plot_bgcolor="white",
        font=dict(size=13),
        margin=dict(l=10, r=10, t=40, b=10),
    )
    fig.update_xaxes(showgrid=True, gridcolor=SCK_GRID, side="top")
    fig.update_yaxes(showgrid=False)

    # Today line (solid red)
    today = pd.Timestamp(date.today())
    if t0 <= today <= t1:
        fig.add_vline(x=today, line_color="red", line_width=3)

    st.plotly_chart(fig, width="stretch")

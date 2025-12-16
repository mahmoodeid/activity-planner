import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import date
from io import BytesIO
import uuid
import re
import colorsys
import hashlib

from st_aggrid import AgGrid, GridOptionsBuilder, JsCode

# =========================================================
# Page
# =========================================================
st.set_page_config(page_title="Activity Planner — Gantt", layout="wide")
st.title("Activity Planner")

HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

# =========================================================
# Excel schema (Tasks + Streams sheets)
# =========================================================
TASKS_SHEET = "Tasks"
STREAMS_SHEET = "Streams"

TASKS_COLUMNS = ["Stream", "Task", "Start", "End", "Progress_pct", "Notes"]
STREAMS_COLUMNS = ["Stream", "Color"]


# =========================================================
# Helpers
# =========================================================
def uid() -> str:
    return uuid.uuid4().hex[:8].upper()


def stable_color_from_name(name: str) -> str:
    """
    Deterministic "random" color from stream name.
    """
    h = int(hashlib.md5(name.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF
    r, g, b = colorsys.hsv_to_rgb(h, 0.60, 0.85)
    return "#{:02X}{:02X}{:02X}".format(int(r * 255), int(g * 255), int(b * 255))


def is_hex_color(x: str) -> bool:
    return bool(HEX_RE.match(str(x).strip()))


def make_template_excel_bytes() -> bytes:
    tasks = pd.DataFrame([], columns=TASKS_COLUMNS)
    streams = pd.DataFrame([], columns=STREAMS_COLUMNS)
    bio = BytesIO()
    with pd.ExcelWriter(bio, engine="openpyxl") as w:
        tasks.to_excel(w, index=False, sheet_name=TASKS_SHEET)
        streams.to_excel(w, index=False, sheet_name=STREAMS_SHEET)
    return bio.getvalue()


def export_excel_bytes(tasks_df: pd.DataFrame, streams_df: pd.DataFrame) -> bytes:
    bio = BytesIO()
    with pd.ExcelWriter(bio, engine="openpyxl") as w:
        tasks_export = tasks_df.drop(
            columns=[c for c in ["ID", "Duration_days", "_valid", "_validation_msg", "Start_dt", "End_dt"] if c in tasks_df.columns],
            errors="ignore",
        ).copy()
        tasks_export.to_excel(w, index=False, sheet_name=TASKS_SHEET)
        streams_df.to_excel(w, index=False, sheet_name=STREAMS_SHEET)
    return bio.getvalue()


def load_from_excel(file) -> tuple[pd.DataFrame, pd.DataFrame]:
    xls = pd.ExcelFile(file)

    if TASKS_SHEET not in xls.sheet_names:
        raise ValueError(f"Excel must contain a sheet named '{TASKS_SHEET}'.")

    tasks = pd.read_excel(xls, sheet_name=TASKS_SHEET)
    tasks.columns = [str(c).strip() for c in tasks.columns]

    # Minimal required
    if "Stream" not in tasks.columns:
        tasks["Stream"] = "Stream 1"
    if "Task" not in tasks.columns:
        tasks["Task"] = ""
    if "Start" not in tasks.columns:
        tasks["Start"] = date.today().strftime("%Y-%m-%d")
    if "End" not in tasks.columns:
        tasks["End"] = date.today().strftime("%Y-%m-%d")
    if "Progress_pct" not in tasks.columns:
        tasks["Progress_pct"] = 0
    if "Notes" not in tasks.columns:
        tasks["Notes"] = ""

    keep = ["Stream", "Task", "Start", "End", "Progress_pct", "Notes"]
    if "ID" in tasks.columns:
        keep = ["ID"] + keep
    tasks = tasks[keep].copy()

    # Streams sheet is optional
    if STREAMS_SHEET in xls.sheet_names:
        streams = pd.read_excel(xls, sheet_name=STREAMS_SHEET)
        streams.columns = [str(c).strip() for c in streams.columns]
        if "Stream" not in streams.columns:
            streams["Stream"] = []
        if "Color" not in streams.columns:
            streams["Color"] = []
        streams = streams[["Stream", "Color"]].copy()
    else:
        streams = pd.DataFrame(columns=["Stream", "Color"])

    return tasks, streams


def normalize_streams(tasks_df: pd.DataFrame, streams_df: pd.DataFrame) -> pd.DataFrame:
    """
    Ensure streams_df covers all streams present in tasks_df.
    Assign deterministic colors for missing/invalid colors.
    """
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

    mapping: dict[str, str] = {}
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
    """
    Normalize tasks, compute derived + validation.
    Keep Start/End as ISO strings (YYYY-MM-DD) for AgGrid date editor.
    """
    df = tasks_df.copy()

    if "ID" not in df.columns:
        df.insert(0, "ID", [uid() for _ in range(len(df))])

    df["ID"] = df["ID"].astype(str)
    bad_id = df["ID"].str.strip().eq("") | df["ID"].str.lower().eq("none")
    if bad_id.any():
        df.loc[bad_id, "ID"] = [uid() for _ in range(bad_id.sum())]

    df["Stream"] = df["Stream"].astype(str).str.strip()
    df.loc[df["Stream"].eq("") | df["Stream"].str.lower().eq("none"), "Stream"] = "Stream 1"

    df["Task"] = df["Task"].astype(str).replace({"None": ""}).fillna("").str.strip()
    df["Notes"] = df["Notes"].astype(str).replace({"None": ""}).fillna("")

    start_dt = pd.to_datetime(df["Start"], errors="coerce").fillna(pd.Timestamp(date.today()))
    end_dt = pd.to_datetime(df["End"], errors="coerce").fillna(start_dt)

    valid = end_dt >= start_dt
    df["_valid"] = valid
    df["_validation_msg"] = ""
    df.loc[~valid, "_validation_msg"] = "Invalid dates: End must be ≥ Start."

    dur_days = (end_dt - start_dt).dt.days
    dur_days = dur_days.where(dur_days >= 0, 0).fillna(0).astype(int)
    df["Duration_days"] = dur_days.astype("int64")

    df["Progress_pct"] = pd.to_numeric(df["Progress_pct"], errors="coerce").fillna(0).clip(0, 100).astype(float)

    # store ISO strings
    df["Start"] = start_dt.dt.strftime("%Y-%m-%d")
    df["End"] = end_dt.dt.strftime("%Y-%m-%d")

    cols = ["ID", "Stream", "Task", "Start", "End", "Duration_days", "Progress_pct", "Notes", "_valid", "_validation_msg"]
    for c in cols:
        if c not in df.columns:
            df[c] = ""
    return df[cols].reset_index(drop=True)


def stream_color_map(streams_df: pd.DataFrame) -> dict[str, str]:
    return {r["Stream"]: r["Color"] for _, r in streams_df.iterrows()}


# =========================================================
# JS editors/renderers
# =========================================================
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
    this.input.value = params.value || '#4F81BD';
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
    const color = params.value || '#4F81BD';
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


# =========================================================
# Seed if nothing loaded (generic demo data)
# =========================================================
def seed_data():
    tasks = pd.DataFrame([
        {"Stream": "Project A", "Task": "Kickoff & planning", "Start": "2025-11-01", "End": "2025-11-15", "Progress_pct": 20, "Notes": ""},
        {"Stream": "Project A", "Task": "Prototype",          "Start": "2025-11-16", "End": "2025-12-20", "Progress_pct": 45, "Notes": ""},
        {"Stream": "Project B", "Task": "Requirements",       "Start": "2025-11-10", "End": "2025-12-05", "Progress_pct": 10, "Notes": ""},
        {"Stream": "Project B", "Task": "Milestone: Review",  "Start": "2026-01-15", "End": "2026-01-15", "Progress_pct": 0,  "Notes": "0-day milestone"},
    ])

    streams = pd.DataFrame(columns=["Stream", "Color"])  # Colors auto-generated later
    streams = normalize_streams(tasks, streams)
    tasks = normalize_tasks(tasks, streams)
    return tasks, streams


if "tasks" not in st.session_state or "streams" not in st.session_state:
    st.session_state.tasks, st.session_state.streams = seed_data()

# Keep normalized
st.session_state.streams = normalize_streams(st.session_state.tasks, st.session_state.streams)
st.session_state.tasks = normalize_tasks(st.session_state.tasks, st.session_state.streams)

# =========================================================
# Sidebar IO
# =========================================================
with st.sidebar:
    st.header("Excel-driven data")

    uploaded = st.file_uploader("Load plan from Excel (.xlsx)", type=["xlsx"])
    if uploaded is not None:
        try:
            tasks_raw, streams_raw = load_from_excel(uploaded)
            streams_norm = normalize_streams(tasks_raw, streams_raw)
            tasks_norm = normalize_tasks(tasks_raw, streams_norm)
            st.session_state.tasks = tasks_norm
            st.session_state.streams = streams_norm
            st.success("Loaded Excel and rebuilt streams + colors.")
            st.rerun()
        except Exception as e:
            st.error(f"Failed to load Excel: {e}")

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

# =========================================================
# Filters (colored toggles based on Streams table)
# =========================================================
streams_df = st.session_state.streams.copy()
cmap = stream_color_map(streams_df)

st.markdown("### Filter by project / stream")
cols = st.columns(max(1, len(streams_df)))
selected_streams = []
for col, (_, r) in zip(cols, streams_df.iterrows()):
    s = r["Stream"]
    c = r["Color"]
    with col:
        on = st.toggle(s, value=True, key=f"filter_{s}")
        st.markdown(
            f"""<div style="height:4px;background:{c};border-radius:2px;margin-top:-6px;"></div>""",
            unsafe_allow_html=True,
        )
        if on:
            selected_streams.append(s)

stream_filter = selected_streams if selected_streams else streams_df["Stream"].tolist()

# Timeline controls
c1, c2 = st.columns([1, 1])
with c1:
    timeline_start = st.date_input("Timeline start", date(2025, 11, 1))
with c2:
    timeline_end = st.date_input("Timeline end", date(2026, 8, 31))

tab_table, tab_chart = st.tabs(["📋 Table (Streams + Tasks)", "📈 Chart"])

# =========================================================
# TAB: TABLES
# =========================================================
with tab_table:
    st.subheader("Streams (edit stream colors)")

    gbS = GridOptionsBuilder.from_dataframe(streams_df)
    gbS.configure_grid_options(
        singleClickEdit=True,
        stopEditingWhenCellsLoseFocus=True,
        rowHeight=32,
        headerHeight=34,
    )
    gbS.configure_column("Stream", editable=True, width=260)
    gbS.configure_column("Color", editable=True, cellEditor=color_editor, cellRenderer=color_renderer, width=200)

    gridS = AgGrid(
        streams_df,
        gridOptions=gbS.build(),
        update_on=["cellValueChanged"],
        allow_unsafe_jscode=True,
        theme="alpine",
        height=220,
        fit_columns_on_grid_load=True,
    )

    streams_edited = pd.DataFrame(gridS["data"]).copy()
    streams_edited["Stream"] = streams_edited["Stream"].astype(str).str.strip()
    streams_edited["Color"] = streams_edited["Color"].astype(str).str.strip()
    streams_edited = streams_edited[streams_edited["Stream"].ne("")].copy()

    streams_norm = normalize_streams(st.session_state.tasks, streams_edited)

    if not streams_norm.equals(st.session_state.streams):
        st.session_state.streams = streams_norm
        st.rerun()

    st.divider()
    st.subheader("Tasks (editable)")
    st.caption("Start/End: calendar editor. Invalid End<Start is highlighted. Milestones are Start==End.")

    if st.button("➕ Add new task"):
        new_task = pd.DataFrame([{
            "ID": uid(),
            "Stream": streams_norm["Stream"].iloc[0] if len(streams_norm) else "Stream 1",
            "Task": "New task",
            "Start": date.today().strftime("%Y-%m-%d"),
            "End": date.today().strftime("%Y-%m-%d"),
            "Progress_pct": 0,
            "Notes": "",
        }])
        tasks_new = pd.concat([
            st.session_state.tasks.drop(columns=["Duration_days", "_valid", "_validation_msg"], errors="ignore"),
            new_task
        ], ignore_index=True)
        st.session_state.tasks = normalize_tasks(tasks_new, st.session_state.streams)
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
        editable=True,
        cellEditor="agSelectCellEditor",
        cellEditorParams={"values": streams_norm["Stream"].tolist()},
        width=180,
    )
    gbT.configure_column("Task", editable=True, width=520)
    gbT.configure_column("Start", headerName="Start 📅", editable=True, cellEditor=date_editor, width=160)
    gbT.configure_column(
        "End",
        headerName="End 📅",
        editable=True,
        cellEditor=date_editor,
        width=160,
        cellStyle=end_cell_style_invalid,
        tooltipValueGetter=end_tooltip_invalid,
    )
    gbT.configure_column("Duration_days", editable=False, width=130)
    gbT.configure_column("Progress_pct", headerName="Progress %", editable=True, width=130)
    gbT.configure_column("Notes", editable=True, width=320)

    gridT = AgGrid(
        tasks_view,
        gridOptions=gbT.build(),
        update_on=["cellValueChanged"],
        allow_unsafe_jscode=True,
        theme="alpine",
        height=520,
        fit_columns_on_grid_load=True,
    )

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
        st.rerun()

    invalid_count = int((~st.session_state.tasks["_valid"]).sum())
    if invalid_count > 0:
        st.error(f"{invalid_count} row(s) have invalid dates (End < Start). Fix them in the table (End cell highlighted).")

# =========================================================
# TAB: CHART
# =========================================================
with tab_chart:
    st.subheader("Gantt chart")
    st.caption("Colors are per stream. Diamonds = milestones (Start==End). Timeline is on top. Red line = today.")

    tasks = st.session_state.tasks.copy()
    streams = st.session_state.streams.copy()
    cmap = stream_color_map(streams)

    tasks = tasks[tasks["Stream"].isin(stream_filter)].copy()

    tasks["Start_dt"] = pd.to_datetime(tasks["Start"], errors="coerce")
    tasks["End_dt"] = pd.to_datetime(tasks["End"], errors="coerce")

    t0 = pd.Timestamp(timeline_start)
    t1 = pd.Timestamp(timeline_end)

    tasks = tasks[tasks["Start_dt"].notna() & tasks["End_dt"].notna()].copy()
    tasks = tasks[(tasks["End_dt"] >= t0) & (tasks["Start_dt"] <= t1)].copy()

    valid_mask = tasks["_valid"] == True
    milestone_mask = valid_mask & (tasks["Start_dt"] == tasks["End_dt"])
    task_mask = valid_mask & (tasks["Start_dt"] < tasks["End_dt"])

    df_tasks = tasks[task_mask].copy()
    df_milestones = tasks[milestone_mask].copy()

    if len(df_tasks) > 0:
        fig = px.timeline(
            df_tasks,
            x_start="Start_dt",
            x_end="End_dt",
            y="Task",
            color="Stream",
            color_discrete_map=cmap,
            hover_data=["Stream", "Start", "End", "Duration_days", "Progress_pct", "Notes"],
        )
    else:
        fig = go.Figure()

    if len(df_milestones) > 0:
        fig.add_trace(
            go.Scatter(
                x=df_milestones["Start_dt"],
                y=df_milestones["Task"],
                mode="markers",
                marker=dict(
                    symbol="diamond",
                    size=12,
                    color=[cmap.get(s, "#4F81BD") for s in df_milestones["Stream"]],
                    line=dict(width=1, color="rgba(0,0,0,0.35)"),
                ),
                hovertemplate=(
                    "<b>%{y}</b><br>"
                    "Date: %{x|%Y-%m-%d}<br>"
                    "Stream: %{customdata[0]}<br>"
                    "Notes: %{customdata[1]}<extra></extra>"
                ),
                customdata=df_milestones[["Stream", "Notes"]].values,
                showlegend=False,
                name="Milestone",
            )
        )

    fig.update_xaxes(
        side="top",
        range=[t0, t1],
        dtick="M1",
        tickformat="%b\n%Y",
    )
    fig.update_yaxes(autorange="reversed")

    fig.update_layout(
        height=900,
        margin=dict(l=10, r=10, t=70, b=10),
        legend_title_text="Stream",
    )

    today = pd.Timestamp(date.today())
    if t0 <= today <= t1:
        fig.add_vline(x=today, line_color="red", line_width=3)

    st.plotly_chart(fig, width="stretch")

# gantt_chart.py
# ------------------------------------------------------------
# v17 - Correct scaling + progress shading overlay + rounded bars (if supported)
#      + weekend shading + milestones 🚩
#      + LEGEND FIX:
#         - Legend shows STREAM COLORS (dark)
#         - Legend does NOT overlap bars (placed above plot with more top margin)
#         - Legend uses separate legend-only traces (so bars remain clean)
# ------------------------------------------------------------

from __future__ import annotations

from typing import Dict, Optional, Tuple

import pandas as pd
import plotly.graph_objects as go


__VERSION__ = "v17-legend-stream-colors-no-overlap"


def _safe_dt(x) -> Optional[pd.Timestamp]:
    if x is None:
        return None
    try:
        t = pd.to_datetime(x)
        if pd.isna(t):
            return None
        return pd.Timestamp(t).normalize()
    except Exception:
        return None


def _hex_to_rgb(hex_color: str) -> Tuple[int, int, int]:
    s = (hex_color or "").strip()
    if not s:
        return (79, 129, 189)
    if s.startswith("#"):
        s = s[1:]
    if len(s) == 3:
        s = "".join([c * 2 for c in s])
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except Exception:
        return (79, 129, 189)


def _rgb_to_hex(rgb: Tuple[int, int, int]) -> str:
    r, g, b = rgb
    return f"#{max(0,min(255,r)):02x}{max(0,min(255,g)):02x}{max(0,min(255,b)):02x}"


def _lighten(hex_color: str, amount: float = 0.55) -> str:
    """
    Mix color with white. amount=0 -> original, amount=1 -> white
    """
    r, g, b = _hex_to_rgb(hex_color)
    r2 = int(r + (255 - r) * amount)
    g2 = int(g + (255 - g) * amount)
    b2 = int(b + (255 - b) * amount)
    return _rgb_to_hex((r2, g2, b2))


def _contrast_text_color(hex_color: str) -> str:
    r, g, b = _hex_to_rgb(hex_color)
    luminance = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0
    return "#111827" if luminance > 0.60 else "#FFFFFF"


def _coalesce_progress(df: pd.DataFrame) -> pd.Series:
    if "Progress" in df.columns:
        s = pd.to_numeric(df["Progress"], errors="coerce")
    elif "Progress_pct" in df.columns:
        s = pd.to_numeric(df["Progress_pct"], errors="coerce")
    else:
        s = pd.Series([0.0] * len(df), index=df.index, dtype=float)
    return s.fillna(0).clip(0, 100).astype(float)


def _dynamic_left_margin_px(task_labels: pd.Series) -> int:
    try:
        max_len = int(task_labels.astype(str).map(len).max())
    except Exception:
        max_len = 30
    m = int(150 + max_len * 6.3)
    return max(200, min(480, m))


def _add_weekend_shading(fig: go.Figure, x0: pd.Timestamp, x1: pd.Timestamp) -> None:
    """
    Shade weekends as ONE block (Sat->Mon) to avoid stripy look.
    """
    if x0 is None or x1 is None:
        return
    day = pd.Timestamp(x0).normalize()
    end = pd.Timestamp(x1).normalize()
    while day <= end:
        if day.weekday() == 5:  # Saturday
            fig.add_vrect(
                x0=day,
                x1=day + pd.Timedelta(days=2),
                fillcolor="rgba(15, 23, 42, 0.025)",
                line_width=0,
                layer="below",
            )
            day += pd.Timedelta(days=2)
        else:
            day += pd.Timedelta(days=1)


def build_gantt_figure(
    tasks: pd.DataFrame,
    stream_color_map: Dict[str, str],
    timeline_start: pd.Timestamp,
    timeline_end: pd.Timestamp,
    show_progress_labels: Optional[bool] = None,
    show_progress_text: Optional[bool] = None,
    show_today_line: bool = False,  # app draws today line itself
    today: Optional[pd.Timestamp] = None,
    show_milestone_vlines: bool = False,
    **_ignored_kwargs,
) -> go.Figure:
    df = tasks.copy()

    if show_progress_labels is None and show_progress_text is not None:
        show_progress_labels = bool(show_progress_text)
    if show_progress_labels is None:
        show_progress_labels = True

    if "Task" not in df.columns:
        df["Task"] = ""
    if "Stream" not in df.columns:
        df["Stream"] = "Default"

    df["Start_dt"] = df["Start"].apply(_safe_dt) if "Start" in df.columns else None
    df["End_dt"] = df["End"].apply(_safe_dt) if "End" in df.columns else None
    df["Progress"] = _coalesce_progress(df)

    # Stable table order
    df = df.reset_index(drop=True)
    df["_ykey"] = df["Task"].astype(str) + "  " + df.index.astype(str)

    # ---- Milestones auto-detection ----
    if "Duration_days" in df.columns:
        dur0 = (
            pd.to_numeric(df["Duration_days"], errors="coerce")
            .fillna(0)
            .astype(int)
            .eq(0)
        )
    else:
        dur0 = df["Start_dt"].eq(df["End_dt"])

    name_hint = df["Task"].astype(str).str.contains("milestone", case=False, na=False)

    if "Type" in df.columns:
        type_norm = df["Type"].astype(str).str.strip().str.lower()
        is_ms = type_norm.eq("milestone") | dur0 | name_hint
    else:
        is_ms = dur0 | name_hint

    df_tasks = df[~is_ms].copy()
    df_ms = df[is_ms].copy()

    # ---- Axis order ----
    y_order = df["_ykey"].tolist()
    y_ticktext = df["Task"].astype(str).tolist()

    # ---- X range ----
    x0 = pd.Timestamp(timeline_start).normalize()
    x1 = pd.Timestamp(timeline_end).normalize()
    try:
        all_starts = pd.to_datetime(df["Start_dt"], errors="coerce")
        all_ends = pd.to_datetime(df["End_dt"], errors="coerce")
        min_dt = pd.concat([all_starts, all_ends]).min()
        max_dt = pd.concat([all_starts, all_ends]).max()
        if pd.notna(min_dt):
            x0 = min(x0, pd.Timestamp(min_dt).normalize())
        if pd.notna(max_dt):
            x1 = max(x1, pd.Timestamp(max_dt).normalize())
    except Exception:
        pass

    fig = go.Figure()

    # Weekend shading behind everything
    _add_weekend_shading(fig, x0, x1)

    # ---- Build Gantt bars per Stream ----
    # Full bar = light (planned)
    # Completed overlay = dark (done portion)
    df_plot = df_tasks.dropna(subset=["Start_dt", "End_dt"]).copy()
    if not df_plot.empty:
        df_plot["dur_ms"] = (df_plot["End_dt"] - df_plot["Start_dt"]).dt.total_seconds() * 1000.0
        df_plot["done_ms"] = df_plot["dur_ms"] * (df_plot["Progress"].astype(float) / 100.0)

        for stream, g in df_plot.groupby("Stream", dropna=False):
            stream = str(stream) if stream is not None else "Default"
            c = (stream_color_map or {}).get(stream, "#4F81BD")
            c_light = _lighten(c, 0.60)

            # Planned (legend OFF)
            fig.add_trace(
                go.Bar(
                    x=g["dur_ms"],
                    base=g["Start_dt"],
                    y=g["_ykey"],
                    orientation="h",
                    name=stream,
                    marker=dict(color=c_light),
                    hovertemplate=(
                        "<b>%{customdata[0]}</b><br>"
                        "Stream: %{customdata[1]}<br>"
                        "Start: %{customdata[2]}<br>"
                        "End: %{customdata[3]}<br>"
                        "Progress: %{customdata[4]:.0f}%<extra></extra>"
                    ),
                    customdata=list(
                        zip(
                            g["Task"].astype(str),
                            g["Stream"].astype(str),
                            g["Start_dt"].dt.strftime("%Y-%m-%d"),
                            g["End_dt"].dt.strftime("%Y-%m-%d"),
                            g["Progress"].astype(float),
                        )
                    ),
                    width=0.78,
                    showlegend=False,
                )
            )

            # Done overlay (legend OFF — legend will be handled separately)
            g_done = g[g["done_ms"] > 0].copy()
            if not g_done.empty:
                fig.add_trace(
                    go.Bar(
                        x=g_done["done_ms"],
                        base=g_done["Start_dt"],
                        y=g_done["_ykey"],
                        orientation="h",
                        name=stream,
                        marker=dict(color=c),
                        hoverinfo="skip",
                        showlegend=False,
                        width=0.78,
                    )
                )

        # Rounded corners (graceful fallback if unsupported)
        fig.update_traces(marker_cornerradius=10, selector=dict(type="bar"))

    # ---- Legend-only traces (STREAM COLORS) ----
    # We add one legend entry per stream using a trace that is legendonly (never drawn).
    if not df_plot.empty:
        # Keep legend order consistent with appearance
        ordered_streams = list(pd.unique(df_plot["Stream"].astype(str)))
        if not ordered_streams:
            ordered_streams = []

        for stream in ordered_streams:
            c = (stream_color_map or {}).get(stream, "#4F81BD")
            fig.add_trace(
                go.Scatter(
                    x=[x0],
                    y=[y_order[0] if y_order else ""],
                    mode="markers",
                    marker=dict(size=10, color=c, symbol="square"),
                    name=stream,
                    hoverinfo="skip",
                    showlegend=True,
                    visible="legendonly",  # ✅ appears only in legend, not on chart
                )
            )

    # ---- Milestones (🚩) ----
    if not df_ms.empty:
        ms_x = df_ms["Start_dt"].fillna(df_ms["End_dt"])
        ms_y = df_ms["_ykey"]
        ms_stream = df_ms["Stream"].astype(str).fillna("Default")
        ms_colors = [stream_color_map.get(s, "#4F81BD") for s in ms_stream]

        if show_milestone_vlines:
            for x, c in zip(ms_x, ms_colors):
                if pd.isna(x):
                    continue
                fig.add_vline(
                    x=x,
                    line_width=2.0,
                    line_dash="dot",
                    line_color=(c or "#666"),
                    layer="above",
                    opacity=0.45,
                )

        fig.add_trace(
            go.Scatter(
                x=ms_x,
                y=ms_y,
                mode="text",
                text=["🚩"] * len(df_ms),
                textposition="middle center",
                textfont=dict(size=18, color=ms_colors),
                hoverinfo="text",
                hovertext=[
                    f"<b>{t}</b><br><b>Milestone</b><br>Stream: {s}<br>Date: {x.date() if pd.notna(x) else ''}"
                    for t, s, x in zip(df_ms["Task"], ms_stream, ms_x)
                ],
                showlegend=False,
                cliponaxis=False,
            )
        )

    # ---- Progress labels INSIDE bars ----
    if show_progress_labels and not df_plot.empty:
        annotations = []
        for _, row in df_plot.iterrows():
            xs = row["Start_dt"]
            xe = row["End_dt"]
            if pd.isna(xs) or pd.isna(xe) or xe <= xs:
                continue

            # Skip very short tasks to avoid ugly overlaps
            if (xe - xs) < pd.Timedelta(days=14):
                continue

            xm = xs + (xe - xs) / 2
            prog_val = float(row.get("Progress", 0.0))
            stream = str(row.get("Stream") or "Default")
            c = (stream_color_map or {}).get(stream, "#4F81BD")
            txt = _contrast_text_color(c)

            annotations.append(
                dict(
                    x=xm,
                    y=row["_ykey"],
                    xref="x",
                    yref="y",
                    text=f"{prog_val:.0f}%",
                    showarrow=False,
                    xanchor="center",
                    yanchor="middle",
                    font=dict(color=txt, size=12),
                    bgcolor="rgba(255,255,255,0.0)",
                )
            )
        fig.update_layout(annotations=annotations)

    # ---- Today line ----
    if show_today_line:
        t = pd.Timestamp(today).normalize() if today is not None else pd.Timestamp.today().normalize()
        fig.add_vline(x=t, line_width=2, line_color="#DC2626", layer="above", opacity=0.75)

    # ---- Axes + layout ----
    fig.update_yaxes(
        title_text="",
        categoryorder="array",
        categoryarray=y_order,
        autorange="reversed",
        tickmode="array",
        tickvals=y_order,
        ticktext=y_ticktext,
        showgrid=False,
        zeroline=False,
        showline=False,
        ticks="",
        automargin=True,
    )

    fig.update_xaxes(
        range=[x0, x1],
        type="date",
        showgrid=False,
        zeroline=False,
        showline=False,
        ticks="outside",
        tickcolor="rgba(15, 23, 42, 0.35)",
        automargin=True,
    )

    # Height heuristic
    try:
        n_rows = max(1, len(y_order))
        fig.update_layout(height=140 + 36 * n_rows)
    except Exception:
        pass

    left_margin = _dynamic_left_margin_px(df["Task"])

    # Legend ABOVE plot + extra top margin so it never overlaps bars
    fig.update_layout(
        barmode="overlay",
        margin=dict(l=left_margin, r=24, t=95, b=28),  # ✅ more top space
        hovermode="closest",
        paper_bgcolor="white",
        plot_bgcolor="white",
        font=dict(
            family="Inter, Segoe UI, Roboto, Arial, sans-serif",
            size=13,
            color="rgba(15, 23, 42, 0.92)",
        ),
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.12,   # ✅ above plotting area
            xanchor="right",
            x=1.0,
            bgcolor="rgba(255,255,255,0.95)",
            borderwidth=0,
            title_text="",
            itemsizing="constant",
        ),
    )

    return fig

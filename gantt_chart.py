import pandas as pd
import plotly.express as px
import plotly.graph_objects as go


def build_gantt_figure(tasks_df: pd.DataFrame, stream_color_map: dict, t0, t1, show_progress_text: bool = True):
    df = tasks_df.copy()
    df["Start_dt"] = pd.to_datetime(df["Start"], errors="coerce")
    df["End_dt"] = pd.to_datetime(df["End"], errors="coerce")
    df = df[df["Start_dt"].notna() & df["End_dt"].notna()].copy()

    # Clip to timeline window
    df = df[(df["End_dt"] >= t0) & (df["Start_dt"] <= t1)].copy()

    valid = df["_valid"] == True
    milestones = valid & (df["Start_dt"] == df["End_dt"])
    bars = valid & (df["Start_dt"] < df["End_dt"])

    df_bars = df[bars].copy()
    df_ms = df[milestones].copy()

    if len(df_bars) > 0:
        fig = px.timeline(
            df_bars,
            x_start="Start_dt",
            x_end="End_dt",
            y="Task",
            color="Stream",
            color_discrete_map=stream_color_map,
            hover_data=["Stream", "Start", "End", "Duration_days", "Progress_pct", "Notes"],
        )
    else:
        fig = go.Figure()

    # Milestone diamonds
    if len(df_ms) > 0:
        fig.add_trace(
            go.Scatter(
                x=df_ms["Start_dt"],
                y=df_ms["Task"],
                mode="markers",
                marker=dict(
                    symbol="diamond",
                    size=12,
                    color=[stream_color_map.get(s, "#4F81BD") for s in df_ms["Stream"]],
                    line=dict(width=1, color="rgba(0,0,0,0.35)"),
                ),
                hovertemplate=(
                    "<b>%{y}</b><br>"
                    "Date: %{x|%Y-%m-%d}<br>"
                    "Stream: %{customdata[0]}<br>"
                    "Notes: %{customdata[1]}<extra></extra>"
                ),
                customdata=df_ms[["Stream", "Notes"]].values,
                showlegend=False,
                name="Milestone",
            )
        )

    # Progress text on bars (midpoint labels)
    if show_progress_text and len(df_bars) > 0:
        mid = df_bars["Start_dt"] + (df_bars["End_dt"] - df_bars["Start_dt"]) / 2
        fig.add_trace(
            go.Scatter(
                x=mid,
                y=df_bars["Task"],
                mode="text",
                text=[f'{int(round(p))}%' for p in df_bars["Progress_pct"]],
                textposition="middle center",
                showlegend=False,
                hoverinfo="skip",
            )
        )

    # Timeline at top, monthly ticks
    fig.update_xaxes(side="top", range=[t0, t1], dtick="M1", tickformat="%b\n%Y")
    fig.update_yaxes(autorange="reversed")

    fig.update_layout(
        height=900,
        margin=dict(l=10, r=10, t=70, b=10),
        legend_title_text="Stream",
    )
    return fig

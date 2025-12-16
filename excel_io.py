import pandas as pd
from datetime import date
from io import BytesIO

TASKS_SHEET = "Tasks"
STREAMS_SHEET = "Streams"

TASKS_COLUMNS = ["Stream", "Task", "Start", "End", "Progress_pct", "Notes"]
STREAMS_COLUMNS = ["Stream", "Color"]


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


def load_from_excel_bytes(file_bytes: bytes) -> tuple[pd.DataFrame, pd.DataFrame]:
    xls = pd.ExcelFile(BytesIO(file_bytes))

    if TASKS_SHEET not in xls.sheet_names:
        raise ValueError(f"Excel must contain a sheet named '{TASKS_SHEET}'.")

    tasks = pd.read_excel(xls, sheet_name=TASKS_SHEET)
    tasks.columns = [str(c).strip() for c in tasks.columns]

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

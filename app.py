from __future__ import annotations

import json
import html
from io import BytesIO
import os
import re
import time
from collections import Counter
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=False)
RESULTS_DIR = BASE_DIR / "github-real-results"
REQUIRED_FILES = {
    "summary": RESULTS_DIR / "summary.json",
    "event_types": RESULTS_DIR / "event_types.csv",
    "repositories": RESULTS_DIR / "repositories.csv",
    "developers": RESULTS_DIR / "developers.csv",
    "daily_activity": RESULTS_DIR / "daily_activity.csv",
    "activity_heatmap": RESULTS_DIR / "activity_heatmap.csv",
    "insights": RESULTS_DIR / "insights.txt",
}
CSV_COLUMNS = {
    "event_types": {"event_type", "count"},
    "repositories": {"repository", "count"},
    "developers": {"developer", "count"},
    "daily_activity": {"date", "count"},
    "activity_heatmap": {"date", "hour", "count"},
}
COLORS = {
    "background": "#0d1117",
    "card": "#161b22",
    "border": "#30363d",
    "text": "#f0f6fc",
    "muted": "#8b949e",
    "green": "#2da44e",
    "blue": "#58a6ff",
    "orange": "#d29922",
    "red": "#f85149",
}
PLOTLY_CONFIG = {"displaylogo": False, "responsive": True}
DATASET_ID = "current"
GITHUB_EVENTS_URL = "https://api.github.com/events?per_page=100&page=1"
LIVE_EVENT_LIMIT = 1000
LIVE_EVENT_DATASET_ID = "live_public"


st.set_page_config(
    page_title="GitHub Public Activity Analytics",
    page_icon="◉",
    layout="wide",
    initial_sidebar_state="expanded",
)


def apply_theme() -> None:
    """Apply the dashboard's GitHub-inspired visual theme."""
    st.markdown(
        f"""
        <style>
        :root {{ color-scheme: dark; }}
        .stApp {{ background: {COLORS['background']}; color: {COLORS['text']}; }}
        [data-testid="stSidebar"] {{ background: #010409; border-right: 1px solid {COLORS['border']}; }}
        [data-testid="stHeader"] {{ background: rgba(13, 17, 23, 0.88); }}
        [data-testid="stMetric"] {{ background: {COLORS['card']}; border: 1px solid {COLORS['border']};
            padding: 1rem 1.1rem; border-radius: 12px; min-height: 112px; }}
        [data-testid="stMetricLabel"] p {{ color: {COLORS['muted']}; font-size: .86rem; }}
        [data-testid="stMetricValue"] {{ color: {COLORS['text']}; font-size: clamp(1.25rem, 2vw, 1.85rem); }}
        div[data-testid="stVerticalBlockBorderWrapper"] {{ border-radius: 12px; }}
        .hero {{ padding: 1.25rem 0 .7rem; }}
        .hero h1 {{ margin: 0 0 .35rem; letter-spacing: -.035em; font-size: clamp(1.8rem, 4vw, 2.55rem); }}
        .hero p {{ color: {COLORS['muted']}; font-size: 1.05rem; margin: 0; }}
        .eyebrow {{ color: {COLORS['green']}; font-weight: 700; letter-spacing: .09em; text-transform: uppercase; font-size: .76rem; }}
        .status-chip {{ display:inline-block; padding:.35rem .65rem; border:1px solid {COLORS['border']};
            border-radius:999px; background:{COLORS['card']}; color:{COLORS['text']}; font-size:.84rem; }}
        .muted {{ color:{COLORS['muted']}; }}
        .insight-card {{ background:{COLORS['card']}; border:1px solid {COLORS['border']}; border-radius:12px;
            padding:1rem 1.1rem; margin:.45rem 0; line-height:1.55; }}
        .section-title {{ padding-top:.5rem; }}
        a {{ color:{COLORS['blue']} !important; }}
        @media (max-width: 700px) {{ .block-container {{ padding-left:1rem; padding-right:1rem; }} }}
        </style>
        """,
        unsafe_allow_html=True,
    )


@st.cache_data(show_spinner=False)
def load_csv(path_text: str, modified_ns: int) -> pd.DataFrame:
    """Load and validate a result CSV, retaining the source's values."""
    del modified_ns  # Included in the cache key so refreshed files are re-read.
    path = Path(path_text)
    if not path.is_file():
        raise FileNotFoundError(f"Missing required file: {path}")
    try:
        frame = pd.read_csv(path)
    except pd.errors.EmptyDataError as exc:
        raise ValueError(f"{path.name} is empty and has no column header.") from exc
    except (OSError, UnicodeError, pd.errors.ParserError) as exc:
        raise ValueError(f"Could not read {path.name}: {exc}") from exc
    if frame.empty and len(frame.columns) == 0:
        raise ValueError(f"{path.name} is empty and has no column header.")
    return frame


@st.cache_data(show_spinner=False)
def load_summary(path_text: str, modified_ns: int) -> dict[str, Any]:
    del modified_ns
    path = Path(path_text)
    if not path.is_file():
        raise FileNotFoundError(f"Missing required file: {path}")
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must contain a JSON object.")
    return data


@st.cache_data(show_spinner=False)
def load_insights(path_text: str, modified_ns: int) -> str:
    del modified_ns
    path = Path(path_text)
    if not path.is_file():
        raise FileNotFoundError(f"Missing required file: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"Could not read {path.name}: {exc}") from exc


def file_version(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def load_file_results() -> tuple[dict[str, Any], dict[str, pd.DataFrame], str, list[str]]:
    """Load all required result files and return actionable validation errors."""
    errors: list[str] = []
    summary: dict[str, Any] = {}
    frames: dict[str, pd.DataFrame] = {}
    insights = ""
    for key, path in REQUIRED_FILES.items():
        if not path.is_file():
            errors.append(f"Missing required file: `{path}`. Restore it from the PySpark results output.")
    if errors:
        return summary, frames, insights, errors

    try:
        summary = load_summary(str(REQUIRED_FILES["summary"]), file_version(REQUIRED_FILES["summary"]))
    except (ValueError, FileNotFoundError) as exc:
        errors.append(str(exc))
    try:
        insights = load_insights(str(REQUIRED_FILES["insights"]), file_version(REQUIRED_FILES["insights"]))
    except (ValueError, FileNotFoundError) as exc:
        errors.append(str(exc))
    for key in CSV_COLUMNS:
        path = REQUIRED_FILES[key]
        try:
            frame = load_csv(str(path), file_version(path))
            missing = CSV_COLUMNS[key] - set(frame.columns)
            if missing:
                errors.append(
                    f"Invalid `{path.name}`: missing columns {', '.join(sorted(missing))}. "
                    "Regenerate the result files with the project notebook."
                )
            else:
                frames[key] = frame
        except (ValueError, FileNotFoundError) as exc:
            errors.append(str(exc))
    return summary, frames, insights, errors


def cassandra_config() -> dict[str, Any]:
    """Read optional Cassandra settings from environment or Streamlit secrets."""
    try:
        secrets = st.secrets.get("cassandra", {})
    except Exception:
        secrets = {}

    def setting(env_name: str, secret_name: str, default: Any = None) -> Any:
        return os.getenv(env_name) or secrets.get(secret_name, default)

    contact_points = setting("CASSANDRA_CONTACT_POINTS", "contact_points", "")
    if isinstance(contact_points, str):
        contact_points = [host.strip() for host in contact_points.split(",") if host.strip()]
    elif not contact_points:
        contact_points = []
    try:
        port = int(setting("CASSANDRA_PORT", "port", 9042))
    except (TypeError, ValueError):
        port = None
    return {
        "contact_points": contact_points,
        "port": port,
        "keyspace": setting("CASSANDRA_KEYSPACE", "keyspace", "github_analytics"),
        "username": setting("CASSANDRA_USERNAME", "username"),
        "password": setting("CASSANDRA_PASSWORD", "password"),
        "secure_connect_bundle": setting("CASSANDRA_SECURE_CONNECT_BUNDLE", "secure_connect_bundle"),
    }


@st.cache_resource(show_spinner=False)
def cassandra_session(
    contact_points: tuple[str, ...],
    port: int,
    keyspace: str,
    username: str | None,
    password: str | None,
    secure_connect_bundle: str | None,
) -> Any:
    """Create one reusable Cassandra session for the Streamlit process."""
    from cassandra.auth import PlainTextAuthProvider
    from cassandra.cluster import Cluster

    auth_provider = PlainTextAuthProvider(username=username, password=password) if username and password else None
    if secure_connect_bundle:
        cluster = Cluster(
            cloud={"secure_connect_bundle": secure_connect_bundle},
            auth_provider=auth_provider,
        )
    else:
        cluster = Cluster(
            list(contact_points),
            port=port,
            auth_provider=auth_provider,
        )
    return cluster.connect(keyspace)


@st.cache_data(ttl=60, show_spinner=False)
def load_cassandra_results(
    contact_points: tuple[str, ...],
    port: int,
    keyspace: str,
    username: str | None,
    password: str | None,
    secure_connect_bundle: str | None,
) -> tuple[dict[str, Any], dict[str, pd.DataFrame], str]:
    """Read the dashboard's query-oriented aggregate tables from Cassandra."""
    try:
        session = cassandra_session(contact_points, port, keyspace, username, password, secure_connect_bundle)
    except Exception as exc:
        raise ValueError(
            f"Could not connect to Cassandra keyspace `{keyspace}`. Check the contact points, port, "
            f"credentials, and that the Cassandra service is running: {exc}"
        ) from exc
    try:
        events = session.execute(
            "SELECT event_type, event_count FROM event_types WHERE dataset_id = %s", (DATASET_ID,)
        )
        repositories = session.execute(
            "SELECT repository, event_count FROM repositories WHERE dataset_id = %s", (DATASET_ID,)
        )
        developers = session.execute(
            "SELECT developer, event_count FROM developers WHERE dataset_id = %s", (DATASET_ID,)
        )
        daily = session.execute(
            "SELECT activity_date, event_count FROM daily_activity WHERE dataset_id = %s", (DATASET_ID,)
        )
        heatmap = session.execute(
            "SELECT activity_date, hour, event_count FROM activity_heatmap WHERE dataset_id = %s", (DATASET_ID,)
        )
        summary_row = session.execute(
            "SELECT payload FROM summary WHERE dataset_id = %s", (DATASET_ID,)
        ).one()
        insights_row = session.execute(
            "SELECT content FROM insights WHERE dataset_id = %s", (DATASET_ID,)
        ).one()
    except Exception as exc:
        raise ValueError(
            f"Could not query keyspace `{keyspace}`. Confirm the tables from `cassandra_schema.cql` "
            f"exist and run `python load_results_to_cassandra.py`. Cassandra reported: {exc}"
        ) from exc

    if summary_row is None or insights_row is None:
        raise ValueError(
            "Cassandra is reachable, but the `current` summary or insights row is missing. "
            "Run `python load_results_to_cassandra.py` after applying the schema."
        )
    try:
        summary = json.loads(summary_row.payload)
        if not isinstance(summary, dict):
            raise ValueError("summary payload must be a JSON object")
    except (TypeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"Invalid JSON in Cassandra summary.payload: {exc}") from exc

    def count_frame(rows: Any, label: str) -> pd.DataFrame:
        return pd.DataFrame(
            [{label: getattr(row, label), "count": getattr(row, "event_count")} for row in rows],
            columns=[label, "count"],
        )

    frames = {
        "event_types": count_frame(events, "event_type"),
        "repositories": count_frame(repositories, "repository"),
        "developers": count_frame(developers, "developer"),
        "daily_activity": pd.DataFrame(
            [{"date": row.activity_date, "count": row.event_count} for row in daily],
            columns=["date", "count"],
        ),
        "activity_heatmap": pd.DataFrame(
            [{"date": row.activity_date, "hour": row.hour, "count": row.event_count} for row in heatmap],
            columns=["date", "hour", "count"],
        ),
    }
    empty_tables = [name for name, frame in frames.items() if frame.empty]
    if empty_tables:
        raise ValueError(
            f"Cassandra returned no rows for {', '.join(empty_tables)}. "
            "Run `python load_results_to_cassandra.py` after applying the schema."
        )
    for frame in frames.values():
        if "date" in frame.columns:
            frame["date"] = frame["date"].astype(str)
    return summary, frames, insights_row.content or ""


def format_event_name(value: Any) -> str:
    """Make event type identifiers readable while preserving raw labels in tooltips."""
    raw = str(value)
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", raw).strip()
    return spaced or raw


def format_number(value: Any) -> str:
    try:
        number = float(value)
        if not pd.notna(number):
            return "Not provided"
        if number.is_integer():
            return f"{int(number):,}"
        return f"{number:,.2f}"
    except (TypeError, ValueError):
        return "Not provided"


def numeric_column(frame: pd.DataFrame, column: str) -> pd.Series:
    return pd.to_numeric(frame[column], errors="coerce").fillna(0).clip(lower=0)


def normalize_counts(frame: pd.DataFrame, label_column: str) -> pd.DataFrame:
    result = frame[[label_column, "count"]].copy()
    result[label_column] = result[label_column].fillna("Unknown").astype(str)
    result["count"] = numeric_column(result, "count")
    return result.sort_values("count", ascending=False, kind="stable")


def chart_layout(fig: go.Figure, *, height: int = 360) -> go.Figure:
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor=COLORS["card"],
        plot_bgcolor=COLORS["card"],
        font={"color": COLORS["text"], "family": "Inter, Segoe UI, sans-serif"},
        margin={"l": 16, "r": 16, "t": 26, "b": 20},
        height=height,
        legend={"orientation": "h", "y": 1.08, "x": 0},
    )
    fig.update_xaxes(gridcolor=COLORS["border"], zerolinecolor=COLORS["border"])
    fig.update_yaxes(gridcolor=COLORS["border"], zerolinecolor=COLORS["border"])
    return fig


def event_figures(events: pd.DataFrame) -> tuple[go.Figure, go.Figure]:
    data = normalize_counts(events, "event_type")
    data["display_name"] = data["event_type"].map(format_event_name)
    bars = px.bar(
        data.sort_values("count", ascending=True),
        x="count",
        y="display_name",
        orientation="h",
        custom_data=["event_type"],
        labels={"count": "Events in sample", "display_name": "Event type", "event_type": "Original event type"},
        color_discrete_sequence=[COLORS["green"]],
    )
    bars.update_traces(hovertemplate="%{customdata[0]}<br>Events: %{x:,}<extra></extra>")
    bars = chart_layout(bars, height=max(350, min(620, 24 * len(data) + 100)))
    bars.update_layout(showlegend=False)
    donut = px.pie(
        data,
        names="display_name",
        values="count",
        hole=0.58,
        custom_data=["event_type"],
        color_discrete_sequence=px.colors.qualitative.Safe,
    )
    donut.update_traces(
        textinfo="percent",
        hovertemplate="%{customdata[0]}<br>Events: %{value:,}<br>Share: %{percent}<extra></extra>",
    )
    donut = chart_layout(donut, height=390)
    return bars, donut


def ranked_figure(data: pd.DataFrame, label_column: str, color: str) -> go.Figure:
    data = data.sort_values("count", ascending=True, kind="stable")
    fig = px.bar(
        data,
        x="count",
        y=label_column,
        orientation="h",
        labels={"count": "Events in sample", label_column: ""},
        color_discrete_sequence=[color],
        hover_data={label_column: True, "count": ":,", "is_bot": False} if "is_bot" in data else {label_column: True, "count": ":,"},
    )
    fig.update_traces(hovertemplate="%{y}<br>Events: %{x:,}<extra></extra>")
    fig.update_yaxes(automargin=True, tickfont={"size": 11})
    fig.update_layout(showlegend=False)
    return chart_layout(fig, height=max(340, min(580, 31 * len(data) + 85)))


def split_insights(text: str) -> dict[str, list[str]]:
    sections = {"Main findings": [], "Recommendations": [], "Limitations": []}
    active = "Main findings"
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lower = line.lower().rstrip(":")
        if lower in {"limitations", "recommendations", "main findings", "findings"}:
            active = "Main findings" if lower == "findings" else lower.title()
            continue
        line = re.sub(r"^[•\-*]\s*", "", line)
        if line.upper() == "ACTUAL PUBLIC GITHUB EVENTS (COLLECTED SAMPLE)":
            continue
        if line.lower().startswith("recommendation:"):
            sections["Recommendations"].append(line.split(":", 1)[1].strip())
        elif line.lower().startswith("limitations:"):
            sections["Limitations"].append(line.split(":", 1)[1].strip())
        else:
            sections.setdefault(active, []).append(line)
    return sections


def build_powerbi_package(
    summary: dict[str, Any], frames: dict[str, pd.DataFrame], insight_text: str
) -> bytes:
    """Bundle the currently selected source data for Power BI Desktop."""
    instructions = """GitHub Public Activity Analytics — Power BI import package

Contents
--------
event_types.csv, repositories.csv, developers.csv, daily_activity.csv,
activity_heatmap.csv, summary.json, and insights.txt are exported from the
currently selected dashboard data source. No data is synthesized or changed.

Import into Power BI Desktop
----------------------------
1. Extract this ZIP file to a folder.
2. In Power BI Desktop, choose Home > Get data > Text/CSV.
3. Import each CSV as a separate table. Import summary.json separately if
   you want its metadata in the model.
4. Choose Close & Apply, then save the report as a .pbix file.

Notes
-----
The source files are aggregates. Event-type filtering is only available in
that table; cross-filtering across event type, repository, and developer is
not possible from these source aggregates. Heatmap zeros represent no row in
this collected sample and do not mean no global GitHub activity occurred.
"""
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for key in ("event_types", "repositories", "developers", "daily_activity", "activity_heatmap"):
            archive.writestr(f"{key}.csv", frames[key].to_csv(index=False).encode("utf-8"))
        archive.writestr("summary.json", json.dumps(summary, indent=2, ensure_ascii=False).encode("utf-8"))
        archive.writestr("insights.txt", insight_text.encode("utf-8"))
        archive.writestr("POWER_BI_IMPORT.txt", instructions)
    return buffer.getvalue()


def show_insight_group(title: str, items: list[str], icon: str) -> None:
    with st.container(border=True):
        st.markdown(f"#### {icon} {title}")
        if items:
            for item in items:
                safe_item = html.escape(item)
                st.markdown(f'<div class="insight-card">{safe_item}</div>', unsafe_allow_html=True)
        else:
            st.caption("No entries provided in insights.txt.")


def fetch_live_public_events(etag: str | None) -> tuple[list[dict[str, Any]], str | None, int, bool]:
    """Fetch the latest public GitHub events, using ETags to avoid repeat payloads."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "GitHub-Public-Activity-Analytics",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if etag:
        headers["If-None-Match"] = etag

    request = Request(GITHUB_EVENTS_URL, headers=headers)
    try:
        with urlopen(request, timeout=20) as response:
            records = json.load(response)
            if not isinstance(records, list):
                raise RuntimeError("GitHub returned an unexpected response; expected a list of events.")
            interval = int(response.headers.get("X-Poll-Interval", "60"))
            return records, response.headers.get("ETag") or etag, max(60, interval), False
    except HTTPError as exc:
        interval = max(60, int(exc.headers.get("X-Poll-Interval", "60")))
        if exc.code == 304:
            return [], etag, interval, True
        if exc.code in (403, 429):
            reset_at = exc.headers.get("X-RateLimit-Reset")
            detail = f" Rate limit reset (Unix time): {reset_at}." if reset_at else ""
            raise RuntimeError(f"GitHub rate limited this session.{detail} Wait before polling again or configure GITHUB_TOKEN.") from None
        raise RuntimeError(f"GitHub Events API returned HTTP {exc.code}.") from None
    except URLError as exc:
        reason = getattr(exc, "reason", "network error")
        raise RuntimeError(f"Could not reach GitHub: {reason}") from None


def describe_live_event(event: dict[str, Any]) -> dict[str, Any]:
    """Select safe, readable fields from one public event payload."""
    event_type = str(event.get("type") or "UnknownEvent")
    payload = event.get("payload") or {}
    repository = (event.get("repo") or {}).get("name") or "Unknown repository"
    actor = (event.get("actor") or {}).get("login") or "Unknown actor"
    created_at = str(event.get("created_at") or "")
    detail = str(payload.get("action") or "")

    if event_type == "PushEvent":
        ref = payload.get("ref")
        if ref:
            detail = f"{detail + ' · ' if detail else ''}{ref}"
    elif event_type in {"PullRequestEvent", "IssuesEvent", "IssueCommentEvent", "PullRequestReviewEvent", "PullRequestReviewCommentEvent"}:
        item = payload.get("pull_request") or payload.get("issue") or {}
        number = item.get("number")
        title = item.get("title")
        parts = [part for part in (f"#{number}" if number else "", title or "") if part]
        if parts:
            detail = f"{detail + ' · ' if detail else ''}{' — '.join(parts)}"

    try:
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00")).astimezone(timezone.utc)
        created_at = parsed.strftime("%Y-%m-%d %H:%M:%S UTC")
        event_date = parsed.date()
        created_at_dt = parsed.replace(tzinfo=None)
    except ValueError:
        event_date = None
        created_at_dt = None
    return {
        "id": str(event.get("id") or ""),
        "time": created_at,
        "event_date": event_date,
        "created_at_dt": created_at_dt,
        "event_type": event_type,
        "repository": str(repository),
        "actor": str(actor),
        "detail": detail or "—",
        "payload": json.dumps(event, ensure_ascii=True, separators=(",", ":")),
    }


def persist_live_events(session: Any, events: list[dict[str, Any]]) -> None:
    """Upsert public events by ID into Cassandra; repeated API pages are idempotent."""
    statement = session.prepare(
        "INSERT INTO live_events_by_day "
        "(dataset_id, event_date, created_at, event_id, event_type, repository, actor, detail, payload, observed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    observed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    for event in events:
        row = describe_live_event(event)
        if not row["id"] or row["event_date"] is None or row["created_at_dt"] is None:
            continue
        session.execute(
            statement,
            (
                LIVE_EVENT_DATASET_ID,
                row["event_date"],
                row["created_at_dt"],
                row["id"],
                row["event_type"],
                row["repository"],
                row["actor"],
                row["detail"],
                row["payload"],
                observed_at,
            ),
        )


def load_persisted_live_events(session: Any, days: int = 7) -> list[dict[str, Any]]:
    """Load recent event partitions so live history survives a browser session."""
    statement = session.prepare(
        "SELECT event_id, created_at, event_type, repository, actor, detail "
        "FROM live_events_by_day WHERE dataset_id = ? AND event_date = ?"
    )
    today = datetime.now(timezone.utc).date()
    rows: list[dict[str, Any]] = []
    for offset in range(days):
        event_date = today - timedelta(days=offset)
        for record in session.execute(statement, (LIVE_EVENT_DATASET_ID, event_date)):
            created = record.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            else:
                created = created.astimezone(timezone.utc)
            rows.append({
                "id": record.event_id,
                "time": created.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "event_date": event_date,
                "created_at_dt": created.replace(tzinfo=None),
                "event_type": record.event_type or "UnknownEvent",
                "repository": record.repository or "Unknown repository",
                "actor": record.actor or "Unknown actor",
                "detail": record.detail or "—",
            })
    rows.sort(key=lambda row: row["created_at_dt"], reverse=True)
    return rows


def render_persistent_dashboard() -> None:
    """Read retained Cassandra events; collection runs separately in live_collector.py."""
    st.markdown(
        '<div class="hero"><div class="eyebrow">GitHub public events · live</div>'
        '<h1>GitHub Public Activity Analytics</h1>'
        '<p>New events are collected in the background and retained in Cassandra.</p></div>',
        unsafe_allow_html=True,
    )
    st.info("GitHub's public Events API is delayed and rate limited. This dashboard shows observed events, not a complete global feed.")
    config = cassandra_config()
    if not config["contact_points"] or config["port"] is None or not 1 <= config["port"] <= 65535:
        st.error("Configure CASSANDRA_CONTACT_POINTS and a valid CASSANDRA_PORT to read saved events.")
        return
    if bool(config["username"]) != bool(config["password"]):
        st.error("Set both CASSANDRA_USERNAME and CASSANDRA_PASSWORD, or leave both unset.")
        return
    try:
        session = cassandra_session(
            tuple(config["contact_points"]), config["port"], config["keyspace"],
            config["username"], config["password"], config["secure_connect_bundle"],
        )
        rows = load_persisted_live_events(session, days=30)
    except Exception as exc:
        st.error(f"Could not load stored events from Cassandra: {exc}")
        return
    if not rows:
        st.warning("No events have been collected yet. Start live_collector.py and refresh shortly.")
        return

    event_counts = Counter(row["event_type"] for row in rows)
    repo_counts = Counter(row["repository"] for row in rows)
    developer_counts = Counter(row["actor"] for row in rows)
    st.caption("Showing the last 30 days of observed Cassandra history. Stored events have no automatic expiry.")
    metrics = st.columns(4)
    metrics[0].metric("Stored events", format_number(len(rows)))
    metrics[1].metric("Event types", format_number(len(event_counts)))
    metrics[2].metric("Active repositories", format_number(len(repo_counts)))
    metrics[3].metric("Active developers", format_number(len(developer_counts)))

    left, right = st.columns(2)
    event_frame = pd.DataFrame(event_counts.most_common(), columns=["Event type", "Events"])
    event_chart = px.bar(event_frame.sort_values("Events"), x="Events", y="Event type", orientation="h")
    left.markdown("### Event types")
    left.plotly_chart(event_chart, width="stretch", config=PLOTLY_CONFIG)
    daily_counts = Counter(row["event_date"] for row in rows if row.get("event_date"))
    daily_frame = pd.DataFrame(sorted(daily_counts.items()), columns=["Date", "Events"])
    right.markdown("### Historical activity")
    right.plotly_chart(px.line(daily_frame, x="Date", y="Events", markers=True), width="stretch", config=PLOTLY_CONFIG)

    c1, c2 = st.columns(2)
    c1.markdown("### Active repositories")
    c1.dataframe(pd.DataFrame(repo_counts.most_common(10), columns=["Repository", "Events"]), width="stretch", hide_index=True)
    c2.markdown("### Active developers")
    c2.dataframe(pd.DataFrame(developer_counts.most_common(10), columns=["Developer", "Events"]), width="stretch", hide_index=True)
    st.markdown("### Recent events")
    recent = pd.DataFrame(rows[:100]).rename(columns={
        "time": "Time (UTC)", "event_type": "Event type", "repository": "Repository",
        "actor": "Developer", "detail": "Details", "id": "Event ID",
    })
    st.dataframe(recent[["Time (UTC)", "Event type", "Repository", "Developer", "Details", "Event ID"]], width="stretch", hide_index=True)
    st.download_button("Download displayed event history (CSV)", recent.to_csv(index=False).encode("utf-8"),
                       file_name="github_live_events.csv", mime="text/csv")


def render_live_dashboard() -> None:
    """Poll GitHub and display recent events persisted in Cassandra."""
    st.markdown(
        '<div class="hero"><div class="eyebrow">Live view · GitHub public events</div>'
        '<h1>Live GitHub Activity</h1>'
        '<p>Recent push, pull request, issue, and other public events from GitHub.</p></div>',
        unsafe_allow_html=True,
    )
    st.info(
        "GitHub's public Events API is delayed and rate limited. This view refreshes at the allowed polling interval; "
        "it is near real time, not a complete or instantaneous feed."
    )

    st.session_state.setdefault("live_event_log", {})
    st.session_state.setdefault("live_etag", None)
    st.session_state.setdefault("live_next_poll", 0.0)
    st.session_state.setdefault("live_last_checked", None)
    st.session_state.setdefault("live_error", None)
    st.session_state.setdefault("live_new_event_count", 0)
    st.session_state.setdefault("live_initialized", False)
    st.session_state.setdefault("live_storage_error", None)

    config = cassandra_config()
    live_session = None
    if config["secure_connect_bundle"] or config["contact_points"]:
        if config["port"] is None or not 1 <= config["port"] <= 65535:
            st.session_state.live_storage_error = "CASSANDRA_PORT must be an integer from 1 through 65535."
        elif bool(config["username"]) != bool(config["password"]):
            st.session_state.live_storage_error = "Set both CASSANDRA_USERNAME and CASSANDRA_PASSWORD, or leave both unset."
        else:
            try:
                live_session = cassandra_session(
                    tuple(config["contact_points"]), config["port"], config["keyspace"],
                    config["username"], config["password"], config["secure_connect_bundle"],
                )
                st.session_state.live_storage_error = None
            except Exception as exc:
                st.session_state.live_storage_error = f"Could not connect to Cassandra: {exc}"
    else:
        st.session_state.live_storage_error = (
            "Cassandra is not configured. Set CASSANDRA_CONTACT_POINTS (and the keyspace) "
            "to persist live events."
        )

    with st.sidebar:
        st.markdown("### Live feed settings")
        auto_update = st.checkbox("Auto refresh", value=True, key="live_auto_update")
        refresh_seconds = st.select_slider(
            "Refresh interval",
            options=[60, 75, 90, 120, 180, 300],
            value=60,
            format_func=lambda value: f"{value} seconds",
            key="live_refresh_seconds",
        )
        st.caption("The API may require a longer interval; the app follows its X-Poll-Interval header.")
        if st.button("Reset live session view", key="clear_live_events"):
            st.session_state.live_event_log = {}
            st.session_state.live_new_event_count = 0
            st.session_state.live_etag = None
            st.session_state.live_next_poll = 0.0
            st.session_state.live_error = None
            st.session_state.live_initialized = False
            st.rerun()
        st.caption("Resetting the view does not delete Cassandra records.")

    def update_and_display() -> None:
        manual_poll = st.button("Poll now", key="poll_live_now")
        now = time.time()
        if manual_poll and now < st.session_state.live_next_poll:
            wait = max(1, int(st.session_state.live_next_poll - now))
            st.warning(f"GitHub's polling interval has not elapsed. Try again in about {wait} seconds.")
        elif now >= st.session_state.live_next_poll:
            try:
                records, etag, server_interval, not_modified = fetch_live_public_events(st.session_state.live_etag)
                st.session_state.live_etag = etag
                if not not_modified:
                    log = st.session_state.live_event_log
                    added = 0
                    for raw in records:
                        row = describe_live_event(raw)
                        if st.session_state.live_initialized and row["id"] and row["id"] not in log:
                            added += 1
                        if row["id"]:
                            log[row["id"]] = row
                    st.session_state.live_initialized = True
                    ordered = sorted(log.values(), key=lambda row: row["time"], reverse=True)
                    st.session_state.live_event_log = {row["id"]: row for row in ordered[:LIVE_EVENT_LIMIT]}
                    st.session_state.live_new_event_count += added
                    st.session_state.live_error = None
                    if live_session is not None:
                        try:
                            persist_live_events(live_session, records)
                            st.session_state.live_storage_error = None
                        except Exception as exc:
                            st.session_state.live_storage_error = (
                                "Could not store events in Cassandra. Apply the updated `cassandra_schema.cql` "
                                f"and check the connection. Cassandra reported: {exc}"
                            )
                st.session_state.live_next_poll = time.time() + max(refresh_seconds, server_interval)
                st.session_state.live_last_checked = datetime.now().astimezone().strftime("%b %d, %Y · %H:%M:%S %Z")
            except (RuntimeError, ValueError) as exc:
                st.session_state.live_error = str(exc)
                st.session_state.live_next_poll = time.time() + refresh_seconds
                st.session_state.live_last_checked = datetime.now().astimezone().strftime("%b %d, %Y · %H:%M:%S %Z")

        if live_session is not None:
            try:
                persisted = load_persisted_live_events(live_session)
                log = st.session_state.live_event_log
                for row in persisted:
                    existing = log.get(row["id"], {})
                    log[row["id"]] = {**row, **existing}
                ordered = sorted(log.values(), key=lambda row: row["created_at_dt"] or datetime.min, reverse=True)
                st.session_state.live_event_log = {row["id"]: row for row in ordered[:LIVE_EVENT_LIMIT]}
                st.session_state.live_storage_error = None
            except Exception as exc:
                st.session_state.live_storage_error = (
                    "Could not read live event history from Cassandra. Apply the updated `cassandra_schema.cql` "
                    f"and check the connection. Cassandra reported: {exc}"
                )

        last_checked = st.session_state.live_last_checked or "Waiting for first poll"
        st.caption(f"Last checked: {last_checked} · Next allowed poll follows the GitHub response header.")
        if st.session_state.live_error:
            st.error(st.session_state.live_error)
        if st.session_state.live_storage_error:
            st.warning(st.session_state.live_storage_error)

        log = list(st.session_state.live_event_log.values())
        total = len(log)
        event_counts = Counter(row["event_type"] for row in log)
        repo_count = len({row["repository"] for row in log})
        cols = st.columns(4)
        cols[0].metric("Recent stored events", format_number(total))
        cols[1].metric("New since initial poll", format_number(st.session_state.live_new_event_count))
        cols[2].metric("Repositories", format_number(repo_count))
        cols[3].metric("Event types", format_number(len(event_counts)))
        st.caption("The live table expires records after 30 days; this view displays up to 1,000 events from the latest 7 days.")

        if not log:
            st.warning("No events are available yet. Wait for the first GitHub API response or try again later.")
            return

        chart_col, filter_col = st.columns([1.2, 0.8])
        with chart_col:
            counts = pd.DataFrame(event_counts.most_common(), columns=["event_type", "count"])
            counts["label"] = counts["event_type"].map(format_event_name)
            chart = px.bar(counts.sort_values("count"), x="count", y="label", orientation="h",
                           labels={"count": "Events in recent stored history", "label": "Event type"},
                           color_discrete_sequence=[COLORS["green"]])
            st.plotly_chart(chart_layout(chart, height=max(300, min(600, 28 * len(counts) + 100))),
                            width="stretch", config=PLOTLY_CONFIG)
        with filter_col:
            st.markdown("### Activity by type")
            for event_type, count in event_counts.most_common(8):
                st.markdown(f"**{format_event_name(event_type)}** · {count:,}")

        event_options = ["All event types"] + sorted(event_counts)
        selected = st.selectbox("Filter recent events", event_options, key="live_event_type_filter")
        filtered = log if selected == "All event types" else [row for row in log if row["event_type"] == selected]
        display = pd.DataFrame(filtered[:100])
        if not display.empty:
            display = display.rename(columns={
                "time": "Time (UTC)", "event_type": "Event type", "repository": "Repository",
                "actor": "Actor", "detail": "Details", "id": "Event ID",
            })
            st.dataframe(display[["Time (UTC)", "Event type", "Repository", "Actor", "Details", "Event ID"]],
                         width="stretch", hide_index=True)
            st.caption(f"Showing up to 100 of {len(filtered):,} matching stored events.")

    fragment = st.fragment(run_every=refresh_seconds if auto_update else None)
    fragment(update_and_display)()


def main() -> None:
    apply_theme()
    with st.sidebar:
        st.markdown("## GitHub Public Activity")
        st.caption("Live collector · Cassandra history")
        if st.button("Refresh dashboard", width="stretch"):
            st.rerun()
        st.caption("Choose a tab to view saved analytics or the live GitHub feed.")

    overview_tab, live_tab = st.tabs(["Analytics & History", "Live Feed"])
    with overview_tab:
        render_persistent_dashboard()
    with live_tab:
        render_live_dashboard()


if __name__ == "__main__":
    main()

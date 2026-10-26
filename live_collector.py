"""Poll GitHub's public Events API and persist unique events in Cassandra."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv

if TYPE_CHECKING:
    from pyspark.sql import SparkSession


load_dotenv()
API_URL = "https://api.github.com/events?per_page=100&page=1"
KEYSPACE = os.getenv("CASSANDRA_KEYSPACE", "github_analytics")
DATASET_ID = "live_public"


def process_with_spark(spark: SparkSession, events: list[dict], observed_at: datetime) -> list[tuple]:
    """Clean one API batch with Spark before Cassandra persistence."""
    from pyspark.sql import functions as F
    from pyspark.sql.types import (
        StringType,
        StructField,
        StructType,
    )

    schema = StructType([
        StructField("event_id", StringType(), True),
        StructField("event_type", StringType(), True),
        StructField("repository", StringType(), True),
        StructField("actor", StringType(), True),
        StructField("created_at", StringType(), True),
        StructField("detail", StringType(), True),
        StructField("payload", StringType(), True),
    ])
    rows = []
    for event in events:
        payload = event.get("payload") or {}
        rows.append({
            "event_id": str(event.get("id") or ""),
            "event_type": str(event.get("type") or "UnknownEvent"),
            "repository": str((event.get("repo") or {}).get("name") or "Unknown repository"),
            "actor": str((event.get("actor") or {}).get("login") or "Unknown actor"),
            "created_at": str(event.get("created_at") or ""),
            "detail": str(payload.get("action") or ""),
            "payload": json.dumps(event, ensure_ascii=True, separators=(",", ":")),
        })
    if not rows:
        return []

    frame = spark.createDataFrame(rows, schema=schema)
    cleaned = (
        frame
        .withColumn("created_ts", F.to_timestamp("created_at", "yyyy-MM-dd'T'HH:mm:ssX"))
        .filter(F.col("event_id").isNotNull() & (F.trim("event_id") != ""))
        .filter(F.col("created_ts").isNotNull())
        .dropDuplicates(["event_id"])
    )
    return [
        (
            DATASET_ID,
            row.event_date,
            row.created_ts.replace(tzinfo=None),
            row.event_id,
            row.event_type,
            row.repository,
            row.actor,
            row.detail,
            row.payload,
            observed_at.replace(tzinfo=None),
        )
        for row in cleaned.withColumn("event_date", F.to_date("created_ts")).select(
            "event_date", "created_ts", "event_id", "event_type", "repository", "actor", "detail", "payload"
        ).collect()
    ]


def fetch_events(etag: str | None) -> tuple[list[dict], str | None, int]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "GitHub-Public-Activity-Analytics",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if etag:
        headers["If-None-Match"] = etag
    request = Request(API_URL, headers=headers)
    try:
        with urlopen(request, timeout=20) as response:
            events = json.load(response)
            if not isinstance(events, list):
                raise RuntimeError("GitHub returned an unexpected response.")
            interval = max(60, int(response.headers.get("X-Poll-Interval", "60")))
            return events, response.headers.get("ETag") or etag, interval
    except HTTPError as exc:
        if exc.code == 304:
            return [], etag, max(60, int(exc.headers.get("X-Poll-Interval", "60")))
        if exc.code in (403, 429):
            raise RuntimeError("GitHub rate limited the request; wait before polling again.") from None
        raise RuntimeError(f"GitHub Events API returned HTTP {exc.code}.") from None
    except URLError as exc:
        raise RuntimeError(f"Could not reach GitHub: {getattr(exc, 'reason', 'network error')}") from None


def main() -> None:
    from cassandra.cluster import Cluster
    from pyspark.sql import SparkSession

    contact_points = [host.strip() for host in os.getenv("CASSANDRA_CONTACT_POINTS", "127.0.0.1").split(",") if host.strip()]
    port = int(os.getenv("CASSANDRA_PORT", "9042"))
    cluster = Cluster(contact_points, port=port)
    spark = (
        SparkSession.builder
        .appName("GitHubLivePublicEvents")
        .master(os.getenv("SPARK_MASTER", "local[2]"))
        .config("spark.sql.session.timeZone", "UTC")
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("WARN")
    session = None
    etag = None
    try:
        session = cluster.connect(KEYSPACE)
        insert = session.prepare(
            "INSERT INTO live_events_by_day "
            "(dataset_id, event_date, created_at, event_id, event_type, repository, actor, detail, payload, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        print(f"Collecting GitHub public events into {KEYSPACE}.live_events_by_day. Press Ctrl+C to stop.")
        while True:
            interval = 60
            try:
                events, etag, interval = fetch_events(etag)
                observed_at = datetime.now(timezone.utc)
                processed = process_with_spark(spark, events, observed_at)
                for values in processed:
                    session.execute(insert, values)
                saved = len(processed)
                print(f"{datetime.now().astimezone():%Y-%m-%d %H:%M:%S}: saved {saved} observed event rows; next poll in {interval}s")
            except (RuntimeError, ValueError, OSError) as exc:
                print(f"Collection error: {exc}")
            except Exception as exc:
                print(f"Cassandra write error: {exc}")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("Collector stopped.")
    except Exception as exc:
        raise SystemExit(f"Could not connect to Cassandra keyspace '{KEYSPACE}': {exc}") from exc
    finally:
        cluster.shutdown()
        spark.stop()


if __name__ == "__main__":
    main()

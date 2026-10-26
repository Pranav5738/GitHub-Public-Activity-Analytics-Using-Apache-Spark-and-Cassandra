# GitHub Public Activity Analytics

A small live big-data analytics dashboard. `live_collector.py` polls GitHub's public Events API, processes each response as a PySpark micro-batch, and writes the cleaned events to Cassandra. `app.py` reads those records and refreshes the dashboard automatically. Cassandra is the persistent source of truth; event IDs are part of the primary key, so polling the same API page is idempotent.

The collector samples only what GitHub's public Events API exposes. Delivery is delayed, the API is rate limited, and it is not a complete history of GitHub activity. The dashboard summarizes the last 30 days, while Cassandra retains records without a TTL until an administrator explicitly removes them.

The processing stage uses Spark locally with `local[2]` by default. Set `SPARK_MASTER` to a Spark cluster master URL when running the same collector on a cluster. Spark validates the schema, parses timestamps, removes invalid records, and deduplicates event IDs before Cassandra writes.

## Install

In PowerShell, from the project directory:

```powershell
python -m pip install -r requirements.txt
```

PySpark on Windows requires a compatible Java installation (Java 17 is recommended) available on `PATH`.

Docker Desktop must be installed and running.

## Start Cassandra

Create the Docker network once (if it already exists, skip that line), then start Cassandra:

```powershell
docker network create cassandra
docker run --detach --name cassandra --hostname cassandra --network cassandra --publish 9042:9042 --restart unless-stopped cassandra
```

Wait for Cassandra to finish starting, then apply the schema. The schema uses `IF NOT EXISTS` and does not delete existing rows.

```powershell
docker cp .\cassandra_schema.cql cassandra:/tmp/cassandra_schema.cql
docker exec cassandra cqlsh -f /tmp/cassandra_schema.cql
```

If a container named `cassandra` already exists, start it with `docker start cassandra` instead of running `docker run` again. If it was created without publishing port 9042, recreate it with the command above; stop it first, and keep any Cassandra data volume you already use.

## Run the collector and dashboard

In each PowerShell terminal, set Cassandra's local connection settings:

```powershell
$env:CASSANDRA_CONTACT_POINTS = "127.0.0.1"
$env:CASSANDRA_PORT = "9042"
$env:CASSANDRA_KEYSPACE = "github_analytics"
```

Optionally set `GITHUB_TOKEN` in the collector terminal to use an authenticated GitHub API quota. Keep the token private.

Start the collector in **Terminal 1** and leave it running:

```powershell
python live_collector.py
```

Start the dashboard in **Terminal 2**:

```powershell
python -m streamlit run app.py
```

Open the local URL printed by Streamlit, usually `http://localhost:8501`. The collector polls no faster than GitHub's `X-Poll-Interval` response (minimum 60 seconds). The dashboard reloads Cassandra every 60 seconds; restarting or refreshing the dashboard does not restart the collector or create duplicate event rows.

## What is stored and displayed

The `github_analytics.live_events_by_day` table stores each observed event's ID, date, timestamp, type, repository, actor, selected details, and original API payload. It is partitioned by event date and has no automatic expiry. The dashboard shows event totals, event type counts, active repositories and developers, recent events, daily activity for its 30-day view, and a CSV download. Rows older than that view remain in Cassandra.

The earlier notebook, JSONL sample, generated result files, and aggregate loader have been removed. The live collector and dashboard use GitHub's public Events API and Cassandra directly.

## Stop

Press `Ctrl+C` in the collector terminal and stop Streamlit with `Ctrl+C` in its terminal. To stop Cassandra:

```powershell
docker stop cassandra
```

Stopping these processes does not remove Cassandra records.

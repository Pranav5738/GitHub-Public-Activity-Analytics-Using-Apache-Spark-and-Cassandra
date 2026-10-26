CREATE KEYSPACE IF NOT EXISTS github_analytics
WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1};

USE github_analytics;

CREATE TABLE IF NOT EXISTS event_types (
    dataset_id text,
    event_type text,
    event_count bigint,
    PRIMARY KEY (dataset_id, event_type)
);

CREATE TABLE IF NOT EXISTS repositories (
    dataset_id text,
    repository text,
    event_count bigint,
    PRIMARY KEY (dataset_id, repository)
);

CREATE TABLE IF NOT EXISTS developers (
    dataset_id text,
    developer text,
    event_count bigint,
    PRIMARY KEY (dataset_id, developer)
);

CREATE TABLE IF NOT EXISTS daily_activity (
    dataset_id text,
    activity_date date,
    event_count bigint,
    PRIMARY KEY (dataset_id, activity_date)
);

CREATE TABLE IF NOT EXISTS activity_heatmap (
    dataset_id text,
    activity_date date,
    hour int,
    event_count bigint,
    PRIMARY KEY (dataset_id, activity_date, hour)
);

CREATE TABLE IF NOT EXISTS summary (
    dataset_id text PRIMARY KEY,
    payload text
);

CREATE TABLE IF NOT EXISTS insights (
    dataset_id text PRIMARY KEY,
    content text
);

-- Public Events API records captured by live_collector.py.
-- Records remain in Cassandra until explicitly removed by an administrator.
CREATE TABLE IF NOT EXISTS live_events_by_day (
    dataset_id text,
    event_date date,
    created_at timestamp,
    event_id text,
    event_type text,
    repository text,
    actor text,
    detail text,
    payload text,
    observed_at timestamp,
    PRIMARY KEY ((dataset_id, event_date), created_at, event_id)
) WITH CLUSTERING ORDER BY (created_at DESC, event_id ASC)
;

-- Also disable an old table-level TTL when applying this schema to an existing database.
ALTER TABLE live_events_by_day WITH default_time_to_live = 0;

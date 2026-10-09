"""Versioned SQLite schema for durable task metadata."""

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY CHECK (length(task_id) > 0),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_specs (
    task_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    spec_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_id, revision),
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);

CREATE TABLE IF NOT EXISTS session_settings (
    revision INTEGER PRIMARY KEY CHECK (revision > 0),
    settings_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY CHECK (length(run_id) > 0),
    task_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    settings_revision INTEGER NOT NULL,
    retry_count_used INTEGER NOT NULL CHECK (retry_count_used >= 0),
    inputs_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id, revision) REFERENCES task_specs(task_id, revision),
    FOREIGN KEY (settings_revision) REFERENCES session_settings(revision),
    UNIQUE (task_id, revision, run_id),
    UNIQUE (task_id, run_id)
);

CREATE TABLE IF NOT EXISTS task_decisions (
    decision_id TEXT PRIMARY KEY CHECK (length(decision_id) > 0),
    task_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    run_id TEXT,
    settings_revision INTEGER NOT NULL,
    retry_count_used INTEGER NOT NULL CHECK (retry_count_used >= 0),
    kind TEXT NOT NULL CHECK (kind IN (
        'scope_approved', 'proceed', 'revoked', 'cancelled',
        'retry', 'continue', 'blocked'
    )),
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id, revision) REFERENCES task_specs(task_id, revision),
    FOREIGN KEY (task_id, revision, run_id) REFERENCES runs(task_id, revision, run_id),
    FOREIGN KEY (settings_revision) REFERENCES session_settings(revision),
    UNIQUE (decision_id, task_id, revision),
    CHECK (
        (kind IN ('scope_approved', 'proceed') AND run_id IS NULL)
        OR (kind = 'revoked')
        OR (kind IN ('cancelled', 'retry', 'continue', 'blocked') AND run_id IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS authorization_consumption (
    decision_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    run_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    FOREIGN KEY (decision_id, task_id, revision)
        REFERENCES task_decisions(decision_id, task_id, revision),
    FOREIGN KEY (task_id, revision, run_id)
        REFERENCES runs(task_id, revision, run_id)
);

CREATE TABLE IF NOT EXISTS active_runs (
    task_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE,
    FOREIGN KEY (task_id, run_id) REFERENCES runs(task_id, run_id)
);

CREATE TABLE IF NOT EXISTS run_events (
    event_id TEXT PRIMARY KEY CHECK (length(event_id) > 0),
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN (
        'started', 'cancelled', 'revoked', 'completed', 'failed'
    )),
    settings_revision INTEGER NOT NULL,
    retry_count_used INTEGER NOT NULL CHECK (retry_count_used >= 0),
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id),
    FOREIGN KEY (settings_revision) REFERENCES session_settings(revision)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_terminal_event_per_run
    ON run_events(run_id)
    WHERE kind IN ('cancelled', 'revoked', 'completed', 'failed');

CREATE TABLE IF NOT EXISTS invalidated_revisions (
    task_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    decision_id TEXT NOT NULL UNIQUE,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_id, revision),
    FOREIGN KEY (task_id, revision) REFERENCES task_specs(task_id, revision),
    FOREIGN KEY (decision_id, task_id, revision)
        REFERENCES task_decisions(decision_id, task_id, revision)
);

CREATE TRIGGER IF NOT EXISTS invalidated_revisions_requires_authority_decision
BEFORE INSERT ON invalidated_revisions
WHEN COALESCE((SELECT kind FROM task_decisions WHERE decision_id = NEW.decision_id), '')
     NOT IN ('cancelled', 'revoked')
BEGIN
    SELECT RAISE(ABORT, 'invalidation requires a cancellation or revocation decision');
END;

CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY CHECK (length(message_id) > 0),
    task_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    content_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id, revision, run_id)
        REFERENCES runs(task_id, revision, run_id)
);

CREATE TABLE IF NOT EXISTS delivery_attempts (
    attempt_id TEXT PRIMARY KEY CHECK (length(attempt_id) > 0),
    message_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    created_at TEXT NOT NULL,
    FOREIGN KEY (message_id) REFERENCES messages(message_id),
    UNIQUE (message_id, attempt_number)
);

CREATE TABLE IF NOT EXISTS delivery_status_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    status TEXT NOT NULL CHECK (status IN (
        'attempted', 'api_returned', 'omp_processed', 'failed', 'unknown'
    )),
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (attempt_id) REFERENCES delivery_attempts(attempt_id),
    UNIQUE (attempt_id, sequence)
);

CREATE TABLE IF NOT EXISTS shell_events (
    event_id TEXT PRIMARY KEY CHECK (length(event_id) > 0),
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('sent', 'accepted', 'started', 'ended', 'failed')),
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS takeover_events (
    event_id TEXT PRIMARY KEY CHECK (length(event_id) > 0),
    run_id TEXT NOT NULL,
    request_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('requested', 'confirmed')),
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id),
    FOREIGN KEY (request_id) REFERENCES takeover_events(event_id),
    CHECK (
        (kind = 'requested' AND request_id IS NULL)
        OR (kind = 'confirmed' AND request_id IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS one_takeover_confirmation
    ON takeover_events(request_id)
    WHERE kind = 'confirmed';

CREATE TRIGGER IF NOT EXISTS authorization_consumption_requires_proceed
BEFORE INSERT ON authorization_consumption
WHEN (SELECT kind FROM task_decisions WHERE decision_id = NEW.decision_id) != 'proceed'
BEGIN
    SELECT RAISE(ABORT, 'only a proceed decision can authorize a run');
END;

CREATE TRIGGER IF NOT EXISTS takeover_confirmation_matches_request
BEFORE INSERT ON takeover_events
WHEN NEW.kind = 'confirmed' AND (
    (SELECT kind FROM takeover_events WHERE event_id = NEW.request_id) != 'requested'
    OR (SELECT run_id FROM takeover_events WHERE event_id = NEW.request_id) != NEW.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'takeover confirmation must reference a request for the same run');
END;
"""

MIGRATION_1_TO_2 = """
BEGIN IMMEDIATE;
DROP TRIGGER IF EXISTS invalidated_revisions_immutable_update;
DROP TRIGGER IF EXISTS invalidated_revisions_immutable_delete;
DROP TRIGGER IF EXISTS invalidated_revisions_requires_authority_decision;
ALTER TABLE invalidated_revisions RENAME TO invalidated_revisions_schema_v1;
CREATE TABLE invalidated_revisions (
    task_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    decision_id TEXT NOT NULL UNIQUE,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_id, revision),
    FOREIGN KEY (task_id, revision) REFERENCES task_specs(task_id, revision),
    FOREIGN KEY (decision_id, task_id, revision)
        REFERENCES task_decisions(decision_id, task_id, revision)
);
CREATE TRIGGER invalidated_revisions_requires_authority_decision
BEFORE INSERT ON invalidated_revisions
WHEN COALESCE((SELECT kind FROM task_decisions WHERE decision_id = NEW.decision_id), '')
     NOT IN ('cancelled', 'revoked')
BEGIN
    SELECT RAISE(ABORT, 'invalidation requires a cancellation or revocation decision');
END;
INSERT INTO invalidated_revisions(task_id, revision, decision_id, reason, created_at)
    SELECT task_id, revision, decision_id, reason, created_at
    FROM invalidated_revisions_schema_v1;
DROP TABLE invalidated_revisions_schema_v1;
PRAGMA user_version = 2;
COMMIT;
"""

LOG_METADATA_SCHEMA = """
CREATE TABLE IF NOT EXISTS log_source_records (
    source_id TEXT PRIMARY KEY,
    subject_kind TEXT NOT NULL CHECK(subject_kind IN ('task','command')),
    subject_id TEXT NOT NULL,
    revision INTEGER,
    source_json TEXT NOT NULL,
    span_start INTEGER NOT NULL CHECK(span_start >= 0),
    span_end INTEGER NOT NULL CHECK(span_end > span_start),
    digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('observed','storage_gap','collector_gap','evicted','unknown')),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS log_sources_subject
    ON log_source_records(subject_kind, subject_id, created_at);

CREATE TABLE IF NOT EXISTS log_observation_boundaries (
    event_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    source_json TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('start','final_drain')),
    offset INTEGER NOT NULL CHECK(offset >= 0),
    observed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id,revision,run_id) REFERENCES runs(task_id,revision,run_id),
    CHECK(kind != 'start' OR offset = 0)
);
CREATE INDEX IF NOT EXISTS log_boundaries_run ON log_observation_boundaries(run_id,kind);
CREATE TABLE IF NOT EXISTS log_observation_coverage (
    coverage_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    source_json TEXT NOT NULL,
    start_event_id TEXT NOT NULL,
    final_event_id TEXT NOT NULL,
    final_offset INTEGER NOT NULL CHECK(final_offset >= 0),
    source_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id,revision,run_id) REFERENCES runs(task_id,revision,run_id),
    FOREIGN KEY(start_event_id) REFERENCES log_observation_boundaries(event_id),
    FOREIGN KEY(final_event_id) REFERENCES log_observation_boundaries(event_id),
    CHECK(start_event_id != final_event_id)
);

CREATE TABLE IF NOT EXISTS log_condition_proofs (
    proof_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision > 0),
    run_id TEXT NOT NULL,
    condition_digest TEXT NOT NULL,
    source_id TEXT,
    coverage_id TEXT,
    matched INTEGER NOT NULL CHECK(matched IN (0,1)),
    observation_complete INTEGER NOT NULL CHECK(observation_complete IN (0,1)),
    match_start INTEGER,
    match_end INTEGER,
    full_match_digest TEXT,
    excerpt BLOB NOT NULL CHECK(length(excerpt) <= 4096),
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id,revision,run_id) REFERENCES runs(task_id,revision,run_id),
    FOREIGN KEY(source_id) REFERENCES log_source_records(source_id),
    FOREIGN KEY(coverage_id) REFERENCES log_observation_coverage(coverage_id),
    CHECK((observation_complete=1 AND coverage_id IS NOT NULL)
       OR (observation_complete=0 AND coverage_id IS NULL)),
    CHECK((matched=1 AND match_start IS NOT NULL AND match_end > match_start
           AND full_match_digest IS NOT NULL)
       OR (matched=0 AND match_start IS NULL AND match_end IS NULL
           AND full_match_digest IS NULL))
);
CREATE INDEX IF NOT EXISTS log_proofs_run ON log_condition_proofs(run_id,condition_digest);

CREATE TABLE IF NOT EXISTS summary_requests_v1 (
    request_id TEXT PRIMARY KEY,
    subject_kind TEXT NOT NULL CHECK(subject_kind IN ('task','command')),
    subject_id TEXT NOT NULL,
    revision INTEGER,
    input_key TEXT NOT NULL,
    skill_digest TEXT NOT NULL,
    request_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS summary_requests_input
    ON summary_requests_v1(subject_kind,subject_id,input_key);
CREATE TABLE IF NOT EXISTS summary_request_ranges_v1 (
    request_id TEXT NOT NULL,
    range_index INTEGER NOT NULL CHECK(range_index >= 0),
    range_kind TEXT NOT NULL CHECK(range_kind IN ('source','gap')),
    source_key TEXT NOT NULL,
    span_start INTEGER NOT NULL CHECK(span_start >= 0),
    span_end INTEGER NOT NULL CHECK(span_end > span_start),
    PRIMARY KEY(request_id,range_index),
    FOREIGN KEY(request_id) REFERENCES summary_requests_v1(request_id)
);
CREATE INDEX IF NOT EXISTS summary_ranges_overlap
    ON summary_request_ranges_v1(source_key,span_start,span_end);
CREATE TABLE IF NOT EXISTS summary_request_events_v1 (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'prepared','sending','executed_observed','not_sent','unknown',
        'stored','rejected','storage_failed'
    )),
    detail TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(request_id) REFERENCES summary_requests_v1(request_id)
);
CREATE INDEX IF NOT EXISTS summary_events_request
    ON summary_request_events_v1(request_id,event_id);
CREATE TABLE IF NOT EXISTS summary_result_receipts_v1 (
    receipt_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    entry_id TEXT NOT NULL UNIQUE,
    result_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(request_id) REFERENCES summary_requests_v1(request_id)
);
CREATE TABLE IF NOT EXISTS summary_result_drops_v1 (
    drop_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    result_digest TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(request_id) REFERENCES summary_requests_v1(request_id)
);
CREATE TRIGGER IF NOT EXISTS summary_stored_requires_receipt
BEFORE INSERT ON summary_request_events_v1
WHEN NEW.state='stored' AND NOT EXISTS(
    SELECT 1 FROM summary_result_receipts_v1 WHERE request_id=NEW.request_id AND entry_id=NEW.detail
)
BEGIN
    SELECT RAISE(ABORT, 'stored requires matching durable receipt');
END;
CREATE UNIQUE INDEX IF NOT EXISTS summary_stored_once
    ON summary_request_events_v1(request_id) WHERE state='stored';
"""

SCHEMA += LOG_METADATA_SCHEMA
MIGRATION_2_TO_3 = "BEGIN IMMEDIATE;\n" + LOG_METADATA_SCHEMA + "\nPRAGMA user_version = 3;\nCOMMIT;"

APPEND_ONLY_TABLES = (
    "tasks",
    "task_specs",
    "session_settings",
    "runs",
    "task_decisions",
    "authorization_consumption",
    "run_events",
    "invalidated_revisions",
    "messages",
    "delivery_attempts",
    "delivery_status_events",
    "shell_events",
    "takeover_events",
    "log_source_records",
    "log_observation_boundaries",
    "log_observation_coverage",
    "log_condition_proofs",
    "summary_requests_v1",
    "summary_request_ranges_v1",
    "summary_request_events_v1",
    "summary_result_receipts_v1",
    "summary_result_drops_v1",
)

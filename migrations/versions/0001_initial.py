"""Frozen initial PostgreSQL schema. Revision ID: 0001."""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE TABLE guards (\n\tid VARCHAR(32) NOT NULL, \n\tPRIMARY KEY (id)\n)")
    op.execute(
        "CREATE TABLE rate_buckets (\n\tid VARCHAR(96) NOT NULL, \n\tcount INTEGER NOT NULL, \n\texpires_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id)\n)"
    )
    op.execute("CREATE INDEX ix_rate_buckets_expires_at ON rate_buckets (expires_at)")
    op.execute(
        "CREATE TABLE sessions (\n\tid VARCHAR(64) NOT NULL, \n\texpires_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id)\n)"
    )
    op.execute("CREATE INDEX ix_sessions_expires_at ON sessions (expires_at)")
    op.execute(
        "CREATE TABLE stream_slots (\n\tid VARCHAR(36) NOT NULL, \n\towner_id VARCHAR(64) NOT NULL, \n\texpires_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id)\n)"
    )
    op.execute("CREATE INDEX ix_stream_slots_expires_at ON stream_slots (expires_at)")
    op.execute("CREATE INDEX ix_stream_slots_owner_id ON stream_slots (owner_id)")
    op.execute(
        "CREATE TABLE runs (\n\tid VARCHAR(36) NOT NULL, \n\towner_id VARCHAR(64) NOT NULL, \n\tenvironment VARCHAR(16) NOT NULL, \n\trequest_key VARCHAR(64) NOT NULL, \n\trequest_digest VARCHAR(64) NOT NULL, \n\tstatus VARCHAR(16) NOT NULL, \n\toutcome VARCHAR(32) NOT NULL, \n\tphase INTEGER NOT NULL, \n\tinput JSONB NOT NULL, \n\tcontext JSONB NOT NULL, \n\tresult JSONB, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tcompleted_at TIMESTAMP WITH TIME ZONE, \n\texpires_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tdeadline_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tnext_attempt_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tlease_until TIMESTAMP WITH TIME ZONE, \n\tclaim_version INTEGER NOT NULL, \n\tattempts INTEGER NOT NULL, \n\tevent_count INTEGER NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_owner_request UNIQUE (owner_id, request_key), \n\tFOREIGN KEY(owner_id) REFERENCES sessions (id)\n)"
    )
    op.execute("CREATE INDEX ix_runs_expires_at ON runs (expires_at)")
    op.execute("CREATE INDEX ix_runs_owner_id ON runs (owner_id)")
    op.execute("CREATE INDEX ix_runs_queue ON runs (status, next_attempt_at, lease_until)")
    op.execute(
        "CREATE TABLE approvals (\n\tid VARCHAR(36) NOT NULL, \n\trun_id VARCHAR(36) NOT NULL, \n\taction_digest VARCHAR(64) NOT NULL, \n\tproposal JSONB NOT NULL, \n\tenvironment VARCHAR(16) NOT NULL, \n\tworkflow_version VARCHAR(32) NOT NULL, \n\texpires_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (run_id), \n\tFOREIGN KEY(run_id) REFERENCES runs (id) ON DELETE CASCADE\n)"
    )
    op.execute(
        "CREATE TABLE effects (\n\toperation_id VARCHAR(64) NOT NULL, \n\trun_id VARCHAR(36) NOT NULL, \n\treceipt JSONB NOT NULL, \n\tPRIMARY KEY (operation_id), \n\tFOREIGN KEY(run_id) REFERENCES runs (id) ON DELETE CASCADE\n)"
    )
    op.execute("CREATE INDEX ix_effects_run_id ON effects (run_id)")
    op.execute(
        "CREATE TABLE events (\n\tid VARCHAR(36) NOT NULL, \n\trun_id VARCHAR(36) NOT NULL, \n\tsequence INTEGER NOT NULL, \n\tpayload JSONB NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_run_sequence UNIQUE (run_id, sequence), \n\tFOREIGN KEY(run_id) REFERENCES runs (id) ON DELETE CASCADE\n)"
    )
    op.execute("CREATE INDEX ix_events_run_id ON events (run_id)")
    op.execute(
        "CREATE TABLE approval_records (\n\tid VARCHAR(36) NOT NULL, \n\trequest_id VARCHAR(36) NOT NULL, \n\tactor VARCHAR(64) NOT NULL, \n\tdecision VARCHAR(16) NOT NULL, \n\tdecided_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (request_id), \n\tFOREIGN KEY(request_id) REFERENCES approvals (id) ON DELETE CASCADE\n)"
    )
    op.execute("INSERT INTO guards (id) VALUES ('admission')")


def downgrade():
    op.execute("DROP TABLE approval_records CASCADE")
    op.execute("DROP TABLE events CASCADE")
    op.execute("DROP TABLE effects CASCADE")
    op.execute("DROP TABLE approvals CASCADE")
    op.execute("DROP TABLE runs CASCADE")
    op.execute("DROP TABLE stream_slots CASCADE")
    op.execute("DROP TABLE sessions CASCADE")
    op.execute("DROP TABLE rate_buckets CASCADE")
    op.execute("DROP TABLE guards CASCADE")

"""Add canonical event projections and isolated service delivery journals."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("events", sa.Column("wire_payload", JSONB(none_as_null=True), nullable=True))
    op.create_table(
        "service_inbox",
        sa.Column("dedup_key", sa.String(64), primary_key=True),
        sa.Column("logical_digest", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("event", JSONB(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_service_inbox_run_id", "service_inbox", ["run_id"])
    op.create_table(
        "service_outbox",
        sa.Column("operation_id", sa.String(96), primary_key=True),
        sa.Column("logical_event", JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("claim_version", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_code", sa.String(64)),
    )
    op.create_index(
        "ix_service_outbox_queue", "service_outbox", ["status", "next_attempt_at", "lease_until"]
    )


def downgrade():
    connection = op.get_bind()
    if connection.execute(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM events WHERE wire_payload IS NOT NULL AND wire_payload <> 'null'::jsonb) OR EXISTS(SELECT 1 FROM service_inbox) OR EXISTS(SELECT 1 FROM service_outbox)"
        )
    ).scalar():
        raise RuntimeError(
            "Contracts data exists; preserve journals and projections before downgrade"
        )
    op.drop_table("service_outbox")
    op.drop_table("service_inbox")
    op.drop_column("events", "wire_payload")

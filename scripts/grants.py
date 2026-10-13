from sqlalchemy import text

from flowops.config import Settings
from flowops.db import database

engine, _ = database(Settings.load())
with engine.begin() as c:
    c.execute(text("REVOKE ALL ON SCHEMA public FROM PUBLIC"))
    c.execute(text("GRANT USAGE ON SCHEMA public TO flowops_runtime"))
    c.execute(text("GRANT SELECT ON ALL TABLES IN SCHEMA public TO flowops_runtime"))
    c.execute(
        text(
            "GRANT INSERT, UPDATE, DELETE ON runs, sessions, rate_buckets, stream_slots TO flowops_runtime"
        )
    )
    c.execute(text("GRANT UPDATE ON guards TO flowops_runtime"))
    c.execute(
        text("GRANT INSERT ON events, approvals, approval_records, effects TO flowops_runtime")
    )
    c.execute(text("GRANT INSERT ON service_inbox TO flowops_runtime"))
    c.execute(text("GRANT INSERT, UPDATE ON service_outbox TO flowops_runtime"))
print("Runtime role granted scoped DML; audit and approval rows are append-only to the runtime.")

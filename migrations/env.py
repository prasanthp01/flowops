from alembic import context

from flowops.config import Settings
from flowops.db import Base, database

engine, _ = database(Settings.load())
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()

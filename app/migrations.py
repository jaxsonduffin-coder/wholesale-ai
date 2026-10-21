"""Additive schema migrations.

`Base.metadata.create_all()` creates missing tables but never alters existing
ones, so a deployed database from the first build would be missing the columns
this version adds (`leads.lead_class`, `leads.consent_evidence`,
`cadences.lead_class`). Rather than pulling in Alembic for three nullable
columns, this does exactly what is needed: add the column if it is absent.

Deliberately additive only. Nothing here drops, renames or retypes anything, so
it is safe to run on every boot and idempotent.
"""
from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

log = logging.getLogger("wholesale_ai.migrations")

# (table, column, DDL fragment)
ADDITIVE_COLUMNS: list[tuple[str, str, str]] = [
    ("leads", "lead_class", "VARCHAR(16) DEFAULT 'list' NOT NULL"),
    ("leads", "consent_evidence", "TEXT DEFAULT ''"),
    ("cadences", "lead_class", "VARCHAR(16) DEFAULT 'list' NOT NULL"),
    ("contacts", "cadence_step", "VARCHAR(40) DEFAULT ''"),
    ("leads", "needs_attention", "BOOLEAN DEFAULT 0"),
    ("leads", "attention_reason", "VARCHAR(255) DEFAULT ''"),
    ("leads", "attention_at", "DATETIME"),
    ("leads", "interested_at", "DATETIME"),
    ("leads", "intent_label", "VARCHAR(24) DEFAULT ''"),
    ("leads", "intent_score", "INTEGER DEFAULT 0"),
]


def ensure_schema(engine: Engine) -> list[str]:
    """Add any missing additive columns. Returns the list of columns added."""
    added: list[str] = []
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    for table, column, ddl in ADDITIVE_COLUMNS:
        if table not in existing_tables:
            continue  # create_all will make it with the column present
        cols = {c["name"] for c in inspector.get_columns(table)}
        if column in cols:
            continue
        try:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            added.append(f"{table}.{column}")
            log.info("migration: added %s.%s", table, column)
        except Exception as e:  # pragma: no cover - depends on dialect
            log.warning("migration failed for %s.%s: %s", table, column, e)

    return added

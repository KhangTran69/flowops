"""Scope event deduplication to tenant and connector identity.

Revision ID: 0013_event_connector_identity
Revises: 0012_workforce_tenant_fencing
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013_event_connector_identity"
down_revision: Union[str, None] = "0012_workforce_tenant_fencing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _fingerprint(
    tenant_key: str | None,
    connector_id: int | None,
    source: str,
    event_type: str,
    external_id: str,
) -> str:
    identity = json.dumps(
        [
            2,
            tenant_key[:64] if tenant_key else tenant_key,
            connector_id,
            source[:40],
            event_type[:80],
            external_id[:255],
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "v2:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("events")}
    indexes = {index["name"] for index in inspector.get_indexes("events")}
    if "connector_id" not in columns:
        op.add_column("events", sa.Column("connector_id", sa.Integer(), nullable=True))
    if "ix_events_connector_id" not in indexes:
        op.create_index("ix_events_connector_id", "events", ["connector_id"])

    select = sa.text(
        "SELECT id, tenant_key, connector_id, source, event_type, external_id, fingerprint "
        "FROM events WHERE id > :last_id ORDER BY id LIMIT 1000"
    )
    update = sa.text("UPDATE events SET fingerprint = :fingerprint WHERE id = :event_id")
    last_id = 0
    while batch := bind.execute(select, {"last_id": last_id}).fetchall():
        updates = []
        for row in batch:
            if re.fullmatch(r"v2:[a-f0-9]{64}", row.fingerprint or ""):
                continue
            updates.append(
                {
                    "event_id": row.id,
                    "fingerprint": _fingerprint(
                        row.tenant_key,
                        row.connector_id,
                        row.source,
                        row.event_type,
                        row.external_id,
                    ),
                }
            )
        if updates:
            bind.execute(update, updates)
        last_id = batch[-1].id


def downgrade() -> None:
    bind = op.get_bind()
    collision = bind.execute(
        sa.text(
            "SELECT 1 FROM events GROUP BY source, event_type, external_id "
            "HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if collision:
        raise RuntimeError(
            "Cannot downgrade event identity: events now contain cross-tenant or "
            "cross-connector IDs that the old global fingerprint cannot represent."
        )

    select = sa.text(
        "SELECT id, source, event_type, external_id "
        "FROM events WHERE id > :last_id ORDER BY id LIMIT 1000"
    )
    update = sa.text(
        "UPDATE events SET fingerprint = :fingerprint WHERE id = :event_id"
    )
    last_id = 0
    while batch := bind.execute(select, {"last_id": last_id}).fetchall():
        bind.execute(
            update,
            [
                {
                    "event_id": row.id,
                    "fingerprint": f"{row.source}:{row.event_type}:{row.external_id}"[:400],
                }
                for row in batch
            ],
        )
        last_id = batch[-1].id
    op.drop_index("ix_events_connector_id", table_name="events")
    op.drop_column("events", "connector_id")

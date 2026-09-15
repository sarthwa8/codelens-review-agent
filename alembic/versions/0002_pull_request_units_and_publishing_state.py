"""pull request units and publishing state

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-15 06:53:05.720492
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "commits", sa.Column("kind", sa.String(length=16), server_default="push", nullable=False)
    )
    op.add_column("commits", sa.Column("pr_number", sa.Integer(), nullable=True))
    op.add_column("commits", sa.Column("base_sha", sa.String(length=40), nullable=True))
    op.add_column("commits", sa.Column("skip_reason", sa.String(length=255), nullable=True))
    op.add_column(
        "commits",
        sa.Column("publish_status", sa.String(length=16), server_default="pending", nullable=False),
    )
    op.add_column("commits", sa.Column("check_run_id", sa.BigInteger(), nullable=True))
    op.add_column("commits", sa.Column("published_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("commits", sa.Column("publish_error", sa.Text(), nullable=True))
    op.add_column("repos", sa.Column("installation_id", sa.BigInteger(), nullable=True))

    # Autogenerate doesn't emit CHECK constraints; keep them in sync with app.db.models.
    op.create_check_constraint("ck_commits_kind", "commits", "kind IN ('push', 'pull_request')")
    op.create_check_constraint(
        "ck_commits_publish_status",
        "commits",
        "publish_status IN ('pending', 'publishing', 'published', 'failed', 'skipped')",
    )
    # Units reviewed before publishing existed have nothing to post.
    op.execute("UPDATE commits SET publish_status = 'skipped'")


def downgrade() -> None:
    op.drop_constraint("ck_commits_publish_status", "commits", type_="check")
    op.drop_constraint("ck_commits_kind", "commits", type_="check")
    op.drop_column("repos", "installation_id")
    op.drop_column("commits", "publish_error")
    op.drop_column("commits", "published_at")
    op.drop_column("commits", "check_run_id")
    op.drop_column("commits", "publish_status")
    op.drop_column("commits", "skip_reason")
    op.drop_column("commits", "base_sha")
    op.drop_column("commits", "pr_number")
    op.drop_column("commits", "kind")

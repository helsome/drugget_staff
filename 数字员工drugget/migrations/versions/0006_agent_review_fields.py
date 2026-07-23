"""add agent-review fields to price comparisons and break events

Revision ID: 0006_agent_review_fields
Revises: 0005_price_comparisons_and_dry_run_queue
"""
from alembic import op
import sqlalchemy as sa


revision = "0006_agent_review_fields"
down_revision = "0005_price_comparisons_and_dry_run_queue"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("price_comparisons") as batch:
        batch.add_column(sa.Column("review_required", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("review_reason", sa.String(length=100), nullable=True))
        batch.add_column(sa.Column("review_status", sa.String(length=40), nullable=True))
        batch.add_column(sa.Column("formal_price_status", sa.String(length=40), nullable=False, server_default="pending"))
    with op.batch_alter_table("price_break_events") as batch:
        batch.add_column(sa.Column("review_status", sa.String(length=40), nullable=True))
        batch.add_column(sa.Column("review_decision", sa.String(length=40), nullable=True))
        batch.add_column(sa.Column("review_attempts", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("review_evidence_path", sa.Text(), nullable=True))
        batch.add_column(sa.Column("review_error_code", sa.String(length=100), nullable=True))
        batch.add_column(sa.Column("review_summary", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("price_break_events") as batch:
        batch.drop_column("review_summary")
        batch.drop_column("review_error_code")
        batch.drop_column("review_evidence_path")
        batch.drop_column("reviewed_at")
        batch.drop_column("review_attempts")
        batch.drop_column("review_decision")
        batch.drop_column("review_status")
    with op.batch_alter_table("price_comparisons") as batch:
        batch.drop_column("formal_price_status")
        batch.drop_column("review_status")
        batch.drop_column("review_reason")
        batch.drop_column("review_required")

"""add strict price comparisons and dry-run routing queue

Revision ID: 0005_price_comparisons_and_dry_run_queue
Revises: 0004_control_rule_confirmation
"""
from alembic import op
import sqlalchemy as sa


revision = "0005_price_comparisons_and_dry_run_queue"
down_revision = "0004_control_rule_confirmation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "price_comparisons",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("observation_id", sa.String(length=36), sa.ForeignKey("price_observations.id"), nullable=False),
        sa.Column("control_price_version_id", sa.String(length=36), sa.ForeignKey("control_price_versions.id")),
        sa.Column("verdict", sa.String(length=40), nullable=False),
        sa.Column("reason_code", sa.String(length=100), nullable=False),
        sa.Column("reason_detail", sa.Text()),
        sa.Column("comparison_unit_price", sa.Numeric(12, 4)),
        sa.Column("control_price", sa.Numeric(12, 4)),
        sa.Column("min_unit", sa.String(length=20)),
        sa.Column("difference", sa.Numeric(12, 4)),
        sa.Column("rule_snapshot", sa.JSON()),
        sa.Column("detail_evidence_snapshot", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("observation_id", name="uq_price_comparison_observation"),
    )
    with op.batch_alter_table("price_break_events") as batch:
        batch.add_column(sa.Column("comparison_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key("fk_price_break_comparison", "price_comparisons", ["comparison_id"], ["id"])
        batch.create_unique_constraint("uq_price_break_comparison", ["comparison_id"])
    op.create_table(
        "central_assignment_queue",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("event_id", sa.String(length=36), sa.ForeignKey("price_break_events.id"), nullable=False),
        sa.Column("reason_code", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("payload", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("event_id", name="uq_assignment_event"),
    )


def downgrade() -> None:
    op.drop_table("central_assignment_queue")
    with op.batch_alter_table("price_break_events") as batch:
        batch.drop_constraint("uq_price_break_comparison", type_="unique")
        batch.drop_constraint("fk_price_break_comparison", type_="foreignkey")
        batch.drop_column("comparison_id")
    op.drop_table("price_comparisons")

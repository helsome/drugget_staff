"""record business approval metadata for control price rules

Revision ID: 0004_control_rule_confirmation
Revises: 0003_min_purchase_box_count
"""
from alembic import op
import sqlalchemy as sa


revision = "0004_control_rule_confirmation"
down_revision = "0003_min_purchase_box_count"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("control_price_versions") as batch:
        batch.add_column(sa.Column("source_line_number", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("business_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("confirmed_by", sa.String(length=100), nullable=True))
        batch.add_column(sa.Column("confirmed_at", sa.Date(), nullable=True))
        batch.add_column(sa.Column("approval_reference", sa.String(length=300), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("control_price_versions") as batch:
        batch.drop_column("approval_reference")
        batch.drop_column("confirmed_at")
        batch.drop_column("confirmed_by")
        batch.drop_column("business_confirmed")
        batch.drop_column("source_line_number")

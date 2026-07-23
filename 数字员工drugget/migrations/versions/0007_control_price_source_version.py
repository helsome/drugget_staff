"""persist authoritative control-price source identity

Revision ID: 0007_control_price_source_version
Revises: 0006_agent_review_fields
"""
from alembic import op
import sqlalchemy as sa


revision = "0007_control_price_source_version"
down_revision = "0006_agent_review_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("control_price_versions") as batch:
        batch.add_column(sa.Column("authority_basis", sa.String(length=40), nullable=True))
        batch.add_column(sa.Column("source_sha256", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("control_price_versions") as batch:
        batch.drop_column("source_sha256")
        batch.drop_column("authority_basis")

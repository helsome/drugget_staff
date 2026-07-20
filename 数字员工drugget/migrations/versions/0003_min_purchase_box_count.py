"""persist a minimum purchase quantity separately from the SKU package

Revision ID: 0003_min_purchase_box_count
Revises: 0002_store_home_url
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_min_purchase_box_count"
down_revision = "0002_store_home_url"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("price_observations") as batch:
        batch.add_column(sa.Column("min_purchase_box_count", sa.Numeric(12, 4), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("price_observations") as batch:
        batch.drop_column("min_purchase_box_count")

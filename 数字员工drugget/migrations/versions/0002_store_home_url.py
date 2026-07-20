"""persist verified storefront home URLs

Revision ID: 0002_store_home_url
Revises: 0001_initial
"""
from alembic import op
import sqlalchemy as sa


revision = "0002_store_home_url"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("store_responsibilities") as batch:
        batch.add_column(sa.Column("shop_home_url", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("store_responsibilities") as batch:
        batch.drop_column("shop_home_url")

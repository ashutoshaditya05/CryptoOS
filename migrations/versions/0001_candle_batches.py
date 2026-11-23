import sqlalchemy as sa
from alembic import op

revision = "0001_candle_batches"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "candle_batches",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("venue", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("first_open", sa.Integer(), nullable=False),
        sa.Column("last_open", sa.Integer(), nullable=False),
        sa.CheckConstraint("first_open <= last_open", name="ck_candle_batches_ordered_range"),
    )
    op.create_index(
        "ix_candle_batches_lookup",
        "candle_batches",
        ["venue", "symbol", "timeframe", "first_open", "last_open"],
    )


def downgrade() -> None:
    op.drop_table("candle_batches")

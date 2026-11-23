import sqlalchemy as sa
from alembic import op

revision = "0002_backtest_runs"
down_revision = "0001_candle_batches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "backtest_runs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("report_json", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("backtest_runs")

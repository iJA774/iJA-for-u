"""增加成员角色、工具审计与可恢复周期任务。"""

import sqlalchemy as sa
from alembic import op

from adapters.persistence.database import Base

revision = "0002_tools_schedules"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def upgrade() -> None:
    if "revision" not in _columns("sessions"):
        with op.batch_alter_table("sessions") as batch:
            batch.add_column(sa.Column("revision", sa.Integer(), nullable=False, server_default="1"))
    if "role" not in _columns("session_members"):
        with op.batch_alter_table("session_members") as batch:
            batch.add_column(sa.Column("role", sa.String(length=20), nullable=False, server_default="member"))

    bind = op.get_bind()
    for name in ("schedules", "schedule_runs", "tool_executions"):
        Base.metadata.tables[name].create(bind=bind, checkfirst=True)

    sessions = bind.execute(sa.text("SELECT id, chat_type FROM sessions")).mappings().all()
    for session in sessions:
        members = (
            bind.execute(
                sa.text(
                    "SELECT id FROM session_members WHERE session_id=:session_id "
                    "ORDER BY joined_at ASC, id ASC"
                ),
                {"session_id": session["id"]},
            )
            .mappings()
            .all()
        )
        if not members:
            continue
        if session["chat_type"] == "private":
            bind.execute(
                sa.text("UPDATE session_members SET role='owner' WHERE session_id=:session_id"),
                {"session_id": session["id"]},
            )
        else:
            bind.execute(
                sa.text("UPDATE session_members SET role='member' WHERE session_id=:session_id"),
                {"session_id": session["id"]},
            )
            bind.execute(
                sa.text("UPDATE session_members SET role='owner' WHERE id=:member_id"),
                {"member_id": members[0]["id"]},
            )


def downgrade() -> None:
    for name in ("tool_executions", "schedule_runs", "schedules"):
        op.drop_table(name)
    with op.batch_alter_table("session_members") as batch:
        batch.drop_column("role")
    with op.batch_alter_table("sessions") as batch:
        batch.drop_column("revision")

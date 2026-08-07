"""工具执行与模型调用安全观测的 SQLite 仓储实现。"""

from __future__ import annotations

import json
import re

from sqlalchemy import case, func, insert, literal, select

from domain.errors import InputValidationError
from domain.models import ModelAttempt, ToolExecution

from ..schema import ModelAttemptRow, SessionRow, ToolExecutionRow
from ._base import RepositoryMixinSupport

_MODEL_ATTEMPT_TASK_RE = re.compile(r"(?:default|[a-z][a-z0-9_.-]{0,99})")


class AuditRepositoryMixin(RepositoryMixinSupport):
    """实现不保存正文的工具与模型调用审计。"""

    async def save_tool_execution(self, execution: ToolExecution) -> None:
        """保存工具执行状态；正文只进入本地权威数据库，不进入日志。"""

        async with self.session_factory() as db:
            row = await db.get(ToolExecutionRow, execution.id)
            values = {
                "result_json": (
                    json.dumps(execution.result, ensure_ascii=False)
                    if execution.result is not None
                    else None
                ),
                "status": execution.status.value,
                "error_code": execution.error_code,
                "error_message": execution.error_message,
                "completed_at": execution.completed_at,
            }
            if row is None:
                row = ToolExecutionRow(
                    id=execution.id,
                    session_id=execution.session_id,
                    tool_call_id=execution.tool_call_id,
                    tool_name=execution.tool_name,
                    arguments_json=json.dumps(
                        execution.arguments,
                        ensure_ascii=False,
                    ),
                    result_json=values["result_json"],
                    status=values["status"],
                    turn_id=execution.turn_id,
                    schedule_run_id=execution.schedule_run_id,
                    error_code=values["error_code"],
                    error_message=values["error_message"],
                    started_at=execution.started_at,
                    completed_at=values["completed_at"],
                )
                db.add(row)
            else:
                row.result_json = values["result_json"]
                row.status = values["status"]
                row.error_code = values["error_code"]
                row.error_message = values["error_message"]
                row.completed_at = values["completed_at"]
            await db.commit()

    async def list_tool_executions(
        self,
        *,
        session_id: str | None = None,
        turn_id: str | None = None,
        schedule_run_id: str | None = None,
    ) -> list[ToolExecution]:
        async with self.session_factory() as db:
            query = select(ToolExecutionRow)
            if session_id is not None:
                query = query.where(ToolExecutionRow.session_id == session_id)
            if turn_id is not None:
                query = query.where(ToolExecutionRow.turn_id == turn_id)
            if schedule_run_id is not None:
                query = query.where(
                    ToolExecutionRow.schedule_run_id == schedule_run_id
                )
            query = query.order_by(ToolExecutionRow.started_at.desc())
            return [
                self._tool_execution_from_row(row)
                for row in (await db.execute(query)).scalars().all()
            ]

    async def save_model_attempt(self, attempt: ModelAttempt) -> None:
        """保存一条不含请求、响应正文或隐藏推理的模型尝试观测。"""

        values: dict[str, object] = {
            "id": attempt.id,
            "invocation_id": attempt.invocation_id,
            "attempt_number": attempt.attempt_number,
            "task": attempt.task,
            "provider": attempt.provider,
            "profile": attempt.profile,
            "model": attempt.model,
            "session_id": attempt.session_id,
            "turn_id": attempt.turn_id,
            "run_id": attempt.run_id,
            "streamed": attempt.streamed,
            "tool_call_count": attempt.tool_call_count,
            "input_tokens": attempt.input_tokens,
            "output_tokens": attempt.output_tokens,
            "total_tokens": attempt.total_tokens,
            "usage_source": attempt.usage_source,
            "latency_ms": attempt.latency_ms,
            "success": attempt.success,
            "error_type": attempt.error_type,
            "error_code": attempt.error_code,
            "cost_microusd": attempt.cost_microusd,
            "started_at": attempt.started_at,
            "completed_at": attempt.completed_at,
        }
        async with self.session_factory() as db:
            if attempt.session_id is None:
                await db.execute(insert(ModelAttemptRow).values(**values))
            else:
                # INSERT ... SELECT 把“会话仍存在”与插入合成同一条语句。
                # 删除先发生时插入 0 行；插入先发生时 FK CASCADE 随后删除，
                # 不留下 check-then-insert 的并发复活窗口。
                columns = tuple(values)
                source = (
                    select(
                        *(
                            literal(values[column]).label(column)
                            for column in columns
                        )
                    )
                    .select_from(SessionRow)
                    .where(SessionRow.id == attempt.session_id)
                )
                await db.execute(
                    insert(ModelAttemptRow).from_select(columns, source)
                )
            await db.commit()

    @staticmethod
    def _validate_model_attempt_filters(
        *,
        session_id: str | None,
        task: str | None,
    ) -> None:
        """集中校验控制面过滤条件，拒绝无界或非内部任务标识。"""

        if session_id is not None and (not session_id or len(session_id) > 80):
            raise InputValidationError("session_id 长度必须在 1 到 80 之间")
        if (
            task is not None
            and _MODEL_ATTEMPT_TASK_RE.fullmatch(task) is None
        ):
            raise InputValidationError("task 必须是安全的小写任务标识")

    async def list_model_attempts(
        self,
        *,
        session_id: str | None = None,
        task: str | None = None,
        limit: int = 100,
    ) -> list[ModelAttempt]:
        """按会话或任务读取最近的安全模型观测。"""

        if limit < 1 or limit > 1000:
            raise InputValidationError("limit 必须在 1 到 1000 之间")
        self._validate_model_attempt_filters(
            session_id=session_id,
            task=task,
        )
        async with self.session_factory() as db:
            query = select(ModelAttemptRow)
            if session_id is not None:
                query = query.where(ModelAttemptRow.session_id == session_id)
            if task is not None:
                query = query.where(ModelAttemptRow.task == task)
            rows = (
                (
                    await db.execute(
                        query.order_by(
                            ModelAttemptRow.started_at.desc(),
                            ModelAttemptRow.id.desc(),
                        ).limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            return [self._model_attempt_from_row(row) for row in rows]

    async def summarize_model_attempts(
        self,
        *,
        session_id: str | None = None,
        task: str | None = None,
    ) -> dict[str, int | float | None]:
        """聚合安全观测；未知 token 或价格继续保持可见，不参与猜算。"""

        self._validate_model_attempt_filters(
            session_id=session_id,
            task=task,
        )
        query = select(
            func.count(ModelAttemptRow.id),
            func.coalesce(
                func.sum(
                    case((ModelAttemptRow.success.is_(True), 1), else_=0)
                ),
                0,
            ),
            func.coalesce(
                func.sum(
                    case((ModelAttemptRow.success.is_(False), 1), else_=0)
                ),
                0,
            ),
            func.coalesce(
                func.sum(
                    case(
                        (ModelAttemptRow.usage_source == "unknown", 1),
                        else_=0,
                    )
                ),
                0,
            ),
            func.coalesce(func.sum(ModelAttemptRow.input_tokens), 0),
            func.coalesce(func.sum(ModelAttemptRow.output_tokens), 0),
            func.coalesce(func.sum(ModelAttemptRow.total_tokens), 0),
            func.avg(ModelAttemptRow.latency_ms),
            func.coalesce(func.sum(ModelAttemptRow.cost_microusd), 0),
            func.coalesce(
                func.sum(
                    case(
                        (ModelAttemptRow.cost_microusd.is_(None), 1),
                        else_=0,
                    )
                ),
                0,
            ),
        )
        if session_id is not None:
            query = query.where(ModelAttemptRow.session_id == session_id)
        if task is not None:
            query = query.where(ModelAttemptRow.task == task)
        async with self.session_factory() as db:
            (
                count,
                success_count,
                error_count,
                usage_unknown_count,
                input_tokens,
                output_tokens,
                total_tokens,
                average_latency_ms,
                known_cost_microusd,
                cost_unknown_count,
            ) = (await db.execute(query)).one()
        return {
            "attempt_count": int(count),
            "success_count": int(success_count),
            "error_count": int(error_count),
            "usage_unknown_count": int(usage_unknown_count),
            "input_tokens": int(input_tokens),
            "output_tokens": int(output_tokens),
            "total_tokens": int(total_tokens),
            "average_latency_ms": (
                round(float(average_latency_ms), 2)
                if average_latency_ms is not None
                else None
            ),
            "known_cost_microusd": int(known_cost_microusd),
            "cost_unknown_count": int(cost_unknown_count),
        }

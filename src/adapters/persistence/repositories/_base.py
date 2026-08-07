"""领域仓储 mixin 共享的 SQLite 上下文与行映射器。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from domain.errors import ConflictError, NotFoundError
from domain.models import TurnDecision

from ..mappers.chat import (
    message_from_row as map_message_from_row,
)
from ..mappers.chat import (
    outbound_from_row as map_outbound_from_row,
)
from ..mappers.memory import (
    memory_from_row as map_memory_from_row,
)
from ..mappers.memory import (
    memory_run_from_row as map_memory_run_from_row,
)
from ..mappers.memory import (
    memory_run_to_row as map_memory_run_to_row,
)
from ..mappers.memory import (
    memory_to_row as map_memory_to_row,
)
from ..mappers.memory import (
    run_from_row as map_run_from_row,
)
from ..mappers.operations import (
    blacklist_from_row as map_blacklist_from_row,
)
from ..mappers.operations import (
    blacklist_to_row as map_blacklist_to_row,
)
from ..mappers.operations import (
    candidate_from_row as map_candidate_from_row,
)
from ..mappers.operations import (
    candidate_to_row as map_candidate_to_row,
)
from ..mappers.operations import (
    drift_run_from_row as map_drift_run_from_row,
)
from ..mappers.operations import (
    drift_run_to_row as map_drift_run_to_row,
)
from ..mappers.operations import (
    expression_from_row as map_expression_from_row,
)
from ..mappers.operations import (
    feed_from_row as map_feed_from_row,
)
from ..mappers.operations import (
    feed_to_row as map_feed_to_row,
)
from ..mappers.operations import (
    group_participation_policy_from_row as map_group_participation_policy_from_row,
)
from ..mappers.operations import (
    image_analysis_from_row as map_image_analysis_from_row,
)
from ..mappers.operations import (
    model_attempt_from_row as map_model_attempt_from_row,
)
from ..mappers.operations import (
    policy_from_row as map_policy_from_row,
)
from ..mappers.operations import (
    proactive_run_from_row as map_proactive_run_from_row,
)
from ..mappers.operations import (
    proactive_run_to_row as map_proactive_run_to_row,
)
from ..mappers.operations import (
    schedule_from_row as map_schedule_from_row,
)
from ..mappers.operations import (
    schedule_run_from_row as map_schedule_run_from_row,
)
from ..mappers.operations import (
    schedule_run_to_row as map_schedule_run_to_row,
)
from ..mappers.operations import (
    schedule_to_row as map_schedule_to_row,
)
from ..mappers.operations import (
    tool_execution_from_row as map_tool_execution_from_row,
)
from ..mappers.operations import (
    update_proactive_candidate_row as map_update_proactive_candidate_row,
)
from ..mappers.operations import (
    update_proactive_run_row as map_update_proactive_run_row,
)
from ..mappers.social import (
    behavior_pattern_from_row as map_behavior_pattern_from_row,
)
from ..mappers.social import (
    behavior_pattern_to_row as map_behavior_pattern_to_row,
)
from ..mappers.social import (
    behavior_selection_from_row as map_behavior_selection_from_row,
)
from ..mappers.social import (
    behavior_selection_to_row as map_behavior_selection_to_row,
)
from ..mappers.social import (
    group_expression_from_row as map_group_expression_from_row,
)
from ..mappers.social import (
    group_expression_to_row as map_group_expression_to_row,
)
from ..mappers.social import (
    jargon_from_row as map_jargon_from_row,
)
from ..mappers.social import (
    jargon_to_row as map_jargon_to_row,
)
from ..mappers.social import (
    load_probability_mapping as map_load_probability_mapping,
)
from ..mappers.social import (
    social_learning_run_from_row as map_social_learning_run_from_row,
)
from ..mappers.social import (
    social_learning_run_to_row as map_social_learning_run_to_row,
)
from ..schema import SessionRow
from ..unit_of_work import SQLiteUnitOfWork


class RepositoryMixinSupport:
    """声明所有 mixin 共享、但只由 DatabaseStore 初始化的状态。"""

    _unit_of_work: SQLiteUnitOfWork
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    _message_fts_available: bool
    _memory_fts_available: bool

    _message_from_row = staticmethod(map_message_from_row)
    _outbound_from_row = staticmethod(map_outbound_from_row)
    _run_from_row = staticmethod(map_run_from_row)
    _memory_to_row = staticmethod(map_memory_to_row)
    _memory_from_row = staticmethod(map_memory_from_row)
    _memory_run_to_row = staticmethod(map_memory_run_to_row)
    _memory_run_from_row = staticmethod(map_memory_run_from_row)
    _load_probability_mapping = staticmethod(map_load_probability_mapping)
    _jargon_to_row = staticmethod(map_jargon_to_row)
    _jargon_from_row = staticmethod(map_jargon_from_row)
    _group_expression_to_row = staticmethod(map_group_expression_to_row)
    _group_expression_from_row = staticmethod(map_group_expression_from_row)
    _behavior_pattern_to_row = staticmethod(map_behavior_pattern_to_row)
    _behavior_pattern_from_row = staticmethod(map_behavior_pattern_from_row)
    _behavior_selection_to_row = staticmethod(map_behavior_selection_to_row)
    _behavior_selection_from_row = staticmethod(map_behavior_selection_from_row)
    _social_learning_run_to_row = staticmethod(map_social_learning_run_to_row)
    _social_learning_run_from_row = staticmethod(map_social_learning_run_from_row)
    _blacklist_from_row = staticmethod(map_blacklist_from_row)
    _blacklist_to_row = staticmethod(map_blacklist_to_row)
    _schedule_to_row = staticmethod(map_schedule_to_row)
    _schedule_from_row = staticmethod(map_schedule_from_row)
    _schedule_run_to_row = staticmethod(map_schedule_run_to_row)
    _schedule_run_from_row = staticmethod(map_schedule_run_from_row)
    _tool_execution_from_row = staticmethod(map_tool_execution_from_row)
    _model_attempt_from_row = staticmethod(map_model_attempt_from_row)
    _expression_from_row = staticmethod(map_expression_from_row)
    _image_analysis_from_row = staticmethod(map_image_analysis_from_row)
    _policy_from_row = staticmethod(map_policy_from_row)
    _group_participation_policy_from_row = staticmethod(
        map_group_participation_policy_from_row
    )
    _feed_to_row = staticmethod(map_feed_to_row)
    _feed_from_row = staticmethod(map_feed_from_row)
    _candidate_to_row = staticmethod(map_candidate_to_row)
    _candidate_from_row = staticmethod(map_candidate_from_row)
    _update_proactive_candidate_row = staticmethod(map_update_proactive_candidate_row)
    _proactive_run_to_row = staticmethod(map_proactive_run_to_row)
    _update_proactive_run_row = staticmethod(map_update_proactive_run_row)
    _proactive_run_from_row = staticmethod(map_proactive_run_from_row)
    _drift_run_to_row = staticmethod(map_drift_run_to_row)
    _drift_run_from_row = staticmethod(map_drift_run_from_row)

    @staticmethod
    async def _require_current_data_epoch(
        db: AsyncSession,
        *,
        session_id: str,
        data_epoch: int,
    ) -> SessionRow:
        """在写事务内校验 Session 与冻结 epoch，阻断旧后台快照提交。"""

        session = await db.get(SessionRow, session_id)
        if session is None:
            raise NotFoundError("后台任务对应会话不存在")
        if session.data_epoch != data_epoch:
            raise ConflictError(
                "后台任务的数据版本已失效",
                details={
                    "session_id": session_id,
                    "expected_epoch": data_epoch,
                    "current_epoch": session.data_epoch,
                },
            )
        return session

    @staticmethod
    async def _advance_group_participation_in_transaction(
        db: AsyncSession,
        *,
        session_id: str,
        expected_state_version: int,
        decision: TurnDecision,
        external_at: datetime,
        observed_at: datetime,
    ) -> None:
        """由 Engagement mixin 实现的跨域事务钩子。"""

        raise NotImplementedError

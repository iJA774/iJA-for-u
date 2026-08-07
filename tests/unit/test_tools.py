import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from adapters.persistence import DatabaseStore
from domain.errors import InputValidationError, NotFoundError
from domain.models import ChatType, InboundMessage, MessageComponent, Participant, ProfileFact, scope_key_for
from tools import ToolContext, ToolRegistry, build_history_tools, build_profile_tools
from tools.information import CurrentTimeArguments, WeatherArguments, WeatherClient, get_current_time


@pytest.mark.asyncio
async def test_current_time_rejects_unknown_timezone() -> None:
    with pytest.raises(InputValidationError, match="IANA"):
        await get_current_time(
            CurrentTimeArguments(timezone="Mars/Olympus"),
            ToolContext(session_id="session-test", actor_id="u1"),
        )


@pytest.mark.asyncio
async def test_weather_resolves_location_and_limits_forecast(settings) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "geocoding-api.open-meteo.com":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "name": "北京",
                            "admin1": "北京市",
                            "country": "中国",
                            "latitude": 39.9,
                            "longitude": 116.4,
                            "timezone": "Asia/Shanghai",
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "current": {
                        "temperature_2m": 26,
                        "apparent_temperature": 27,
                        "weather_code": 0,
                    },
                    "daily": {"time": ["2026-07-22"]},
                }
            ).encode(),
        )

    client = WeatherClient(settings, transport=httpx.MockTransport(handler))
    result = await client.get_weather(
        WeatherArguments(location="北京", days=7),
        ToolContext(session_id="session-test", actor_id="u1"),
    )
    assert result["resolved_name"] == "中国 北京市 北京"
    assert result["summary"].endswith("26°C，体感 27°C")
    assert requests[1].url.params["forecast_days"] == "7"
    await client.close()


@pytest.mark.asyncio
async def test_history_and_profile_tools_are_limited_to_current_session(settings) -> None:
    """迁移的查询工具只能读取调用上下文所属会话。"""

    store = DatabaseStore(settings.storage.database_path)
    await store.initialize()
    session = await store.create_session(
        platform="web-simulator",
        account_id="ija-local",
        external_chat_id="tool-history",
        chat_type=ChatType.PRIVATE,
        display_name="工具测试",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )
    message, created = await store.append_inbound(
        InboundMessage(
            platform="web-simulator",
            account_id="ija-local",
            external_message_id="tool-history-1",
            external_chat_id="tool-history",
            chat_type=ChatType.PRIVATE,
            sender_id="u1",
            sender_name="小明",
            components=[MessageComponent.text_component("我喜欢手冲咖啡")],
        )
    )
    assert created
    await store.save_fact(
        ProfileFact(
            subject_id="u1",
            scope_key=scope_key_for(session),
            category="偏好",
            content="喜欢手冲咖啡",
            confidence=0.9,
            source_message_ids=[message.id],
        )
    )
    registry = ToolRegistry([*build_history_tools(store), *build_profile_tools(store)])
    context = ToolContext(session_id=session.id, actor_id="u1")

    history = await registry.execute("fetch_history", {"limit": 10}, context)
    profile = await registry.execute("query_profile", {}, context)

    assert history.value["messages"][0]["content"] == "我喜欢手冲咖啡"
    facts = profile.value["facts"]
    assert len(facts) == 1
    assert facts[0]["category"] == "偏好"
    assert facts[0]["content"] == "喜欢手冲咖啡"
    assert facts[0]["source_message_ids"] == [message.id]
    await store.close()


@pytest.mark.asyncio
async def test_history_search_cursor_and_precise_source_are_session_scoped(settings) -> None:
    store = DatabaseStore(settings.storage.database_path)
    await store.initialize()
    session = await store.create_session(
        platform="web-simulator",
        account_id="ija-local",
        external_chat_id="history-page",
        chat_type=ChatType.PRIVATE,
        display_name="分页历史",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )
    other = await store.create_session(
        platform="web-simulator",
        account_id="ija-local",
        external_chat_id="history-other",
        chat_type=ChatType.PRIVATE,
        display_name="其他历史",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )
    base = datetime(2026, 7, 27, tzinfo=UTC)
    created = []
    for index, text in enumerate(["第一条咖啡记录", "第二条茶记录", "第三条咖啡记录"]):
        item, _ = await store.append_inbound(
            InboundMessage(
                platform="web-simulator",
                account_id="ija-local",
                external_message_id=f"page-{index}",
                external_chat_id="history-page",
                chat_type=ChatType.PRIVATE,
                sender_id="u1",
                sender_name="小明",
                components=[MessageComponent.text_component(text)],
                received_at=base + timedelta(seconds=index),
            )
        )
        created.append(item)
    other_message, _ = await store.append_inbound(
        InboundMessage(
            platform="web-simulator",
            account_id="ija-local",
            external_message_id="other-1",
            external_chat_id="history-other",
            chat_type=ChatType.PRIVATE,
            sender_id="u1",
            sender_name="小明",
            components=[MessageComponent.text_component("其他会话秘密")],
            received_at=base,
        )
    )
    registry = ToolRegistry(build_history_tools(store))
    context = ToolContext(session_id=session.id, actor_id="u1")

    first = await registry.execute("fetch_history", {"limit": 2}, context)
    assert [item["content"] for item in first.value["messages"]] == [
        "第三条咖啡记录",
        "第二条茶记录",
    ]
    assert first.value["has_more"] is True
    second = await registry.execute(
        "fetch_history",
        {"limit": 2, "cursor": first.value["next_cursor"]},
        context,
    )
    assert [item["content"] for item in second.value["messages"]] == ["第一条咖啡记录"]

    search = await registry.execute(
        "search_messages", {"query": "咖啡", "limit": 10}, context
    )
    assert [item["message_id"] for item in search.value["messages"]] == [
        created[2].id,
        created[0].id,
    ]
    source = await registry.execute(
        "fetch_source",
        {"source_ref": f"message:{created[0].id}"},
        context,
    )
    assert source.value["source"]["content"] == "第一条咖啡记录"
    with pytest.raises(NotFoundError):
        await registry.execute(
            "fetch_source",
            {"source_ref": f"message:{other_message.id}"},
            context,
        )
    assert other.id != session.id
    await store.close()

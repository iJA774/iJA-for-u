"""首批无外部写入副作用的信息工具。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import BaseModel, Field

from config import AppSettings
from domain.errors import InputValidationError, ToolExecutionError
from tools.registry import RegisteredTool, ToolContext


class CurrentTimeArguments(BaseModel):
    timezone: str = Field(min_length=1, max_length=100)


class WeatherArguments(BaseModel):
    location: str = Field(min_length=2, max_length=100)
    days: int = Field(default=1, ge=1, le=7)


WEATHER_TEXT = {
    0: "晴",
    1: "大致晴朗",
    2: "局部多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "强毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    80: "小阵雨",
    81: "阵雨",
    82: "强阵雨",
    95: "雷暴",
    96: "雷暴伴小冰雹",
    99: "雷暴伴强冰雹",
}


async def get_current_time(arguments: BaseModel, _: ToolContext) -> dict[str, Any]:
    """返回指定 IANA 时区的当前本地时间。"""

    assert isinstance(arguments, CurrentTimeArguments)
    try:
        zone = ZoneInfo(arguments.timezone)
    except ZoneInfoNotFoundError as exc:
        raise InputValidationError(f"未知 IANA 时区: {arguments.timezone}") from exc
    now = datetime.now(zone)
    return {
        "timezone": arguments.timezone,
        "iso8601": now.isoformat(),
        "weekday": now.isoweekday(),
        "summary": f"{arguments.timezone} 当前时间为 {now:%Y-%m-%d %H:%M:%S}",
    }


class WeatherClient:
    """对 Open-Meteo 地理编码和天气接口的受限只读客户端。"""

    def __init__(self, settings: AppSettings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.max_response_bytes = settings.tools.weather_max_response_bytes
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.tools.weather_timeout_seconds),
            transport=transport,
            follow_redirects=False,
        )

    async def _json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self.client.get(url, params=params)
            response.raise_for_status()
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ToolExecutionError("天气服务网络请求失败") from exc
        except httpx.HTTPStatusError as exc:
            raise ToolExecutionError(f"天气服务返回 HTTP {exc.response.status_code}") from exc
        if len(response.content) > self.max_response_bytes:
            raise ToolExecutionError("天气服务响应超过大小上限")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ToolExecutionError("天气服务返回了无效 JSON") from exc
        if not isinstance(payload, dict):
            raise ToolExecutionError("天气服务响应结构无效")
        if payload.get("error"):
            raise ToolExecutionError(f"天气服务拒绝请求: {payload.get('reason', '未知原因')}")
        return payload

    async def get_weather(self, arguments: BaseModel, _: ToolContext) -> dict[str, Any]:
        assert isinstance(arguments, WeatherArguments)
        geocoding = await self._json(
            "https://geocoding-api.open-meteo.com/v1/search",
            {"name": arguments.location, "count": 5, "language": "zh", "format": "json"},
        )
        results = geocoding.get("results")
        if not isinstance(results, list) or not results:
            raise InputValidationError(f"找不到地点: {arguments.location}")
        location = results[0]
        try:
            latitude = float(location["latitude"])
            longitude = float(location["longitude"])
            timezone = str(location["timezone"])
            resolved_name = " ".join(
                str(value)
                for value in (
                    location.get("country"),
                    location.get("admin1"),
                    location.get("name"),
                )
                if value
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ToolExecutionError("地理编码响应缺少必要字段") from exc
        forecast = await self._json(
            "https://api.open-meteo.com/v1/forecast",
            {
                "latitude": latitude,
                "longitude": longitude,
                "timezone": timezone,
                "forecast_days": arguments.days,
                "current": (
                    "temperature_2m,relative_humidity_2m,apparent_temperature,"
                    "precipitation,weather_code,wind_speed_10m"
                ),
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum",
            },
        )
        current = forecast.get("current")
        daily = forecast.get("daily")
        if not isinstance(current, dict) or not isinstance(daily, dict):
            raise ToolExecutionError("天气响应缺少 current 或 daily")
        code = int(current.get("weather_code", -1))
        summary = (
            f"{resolved_name} 当前{WEATHER_TEXT.get(code, f'天气代码 {code}')}，"
            f"{current.get('temperature_2m')}°C，体感 {current.get('apparent_temperature')}°C"
        )
        candidates = [
            {
                "name": item.get("name"),
                "admin1": item.get("admin1"),
                "country": item.get("country"),
            }
            for item in results[:3]
        ]
        return {
            "query": arguments.location,
            "resolved_name": resolved_name,
            "timezone": timezone,
            "latitude": latitude,
            "longitude": longitude,
            "current": current,
            "daily": daily,
            "candidates": candidates,
            "summary": summary,
        }

    async def close(self) -> None:
        await self.client.aclose()


def build_information_tools(weather: WeatherClient) -> list[RegisteredTool]:
    """构造所有对普通聊天和后台任务开放的只读工具。"""

    return [
        RegisteredTool(
            name="get_current_time",
            description="查询指定 IANA 时区的当前日期、时间和星期。不得猜测当前时间。",
            arguments_model=CurrentTimeArguments,
            handler=get_current_time,
        ),
        RegisteredTool(
            name="get_weather",
            description="查询一个地点的当前天气和未来 1 到 7 天预报。",
            arguments_model=WeatherArguments,
            handler=weather.get_weather,
        ),
    ]

import asyncio
import re
from typing import Any, cast

import aiohttp
from loguru import logger

from twipsybot.plugin import PluginBase


class WeatherPlugin(PluginBase):
    description = "天气插件，查询指定城市的天气信息"
    _LOCATION_TOKEN = r"[\u4e00-\u9fa5A-Za-z]+(?:\s+[\u4e00-\u9fa5A-Za-z]+)*"
    LOCATION_BEFORE_KEYWORD_PATTERN = re.compile(
        rf"(?P<loc>{_LOCATION_TOKEN})\s*(?:天气|weather)", re.IGNORECASE
    )
    LOCATION_AFTER_KEYWORD_PATTERN = re.compile(
        rf"(?:天气|weather)\s*(?P<loc>{_LOCATION_TOKEN})", re.IGNORECASE
    )
    MENTION_PATTERN = re.compile(r"@\w+\s*")

    def __init__(self, context):
        super().__init__(context)
        self.api_key = self.config.get("api_key", "")
        self.enabled = self.enabled and bool(self.api_key)
        self.base_url = "https://api.openweathermap.org/data/2.5/weather"
        self.geocoding_url = "https://api.openweathermap.org/geo/1.0/direct"
        self.session: aiohttp.ClientSession | None = None

    async def initialize(self) -> bool:
        if not self.api_key:
            logger.warning("Weather plugin missing API key; disabling plugin")
            self.enabled = False
            return False
        self.session = aiohttp.ClientSession()
        self._register_resource(self.session, "close")
        self._log_plugin_action("initialized")
        return True

    async def on_mention(self, data: dict[str, Any]) -> dict[str, Any] | None:
        note_data = (
            data.get("note", data) if "note" in data and "type" in data else data
        )
        return await self._process_weather_message(note_data)

    async def on_message(self, message_data: dict[str, Any]) -> dict[str, Any] | None:
        return await self._process_weather_message(message_data)

    async def _process_weather_message(
        self, data: dict[str, Any]
    ) -> dict[str, Any] | None:
        text = data.get("text") or ""
        if "天气" not in text and "weather" not in text:
            return None
        username = self._extract_username(data)
        cleaned_text = self.MENTION_PATTERN.sub("", text)
        location_match = self.LOCATION_BEFORE_KEYWORD_PATTERN.search(
            cleaned_text
        ) or self.LOCATION_AFTER_KEYWORD_PATTERN.search(cleaned_text)
        return await self._handle_weather_request(username, location_match)

    async def _handle_weather_request(
        self, username: str, location_match
    ) -> dict[str, Any] | None:
        location = location_match.group("loc").strip() if location_match else ""
        if not location:
            return self.handled("请指定要查询的城市，例如：北京天气 或 天气上海")
        self._log_plugin_action(
            "handling weather request", f"from @{username}: {location}"
        )
        weather_info = await self._get_weather(location)
        return self.handled(weather_info or f"抱歉，无法获取 {location} 的天气信息。")

    async def _get_weather(self, city: str) -> str | None:
        try:
            session = self.session
            if session is None or session.closed:
                return None
            session = cast("aiohttp.ClientSession", session)
            coordinates = await self._get_coordinates(city)
            if not coordinates:
                return f"抱歉，找不到城市 '{city}' 的位置信息。"
            lat, lon, display_name = coordinates
            params = {
                "lat": lat,
                "lon": lon,
                "appid": self.api_key,
                "units": "metric",
                "lang": "zh_cn",
            }
            async with session.get(self.base_url, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    return self._format_weather_info_v25(data, display_name)
                logger.warning(
                    f"Weather API v2.5 request failed: status={response.status}"
                )
                return "抱歉，天气服务暂时不可用。"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Failed to fetch weather data: {e}")
            return "抱歉，获取天气信息时出现错误。"

    async def _get_coordinates(self, city: str) -> tuple[float, float, str] | None:
        try:
            session = self.session
            if session is None or session.closed:
                return None
            session = cast("aiohttp.ClientSession", session)
            params = {"q": city, "limit": 1, "appid": self.api_key}
            async with session.get(self.geocoding_url, params=params) as response:
                if response.status != 200:
                    logger.warning(
                        f"Geocoding API request failed: status={response.status}"
                    )
                    return None
                data = await response.json()
                if not data:
                    return None
                location = data[0]
                display_name = location["name"]
                if "country" in location:
                    display_name += f", {location['country']}"
                return float(location["lat"]), float(location["lon"]), display_name
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Failed to fetch city coordinates: {e}")
            return None

    @staticmethod
    def _format_weather_info_v25(data: dict[str, Any], display_name: str) -> str:
        try:
            main = data["main"]
            temp = round(main["temp"])
            feels_like = round(main["feels_like"])
            humidity = main["humidity"]
            pressure = main["pressure"]
            description = data["weather"][0]["description"]
            wind_speed = data.get("wind", {}).get("speed", 0)
            visibility = (
                data.get("visibility", 0) / 1000 if data.get("visibility") else 0
            )
            weather_text = (
                f"🌤️ {display_name} 的天气:\n"
                f"🌡️ 温度: {temp}°C (体感 {feels_like}°C)\n"
                f"💧 湿度: {humidity}%\n"
                f"☁️ 天气: {description}\n"
                f"💨 风速: {wind_speed} m/s\n"
                f"🌊 气压: {pressure} hPa"
            )
            if visibility > 0:
                weather_text += f"\n👁️ 能见度: {visibility:.1f} km"
            return weather_text
        except Exception as e:
            logger.error(f"Error parsing Weather API v2.5 data: {e}")
            return "抱歉，天气数据解析失败。"

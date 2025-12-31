import re
from typing import Any, Optional

import aiohttp
from loguru import logger

from src.plugin import PluginBase


class WeatherPlugin(PluginBase):
    description = "天气插件，查询指定城市的天气信息"
    LOCATION_PATTERN = re.compile(
        r"([\u4e00-\u9fa5a-zA-Z]+(?:\s+[\u4e00-\u9fa5a-zA-Z]+)*)(?:天气|weather)|(?:天气|weather)\s*([\u4e00-\u9fa5a-zA-Z]+(?:\s+[\u4e00-\u9fa5a-zA-Z]+)*)"
    )
    MENTION_PATTERN = re.compile(r"@\w+\s*")

    def __init__(self, context):
        super().__init__(context)
        self.api_key = self.config.get("api_key", "")
        self.enabled = self.enabled and bool(self.api_key)
        self.base_url = "https://api.openweathermap.org/data/2.5/weather"
        self.geocoding_url = "https://api.openweathermap.org/geo/1.0/direct"
        self.session: Optional[aiohttp.ClientSession] = None

    async def initialize(self) -> bool:
        if not self.api_key:
            logger.warning("Weather 插件未配置 API 密钥，插件将被禁用")
            self.enabled = False
            return False
        self.session = aiohttp.ClientSession()
        self._register_resource(self.session, "close")
        self._log_plugin_action("初始化完成")
        return True

    async def cleanup(self) -> None:
        await super().cleanup()

    async def on_mention(self, data: dict[str, Any]) -> Optional[dict[str, Any]]:
        try:
            note_data = (
                data.get("note", data) if "note" in data and "type" in data else data
            )
            return await self._process_weather_message(note_data)
        except (ValueError, KeyError) as e:
            logger.error(f"Weather 插件处理提及时出错: {e}")
            return None

    async def on_message(
        self, message_data: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        try:
            return await self._process_weather_message(message_data)
        except (ValueError, KeyError) as e:
            logger.error(f"Weather 插件处理消息时出错: {e}")
            return None

    async def _process_weather_message(
        self, data: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        text = data.get("text", "")
        if "天气" not in text and "weather" not in text:
            return None
        username = self._extract_username(data)
        cleaned_text = self.MENTION_PATTERN.sub("", text)
        location_match = self.LOCATION_PATTERN.search(cleaned_text)
        return await self._handle_weather_request(username, location_match)

    async def _handle_weather_request(
        self, username: str, location_match
    ) -> Optional[dict[str, Any]]:
        location = (
            (location_match.group(1) or location_match.group(2) or "").strip()
            if location_match
            else ""
        )
        if not location:
            return {
                "handled": True,
                "plugin_name": self.name,
                "response": "请指定要查询的城市，例如：北京天气 或 天气上海",
            }
        self._log_plugin_action("处理天气查询", f"来自 @{username}，查询 {location}")
        weather_info = await self._get_weather(location)
        response = {
            "handled": True,
            "plugin_name": self.name,
            "response": weather_info or f"抱歉，无法获取 {location} 的天气信息。",
        }
        return (
            response
            if self._validate_plugin_response(response)
            else (logger.error("Weather 插件响应验证失败") or None)
        )

    async def _get_weather(self, city: str) -> Optional[str]:
        try:
            if not self.session or self.session.closed:
                return None
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
            async with self.session.get(self.base_url, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    return self._format_weather_info_v25(data, display_name)
                logger.warning(f"Weather API 2.5 请求失败，状态码: {response.status}")
                return "抱歉，天气服务暂时不可用。"
        except (aiohttp.ClientError, OSError, ValueError, KeyError) as e:
            logger.error(f"获取天气信息失败: {e}")
            return "抱歉，获取天气信息时出现错误。"

    async def _get_coordinates(self, city: str) -> Optional[tuple]:
        try:
            if not self.session or self.session.closed:
                return None
            params = {"q": city, "limit": 1, "appid": self.api_key}
            async with self.session.get(self.geocoding_url, params=params) as response:
                if response.status != 200:
                    logger.warning(f"Geocoding API 请求失败，状态码: {response.status}")
                    return None
                data = await response.json()
                if not data:
                    return None
                location = data[0]
                display_name = location["name"]
                if "country" in location:
                    display_name += f", {location['country']}"
                return location["lat"], location["lon"], display_name
        except (aiohttp.ClientError, OSError, ValueError, KeyError) as e:
            logger.error(f"获取城市坐标失败: {e}")
            return None

    def _format_weather_info_v25(self, data: dict[str, Any], display_name: str) -> str:
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
        except (ValueError, KeyError, TypeError) as e:
            logger.error(f"解析 Weather API 2.5 天气数据时出错: {e}")
            return "抱歉，天气数据解析失败。"

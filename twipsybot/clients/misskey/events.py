import asyncio
import inspect
import json
from typing import Any

from loguru import logger

from .channels import CHAT_CHANNELS, NOTE_CHANNELS, ChannelType

__all__ = ("_StreamingEventsMixin",)

_EVENT_DATA_LOG_TEMPLATE = "Event data: {}"


class _StreamingEventsMixin:
    async def _handle_channel_message(self, body: dict[str, Any]) -> None:
        channel_id = body.get("id")
        if channel_id not in self.channels:
            logger.debug(f"Message received for unknown channel: {channel_id}")
            return
        channel_info = self.channels[channel_id]
        channel_name = channel_info.get("name", "unknown")
        outer_type = body.get("type")
        event_body = body.get("body")
        if not isinstance(outer_type, str) or not outer_type:
            logger.debug(
                f"Received {channel_name} data without standard event type; skipping (channel_id={channel_id})"
            )
            if self.log_dump_events:
                logger.opt(lazy=True).debug(
                    _EVENT_DATA_LOG_TEMPLATE,
                    lambda: json.dumps(body, ensure_ascii=False, indent=2),
                )
            return
        if event_body is None:
            event_body = {}
        event_data: dict[str, Any] = {"type": outer_type, "body": event_body}
        event_type, event_data = self._normalize_channel_event(channel_name, event_data)
        if isinstance(event_data, dict) and "streamingChannelId" not in event_data:
            event_data["streamingChannelId"] = channel_id
        event_id = self._extract_event_id(event_data, event_type)
        if self._is_duplicate_event(event_id, event_type):
            return
        self._track_event(event_id, event_type)
        if event_type:
            logger.debug(
                f"Received {channel_name} event: {event_type} (channel_id={channel_id}, event_id={event_id})"
            )
        await self._enqueue_event(channel_name, event_data)

    def _normalize_channel_event(
        self, channel_name: str, event_data: dict[str, Any]
    ) -> tuple[str | None, dict[str, Any]]:
        event_type = event_data.get("type")
        if channel_name == ChannelType.MAIN.value:
            return self._normalize_main_channel_event(event_type, event_data)
        if channel_name in CHAT_CHANNELS:
            return self._normalize_chat_channel_event(event_type, event_data)
        return event_type, event_data

    @staticmethod
    def _extract_event_id(
        event_data: dict[str, Any], event_type: str | None
    ) -> str | None:
        event_id = event_data.get("id")
        if event_id or event_type != "note":
            return event_id
        inner_id = (event_data.get("body") or {}).get("id")
        return inner_id if isinstance(inner_id, str) else None

    def _normalize_main_channel_event(
        self, event_type: Any, event_data: dict[str, Any]
    ) -> tuple[str | None, dict[str, Any]]:
        if not isinstance(event_type, str) or not event_type:
            return event_type, event_data
        payload = event_data.get("body")
        if not isinstance(payload, dict):
            return event_type, event_data
        normalizers = {
            "mention": lambda: self._wrap_note_event("mention", payload),
            "reply": lambda: self._wrap_note_event("reply", payload),
            "newChatMessage": lambda: self._wrap_new_chat_message(payload),
            "notification": lambda: self._wrap_notification(payload),
            "unreadNotification": lambda: self._wrap_notification(payload),
        }
        normalizer = normalizers.get(event_type)
        return normalizer() if normalizer else (event_type, event_data)

    def _normalize_chat_channel_event(
        self, event_type: Any, event_data: dict[str, Any]
    ) -> tuple[str | None, dict[str, Any]]:
        if event_type != "message":
            return event_type, event_data
        payload = event_data.get("body")
        if not isinstance(payload, dict):
            return event_type, event_data
        normalized = dict(payload)
        normalized.setdefault("type", "message")
        msg_id = normalized.get("id")
        if isinstance(msg_id, str) and msg_id:
            normalized["id"] = msg_id
        return "message", normalized

    @staticmethod
    def _extract_dict(container: dict[str, Any], key: str) -> dict[str, Any] | None:
        value = container.get(key)
        return value if isinstance(value, dict) else None

    def _wrap_note_event(
        self, event_type: str, note: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        note_id = note.get("id") if isinstance(note.get("id"), str) else None
        wrapped: dict[str, Any] = {"type": event_type, "note": note}
        if note_id:
            wrapped["id"] = note_id
        return event_type, wrapped

    @staticmethod
    def _wrap_new_chat_message(message: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        normalized = dict(message)
        normalized["type"] = "newChatMessage"
        return "newChatMessage", normalized

    @staticmethod
    def _wrap_notification(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        wrapped: dict[str, Any] = {"type": "notification", "notification": payload}
        notification_id = payload.get("id")
        if isinstance(notification_id, str) and notification_id:
            wrapped["id"] = notification_id
        return "notification", wrapped

    def _ensure_workers_started(self) -> None:
        if self._workers:
            return
        self._workers = [
            asyncio.create_task(self._worker_loop(), name=f"stream-worker-{i}")
            for i in range(self._worker_count)
        ]

    async def _stop_workers(self) -> None:
        if not self._workers:
            return
        while not self._event_queue.empty():
            try:
                self._event_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        for _ in self._workers:
            await self._event_queue.put(None)
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    async def _enqueue_event(self, channel_name: str, event_data: dict[str, Any]):
        try:
            await asyncio.wait_for(
                self._event_queue.put((channel_name, event_data)),
                timeout=self._queue_put_timeout,
            )
        except TimeoutError:
            event_id = event_data.get("id", "unknown")
            event_type = event_data.get("type", "unknown")
            logger.warning(
                f"Event queue congested; dropping event: {event_type} (id={event_id})"
            )

    async def _worker_loop(self) -> None:
        while True:
            item = await self._event_queue.get()
            if item is None:
                return
            channel_name, event_data = item
            try:
                await self._dispatch_event(channel_name, event_data)
            except Exception as e:
                if isinstance(e, asyncio.CancelledError):
                    raise
                logger.exception(f"Failed to process event: {e}")

    async def _dispatch_event(
        self, channel_name: str, event_data: dict[str, Any]
    ) -> None:
        event_type = event_data.get("type")
        if not event_type:
            self._handle_no_event_type(channel_name, event_data)
        else:
            await self._handle_typed_event(channel_name, event_type, event_data)

    def _handle_no_event_type(
        self, channel_name: str, event_data: dict[str, Any]
    ) -> None:
        event_id = event_data.get("id", "unknown")
        logger.debug(
            f"Received data without event type - channel: {channel_name}, event_id={event_id}"
        )
        if self.log_dump_events:
            logger.opt(lazy=True).debug(
                _EVENT_DATA_LOG_TEMPLATE,
                lambda: json.dumps(event_data, ensure_ascii=False, indent=2),
            )

    async def _handle_typed_event(
        self,
        channel_name: str,
        event_type: str,
        event_data: dict[str, Any],
    ) -> None:
        if channel_name == ChannelType.MAIN.value:
            await self._handle_main_channel_event(event_type, event_data)
            return
        if channel_name in CHAT_CHANNELS:
            await self._handle_chat_channel_event(channel_name, event_type, event_data)
            return
        if channel_name in NOTE_CHANNELS:
            await self._handle_note_channel_event(channel_name, event_type, event_data)

    async def _handle_main_channel_event(
        self, event_type: str, event_data: dict[str, Any]
    ) -> None:
        if event_type == "newChatMessage":
            await self._handle_main_new_chat_message(event_data)
            return
        if event_type == "notification":
            await self._handle_main_notification(event_data)
            return
        handler_event_type = self._main_handler_event_type(event_type)
        if handler_event_type:
            await self._call_handlers(handler_event_type, event_data)
            return
        self._log_unknown_main_event(event_type, event_data)

    async def _handle_main_new_chat_message(self, event_data: dict[str, Any]) -> None:
        channel_id = await self._ensure_chat_user_stream(event_data)
        if not channel_id:
            return
        message = dict(event_data)
        message["streamingChannelId"] = channel_id
        message["type"] = "message"
        await self._handle_chat_channel_event(
            ChannelType.CHAT_USER.value, "message", message
        )

    async def _handle_main_notification(self, event_data: dict[str, Any]) -> None:
        notification = self._extract_dict(event_data, "notification")
        inner_type = notification.get("type") if notification else None
        if inner_type in {"mention", "reply", "newChatMessage"}:
            return
        if (
            isinstance(inner_type, str)
            and inner_type
            and inner_type in self.event_handlers
        ):
            await self._call_handlers(inner_type, notification)
            return
        await self._call_handlers("notification", event_data)

    @staticmethod
    def _main_handler_event_type(event_type: str) -> str | None:
        if event_type in {"mention", "reply"}:
            return "mention"
        return None

    def _log_unknown_main_event(
        self, event_type: str, event_data: dict[str, Any]
    ) -> None:
        logger.debug(f"Unknown main channel event type: {event_type}")
        if self.log_dump_events:
            logger.opt(lazy=True).debug(
                _EVENT_DATA_LOG_TEMPLATE,
                lambda: json.dumps(event_data, ensure_ascii=False, indent=2),
            )

    async def _handle_chat_channel_event(
        self, channel_name: str, event_type: str, event_data: dict[str, Any]
    ) -> None:
        if event_type != "message":
            logger.debug(f"Unknown {channel_name} channel event type: {event_type}")
            if self.log_dump_events:
                logger.opt(lazy=True).debug(
                    _EVENT_DATA_LOG_TEMPLATE,
                    lambda: json.dumps(event_data, ensure_ascii=False, indent=2),
                )
            return
        message = dict(event_data)
        from_user_id = message.get("fromUserId")
        if (
            isinstance(from_user_id, str)
            and from_user_id
            and "fromUser" not in message
            and from_user_id in self._chat_user_cache
        ):
            message["fromUser"] = self._chat_user_cache[from_user_id]
        channel_id = message.get("streamingChannelId")
        msg_id = message.get("id")
        if (
            isinstance(channel_id, str)
            and channel_id
            and isinstance(msg_id, str)
            and msg_id
        ):
            await self._send_channel_message(channel_id, "read", {"id": msg_id})
            self._refresh_chat_channel_timer(channel_id)
        await self._call_handlers("message", message)

    async def _ensure_chat_user_stream(self, message: dict[str, Any]) -> str | None:
        other_id = message.get("fromUserId")
        if not isinstance(other_id, str) or not other_id:
            return None
        from_user = message.get("fromUser")
        if isinstance(from_user, dict):
            self._chat_user_cache[other_id] = from_user
        try:
            channel_id = await self.connect_channel(
                ChannelType.CHAT_USER, {"otherId": other_id}
            )
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.debug(f"Failed to connect chatUser channel for {other_id}: {e}")
            return None
        self._chat_user_channel_ids[other_id] = channel_id
        self._chat_channel_other_ids[channel_id] = other_id
        if task := self._chat_channel_tasks.get(channel_id):
            task.cancel()
        self._chat_channel_tasks[channel_id] = asyncio.create_task(
            self._disconnect_chat_channel_later(other_id, channel_id),
            name=f"chatUser-disconnect-{other_id}",
        )
        return channel_id

    async def _disconnect_chat_channel_later(
        self, other_id: str, channel_id: str
    ) -> None:
        try:
            await asyncio.sleep(120)
            await self.disconnect_channel_id(channel_id)
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.debug(f"Failed to disconnect chatUser channel {channel_id}: {e}")
        finally:
            if self._chat_user_channel_ids.get(other_id) == channel_id:
                self._chat_user_channel_ids.pop(other_id, None)
            self._chat_channel_other_ids.pop(channel_id, None)
            self._chat_channel_tasks.pop(channel_id, None)

    def _refresh_chat_channel_timer(self, channel_id: str) -> None:
        other_id = self._chat_channel_other_ids.get(channel_id)
        if not other_id:
            return
        if task := self._chat_channel_tasks.get(channel_id):
            task.cancel()
        self._chat_channel_tasks[channel_id] = asyncio.create_task(
            self._disconnect_chat_channel_later(other_id, channel_id),
            name=f"chatUser-disconnect-{other_id}",
        )

    def _cancel_chat_channel_tasks(self) -> None:
        tasks = list(self._chat_channel_tasks.values())
        self._chat_channel_tasks.clear()
        self._chat_user_channel_ids.clear()
        self._chat_channel_other_ids.clear()
        self._chat_user_cache.clear()
        for task in tasks:
            task.cancel()

    async def _handle_note_channel_event(
        self, channel_name: str, event_type: str, event_data: dict[str, Any]
    ) -> None:
        if event_type != "note":
            logger.debug(f"Unknown {channel_name} channel event type: {event_type}")
            if self.log_dump_events:
                logger.opt(lazy=True).debug(
                    _EVENT_DATA_LOG_TEMPLATE,
                    lambda: json.dumps(event_data, ensure_ascii=False, indent=2),
                )
            return
        payload = event_data.get("body")
        if not isinstance(payload, dict):
            payload = event_data
        else:
            payload = dict(payload)
        if isinstance(payload, dict) and "streamingChannel" not in payload:
            payload["streamingChannel"] = channel_name
        logger.debug(f"Received {channel_name} note")
        if channel_name == ChannelType.ANTENNA.value:
            logger.debug(f"Antenna note received: {payload.get('id', 'unknown')}")
        if self.log_dump_events:
            logger.opt(lazy=True).debug(
                _EVENT_DATA_LOG_TEMPLATE,
                lambda: json.dumps(payload, ensure_ascii=False, indent=2),
            )
        await self._call_handlers("note", payload)

    async def _call_handlers(self, event_type: str, data: dict[str, Any]) -> None:
        handlers = self.event_handlers.get(event_type, [])
        for handler in handlers:
            try:
                if inspect.iscoroutinefunction(handler):
                    await handler(data)
                else:
                    handler(data)
            except Exception as e:
                if isinstance(e, asyncio.CancelledError):
                    raise
                logger.exception(f"Event handler failed ({event_type}): {e}")

    def _is_duplicate_event(self, event_id: str | None, event_type: str | None) -> bool:
        dedup_key = self._event_dedup_key(event_id, event_type)
        if dedup_key and dedup_key in self.processed_events:
            logger.debug(
                f"Duplicate event detected; skipping - {event_type}, event_id={event_id}"
            )
            return True
        return False

    def _track_event(self, event_id: str | None, event_type: str | None) -> None:
        self._track_dedup_key(self._event_dedup_key(event_id, event_type))

    def _track_dedup_key(self, dedup_key: str | None) -> None:
        if dedup_key:
            self.processed_events[dedup_key] = True

    @staticmethod
    def _event_dedup_key(event_id: str | None, event_type: str | None) -> str | None:
        if not event_id:
            return None
        if not event_type:
            return event_id
        if event_type in {"newChatMessage", "message"}:
            return f"chatMessage:{event_id}"
        return f"{event_type}:{event_id}"

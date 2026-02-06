from typing import Any

__all__ = ("PluginContext",)


class PluginContext:
    def __init__(self, name: str, config: dict[str, Any], **context_objects):
        self.name = name
        self.config = config
        for key, value in context_objects.items():
            setattr(self, key, value)

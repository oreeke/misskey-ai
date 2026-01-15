import asyncio
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

from ..bot.core import MisskeyBot
from ..shared.config import Config
from ..shared.constants import ConfigKeys
from ..shared.exceptions import (
    APIConnectionError,
    AuthenticationError,
    ConfigurationError,
)


class BotRunner:
    def __init__(self):
        self.bot: MisskeyBot | None = None
        self.shutdown_event: asyncio.Event | None = None
        self._shutdown_called = False

    async def run(self) -> None:
        self.shutdown_event = asyncio.Event()
        load_dotenv()
        config = Config()
        await config.load()
        log_path = Path(config.get(ConfigKeys.LOG_PATH))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            log_path,
            level=config.get(ConfigKeys.LOG_LEVEL),
            rotation="10 MB",
            compression="zip",
            enqueue=True,
        )
        logger.info("Starting bot...")
        try:
            self.bot = MisskeyBot(config)
            await self.bot.start()
            self._setup_monitoring_and_signals()
            await self.shutdown_event.wait()
        finally:
            await self.shutdown()

    def _setup_monitoring_and_signals(self) -> None:
        signals = (
            (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
            if sys.platform != "win32"
            else (signal.SIGINT, signal.SIGTERM)
        )

        def signal_handler(sig, _):
            logger.info(
                f"Received signal {signal.Signals(sig).name}; preparing to shut down..."
            )
            if self.shutdown_event and not self.shutdown_event.is_set():
                self.shutdown_event.set()
                try:
                    loop = asyncio.get_running_loop()
                    loop.call_soon_threadsafe(lambda: None)
                except RuntimeError:
                    pass

        for sig in signals:
            try:
                signal.signal(sig, signal_handler)
            except Exception:
                logger.warning(f"Failed to register signal handler: {sig}")

    async def shutdown(self) -> None:
        if self._shutdown_called:
            return
        self._shutdown_called = True
        logger.info("Shutting down bot...")
        if self.bot:
            await self.bot.stop()
        logger.info("Bot shut down")


def main() -> int:
    runner = BotRunner()
    try:
        asyncio.run(runner.run())
        logger.info("Bye")
        return 0
    except KeyboardInterrupt:
        try:
            asyncio.run(runner.shutdown())
        except Exception:
            logger.exception("Error during shutdown")
        return 130
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        RuntimeError,
        ImportError,
        ConfigurationError,
        APIConnectionError,
        AuthenticationError,
    ) as e:
        if isinstance(e, (ConfigurationError, AuthenticationError, APIConnectionError)):
            logger.error(f"Startup error: {e}")
        else:
            logger.exception("Unhandled exception during startup")
        try:
            asyncio.run(runner.shutdown())
        except Exception:
            logger.exception("Error during shutdown")
        if isinstance(e, ConfigurationError):
            return 2
        if isinstance(e, AuthenticationError):
            return 3
        if isinstance(e, APIConnectionError):
            return 4
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

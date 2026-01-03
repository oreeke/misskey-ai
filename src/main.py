import asyncio
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

from .bot import MisskeyBot
from .config import Config
from .constants import ConfigKeys
from .exceptions import APIConnectionError, AuthenticationError, ConfigurationError


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
        logger.add(log_path, level=config.get(ConfigKeys.LOG_LEVEL))
        logger.info("启动机器人...")
        try:
            self.bot = MisskeyBot(config)
            await self.bot.start()
            await self._setup_monitoring_and_signals()
            await self.shutdown_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    async def _setup_monitoring_and_signals(self) -> None:
        signals = (
            (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
            if sys.platform != "win32"
            else (signal.SIGINT, signal.SIGTERM)
        )

        def signal_handler(sig, _):
            logger.info(f"收到信号 {signal.Signals(sig).name}，准备关闭...")
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
                logger.warning(f"无法注册信号处理器: {sig}")

    async def shutdown(self) -> None:
        if self._shutdown_called:
            return
        self._shutdown_called = True
        logger.info("关闭机器人...")
        if self.bot:
            await self.bot.stop()
        logger.info("机器人已关闭")


def main() -> int:
    runner = BotRunner()
    try:
        asyncio.run(runner.run())
        logger.info("再见~")
        return 0
    except KeyboardInterrupt:
        try:
            asyncio.run(runner.shutdown())
        except Exception:
            logger.exception("关闭时出错")
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
            logger.error(f"启动时出错: {e}")
        else:
            logger.exception("启动时发生未处理异常")
        try:
            asyncio.run(runner.shutdown())
        except Exception:
            logger.exception("关闭时出错")
        if isinstance(e, ConfigurationError):
            return 2
        if isinstance(e, AuthenticationError):
            return 3
        if isinstance(e, APIConnectionError):
            return 4
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

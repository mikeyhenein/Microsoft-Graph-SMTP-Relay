"""Entry point: python main.py"""

import asyncio
import logging
import signal
import sys
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv

from config import ConfigError, Settings, load_settings
from smtp_server import Relay


def configure_logging(settings: Settings) -> None:
    level = getattr(logging, settings.log_level)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    file_error = None
    if settings.log_file:
        try:
            settings.log_file.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(RotatingFileHandler(settings.log_file, maxBytes=10 * 1024 * 1024, backupCount=5))
        except OSError as error:
            file_error = error
    for handler in handlers:
        handler.setLevel(level)  # stdout/file follow LOG_LEVEL ...
    logging.basicConfig(
        level=min(level, logging.INFO),  # ... while the dashboard still receives INFO events
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=handlers,
        force=True,
    )
    # aiosmtpd logs every client command at INFO; keep that for DEBUG only.
    logging.getLogger("mail.log").setLevel(logging.INFO if level == logging.DEBUG else logging.WARNING)
    for noisy in ("msal", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if file_error:
        logging.warning(f"Log file {settings.log_file} unavailable ({file_error}); logging to stdout only")


async def run(settings: Settings) -> None:
    relay = Relay(settings)
    await relay.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass  # Windows: Ctrl+C still ends asyncio.run()
    try:
        await stop.wait()
    finally:
        await relay.stop()


def main() -> int:
    load_dotenv()  # .env fills in anything not already set in the environment
    try:
        settings = load_settings()
    except ConfigError as error:
        print("Configuration problems:", file=sys.stderr)
        for problem in error.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 2
    configure_logging(settings)
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

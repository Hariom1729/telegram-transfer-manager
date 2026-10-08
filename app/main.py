"""Main application entrypoint."""

import asyncio
import logging
import signal
import sys
from telegram.ext import Application
from app.bot.commands import setup_bot_commands
from app.bot.handlers import register_handlers
from app.config import settings
from app.database import close_db, init_db
from app.logging_config import setup_logging
from app.telegram.user_client import user_client_manager
from app.transfer.manager import transfer_manager

logger = logging.getLogger("app.main")


async def main() -> None:
    """Initialize systems, launch background workers, and start bot polling."""
    # 1. Setup structured logging
    setup_logging()
    logger.info("Initializing Telegram Transfer Manager...")

    # 2. Validate configuration and log storage diagnostics
    try:
        settings.validate()
        settings.run_startup_diagnostics()
    except ValueError as e:
        logger.critical("Configuration validation failed: %s", e)
        print(f"\n[FATAL CONFIG ERROR] {e}\nPlease check your .env file.\n")
        sys.exit(1)

    # 3. Initialize database tables
    logger.info("Initializing database schema at %s", settings.DATABASE_URL)
    await init_db()
    logger.info("Database initialized successfully.")

    # 4. Start transfer engine and recovery
    logger.info("Starting Transfer Engine and recovering interrupted jobs...")
    await transfer_manager.start()

    # 5. Build python-telegram-bot application
    logger.info("Building Telegram Bot interface...")
    application = (
        Application.builder()
        .token(settings.BOT_TOKEN)
        .build()
    )

    # Register all handlers
    register_handlers(application)

    # Register global error handler
    async def global_error_handler(update: object, context) -> None:
        err = context.error
        if err and "not modified" in str(err).lower():
            return
        logger.error("Unhandled exception processing update %s: %s", update, err, exc_info=err)

    application.add_error_handler(global_error_handler)

    # 6. Lifecycle management
    stop_event = asyncio.Event()

    def signal_handler():
        logger.info("Shutdown signal received. Stopping services...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            # Signal handling on some platforms
            pass

    # Start bot
    await application.initialize()
    await setup_bot_commands(application)
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)
    logger.info("Telegram Transfer Manager is now LIVE and polling for updates.")

    # Wait for shutdown signal
    await stop_event.wait()

    # 7. Graceful teardown
    logger.info("Stopping bot polling...")
    await application.updater.stop()
    await application.stop()
    await application.shutdown()

    logger.info("Stopping transfer manager queue...")
    await transfer_manager.stop()

    logger.info("Disconnecting MTProto user clients...")
    await user_client_manager.close_all()

    logger.info("Closing database engine...")
    await close_db()
    logger.info("Telegram Transfer Manager shutdown complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass


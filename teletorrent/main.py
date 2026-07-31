import asyncio
import threading
from waitress import serve

from .config import CONFIG
from .db import init_db, event
from .bot import build_app
from .webapp import create_app
from .worker import run_worker
from .file_server import run_file_server


def web_thread():
    serve(create_app(), host=CONFIG.dashboard_host, port=CONFIG.dashboard_port, threads=4)


async def main():
    if not CONFIG.telegram_token or not CONFIG.allowed_ids:
        raise SystemExit("Telegram token and allowed user IDs are required.")
    init_db()
    event("info", "TeleTorrent starting")
    threading.Thread(target=web_thread, daemon=True).start()
    threading.Thread(target=run_file_server, daemon=True).start()
    app = build_app()
    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        try:
            await run_worker()
        finally:
            await app.updater.stop()
            await app.stop()


if __name__ == "__main__":
    asyncio.run(main())

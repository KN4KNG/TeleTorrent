import asyncio
import html
import os
import shutil
import time
from pathlib import Path

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

from .formatting import human

from .config import CONFIG
from . import qbit
from .db import event, rows, update_job, upsert_job, now
from .scanner import scan, reject

COMPLETE = {"uploading", "stalledUP", "pausedUP", "queuedUP", "forcedUP", "checkingUP"}
MOVE_TIMEOUT = int(os.getenv("APPROVED_MOVE_TIMEOUT_SECONDS", "180"))
LIVE_REFRESH_SECONDS = int(os.getenv("TELEGRAM_LIVE_REFRESH_SECONDS", "10"))
LIVE_MESSAGE_IDS: dict[int, int] = {}
ACTIVE_STATES = {"downloading", "forcedDL", "metaDL", "stalledDL", "queuedDL", "checkingDL", "allocating", "moving"}


def _active(torrent) -> bool:
    return (
        str(getattr(torrent, "state", "")) in ACTIVE_STATES
        or int(getattr(torrent, "dlspeed", 0) or 0) > 0
    )


def _eta(seconds) -> str:
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "unknown"
    if seconds <= 0 or seconds >= 8_640_000:
        return "unknown"
    hours, rem = divmod(seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _live_text(torrents, transfer, final=False) -> str:
    active = [t for t in torrents if _active(t)]
    heading = "✅ <b>Torrent activity finished</b>" if final else "🛡️ <b>TeleTorrent — Live</b>"
    lines = [
        heading, "",
        f"⬇️ <b>Download:</b> {human(transfer.dl_info_speed)}/s",
        f"⬆️ <b>Upload:</b> {human(transfer.up_info_speed)}/s",
        f"📦 <b>Active:</b> {len(active)}",
    ]
    for index, torrent in enumerate((active or torrents)[:8], 1):
        progress = float(getattr(torrent, "progress", 0) or 0)
        filled = round(progress * 14)
        bar = "█" * filled + "░" * (14 - filled)
        lines.extend([
            "",
            f"<b>{index}. {html.escape(str(torrent.name)[:55])}</b>",
            f"<code>{bar}</code> {progress * 100:.1f}%",
            f"↓ {human(getattr(torrent, 'dlspeed', 0))}/s · ↑ {human(getattr(torrent, 'upspeed', 0))}/s",
            f"ETA {_eta(getattr(torrent, 'eta', 0))} · Seeds {getattr(torrent, 'num_seeds', 0)} · Peers {getattr(torrent, 'num_leechs', 0)}",
            f"<i>{html.escape(str(getattr(torrent, 'state', 'unknown')))}</i>",
        ])
    lines.extend(["", f"🕒 Updated {time.strftime('%I:%M:%S %p')}"])
    return "\n".join(lines)


async def update_live(bot: Bot, torrents, transfer) -> None:
    active = [torrent for torrent in torrents if _active(torrent)]
    if active:
        text = _live_text(torrents, transfer)
        for uid in CONFIG.allowed_ids:
            message_id = LIVE_MESSAGE_IDS.get(uid)
            try:
                if message_id is None:
                    message = await bot.send_message(uid, text, parse_mode="HTML")
                    LIVE_MESSAGE_IDS[uid] = message.message_id
                else:
                    await bot.edit_message_text(text, chat_id=uid, message_id=message_id, parse_mode="HTML")
            except BadRequest as exc:
                if "message is not modified" not in str(exc).lower():
                    LIVE_MESSAGE_IDS.pop(uid, None)
            except Exception as exc:
                event("error", f"Live Telegram status failed: {exc}")
    elif LIVE_MESSAGE_IDS:
        text = _live_text(torrents, transfer, final=True)
        for uid, message_id in list(LIVE_MESSAGE_IDS.items()):
            try:
                await bot.edit_message_text(text, chat_id=uid, message_id=message_id, parse_mode="HTML")
            except Exception:
                pass
            LIVE_MESSAGE_IDS.pop(uid, None)


def actual_path(t):
    content = Path(str(t.content_path))
    if content.exists():
        return content
    candidate = CONFIG.quarantine / t.name
    return candidate if candidate.exists() else content


def inside(path: Path, root: Path) -> bool:
    path = path.resolve(strict=False)
    root = root.resolve(strict=False)
    return path == root or root in path.parents


async def notify(text, markup=None):
    bot = Bot(CONFIG.telegram_token)
    for uid in CONFIG.allowed_ids:
        try:
            await bot.send_message(uid, text, parse_mode="HTML", reply_markup=markup)
        except Exception as exc:
            event("error", f"Telegram notification failed: {exc}")


async def wait_for_approved_location(torrent_hash: str, name: str) -> Path:
    deadline = asyncio.get_running_loop().time() + MOVE_TIMEOUT
    expected = CONFIG.approved / name
    while asyncio.get_running_loop().time() < deadline:
        if expected.exists():
            return expected.resolve()
        try:
            items = await asyncio.to_thread(
                qbit.client().torrents_info,
                torrent_hashes=torrent_hash,
            )
            if items:
                content = Path(str(items[0].content_path))
                if content.exists() and inside(content, CONFIG.approved):
                    return content.resolve()
        except Exception:
            pass
        await asyncio.sleep(2)
    raise TimeoutError(f"qBittorrent did not move {name} into approved storage within {MOVE_TIMEOUT}s")


async def approve_and_verify(torrent_hash: str, name: str) -> Path:
    CONFIG.approved.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(qbit.set_location, torrent_hash, CONFIG.approved)
    destination = await wait_for_approved_location(torrent_hash, name)
    update_job(
        torrent_hash,
        status="approved",
        approved_path=str(destination),
        completed_at=now(),
    )
    return destination


async def repair_approved_jobs() -> None:
    """Repair old rows marked approved before qBittorrent finished moving them."""
    for job in rows("SELECT hash,name,approved_path,save_path FROM jobs WHERE status='approved'"):
        candidates = []
        if job.get("approved_path"):
            candidates.append(Path(job["approved_path"]))
        candidates.append(CONFIG.approved / job["name"])
        existing = next((p for p in candidates if p.exists() and inside(p, CONFIG.approved)), None)
        if existing:
            if str(existing) != job.get("approved_path"):
                update_job(job["hash"], approved_path=str(existing.resolve()))
            continue
        try:
            await asyncio.to_thread(qbit.set_location, job["hash"], CONFIG.approved)
            moved = await wait_for_approved_location(job["hash"], job["name"])
            update_job(job["hash"], approved_path=str(moved), completed_at=now())
            event("info", f"Repaired approved file location: {job['name']}", job["hash"])
        except Exception as exc:
            event("error", f"Approved-location repair failed for {job['name']}: {exc}", job["hash"])


async def process(t):
    existing = rows("SELECT status FROM jobs WHERE hash=?", (t.hash,))
    if existing and existing[0]["status"] in {
        "scanning", "approved", "rejected", "manual_review", "scan_error"
    }:
        return

    await asyncio.to_thread(qbit.pause, t.hash)
    path = actual_path(t)
    upsert_job(t.hash, t.name, "scanning", "qBittorrent", str(path))
    event("info", f"Scanning {t.name}", t.hash)
    await notify(f"🔎 <b>Scanning completed download</b>\n{html.escape(t.name)}")

    try:
        result = await asyncio.to_thread(scan, path)
    except Exception as exc:
        update_job(t.hash, status="scan_error", scan_output=str(exc))
        event("error", f"Scan error: {exc}", t.hash)
        await notify(f"❌ Scan error for {html.escape(t.name)}: {html.escape(str(exc))}")
        return

    output = result["output"][-8000:]
    if not result["clean"]:
        destination = await asyncio.to_thread(reject, path, t.name)
        update_job(
            t.hash,
            status="rejected",
            malware="ClamAV detection/error",
            scan_output=output,
            scanned_at=now(),
            approved_path=str(destination or "deleted"),
        )
        event("critical", f"Rejected {t.name}: ClamAV code {result['clam_code']}", t.hash)
        await notify(
            f"🚨 <b>Rejected</b>\n{html.escape(t.name)}\n"
            "ClamAV reported a detection or scan failure."
        )
        return

    if result["suspicious"]:
        update_job(t.hash, status="manual_review", scan_output=output, scanned_at=now())
        files = "\n".join(html.escape(item) for item in result["suspicious"][:15])
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Approve", callback_data=f"approve:{t.hash}"),
            InlineKeyboardButton("Reject + delete", callback_data=f"reject:{t.hash}"),
        ]])
        await notify(
            f"⚠️ <b>Manual approval required</b>\n{html.escape(t.name)}\n"
            f"Executable/script-like files found:\n<code>{files}</code>",
            keyboard,
        )
        return

    if CONFIG.auto_approve:
        try:
            destination = await approve_and_verify(t.hash, t.name)
        except Exception as exc:
            update_job(
                t.hash,
                status="move_error",
                scan_output=output + "\nMOVE ERROR: " + str(exc),
                scanned_at=now(),
            )
            event("error", f"Approval move failed for {t.name}: {exc}", t.hash)
            await notify(
                f"⚠️ <b>Scan passed but file move failed</b>\n"
                f"{html.escape(t.name)}\n{html.escape(str(exc))}"
            )
            return
        update_job(t.hash, scan_output=output, scanned_at=now())
        event("info", f"Approved {t.name}", t.hash)
        await notify(
            f"✅ <b>Scanned and approved</b>\n{html.escape(t.name)}\n"
            f"Available in <code>{html.escape(str(destination))}</code>."
        )
    else:
        update_job(t.hash, status="manual_review", scan_output=output, scanned_at=now())


async def run_worker():
    repaired = False
    bot = Bot(CONFIG.telegram_token)
    last_live = 0.0
    while True:
        try:
            if not repaired:
                await repair_approved_jobs()
                repaired = True
            torrents = await asyncio.to_thread(qbit.torrents)
            if time.monotonic() - last_live >= LIVE_REFRESH_SECONDS:
                transfer = await asyncio.to_thread(qbit.transfer)
                await update_live(bot, torrents, transfer)
                last_live = time.monotonic()
            for torrent in torrents:
                prior = rows("SELECT status FROM jobs WHERE hash=?", (torrent.hash,))
                if not prior or prior[0]["status"] not in {
                    "scanning", "approved", "rejected", "manual_review", "scan_error", "move_error"
                }:
                    upsert_job(
                        torrent.hash, torrent.name,
                        "downloading" if torrent.progress < 1 else "completed",
                        "qBittorrent", str(actual_path(torrent)),
                    )
                if torrent.progress >= 1 and str(torrent.state) in COMPLETE:
                    await process(torrent)
        except Exception as exc:
            event("error", f"Worker loop: {exc}")
        await asyncio.sleep(min(CONFIG.scan_interval, LIVE_REFRESH_SECONDS))

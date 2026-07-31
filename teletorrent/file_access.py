from __future__ import annotations

import asyncio
import hashlib
import html
import os
import secrets
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from flask import Flask, abort, send_file
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from .config import CONFIG
from .db import event, rows
from .formatting import human

APPROVED = CONFIG.approved.resolve()
DIRECT_LIMIT = int(os.getenv("TELEGRAM_DIRECT_LIMIT_BYTES", "1990000000"))
UPLOAD_TIMEOUT = int(os.getenv("TELEGRAM_UPLOAD_TIMEOUT_SECONDS", "1800"))
LINK_MINUTES = int(os.getenv("TEMP_LINK_DURATION_MINUTES", "15"))
DOWNLOAD_PORT = int(os.getenv("TEMP_DOWNLOAD_PORT", "8790"))
CLOUDFLARED = os.getenv("CLOUDFLARED_BIN", "/usr/local/bin/cloudflared")
PAGE_SIZE = max(5, min(40, int(os.getenv("FILE_BROWSER_PAGE_SIZE", "20"))))
MAX_RESULTS = max(20, min(500, int(os.getenv("FILE_BROWSER_MAX_RESULTS", "100"))))


@dataclass
class Link:
    path: Path
    expires: float


@dataclass
class BrowserRef:
    path: Path
    created: float


_links: dict[str, Link] = {}
_links_lock = threading.RLock()
_tunnel_lock = threading.RLock()
_tunnel_process: Optional[subprocess.Popen] = None
_tunnel_url: Optional[str] = None
_browser: dict[str, BrowserRef] = {}
_browser_lock = threading.RLock()


def _inside(path: Path, root: Path = APPROVED) -> bool:
    try:
        resolved = path.resolve(strict=False)
        return resolved == root or root in resolved.parents
    except OSError:
        return False


def _safe(path: Path, *, must_file: bool | None = None) -> Path:
    resolved = path.resolve(strict=True)
    if not _inside(resolved):
        raise ValueError("Path is outside approved storage")
    # A symlink anywhere in the requested relative path is rejected.
    current = APPROVED
    for part in resolved.relative_to(APPROVED).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Symbolic links are not allowed")
    if must_file is True and not resolved.is_file():
        raise ValueError("Not a file")
    if must_file is False and not resolved.is_dir():
        raise ValueError("Not a directory")
    return resolved


def _authorized(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id in CONFIG.allowed_ids)


def _prune_refs() -> None:
    cutoff = time.time() - 3600
    with _browser_lock:
        stale = [key for key, value in _browser.items() if value.created < cutoff]
        for key in stale:
            _browser.pop(key, None)
        while len(_browser) > 4000:
            _browser.pop(next(iter(_browser)), None)


def _ref(path: Path) -> str:
    safe = _safe(path)
    token = secrets.token_urlsafe(9)
    with _browser_lock:
        _browser[token] = BrowserRef(path=safe, created=time.time())
    _prune_refs()
    return token


def _get_ref(token: str) -> Path:
    with _browser_lock:
        ref = _browser.get(token)
    if ref is None:
        raise ValueError("This browser button expired; run /files again")
    return _safe(ref.path)


def _approved_files() -> list[Path]:
    APPROVED.mkdir(parents=True, exist_ok=True)
    output: list[Path] = []
    for path in APPROVED.rglob("*"):
        try:
            safe = _safe(path)
            if safe.is_file():
                output.append(safe)
        except (OSError, ValueError):
            continue
    return output


def _directory_items(directory: Path) -> list[Path]:
    directory = _safe(directory, must_file=False)
    items: list[Path] = []
    for item in directory.iterdir():
        try:
            items.append(_safe(item))
        except (OSError, ValueError):
            continue
    return sorted(items, key=lambda p: (not p.is_dir(), p.name.casefold()))


def _page_keyboard(directory: Path, page: int = 0) -> InlineKeyboardMarkup:
    directory = _safe(directory, must_file=False)
    items = _directory_items(directory)
    page_count = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, page_count - 1))
    start = page * PAGE_SIZE
    shown = items[start:start + PAGE_SIZE]
    rows_out: list[list[InlineKeyboardButton]] = []

    if directory != APPROVED:
        rows_out.append([
            InlineKeyboardButton("⬅️ Up", callback_data=f"fdir:{_ref(directory.parent)}:0")
        ])

    for item in shown:
        icon = "📁" if item.is_dir() else "📄"
        action = "fdir" if item.is_dir() else "finfo"
        label = item.name if len(item.name) <= 48 else item.name[:45] + "…"
        rows_out.append([
            InlineKeyboardButton(f"{icon} {label}", callback_data=f"{action}:{_ref(item)}:0")
        ])

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"fdir:{_ref(directory)}:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{page_count}", callback_data="fnoop:x:0"))
    if page + 1 < page_count:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"fdir:{_ref(directory)}:{page+1}"))
    rows_out.append(nav)
    rows_out.append([
        InlineKeyboardButton("🕒 Recent", callback_data="frecent:x:0"),
        InlineKeyboardButton("🔄 Refresh", callback_data=f"fdir:{_ref(directory)}:{page}"),
    ])
    return InlineKeyboardMarkup(rows_out)


def _directory_title(path: Path) -> str:
    if path == APPROVED:
        return "📁 <b>Approved files</b>"
    rel = path.relative_to(APPROVED)
    return f"📁 <b>{html.escape(str(rel))}</b>"


def _file_info(path: Path) -> str:
    path = _safe(path, must_file=True)
    stat = path.stat()
    rel = path.relative_to(APPROVED)
    digest = hashlib.sha256()
    # Hash only the first 16 MiB here to keep the info screen responsive.
    with path.open("rb") as handle:
        remaining = 16 * 1024 * 1024
        while remaining > 0:
            chunk = handle.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    return (
        f"📄 <b>{html.escape(path.name)}</b>\n"
        f"Path: <code>{html.escape(str(rel))}</code>\n"
        f"Size: {human(stat.st_size)}\n"
        f"Modified: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stat.st_mtime))}\n"
        f"SHA-256 prefix: <code>{digest.hexdigest()[:20]}</code>\n"
        "Status: ✅ Approved"
    )


def _prune_links() -> None:
    global _tunnel_process, _tunnel_url
    now = time.time()
    with _links_lock:
        for token in [token for token, link in _links.items() if link.expires <= now]:
            _links.pop(token, None)
        empty = not _links
    if empty:
        with _tunnel_lock:
            if _tunnel_process and _tunnel_process.poll() is None:
                _tunnel_process.terminate()
                try:
                    _tunnel_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _tunnel_process.kill()
            _tunnel_process = None
            _tunnel_url = None


def _ensure_tunnel() -> str:
    global _tunnel_process, _tunnel_url
    with _tunnel_lock:
        if _tunnel_process and _tunnel_process.poll() is None and _tunnel_url:
            return _tunnel_url

        home = Path("/tmp/teletorrent-cloudflared")
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        env = os.environ.copy()
        env["HOME"] = str(home)
        proc = subprocess.Popen(
            [
                CLOUDFLARED,
                "tunnel",
                "--no-autoupdate",
                "--url",
                f"http://127.0.0.1:{DOWNLOAD_PORT}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            bufsize=1,
        )
        deadline = time.time() + 45
        url: Optional[str] = None
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            line = proc.stdout.readline() if proc.stdout else ""
            if not line:
                time.sleep(0.1)
                continue
            for word in line.split():
                clean = word.strip("|,[]()\"'")
                if clean.startswith("https://") and ".trycloudflare.com" in clean:
                    url = clean.rstrip("/")
                    break
            if url:
                break
        if not url:
            proc.terminate()
            raise RuntimeError("Cloudflare Quick Tunnel did not return a URL")
        _tunnel_process = proc
        _tunnel_url = url
        return url


def create_link(path: Path) -> tuple[str, float]:
    path = _safe(path, must_file=True)
    _prune_links()
    base = _ensure_tunnel()
    token = secrets.token_urlsafe(32)
    expires = time.time() + LINK_MINUTES * 60
    with _links_lock:
        _links[token] = Link(path=path, expires=expires)
    event("info", f"Temporary link created for {path.name}")
    return f"{base}/dl/{token}", expires


async def _send_or_link(query, path: Path) -> None:
    path = _safe(path, must_file=True)
    size = path.stat().st_size
    status = await query.message.reply_text(f"Preparing {path.name}…")

    if size < DIRECT_LIMIT:
        try:
            await query.message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
            with path.open("rb") as handle:
                await query.message.reply_document(
                    document=handle,
                    filename=path.name,
                    caption=f"✅ Approved file · {human(size)}",
                    read_timeout=UPLOAD_TIMEOUT,
                    write_timeout=UPLOAD_TIMEOUT,
                    connect_timeout=60,
                    pool_timeout=60,
                )
            await status.edit_text("✅ Sent in Telegram.")
            event("info", f"Telegram file sent: {path.name}")
            return
        except Exception as exc:
            event("error", f"Telegram upload failed for {path.name}: {exc}; using temporary link")
            await status.edit_text("Direct upload failed; creating a 15-minute link…")

    try:
        url, expires = await asyncio.to_thread(create_link, path)
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬇️ Download file", url=url)],
            [InlineKeyboardButton("❌ Revoke", callback_data=f"frevoke:{_ref(path)}:0")],
        ])
        await status.edit_text(
            "🔗 <b>Secure temporary link</b>\n"
            f"File: {html.escape(path.name)}\n"
            f"Size: {human(size)}\n"
            f"Expires: {time.strftime('%I:%M:%S %p', time.localtime(expires))}",
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )
    except Exception as exc:
        await status.edit_text(f"❌ Could not create temporary link: {exc}")


async def files_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await update.effective_message.reply_text("Unauthorized.")
        return
    APPROVED.mkdir(parents=True, exist_ok=True)
    await update.effective_message.reply_html(
        _directory_title(APPROVED),
        reply_markup=_page_keyboard(APPROVED),
    )


async def recent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    files = _approved_files()
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    buttons = [
        [InlineKeyboardButton(f"📄 {path.name[:47]}", callback_data=f"finfo:{_ref(path)}:0")]
        for path in files[:PAGE_SIZE]
    ]
    if not buttons:
        await update.effective_message.reply_text("No approved files are physically present yet.")
        return
    buttons.append([InlineKeyboardButton("📁 Browse all", callback_data=f"fdir:{_ref(APPROVED)}:0")])
    await update.effective_message.reply_text(
        "🕒 Recent approved files",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    term = " ".join(context.args).strip().casefold()
    if not term:
        await update.effective_message.reply_text("Usage: /search filename")
        return
    matches: list[Path] = []
    for path in APPROVED.rglob("*"):
        if term not in path.name.casefold():
            continue
        try:
            matches.append(_safe(path))
        except (OSError, ValueError):
            continue
        if len(matches) >= MAX_RESULTS:
            break
    buttons: list[list[InlineKeyboardButton]] = []
    for path in matches[:PAGE_SIZE]:
        action = "fdir" if path.is_dir() else "finfo"
        icon = "📁" if path.is_dir() else "📄"
        buttons.append([
            InlineKeyboardButton(f"{icon} {path.name[:47]}", callback_data=f"{action}:{_ref(path)}:0")
        ])
    await update.effective_message.reply_text(
        f"Search results for: {term}" if buttons else "No matches in approved storage.",
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
    )


async def file_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _authorized(update):
        await query.answer("Unauthorized", show_alert=True)
        return
    await query.answer()
    parts = query.data.split(":", 2)
    action = parts[0]
    token = parts[1] if len(parts) > 1 else ""
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0

    try:
        if action == "fnoop":
            return
        if action == "frecent":
            files = _approved_files()
            files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            buttons = [
                [InlineKeyboardButton(f"📄 {p.name[:47]}", callback_data=f"finfo:{_ref(p)}:0")]
                for p in files[:PAGE_SIZE]
            ]
            buttons.append([InlineKeyboardButton("📁 Browse all", callback_data=f"fdir:{_ref(APPROVED)}:0")])
            await query.edit_message_text(
                "🕒 Recent approved files" if files else "No approved files are physically present yet.",
                reply_markup=InlineKeyboardMarkup(buttons) if files else None,
            )
            return

        path = _get_ref(token)
        if action == "fdir":
            await query.edit_message_text(
                _directory_title(path),
                parse_mode=ParseMode.HTML,
                reply_markup=_page_keyboard(path, page),
            )
        elif action == "finfo":
            buttons = [
                [InlineKeyboardButton("⬇️ Download", callback_data=f"fsend:{_ref(path)}:0")],
                [InlineKeyboardButton("🔗 Force 15-min link", callback_data=f"flink:{_ref(path)}:0")],
                [InlineKeyboardButton("⬅️ Back", callback_data=f"fdir:{_ref(path.parent)}:0")],
            ]
            await query.edit_message_text(
                _file_info(path),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        elif action == "fsend":
            await _send_or_link(query, path)
        elif action == "flink":
            size = path.stat().st_size
            status = await query.message.reply_text("Creating a 15-minute secure link…")
            url, expires = await asyncio.to_thread(create_link, path)
            await status.edit_text(
                f"🔗 {path.name}\nSize: {human(size)}\nExpires: "
                f"{time.strftime('%I:%M:%S %p', time.localtime(expires))}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬇️ Download file", url=url)]]),
            )
        elif action == "frevoke":
            revoked = 0
            with _links_lock:
                for key, link in list(_links.items()):
                    if link.path == path:
                        _links.pop(key, None)
                        revoked += 1
            _prune_links()
            await query.message.reply_text(f"Revoked {revoked} active link(s).")
    except Exception as exc:
        await query.answer(str(exc), show_alert=True)


def register_handlers(app) -> None:
    app.add_handler(CommandHandler("files", files_cmd), group=-10)
    app.add_handler(CommandHandler("recent", recent_cmd), group=-10)
    app.add_handler(CommandHandler("search", search_cmd), group=-10)
    app.add_handler(
        CallbackQueryHandler(
            file_callback,
            pattern=r"^(fdir|finfo|fsend|flink|frevoke|frecent|fnoop):",
        ),
        group=-10,
    )


def create_download_app() -> Flask:
    app = Flask("teletorrent_downloads")

    @app.get("/health")
    def health():
        return {"ok": True, "links": len(_links)}

    @app.get("/dl/<token>")
    def download(token: str):
        _prune_links()
        with _links_lock:
            link = _links.get(token)
        if not link or link.expires <= time.time():
            abort(404)
        try:
            path = _safe(link.path, must_file=True)
        except Exception:
            abort(404)
        event("info", f"Temporary link accessed: {path.name}")
        response = send_file(
            path,
            as_attachment=True,
            download_name=path.name,
            conditional=True,
        )
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    return app


def cleanup_loop() -> None:
    while True:
        time.sleep(15)
        _prune_links()
        _prune_refs()

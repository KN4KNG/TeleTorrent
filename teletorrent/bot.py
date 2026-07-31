import os
import asyncio, html, os, tempfile
from pathlib import Path
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, ContextTypes, filters
from .config import CONFIG
from . import qbit
from .db import event, rows, update_job, upsert_job
from .file_access import register_handlers
from .worker import approve_and_verify
from .formatting import bar, human, state_icon


async def authorized(update: Update):
    uid=update.effective_user.id if update.effective_user else None
    if uid not in CONFIG.allowed_ids:
        if update.effective_message: await update.effective_message.reply_text('Unauthorized.')
        return False
    return True

def controls(h):
    return InlineKeyboardMarkup([[InlineKeyboardButton('Pause',callback_data=f'pause:{h}'),InlineKeyboardButton('Resume',callback_data=f'resume:{h}')],[InlineKeyboardButton('Refresh',callback_data=f'info:{h}'),InlineKeyboardButton('Delete',callback_data=f'delask:{h}')]])

def torrent_text(t):
    return (f"{state_icon(t.state)} <b>{html.escape(t.name)}</b>\n"
            f"{bar(t.progress)} {t.progress*100:.1f}%\n"
            f"↓ {human(t.dlspeed)}/s  ↑ {human(t.upspeed)}/s\n"
            f"Size: {human(t.size)} · Ratio: {t.ratio:.2f}\n"
            f"State: <code>{html.escape(str(t.state))}</code>\n"
            f"Hash: <code>{t.hash[:12]}</code>")

async def start(update, context):
    if not await authorized(update): return
    await update.message.reply_text('🛡️ TeleTorrent is online. Send a legal .torrent file or magnet link.\n\n/files /recent /search /list /status /history /storage /help')

async def help_cmd(update, context):
    if not await authorized(update): return
    await update.message.reply_text('/files browse approved files\n/recent newest approved files\n/search TERM search approved files\n/list active torrents\n/status server status\n/history recent scan history\n/storage disk usage\n/pause HASH\n/resume HASH\n/delete HASH\n/deletefiles HASH\nSend magnet links or .torrent files directly.')

async def add_text(update, context):
    if not await authorized(update): return
    text=(update.message.text or '').strip()
    if not text.startswith('magnet:?'): return
    try:
        ok=await asyncio.to_thread(qbit.add_magnet,text)
        event('info','Magnet submitted')
        await update.message.reply_text('✅ Magnet submitted to quarantine.' if ok!='Fails.' else '❌ qBittorrent rejected the magnet.')
    except Exception as e: await update.message.reply_text(f'❌ Add failed: {e}')

async def add_document(update, context):
    if not await authorized(update): return
    doc=update.message.document
    if not doc.file_name.lower().endswith('.torrent'):
        await update.message.reply_text('Only .torrent files are accepted.'); return
    if doc.file_size and doc.file_size>CONFIG.max_torrent_mb*1024*1024:
        await update.message.reply_text('Torrent metadata file is too large.'); return
    tmp=Path(tempfile.gettempdir())/f'tg-{doc.file_unique_id}.torrent'
    try:
        f=await doc.get_file(); await f.download_to_drive(tmp)
        ok=await asyncio.to_thread(qbit.add_torrent,str(tmp))
        event('info',f'Torrent file submitted: {doc.file_name}')
        await update.message.reply_text('✅ Torrent submitted to quarantine.' if ok!='Fails.' else '❌ qBittorrent rejected the file.')
    except Exception as e: await update.message.reply_text(f'❌ Add failed: {e}')
    finally: tmp.unlink(missing_ok=True)

async def list_cmd(update, context):
    if not await authorized(update): return
    try: ts=await asyncio.to_thread(qbit.torrents)
    except Exception as e: await update.message.reply_text(f'❌ qBittorrent unavailable: {e}'); return
    if not ts: await update.message.reply_text('No torrents.'); return
    for t in ts[:15]: await update.message.reply_html(torrent_text(t),reply_markup=controls(t.hash))

async def status(update, context):
    if not await authorized(update): return
    try:
        tr=await asyncio.to_thread(qbit.transfer); ts=await asyncio.to_thread(qbit.torrents)
        scanning=len(rows("SELECT hash FROM jobs WHERE status='scanning'")); pending=len(rows("SELECT hash FROM jobs WHERE status='manual_review'"))
        await update.message.reply_text(f'🟢 TeleTorrent online\nTorrents: {len(ts)}\n↓ {human(tr.dl_info_speed)}/s\n↑ {human(tr.up_info_speed)}/s\nScanning: {scanning}\nManual review: {pending}')
    except Exception as e: await update.message.reply_text(f'🔴 qBittorrent unavailable: {e}')

async def history(update, context):
    if not await authorized(update): return
    data=rows('SELECT name,status,malware,updated_at FROM jobs ORDER BY updated_at DESC LIMIT 12')
    if not data: await update.message.reply_text('No scan history yet.'); return
    await update.message.reply_text('\n'.join(f"{'✅' if r['status']=='approved' else '⚠️' if r['status'] in ('rejected','manual_review') else '•'} {r['name']} — {r['status']}" for r in data))

async def storage(update, context):
    if not await authorized(update): return
    import shutil
    u=shutil.disk_usage(CONFIG.quarantine)
    await update.message.reply_text(f'Disk total: {human(u.total)}\nUsed: {human(u.used)} ({u.used/u.total*100:.1f}%)\nFree: {human(u.free)}')

async def hash_command(update, context, fn, label):
    if not await authorized(update): return
    if not context.args: await update.message.reply_text(f'Usage: /{label.lower()} HASH'); return
    try: await asyncio.to_thread(fn,context.args[0]); await update.message.reply_text(f'✅ {label} requested.')
    except Exception as e: await update.message.reply_text(f'❌ {e}')

async def callback(update, context):
    q=update.callback_query
    if q.from_user.id not in CONFIG.allowed_ids: await q.answer('Unauthorized',show_alert=True); return
    await q.answer(); action,h=q.data.split(':',1)
    try:
        if action=='pause': await asyncio.to_thread(qbit.pause,h); await q.edit_message_reply_markup(reply_markup=controls(h))
        elif action=='resume': await asyncio.to_thread(qbit.resume,h); await q.edit_message_reply_markup(reply_markup=controls(h))
        elif action=='info':
            t=(await asyncio.to_thread(qbit.client().torrents_info,torrent_hashes=h))[0]
            await q.edit_message_text(torrent_text(t),parse_mode='HTML',reply_markup=controls(h))
        elif action=='delask':
            await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Torrent only',callback_data=f'del:{h}'),InlineKeyboardButton('Torrent + files',callback_data=f'delfiles:{h}')],[InlineKeyboardButton('Cancel',callback_data=f'info:{h}')]]))
        elif action in ('del','delfiles'): await asyncio.to_thread(qbit.delete,h,action=='delfiles'); await q.edit_message_text('Deleted.')
        elif action=='approve':
            job=rows('SELECT * FROM jobs WHERE hash=?',(h,))[0]
            dst=await approve_and_verify(h,job['name'])
            await q.edit_message_text(f'✅ Approved and moved to {dst}.')
        elif action=='reject':
            await asyncio.to_thread(qbit.delete,h,True); update_job(h,status='rejected'); await q.edit_message_text('🗑️ Rejected and torrent data deleted.')
    except Exception as e: await q.answer(str(e),show_alert=True)

def build_app():
    builder = Application.builder().token(CONFIG.telegram_token)
    local_url = CONFIG.telegram_local_api_url
    if local_url:
        builder = (
            builder
            .base_url(local_url + "/bot")
            .base_file_url(local_url + "/file/bot")
            .local_mode(True)
            .read_timeout(1800)
            .write_timeout(1800)
            .connect_timeout(60)
            .pool_timeout(60)
        )
    app = builder.build()
    register_handlers(app)
    app.add_handler(CommandHandler('start',start)); app.add_handler(CommandHandler('help',help_cmd)); app.add_handler(CommandHandler('list',list_cmd)); app.add_handler(CommandHandler('status',status)); app.add_handler(CommandHandler('history',history)); app.add_handler(CommandHandler('storage',storage))
    app.add_handler(CommandHandler('pause',lambda u,c: hash_command(u,c,qbit.pause,'Pause'))); app.add_handler(CommandHandler('resume',lambda u,c: hash_command(u,c,qbit.resume,'Resume')))
    app.add_handler(CommandHandler('delete',lambda u,c: hash_command(u,c,lambda h:qbit.delete(h,False),'Delete'))); app.add_handler(CommandHandler('deletefiles',lambda u,c: hash_command(u,c,lambda h:qbit.delete(h,True),'Deletefiles')))
    app.add_handler(CallbackQueryHandler(callback)); app.add_handler(MessageHandler(filters.Document.ALL,add_document)); app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,add_text))
    return app

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from .config import CONFIG

SCHEMA = '''
CREATE TABLE IF NOT EXISTS jobs (
 hash TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL,
 source TEXT, save_path TEXT, approved_path TEXT, malware TEXT,
 scan_output TEXT, created_at TEXT NOT NULL, completed_at TEXT,
 scanned_at TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
 level TEXT NOT NULL, message TEXT NOT NULL, torrent_hash TEXT
);
'''

def now(): return datetime.now(timezone.utc).isoformat(timespec='seconds')

@contextmanager
def connect():
    CONFIG.db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(CONFIG.db_path)
    con.row_factory = sqlite3.Row
    try: yield con; con.commit()
    finally: con.close()

def init_db():
    with connect() as c: c.executescript(SCHEMA)

def upsert_job(hash_, name, status, source='', save_path=''):
    t=now()
    with connect() as c:
        c.execute('''INSERT INTO jobs(hash,name,status,source,save_path,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?) ON CONFLICT(hash) DO UPDATE SET name=excluded.name,status=excluded.status,
        source=COALESCE(NULLIF(excluded.source,''),jobs.source),save_path=COALESCE(NULLIF(excluded.save_path,''),jobs.save_path),updated_at=excluded.updated_at''',
        (hash_,name,status,source,save_path,t,t))

def update_job(hash_, **fields):
    if not fields: return
    fields['updated_at']=now()
    keys=list(fields)
    with connect() as c: c.execute(f"UPDATE jobs SET {','.join(k+'=?' for k in keys)} WHERE hash=?", [fields[k] for k in keys]+[hash_])

def event(level, message, hash_=None):
    with connect() as c: c.execute('INSERT INTO events(ts,level,message,torrent_hash) VALUES(?,?,?,?)',(now(),level,message,hash_))

def rows(sql, args=()):
    with connect() as c: return [dict(r) for r in c.execute(sql,args).fetchall()]

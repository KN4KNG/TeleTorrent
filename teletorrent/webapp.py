from functools import wraps
from flask import Flask, Response, jsonify, render_template, request
import psutil
import shutil

from .config import CONFIG
from .db import rows
from .formatting import human
from . import qbit


def auth_required(fn):
    @wraps(fn)
    def wrap(*args, **kwargs):
        auth = request.authorization
        valid = (
            auth is not None
            and auth.username == CONFIG.dashboard_user
            and auth.password == CONFIG.dashboard_password
        )
        if not valid:
            response = Response("Authentication required", status=401, content_type="text/plain")
            response.headers["WWW-Authenticate"] = 'Basic realm="TeleTorrent", charset="UTF-8"'
            response.headers["Cache-Control"] = "no-store"
            return response
        return fn(*args, **kwargs)

    return wrap


def create_app():
    app = Flask(__name__)

    @app.get("/")
    @auth_required
    def index():
        return render_template("dashboard.html")

    @app.get("/api/status")
    @auth_required
    def status():
        try:
            torrents = qbit.torrents()
            transfer = qbit.transfer()
            qbit_ok = True
            error = ""
        except Exception as exc:
            torrents = []
            transfer = None
            qbit_ok = False
            error = str(exc)

        disk = shutil.disk_usage(CONFIG.quarantine)
        jobs = rows(
            "SELECT name,status,malware,approved_path,updated_at "
            "FROM jobs ORDER BY updated_at DESC LIMIT 20"
        )
        events = rows("SELECT ts,level,message FROM events ORDER BY id DESC LIMIT 20")
        torrent_data = [
            {
                "hash": item.hash,
                "name": item.name,
                "progress": round(item.progress * 100, 1),
                "state": str(item.state),
                "dlspeed": human(item.dlspeed) + "/s",
                "upspeed": human(item.upspeed) + "/s",
                "size": human(item.size),
            }
            for item in torrents
        ]
        return jsonify(
            qbit_ok=qbit_ok,
            error=error,
            cpu=psutil.cpu_percent(),
            memory=psutil.virtual_memory().percent,
            temp=get_temp(),
            disk={
                "used": human(disk.used),
                "free": human(disk.free),
                "percent": round(disk.used / disk.total * 100, 1),
            },
            transfer={
                "down": human(transfer.dl_info_speed) + "/s",
                "up": human(transfer.up_info_speed) + "/s",
            }
            if transfer
            else {},
            torrents=torrent_data,
            jobs=jobs,
            events=events,
        )

    return app


def get_temp():
    try:
        return round(psutil.sensors_temperatures()["cpu_thermal"][0].current, 1)
    except Exception:
        return None

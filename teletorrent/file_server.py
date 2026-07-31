import threading
from waitress import serve
from .file_access import DOWNLOAD_PORT, cleanup_loop, create_download_app


def run_file_server():
    threading.Thread(target=cleanup_loop, daemon=True).start()
    serve(create_download_app(), host="127.0.0.1", port=DOWNLOAD_PORT, threads=8)

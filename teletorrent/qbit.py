import qbittorrentapi
from .config import CONFIG

def client():
    q=qbittorrentapi.Client(host=CONFIG.qbit_host, username=CONFIG.qbit_user, password=CONFIG.qbit_password, REQUESTS_ARGS={'timeout':15})
    q.auth_log_in()
    return q

def add_magnet(uri: str):
    return client().torrents_add(urls=uri, save_path=str(CONFIG.quarantine), category='quarantine')

def add_torrent(path: str):
    with open(path,'rb') as f: return client().torrents_add(torrent_files=f, save_path=str(CONFIG.quarantine), category='quarantine')

def torrents(): return list(client().torrents_info())
def transfer(): return client().transfer_info()
def pause(h): return client().torrents_pause(torrent_hashes=h)
def resume(h): return client().torrents_resume(torrent_hashes=h)
def delete(h, files=False): return client().torrents_delete(delete_files=files, torrent_hashes=h)

def set_location(h, location): return client().torrents_set_location(location=str(location), torrent_hashes=h)

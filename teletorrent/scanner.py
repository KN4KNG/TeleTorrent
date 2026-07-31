import hashlib, os, shutil, subprocess
from pathlib import Path
from .config import CONFIG

DANGEROUS={'.exe','.dll','.scr','.com','.msi','.ps1','.bat','.cmd','.vbs','.js','.jse','.wsf','.jar','.apk','.appimage','.deb','.rpm','.sh'}

def sha256(path: Path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''): h.update(chunk)
    return h.hexdigest()

def inspect_types(root: Path):
    suspicious=[]; count=0; total=0
    paths=[root] if root.is_file() else root.rglob('*')
    for p in paths:
        if p.is_file():
            count+=1; total+=p.stat().st_size
            if p.suffix.lower() in DANGEROUS: suspicious.append(str(p.relative_to(root.parent)))
    return count,total,suspicious

def clam_scan(path: Path):
    cmd=['clamdscan','--fdpass','--multiscan','--no-summary',str(path)]
    proc=subprocess.run(cmd,text=True,capture_output=True,timeout=21600)
    output=(proc.stdout+'\n'+proc.stderr).strip()
    return proc.returncode, output

def scan(path: Path):
    count,total,suspicious=inspect_types(path)
    code,output=clam_scan(path)
    return {'clean':code==0,'clam_code':code,'output':output,'files':count,'bytes':total,'suspicious':suspicious}

def promote(src: Path, name: str):
    CONFIG.approved.mkdir(parents=True,exist_ok=True)
    dst=CONFIG.approved/name
    if dst.exists(): dst=CONFIG.approved/f'{name}-{os.urandom(3).hex()}'
    shutil.move(str(src),str(dst)); return dst

def reject(src: Path, name: str):
    if CONFIG.delete_rejected:
        shutil.rmtree(src) if src.is_dir() else src.unlink(missing_ok=True); return None
    CONFIG.rejected.mkdir(parents=True,exist_ok=True)
    dst=CONFIG.rejected/name
    if dst.exists(): dst=CONFIG.rejected/f'{name}-{os.urandom(3).hex()}'
    shutil.move(str(src),str(dst)); return dst

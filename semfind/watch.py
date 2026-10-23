"""Indicizzazione in tempo reale: inotify sulle cartelle principali, vettori via llama-server.

- nome e metadati di ogni file nuovo entrano nell'indice dopo pochi secondi;
- i documenti di testo piccoli vengono trasformati in vettori subito, sulla CPU, un pezzo per
  richiesta (così le query di Spotlight non aspettano più di un pezzo);
- file grandi, immagini e PDF scansionati restano al giro notturno sulla GPU (text_sig/image_sig
  restano diversi da sig, quindi `semfind index` li prende);
- spostamenti dentro le cartelle osservate aggiornano solo il percorso, senza ricalcolare nulla.
"""
import ctypes
import ctypes.util
import os
import select
import struct
import time
import urllib.error

from . import config, extract, qembed, store
from .index import log, on_ac

IN_MODIFY, IN_CLOSE_WRITE, IN_MOVED_FROM, IN_MOVED_TO = 0x2, 0x8, 0x40, 0x80
IN_CREATE, IN_DELETE, IN_DELETE_SELF, IN_IGNORED, IN_ISDIR, IN_Q_OVERFLOW = 0x100, 0x200, 0x400, 0x8000, 0x40000000, 0x4000
MASK = IN_CLOSE_WRITE | IN_MOVED_FROM | IN_MOVED_TO | IN_CREATE | IN_DELETE | IN_DELETE_SELF
EVENT = struct.Struct("iIII")

SETTLE = 4.0            # secondi senza nuovi eventi prima di indicizzare un file
MAX_CHUNKS_AC = 60      # ~40 s di CPU; oltre si aspetta il giro GPU
MAX_CHUNKS_BATTERY = 20
TEMP_SUFFIXES = (".part", ".crdownload", ".tmp", ".download", ".partial", "~")


class Inotify:
    def __init__(self):
        self.libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        self.fd = self.libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1")
        self.wd: dict[int, str] = {}

    def add(self, path: str) -> bool:
        wd = self.libc.inotify_add_watch(self.fd, os.fsencode(path), MASK)
        if wd < 0:
            return False
        self.wd[wd] = path
        return True

    def read(self, timeout: float):
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return []
        try:
            buf = os.read(self.fd, 65536)
        except BlockingIOError:
            return []
        out, i = [], 0
        while i < len(buf):
            wd, mask, cookie, length = EVENT.unpack_from(buf, i)
            i += EVENT.size
            name = buf[i:i + length].rstrip(b"\0").decode(errors="surrogateescape")
            i += length
            out.append((wd, mask, cookie, name))
        return out


class Watcher:
    def __init__(self, cfg):
        self.cfg = cfg
        self.ino = Inotify()
        self.db = store.connect()
        self.pending: dict[str, float] = {}   # percorso -> istante dell'ultimo evento
        self.moved_from: dict[int, tuple[str, bool, float]] = {}  # cookie -> (vecchio percorso, è dir, t)
        self.roots = [os.path.realpath(d) for d in cfg.watch_dirs if os.path.isdir(d)]

    # -- cartelle osservate
    def excluded(self, path: str) -> bool:
        parts = path.split("/")
        return any(self.cfg.is_excluded_dir(p, "/".join(parts[:i + 1])) for i, p in enumerate(parts) if p)

    def watch_tree(self, top: str, register_files=False):
        n = 0
        for d, subdirs, files in os.walk(top):
            subdirs[:] = [s for s in subdirs if not self.cfg.is_excluded_dir(s, os.path.join(d, s))]
            if self.ino.add(d):
                n += 1
            if register_files:
                for f in files:
                    self.pending[os.path.join(d, f)] = time.time()
        return n

    # -- indice
    def upsert(self, path: str):
        try:
            st = os.stat(path, follow_symlinks=False)
        except OSError:
            return None
        name = os.path.basename(path)
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        sig = f"{st.st_size}:{st.st_mtime_ns}"
        scan_id = int(store.get_meta(self.db, "scan_id", 0))
        self.db.execute("""INSERT INTO files(path, name, ext, size, mtime, sig, seen) VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime, sig=excluded.sig""",
                        (path, name, ext, st.st_size, st.st_mtime, sig, scan_id))
        return self.db.execute("SELECT id, name, ext, sig, text_sig FROM files WHERE path=?", (path,)).fetchone()

    def remove(self, path: str, is_dir: bool):
        if is_dir:
            rows = self.db.execute("SELECT id FROM files WHERE path >= ? AND path < ?", (path + "/", path + "0")).fetchall()
        else:
            rows = self.db.execute("SELECT id FROM files WHERE path=?", (path,)).fetchall()
        for (fid,) in rows:
            store.delete_file(self.db, fid)
        self.db.commit()
        if rows:
            log(f"rimossi {len(rows)} file: {path}")

    def rename(self, old: str, new: str, is_dir: bool):
        if is_dir:
            n = self.db.execute("""UPDATE files SET path = ? || substr(path, ?) WHERE path >= ? AND path < ?""",
                                (new, len(old) + 1, old + "/", old + "0")).rowcount
            self.watch_tree(new)
        else:
            name = os.path.basename(new)
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            n = self.db.execute("UPDATE OR REPLACE files SET path=?, name=?, ext=? WHERE path=?",
                                (new, name, ext, old)).rowcount
            if not n or ext != (old.rsplit(".", 1)[-1].lower() if "." in old else ""):
                self.pending[new] = time.time()  # nuovo nel nostro indice o cambiato tipo
        self.db.commit()
        log(f"spostato ({n} file): {old} -> {new}")

    def index_text(self, path: str, row):
        fid, name, ext, sig, text_sig = row
        if ext not in self.cfg.text_exts or text_sig == sig:
            return
        if ext not in ("pdf", "epub", "docx", "pptx") and os.path.getsize(path) > self.cfg.max_text_bytes:
            return
        try:
            chunks, scanned = extract.text_chunks(path, ext, self.cfg)
        except Exception as ex:
            log(f"estrazione fallita ({type(ex).__name__}): {path}")
            return
        if scanned:
            self.db.execute("UPDATE files SET scanned=1, status='scanned', text_sig=? WHERE id=?", (sig, fid))
            self.db.commit()
            return  # le pagine come immagini le fa il giro GPU
        limit = MAX_CHUNKS_AC if on_ac() else MAX_CHUNKS_BATTERY
        if len(chunks) > limit:
            log(f"{len(chunks)} pezzi, rimandato al giro GPU: {path}")
            return
        t0, vecs = time.time(), []
        for _, c in chunks:
            text = f"title: {name} | text: {c}"
            for _ in range(3):
                try:
                    vecs.append(qembed.doc_vector(text))
                    break
                except urllib.error.HTTPError:  # pezzo oltre il contesto del server: accorcia
                    text = text[: len(text) * 2 // 3]
                except OSError as ex:
                    log(f"llama-server non risponde ({ex}), rimando: {path}")
                    return
            else:
                return
        # il file potrebbe essere cambiato durante il calcolo: in quel caso riparte dopo
        cur = self.db.execute("SELECT sig FROM files WHERE id=?", (fid,)).fetchone()
        if not cur or cur[0] != sig:
            return
        store.delete_items(self.db, fid, "text")
        store.add_items(self.db, fid, "text", chunks, vecs)
        self.db.execute("UPDATE files SET text_sig=?, status='ok', scanned=0 WHERE id=?", (sig, fid))
        self.db.commit()
        log(f"indicizzato: {path} ({len(chunks)} pezzi, {time.time() - t0:.1f}s)")

    def process(self, path: str):
        name = os.path.basename(path)
        if name.startswith(".") or name.endswith(TEMP_SUFFIXES) or self.excluded(os.path.dirname(path)):
            return
        if not os.path.isfile(path):
            return
        row = self.upsert(path)
        self.db.commit()
        if row:
            self.index_text(path, row)

    # -- ciclo principale
    def handle(self, wd, mask, cookie, name):
        base = self.ino.wd.get(wd)
        if mask & IN_Q_OVERFLOW:
            log("coda inotify piena: ci penserà il giro notturno")
            return
        if mask & IN_IGNORED:
            self.ino.wd.pop(wd, None)
            return
        if base is None or not name:
            return
        path = os.path.join(base, name)
        is_dir = bool(mask & IN_ISDIR)
        if is_dir and self.cfg.is_excluded_dir(name, path):
            return
        if mask & IN_MOVED_FROM:
            self.moved_from[cookie] = (path, is_dir, time.time())
        elif mask & IN_MOVED_TO:
            src = self.moved_from.pop(cookie, None)
            if src and not src[0].endswith(TEMP_SUFFIXES):
                self.rename(src[0], path, is_dir)
            elif is_dir:
                self.watch_tree(path, register_files=True)
            else:
                self.pending[path] = time.time()  # es. download: .part -> nome finale
        elif mask & IN_CREATE and is_dir:
            self.watch_tree(path, register_files=True)
        elif mask & IN_DELETE:
            self.remove(path, is_dir)
        elif mask & IN_CLOSE_WRITE:
            self.pending[path] = time.time()

    def run(self):
        n = sum(self.watch_tree(r) for r in self.roots)
        log(f"osservo {n} cartelle in {', '.join(r.replace(config.HOME.as_posix(), '~') for r in self.roots)}")
        while True:
            for ev in self.ino.read(1.0):
                self.handle(*ev)
            now = time.time()
            # spostamenti verso fuori dalle cartelle osservate: arriva solo MOVED_FROM
            for cookie, (path, is_dir, t) in list(self.moved_from.items()):
                if now - t > 1.0:
                    del self.moved_from[cookie]
                    self.remove(path, is_dir)
            ready = [p for p, t in self.pending.items() if now - t >= SETTLE]
            for p in ready:
                del self.pending[p]
                try:
                    self.process(p)
                except Exception as ex:  # un file strano non deve fermare il watcher
                    log(f"errore su {p}: {ex!r}")


def main():
    Watcher(config.load()).run()

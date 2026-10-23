"""Scansione del filesystem e indicizzazione incrementale (testo e immagini)."""
import fcntl
import glob
import os
import sys
import time

from . import embed, extract, store
from .config import LOCK_PATH, MODEL_ID


class Stop(Exception):
    pass


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def on_ac() -> bool:
    for d in glob.glob("/sys/class/power_supply/*"):
        try:
            if open(f"{d}/type").read().strip() == "Mains":
                return open(f"{d}/online").read().strip() == "1"
        except OSError:
            pass
    return True  # nessun alimentatore trovato: desktop


def acquire_lock():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    f = open(LOCK_PATH, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("un'altra indicizzazione è già in corso, esco")
        sys.exit(0)
    return f


# --------------------------------------------------------------------------- scan

def scan(db, cfg) -> int:
    scan_id = int(store.get_meta(db, "scan_id", 0)) + 1
    t0, n = time.time(), 0
    rows = []

    def flush():
        db.executemany("""
            INSERT INTO files(path, name, ext, size, mtime, sig, seen) VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime,
                sig=excluded.sig, seen=excluded.seen""", rows)
        rows.clear()

    stack = list(cfg.roots)
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        if not cfg.is_excluded_dir(e.name, e.path):
                            stack.append(e.path)
                    elif e.is_file(follow_symlinks=False) and not e.name.startswith("."):
                        st = e.stat(follow_symlinks=False)
                        ext = e.name.rsplit(".", 1)[-1].lower() if "." in e.name else ""
                        rows.append((e.path, e.name, ext, st.st_size, st.st_mtime,
                                     f"{st.st_size}:{st.st_mtime_ns}", scan_id))
                        n += 1
                        if len(rows) >= 5000:
                            flush()
                except OSError:
                    continue
    flush()
    gone = [r[0] for r in db.execute("SELECT id FROM files WHERE seen != ?", (scan_id,))]
    for fid in gone:
        store.delete_file(db, fid)
    store.set_meta(db, "scan_id", scan_id)
    db.commit()
    log(f"scansione: {n} file, {len(gone)} rimossi dall'indice, {time.time() - t0:.1f}s")
    return n


# --------------------------------------------------------------------------- passate

class Pass:
    """Accumula pezzi di più file e li trasforma in vettori a blocchi."""

    def __init__(self, db, model, kind, ac_only, deadline, batch_items):
        self.db, self.model, self.kind = db, model, kind
        self.ac_only, self.deadline, self.batch_items = ac_only, deadline, batch_items
        self.buf = []  # (file_id, sig, status, scanned, [(page, text)], [inputs])
        self.n_items = self.n_files = 0
        self.t0 = time.time()

    def add(self, fid, sig, status, scanned, rows, inputs):
        self.buf.append((fid, sig, status, scanned, rows, inputs))
        if sum(len(b[5]) for b in self.buf) >= self.batch_items:
            self.flush()

    def flush(self):
        if not self.buf:
            return
        inputs = [x for b in self.buf for x in b[5]]
        vecs = embed.encode(self.model, inputs, batch_size=8) if inputs else []
        i = 0
        col = "text_sig" if self.kind == "text" else "image_sig"
        for fid, sig, status, scanned, rows, ins in self.buf:
            store.delete_items(self.db, fid, self.kind)
            if self.kind == "text" and not scanned:
                store.delete_items(self.db, fid, "image")  # PDF che ora ha testo
            store.add_items(self.db, fid, self.kind, rows, vecs[i:i + len(ins)])
            i += len(ins)
            self.db.execute(f"UPDATE files SET {col}=?, status=?, scanned=? WHERE id=?",
                            (sig, status, scanned, fid))
        self.db.commit()
        self.n_items += len(inputs)
        self.n_files += len(self.buf)
        self.buf.clear()
        if self.ac_only and not on_ac():
            raise Stop("alimentatore scollegato")
        if self.deadline and time.time() > self.deadline:
            raise Stop("tempo massimo raggiunto")

    def mark(self, fid, sig, status, scanned=0):
        """File senza contenuto da trasformare in vettori: registra solo lo stato."""
        self.add(fid, sig, status, scanned, [], [])


def text_pass(db, cfg, ac_only=False, deadline=None):
    exts = cfg.text_exts
    todo = db.execute(f"""SELECT id, path, name, ext, size, sig FROM files
        WHERE ext IN ({','.join('?' * len(exts))}) AND (text_sig IS NULL OR text_sig != sig)
        ORDER BY size""", exts).fetchall()
    log(f"testo: {len(todo)} file da (re)indicizzare")
    if not todo:
        return
    model = embed.load("text")
    p = Pass(db, model, "text", ac_only, deadline, batch_items=256)
    last = time.time()
    for k, (fid, path, name, ext, size, sig) in enumerate(todo):
        if ext not in ("pdf", "epub", "docx", "pptx") and size > cfg.max_text_bytes:
            p.mark(fid, sig, "skipped:grande")
            continue
        try:
            chunks, scanned = extract.text_chunks(path, ext, cfg)
        except Exception as ex:
            p.mark(fid, sig, f"error:{type(ex).__name__}")
            continue
        if scanned:
            p.mark(fid, sig, "scanned", 1)
            continue
        if not chunks:
            p.mark(fid, sig, "empty")
            continue
        p.add(fid, sig, "ok", 0, chunks, [embed.doc_text(path, c) for _, c in chunks])
        if time.time() - last > 30:
            last = time.time()
            rate = p.n_items / max(1e-9, time.time() - p.t0)
            log(f"testo: {k + 1}/{len(todo)} file, {p.n_items} pezzi, {rate:.1f} pezzi/s")
    p.flush()
    log(f"testo: fatto, {p.n_files} file, {p.n_items} pezzi in {time.time() - p.t0:.0f}s")


def image_pass(db, cfg, ac_only=False, deadline=None):
    exts = cfg.image_exts
    todo = db.execute(f"""SELECT id, path, name, ext, size, sig, scanned FROM files
        WHERE ((ext IN ({','.join('?' * len(exts))}) AND size >= ?) OR (ext='pdf' AND scanned=1))
          AND (image_sig IS NULL OR image_sig != sig)
        ORDER BY mtime DESC""", (*exts, cfg.min_image_bytes)).fetchall()
    log(f"immagini: {len(todo)} file da (re)indicizzare")
    if not todo:
        return
    model = embed.load("image")
    p = Pass(db, model, "image", ac_only, deadline, batch_items=64)
    last = time.time()
    for k, (fid, path, name, ext, size, sig, scanned) in enumerate(todo):
        try:
            if ext == "pdf":
                pages = extract.pdf_page_images(path, cfg)
                status = "scanned"
            else:
                im = extract.load_image(path, cfg)
                pages = [(None, im)] if im else []
                status = "ok" if im else "skipped:piccola"
        except Exception as ex:
            p.mark(fid, sig, f"error:{type(ex).__name__}", scanned)
            continue
        p.add(fid, sig, status, scanned, [(pg, None) for pg, _ in pages], [im for _, im in pages])
        if time.time() - last > 30:
            last = time.time()
            rate = p.n_items / max(1e-9, time.time() - p.t0)
            log(f"immagini: {k + 1}/{len(todo)} file, {p.n_items} immagini, {rate:.1f}/s")
    p.flush()
    log(f"immagini: fatto, {p.n_files} file, {p.n_items} immagini in {time.time() - p.t0:.0f}s")


def run(cfg, text=True, images=True, ac_only=False, max_minutes=None, min_interval_hours=None, retext=False):
    lock = acquire_lock()
    if ac_only and not on_ac():
        log("a batteria: salto (--ac-only)")
        return
    db = store.connect()
    last = float(store.get_meta(db, "last_complete", 0))
    if min_interval_hours and time.time() - last < min_interval_hours * 3600:
        log(f"ultimo giro completo {(time.time() - last) / 3600:.1f} h fa: salto")
        return
    if store.get_meta(db, "model") not in (None, MODEL_ID):
        log("ATTENZIONE: indice creato con un altro modello, usa --rebuild")
    store.set_meta(db, "model", MODEL_ID)
    from .qembed import TITLE_VERSION
    if retext or int(store.get_meta(db, "title_version", 1)) != TITLE_VERSION:
        # cambiato il formato dei titoli: tutto il testo va ricalcolato (le immagini no)
        db.execute("UPDATE files SET text_sig = NULL WHERE text_sig IS NOT NULL AND scanned = 0")
        store.set_meta(db, "title_version", TITLE_VERSION)
        db.commit()
        log(f"formato dei titoli v{TITLE_VERSION}: ricalcolo tutto il testo")
    deadline = time.time() + max_minutes * 60 if max_minutes else None
    try:
        scan(db, cfg)
        if text:
            text_pass(db, cfg, ac_only, deadline)
        if images:
            image_pass(db, cfg, ac_only, deadline)
        store.set_meta(db, "last_complete", time.time())
    except Stop as s:
        log(f"interrotto: {s} (riprende dal punto giusto la prossima volta)")
    finally:
        db.commit()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.close()
        lock.close()

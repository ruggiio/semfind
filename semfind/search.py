"""Ricerca ibrida: vettori (testo e immagini) + parole chiave + nomi file, fusi con RRF."""
import os
import re
import sys
from dataclasses import dataclass, field

from . import qembed, store

RRF_K = 60
WEIGHTS = {"sem": 1.0, "kw": 1.0, "name": 0.8, "img": 0.8}
MIN_IMG_SIM = 0.30   # sotto questa similarità un'immagine non è considerata pertinente
MIN_TEXT_SIM = 0.30


@dataclass
class Hit:
    file_id: int
    path: str
    score: float = 0.0
    page: int | None = None
    snippet: str = ""
    sim: float | None = None
    why: list[str] = field(default_factory=list)
    dups: list[str] = field(default_factory=list)


STOP = set("""il lo la i gli le un uno una di del della dei delle a al alla ai da dal in nel nella
con su per tra fra e o ed che come the of and or to in on for with an is""".split())


def _fts_query(q: str, min_len: int = 2) -> str | None:
    toks = [t for t in re.findall(r"\w+", q, re.UNICODE) if len(t) >= min_len and t.lower() not in STOP]
    return " OR ".join(f'"{t}"' for t in toks) or None


def _snippet(text: str, q: str, width: int = 220) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    pos = -1
    for t in sorted(re.findall(r"\w{3,}", q), key=len, reverse=True):
        pos = text.lower().find(t.lower())
        if pos >= 0:
            break
    start = max(0, pos - width // 3) if pos >= 0 else 0
    s = text[start:start + width]
    return ("…" if start else "") + s + ("…" if start + width < len(text) else "")


def search(q: str, n: int = 10, exts: list[str] | None = None, under: str | None = None,
           kinds=("text", "image", "name")) -> list[Hit]:
    db = store.connect()
    filt = bool(exts or under)
    k = 400 if filt else 80

    def file_ok(path, ext):
        if exts and ext not in exts:
            return False
        return not under or path.startswith(under.rstrip("/") + "/")

    item_sql = "SELECT i.file_id, i.page, i.text, f.path, f.ext FROM items i JOIN files f ON f.id=i.file_id WHERE i.id=?"
    lists: dict[str, list[tuple[int, str, int | None, str | None, float | None]]] = {}

    if "text" in kinds or "image" in kinds:
        qv = qembed.query_vector(q)
    if "text" in kinds:
        rows = db.execute("SELECT rowid, distance FROM vec_text WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                          (qv, k)).fetchall()
        out = []
        for rid, dist in rows:
            fid, page, text, path, ext = db.execute(item_sql, (rid,)).fetchone()
            if 1 - dist >= MIN_TEXT_SIM and file_ok(path, ext):
                out.append((fid, path, page, text, 1 - dist))
        lists["sem"] = out
        fq = _fts_query(q)
        if fq:
            rows = db.execute("SELECT rowid FROM items_fts WHERE items_fts MATCH ? ORDER BY rank LIMIT ?",
                              (fq, k)).fetchall()
            out = []
            for (rid,) in rows:
                fid, page, text, path, ext = db.execute(item_sql, (rid,)).fetchone()
                if file_ok(path, ext):
                    out.append((fid, path, page, text, None))
            lists["kw"] = out
    if "image" in kinds:
        rows = db.execute("SELECT rowid, distance FROM vec_image WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                          (qv, k)).fetchall()
        out = []
        for rid, dist in rows:
            fid, page, text, path, ext = db.execute(item_sql, (rid,)).fetchone()
            if 1 - dist >= MIN_IMG_SIM and file_ok(path, ext):
                out.append((fid, path, page, None, 1 - dist))
        lists["img"] = out
    if "name" in kinds:
        nq = _fts_query(q, min_len=3)  # il tokenizer trigram vuole almeno 3 caratteri
        if nq:
            rows = db.execute("""SELECT f.id, f.path, f.ext FROM files_fts JOIN files f ON f.id=files_fts.rowid
                WHERE files_fts MATCH ? ORDER BY rank LIMIT ?""", (f"name : ({nq})", k)).fetchall()
            lists["name"] = [(fid, path, None, None, None) for fid, path, ext in rows if file_ok(path, ext)]

    hits: dict[int, Hit] = {}
    for src, rows in lists.items():
        seen = set()
        for fid, path, page, text, sim in rows:
            if fid in seen:
                continue
            seen.add(fid)
            h = hits.setdefault(fid, Hit(fid, path))
            h.score += WEIGHTS[src] / (RRF_K + len(seen))
            h.why.append(src)
            if sim is not None and (h.sim is None or sim > h.sim):
                h.sim = sim
            if text and not h.snippet:
                h.page, h.snippet = page, _snippet(text, q)
            elif page and h.page is None:
                h.page = page
    # stesso nome e stessa dimensione = copia dello stesso file: un solo risultato
    ranked = sorted(hits.values(), key=lambda h: -h.score)
    out: list[Hit] = []
    groups: dict[tuple, Hit] = {}
    for h in ranked:
        name, size = db.execute("SELECT name, size FROM files WHERE id=?", (h.file_id,)).fetchone()
        key = (name, size)
        if key in groups:
            groups[key].dups.append(h.path)
            continue
        groups[key] = h
        out.append(h)
        if len(out) == n:
            break
    db.close()
    return out


def link(path: str, label: str) -> str:
    if not sys.stdout.isatty():
        return label
    return f"\x1b]8;;file://{path}\x1b\\{label}\x1b]8;;\x1b\\"


def show(hits: list[Hit]):
    home = os.path.expanduser("~")
    for i, h in enumerate(hits, 1):
        short = h.path.replace(home, "~", 1)
        page = f"  p.{h.page}" if h.page else ""
        sim = f" {h.sim:.2f}" if h.sim is not None else ""
        print(f"\x1b[1m{i:2}. {link(h.path, short)}\x1b[0m{page}  \x1b[2m[{'+'.join(h.why)}{sim}]\x1b[0m")
        if h.snippet:
            print(f"    {h.snippet}")
        if h.dups:
            more = f" (+{len(h.dups) - 1})" if len(h.dups) > 1 else ""
            print(f"    \x1b[2manche in: {h.dups[0].replace(home, '~', 1)}{more}\x1b[0m")

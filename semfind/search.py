"""Ricerca ibrida: vettori (testo e immagini) + parole chiave + nomi file, fusi con RRF."""
import math
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


# Parametri della classifica (eval/run.py --set chiave=valore per provarne altri)
TUNING = {
    "fusion": "score",      # rrf: somma di 1/(60+posizione) | score: combinazione dei punteggi normalizzati
    "kw_and": False,      # parole chiave: prima tutte insieme (AND), poi una qualsiasi (OR)
    "name_path": False,   # cerca le parole della query anche nei nomi delle cartelle
    "w_sem": 1.0, "w_kw": 0.2, "w_name": 0.2, "w_img": 0.8,
    "multi": 0.0,         # bonus per ogni altro pezzo dello stesso file quasi altrettanto vicino
    "translate": "auto",  # "auto": semfind.translate se disponibile | None | funzione q -> inglese
    "len_pen": 0.025,      # penalità per i documenti lunghi: sim -= len_pen * log10(pezzi)
}


@dataclass
class _F:
    """Indizi raccolti per un file prima di calcolare il punteggio."""
    path: str
    ext: str
    sem: float = 0.0       # similarità del pezzo migliore
    sem_n: int = 0         # pezzi entro 0,03 dal migliore
    kw: float = 0.0        # bm25 del pezzo migliore
    name: float = 0.0      # bm25 su nome (e cartelle)
    img: float = 0.0
    ranks: dict = field(default_factory=dict)
    page: int | None = None
    text: str | None = None
    text_src: str = ""


def _tokens(q: str, min_len: int) -> list[str]:
    return [t for t in re.findall(r"\w+", q, re.UNICODE) if len(t) >= min_len and t.lower() not in STOP]


def search(q: str, n: int = 10, exts: list[str] | None = None, under: str | None = None,
           kinds=("text", "image", "name")) -> list[Hit]:
    T = TUNING
    db = store.connect()
    filt = bool(exts or under)
    k = 400 if filt else 80

    def file_ok(path, ext):
        if exts and ext not in exts:
            return False
        return not under or path.startswith(under.rstrip("/") + "/")

    item_sql = "SELECT i.file_id, i.page, i.text, f.path, f.ext FROM items i JOIN files f ON f.id=i.file_id WHERE i.id=?"
    files: dict[int, _F] = {}

    def add(src, rank_list, fid, path, ext, page, text, value):
        f = files.setdefault(fid, _F(path, ext))
        if src not in f.ranks:
            f.ranks[src] = len(rank_list) + 1
            rank_list.append(fid)
        if src == "sem":
            if value > f.sem:
                f.sem = value
            if text and (f.text_src != "sem" or value >= f.sem):
                f.page, f.text, f.text_src = page, text, "sem"
        elif src == "kw":
            if value > f.kw:
                f.kw = value
            if text and not f.text:
                f.page, f.text, f.text_src = page, text, "kw"
        elif src == "name":
            f.name = max(f.name, value)
        elif src == "img":
            f.img = max(f.img, value)
            if page and f.page is None:
                f.page = page

    if "text" in kinds or "image" in kinds:
        qv = qembed.query_vector(q)
    if "text" in kinds:
        rl: list = []
        rows = db.execute("SELECT rowid, distance FROM vec_text WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                          (qv, k)).fetchall()
        tr = T["translate"]
        if tr == "auto":
            from .translate import translate as tr
        if tr:
            # per ogni pezzo vale la distanza minore tra domanda originale e tradotta
            qt = tr(q)
            if qt and qt.strip().lower() != q.strip().lower():
                d = dict(rows)
                for rid, dist in db.execute("SELECT rowid, distance FROM vec_text WHERE embedding MATCH ? AND k = ?",
                                            (qembed.query_vector(qt), k)):
                    d[rid] = min(dist, d.get(rid, 9))
                rows = sorted(d.items(), key=lambda r: r[1])[:k]
        best: dict[int, float] = {}
        for rid, dist in rows:
            fid, page, text, path, ext = db.execute(item_sql, (rid,)).fetchone()
            sim = 1 - dist
            if sim >= MIN_TEXT_SIM and file_ok(path, ext):
                add("sem", rl, fid, path, ext, page, text, sim)
                if sim >= best.setdefault(fid, sim) - 0.03:
                    files[fid].sem_n += 1
        toks = _tokens(q, 2)
        queries = []
        if toks:
            if T["kw_and"] and len(toks) > 1:
                queries.append(" AND ".join(f'"{t}"' for t in toks))
            queries.append(" OR ".join(f'"{t}"' for t in toks))
        rl = []
        for i, fq in enumerate(queries):
            rows = db.execute("SELECT rowid, -bm25(items_fts) FROM items_fts WHERE items_fts MATCH ? ORDER BY rank LIMIT ?",
                              (fq, k)).fetchall()
            for rid, score in rows:
                fid, page, text, path, ext = db.execute(item_sql, (rid,)).fetchone()
                if file_ok(path, ext):
                    # i pezzi che contengono tutte le parole valgono il doppio
                    add("kw", rl, fid, path, ext, page, text, score * (2.0 if len(queries) == 2 and i == 0 else 1.0))
    if "image" in kinds:
        rl = []
        rows = db.execute("SELECT rowid, distance FROM vec_image WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                          (qv, k)).fetchall()
        for rid, dist in rows:
            fid, page, text, path, ext = db.execute(item_sql, (rid,)).fetchone()
            if 1 - dist >= MIN_IMG_SIM and file_ok(path, ext):
                add("img", rl, fid, path, ext, page, None, 1 - dist)
    if "name" in kinds:
        toks = _tokens(q, 3)  # il tokenizer trigram vuole almeno 3 caratteri
        if toks:
            col = "{name path}" if T["name_path"] else "name"
            nq = f"{col} : ({' OR '.join(f'{chr(34)}{t}{chr(34)}' for t in toks)})"
            rows = db.execute("""SELECT f.id, f.path, f.ext, -bm25(files_fts, 3.0, 1.0) FROM files_fts
                JOIN files f ON f.id=files_fts.rowid WHERE files_fts MATCH ? ORDER BY rank LIMIT ?""",
                              (nq, k)).fetchall()
            rl = []
            for fid, path, ext, score in rows:
                if file_ok(path, ext):
                    add("name", rl, fid, path, ext, None, None, score)

    # ---- documenti lunghi: con centinaia di pezzi ne hanno sempre uno vicino a qualunque domanda
    if T["len_pen"] and files:
        for fid, f in files.items():
            f.nchunks = db.execute("SELECT count(*) FROM items WHERE file_id=? AND kind='text'", (fid,)).fetchone()[0]
            pen = T["len_pen"] * math.log10(max(1, f.nchunks))
            if f.sem:
                f.sem -= pen
            if f.kw:
                f.kw *= max(0.2, 1 - pen * 4)
        for src, attr in (("sem", "sem"), ("kw", "kw")):
            ranked_src = sorted((f for f in files.values() if src in f.ranks), key=lambda f: -getattr(f, attr))
            for r, f in enumerate(ranked_src, 1):
                f.ranks[src] = r

    # ---- punteggio
    W = {"sem": T["w_sem"], "kw": T["w_kw"], "name": T["w_name"], "img": T["w_img"]}
    if T["fusion"] == "rrf":
        for f in files.values():
            f.score = sum(W[s] / (RRF_K + r) for s, r in f.ranks.items())
    else:
        # normalizza ogni indizio sul migliore tra i candidati, poi combina.
        # La similarità semantica è riscalata tra la mediana e il massimo: sotto la mediana vale 0.
        def norm(attr):
            vals = sorted(getattr(f, attr) for f in files.values() if getattr(f, attr) > 0)
            if not vals:
                return lambda v: 0.0
            lo = vals[len(vals) // 2] if attr in ("sem", "img") else 0.0
            hi = vals[-1]
            return lambda v: max(0.0, (v - lo) / (hi - lo)) if hi > lo else (1.0 if v > 0 else 0.0)
        ns, nk, nn, ni = norm("sem"), norm("kw"), norm("name"), norm("img")
        for f in files.values():
            f.score = (W["sem"] * ns(f.sem) * (1 + T["multi"] * min(f.sem_n - 1, 5) if f.sem_n else 1)
                       + W["kw"] * nk(f.kw) + W["name"] * nn(f.name) + W["img"] * ni(f.img))

    hits: list[Hit] = []
    for fid, f in files.items():
        h = Hit(fid, f.path, f.score, f.page, _snippet(f.text, q) if f.text else "",
                f.sem or f.img or None, [s for s in ("sem", "kw", "name", "img") if s in f.ranks])
        hits.append(h)

    # stesso nome e stessa dimensione = copia dello stesso file: un solo risultato
    ranked = sorted(hits, key=lambda h: -h.score)
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

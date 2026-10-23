"""Indice SQLite: metadati di tutti i file, FTS5 per nomi e testo, sqlite-vec per i vettori."""
import sqlite3

import sqlite_vec

from .config import DATA_DIR, DB_PATH, DIM

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    ext TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    sig TEXT NOT NULL,            -- size:mtime_ns attuale
    text_sig TEXT,                -- sig all'ultima indicizzazione testo
    image_sig TEXT,               -- sig all'ultima indicizzazione immagini
    scanned INTEGER DEFAULT 0,    -- PDF senza testo: va trattato come immagini
    status TEXT,                  -- ok / empty / error:<msg> / skipped
    seen INTEGER NOT NULL         -- id della scansione in cui è stato visto
);
CREATE INDEX IF NOT EXISTS files_ext ON files(ext);

CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
    name, path, content='files', content_rowid='id', tokenize='trigram remove_diacritics 1'
);
CREATE TRIGGER IF NOT EXISTS files_ai AFTER INSERT ON files BEGIN
    INSERT INTO files_fts(rowid, name, path) VALUES (new.id, new.name, new.path);
END;
CREATE TRIGGER IF NOT EXISTS files_ad AFTER DELETE ON files BEGIN
    INSERT INTO files_fts(files_fts, rowid, name, path) VALUES ('delete', old.id, old.name, old.path);
END;

CREATE TRIGGER IF NOT EXISTS files_au AFTER UPDATE OF name, path ON files BEGIN
    INSERT INTO files_fts(files_fts, rowid, name, path) VALUES ('delete', old.id, old.name, old.path);
    INSERT INTO files_fts(rowid, name, path) VALUES (new.id, new.name, new.path);
END;

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,           -- text / image
    page INTEGER,                 -- pagina (PDF) o NULL
    text TEXT                     -- testo del pezzo (NULL per immagini)
);
CREATE INDEX IF NOT EXISTS items_file ON items(file_id, kind);

CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    text, content='items', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items WHEN new.text IS NOT NULL BEGIN
    INSERT INTO items_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items WHEN old.text IS NOT NULL BEGIN
    INSERT INTO items_fts(items_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS vec_text USING vec0(embedding float[{DIM}] distance_metric=cosine);
CREATE VIRTUAL TABLE IF NOT EXISTS vec_image USING vec0(embedding float[{DIM}] distance_metric=cosine);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    return db


def get_meta(db, key, default=None):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(db, key, value):
    db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, str(value)))


def delete_items(db, file_id: int, kind: str):
    table = "vec_text" if kind == "text" else "vec_image"
    ids = [r[0] for r in db.execute("SELECT id FROM items WHERE file_id=? AND kind=?", (file_id, kind))]
    if ids:
        db.executemany(f"DELETE FROM {table} WHERE rowid=?", [(i,) for i in ids])
        db.execute("DELETE FROM items WHERE file_id=? AND kind=?", (file_id, kind))


def delete_file(db, file_id: int):
    delete_items(db, file_id, "text")
    delete_items(db, file_id, "image")
    db.execute("DELETE FROM files WHERE id=?", (file_id,))


def add_items(db, file_id: int, kind: str, rows: list[tuple[int | None, str | None]], vecs):
    """vecs: array numpy (indicizzatore GPU) oppure lista di bytes float32 (watcher)."""
    table = "vec_text" if kind == "text" else "vec_image"
    for (page, text), v in zip(rows, vecs):
        cur = db.execute("INSERT INTO items(file_id, kind, page, text) VALUES (?, ?, ?, ?)",
                         (file_id, kind, page, text))
        blob = v if isinstance(v, bytes) else v.astype("float32").tobytes()
        db.execute(f"INSERT INTO {table}(rowid, embedding) VALUES (?, ?)", (cur.lastrowid, blob))

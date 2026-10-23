"""semfind — ricerca semantica locale dei file con EmbeddingGemma 2."""
import argparse
import json
import os
import sys

from . import config


def main(argv=None):
    ap = argparse.ArgumentParser(prog="semfind", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", aliases=["s"], help="cerca (default se si scrive solo la query)")
    s.add_argument("query", nargs="+")
    s.add_argument("-n", type=int, default=10, help="numero di risultati")
    s.add_argument("-t", "--type", help="estensioni separate da virgola, es. pdf,md")
    s.add_argument("-i", "--in", dest="under", help="solo sotto questa cartella")
    s.add_argument("--img", action="store_true", help="solo immagini e PDF scansionati")
    s.add_argument("--name", action="store_true", help="solo nomi dei file")
    s.add_argument("--json", action="store_true")

    i = sub.add_parser("index", help="aggiorna l'indice")
    i.add_argument("--no-text", action="store_true")
    i.add_argument("--no-images", action="store_true")
    i.add_argument("--ac-only", action="store_true", help="esci se a batteria (per il timer)")
    i.add_argument("--max-minutes", type=float)
    i.add_argument("--min-interval-hours", type=float,
                   help="non fare nulla se l'ultimo giro completo è più recente di così")
    i.add_argument("--rebuild", action="store_true", help="cancella e ricrea l'indice")
    i.add_argument("--retext", action="store_true", help="ricalcola i vettori di tutto il testo")

    sub.add_parser("stats", help="statistiche dell'indice")
    sub.add_parser("watch", help="indicizza in tempo reale i file nuovi (servizio)")

    known = {"search", "s", "index", "stats", "watch", "-h", "--help"}
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] not in known:
        argv = ["search", *argv]
    a = ap.parse_args(argv)
    cfg = config.load()

    if a.cmd in ("search", "s"):
        from . import search
        kinds = ("image",) if a.img else ("name",) if a.name else ("text", "image", "name")
        exts = [e.strip().lower().lstrip(".") for e in a.type.split(",")] if a.type else None
        under = os.path.abspath(os.path.expanduser(a.under)) if a.under else None
        hits = search.search(" ".join(a.query), a.n, exts, under, kinds)
        if a.json:
            print(json.dumps([h.__dict__ for h in hits], ensure_ascii=False, indent=1))
        else:
            search.show(hits) if hits else print("nessun risultato")
    elif a.cmd == "index":
        from . import index
        if a.rebuild and config.DB_PATH.exists():
            for suffix in ("", "-wal", "-shm"):
                p = config.DB_PATH.with_name(config.DB_PATH.name + suffix)
                p.unlink(missing_ok=True)
        index.run(cfg, text=not a.no_text, images=not a.no_images,
                  ac_only=a.ac_only, max_minutes=a.max_minutes,
                  min_interval_hours=a.min_interval_hours, retext=a.retext)
    elif a.cmd == "watch":
        from . import watch
        watch.main()
    elif a.cmd == "stats":
        from . import store
        db = store.connect()
        q = lambda sql: db.execute(sql).fetchone()[0]
        print(f"file visti:        {q('SELECT count(*) FROM files')}")
        print(f"pezzi di testo:    {q('SELECT count(*) FROM vec_text')}")
        print(f"immagini/pagine:   {q('SELECT count(*) FROM vec_image')}")
        print(f"PDF scansionati:   {q('SELECT count(*) FROM files WHERE scanned=1')}")
        print(f"ultima scansione:  #{store.get_meta(db, 'scan_id', 0)}")
        for st, c in db.execute("SELECT status, count(*) FROM files WHERE status IS NOT NULL GROUP BY status ORDER BY 2 DESC"):
            print(f"  {st:20} {c}")
        size = sum(p.stat().st_size for p in config.DATA_DIR.glob("index.db*"))
        print(f"dimensione indice: {size / 1e6:.0f} MB")


if __name__ == "__main__":
    main()

"""Banco di prova della ricerca: per ogni domanda, in che posizione esce il file atteso.

    .venv/bin/python -m eval.run                 # sistema attuale
    .venv/bin/python -m eval.run -v              # con il dettaglio delle domande sbagliate
    .venv/bin/python -m eval.run --set fusion=rrf  # parametri di search.TUNING da provare
"""
import argparse
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from semfind import search  # noqa: E402

QUERIES = Path(__file__).with_name("queries.tsv")  # personale, escluso da git: vedi queries.example.tsv


def load():
    out = []
    for line in QUERIES.read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            q, pat = line.split("\t")
            out.append((q, re.compile(pat)))
    return out


def rank_of(hits, pat):
    for i, h in enumerate(hits, 1):
        if any(pat.search(os.path.basename(p)) for p in [h.path, *h.dups]):
            return i
    return None


def evaluate(queries, k=10, verbose=False):
    ranks, t0 = [], time.time()
    for q, pat in queries:
        hits = search.search(q, n=k, kinds=("text", "name"))
        r = rank_of(hits, pat)
        ranks.append(r)
        if verbose and r != 1:
            print(f"  [{r or '—':>2}] {q}")
            for h in hits[:3]:
                print(f"         {os.path.basename(h.path)}  [{'+'.join(h.why)}]")
    n = len(ranks)
    mrr = sum(1 / r for r in ranks if r) / n
    h1 = sum(1 for r in ranks if r == 1) / n
    h5 = sum(1 for r in ranks if r and r <= 5) / n
    miss = sum(1 for r in ranks if not r)
    ms = (time.time() - t0) / n * 1000
    return mrr, h1, h5, miss, ms


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", action="store_true")
    ap.add_argument("--set", action="append", default=[], help="chiave=valore in search.TUNING")
    a = ap.parse_args()
    for kv in a.set:
        k, v = kv.split("=", 1)
        old = search.TUNING[k]
        if k == "translate":
            search.TUNING[k] = None if v in ("0", "none") else "auto"
        else:
            search.TUNING[k] = (v == "1") if isinstance(old, bool) else type(old)(v)
    qs = load()
    mrr, h1, h5, miss, ms = evaluate(qs, verbose=a.v)
    label = " ".join(a.set) or "attuale"
    print(f"{label:40} MRR {mrr:.3f}  primo {h1:.0%}  top5 {h5:.0%}  fuori top10 {miss}/{len(qs)}  {ms:.0f} ms/query")


if __name__ == "__main__":
    main()

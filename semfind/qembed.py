"""Vettore della query senza torch né numpy: chiede a llama-server (semfind-embed.service).

Se il server non risponde ricade sul modello torch, lento da caricare ma equivalente
(verificato: coseno medio 1,000 tra Q8_0 GGUF e i vettori dell'indice).
"""
import json
import math
import re
import struct
import urllib.request
from pathlib import Path

from .config import DIM, EMBED_URL

QUERY_PREFIX = "task: search result | query: "
TITLE_VERSION = 2  # 1: solo nome del file; 2: cartelle significative + nome

_GENERIC = {"Scrivania", "Desktop", "Scaricati", "Downloads", "Documenti", "Documents",
            "Immagini", "Pictures", "home", "media", "mnt"}


def doc_title(path: str) -> str:
    """Titolo per EmbeddingGemma ("title: … | text: …"): le cartelle danno il contesto
    (corso, progetto, …) che il pezzo di testo da solo non ha.
    ~/Scrivania/RWTH/RLLBC Exam/(VO) …/Lecture 12/Lecture 12/x.pdf -> "RWTH › RLLBC Exam › Lecture 12 › x"
    """
    p = Path(path)
    try:
        parts = list(p.parent.relative_to(Path.home()).parts)
    except ValueError:
        parts = list(p.parent.parts[1:])
    parts = [x for x in parts if x not in _GENERIC and not x.startswith(".")]
    if len(parts) > 3:
        parts = parts[:2] + parts[-1:]
    out = []
    for x in [*parts, p.stem]:
        x = re.sub(r"[_]+", " ", x).strip()
        if x and (not out or out[-1] != x):
            out.append(x)
    return " › ".join(out)[:160]


def doc_text(path: str, chunk: str) -> str:
    return f"title: {doc_title(path)} | text: {chunk}"
_torch_model = None


def _server(text: str, timeout: float) -> list[float]:
    req = urllib.request.Request(f"{EMBED_URL}/v1/embeddings", json.dumps({"input": [text]}).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)["data"][0]["embedding"]


def _pack(v: list[float]) -> bytes:
    v = v[:DIM]
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return struct.pack(f"{DIM}f", *(x / n for x in v))


def doc_vector(text: str, timeout: float = 60.0) -> bytes:
    """Vettore di un pezzo di documento (testo già nel formato "title: … | text: …")."""
    return _pack(_server(text, timeout))


def query_vector(q: str, timeout: float = 5.0) -> bytes:
    """Vettore float32 a DIM dimensioni, troncato (MRL) e rinormalizzato, pronto per sqlite-vec."""
    text = QUERY_PREFIX + q
    try:
        return _pack(_server(text, timeout))
    except OSError:
        global _torch_model
        from . import embed
        _torch_model = _torch_model or embed.load("text", device="cpu")
        return embed.encode(_torch_model, [text])[0].tobytes()

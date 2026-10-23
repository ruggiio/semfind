"""Traduzione italiano -> inglese della domanda, per trovare anche il materiale in inglese.

Sul banco di prova (eval/) cercare con domanda originale + traduzione porta l'MRR da 0,55 a 0,63:
EmbeddingGemma 2 è multilingue, ma tra lingue diverse la similarità cala abbastanza da far
vincere documenti in italiano meno pertinenti.

opus-mt-it-en convertito per CTranslate2 (int8, 87 MB): ~65 ms a domanda, ~115 MB di RAM.
Si carica alla prima domanda e si scarica dopo IDLE_UNLOAD secondi senza domande, così nel
processo di Spotlight non resta in memoria quando non serve. Se ctranslate2 o il modello
mancano, translate() restituisce None e la ricerca va avanti senza.
"""
import threading
from functools import lru_cache

from .config import DATA_DIR

MODEL_DIR = DATA_DIR / "models/opus-mt-it-en-ct2"
IDLE_UNLOAD = 300  # secondi

_lock = threading.Lock()
_state = {"tr": None, "src": None, "tgt": None, "timer": None, "broken": False}


def _unload():
    with _lock:
        _state["tr"] = _state["src"] = _state["tgt"] = None
        _state["timer"] = None
        _translate_cached.cache_clear()


def _load():
    import ctranslate2
    import sentencepiece as spm
    _state["tr"] = ctranslate2.Translator(str(MODEL_DIR), device="cpu", compute_type="int8",
                                          inter_threads=1, intra_threads=2)
    _state["src"] = spm.SentencePieceProcessor(model_file=str(MODEL_DIR / "source.spm"))
    _state["tgt"] = spm.SentencePieceProcessor(model_file=str(MODEL_DIR / "target.spm"))


@lru_cache(maxsize=256)
def _translate_cached(q: str) -> str:
    toks = _state["src"].encode(q, out_type=str) + ["</s>"]
    out = _state["tr"].translate_batch([toks], beam_size=2, max_decoding_length=64)[0].hypotheses[0]
    return _state["tgt"].decode(out)


def available() -> bool:
    return not _state["broken"] and (MODEL_DIR / "model.bin").exists()


def translate(q: str) -> str | None:
    if not available() or len(q) < 3:
        return None
    with _lock:
        try:
            if _state["tr"] is None:
                _load()
        except Exception:  # ctranslate2 non installato, modello rovinato, ...
            _state["broken"] = True
            return None
        if _state["timer"]:
            _state["timer"].cancel()
        t = threading.Timer(IDLE_UNLOAD, _unload)
        t.daemon = True
        t.start()
        _state["timer"] = t
        return _translate_cached(q)

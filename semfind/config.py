"""Configurazione: default nel codice, override in ~/.config/semfind/config.toml."""
import fnmatch
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home()
CONFIG_PATH = HOME / ".config/semfind/config.toml"
DATA_DIR = HOME / ".local/share/semfind"
DB_PATH = DATA_DIR / "index.db"
LOCK_PATH = DATA_DIR / "index.lock"

MODEL_ID = "google/embeddinggemma-2"
DIM = 256  # MRL: quasi lossless fino a 256 (vedi model card)
EMBED_URL = "http://127.0.0.1:47615"  # llama-server per le query (semfind-embed.service)


def xdg_user_dirs() -> list[str]:
    """Scaricati/Downloads, Scrivania/Desktop, ... dal file user-dirs.dirs, nella lingua del sistema."""
    out = []
    f = HOME / ".config/user-dirs.dirs"
    if f.exists():
        for line in f.read_text().splitlines():
            m = re.match(r'XDG_(DOWNLOAD|DESKTOP|DOCUMENTS|PICTURES)_DIR="(.+)"', line.strip())
            if m:
                d = m.group(2).replace("$HOME", str(HOME))
                if d.rstrip("/") != str(HOME):
                    out.append(d)
    return out or [str(HOME / d) for d in ("Downloads", "Desktop", "Documents", "Pictures")]


@dataclass
class Config:
    roots: list[str] = field(default_factory=lambda: [str(HOME)])
    # nomi di cartella esclusi ovunque (glob); le cartelle nascoste sono sempre escluse
    exclude_dirs: list[str] = field(default_factory=lambda: [
        "node_modules", "venv", "env", "__pycache__", "site-packages", "build", "dist",
        "snap", "slprj", "*_ert_rtw", "*_grt_rtw", "*_rtw", "WhiteSur-*", "semfind",
        "CMakeFiles", "target", "Trash", "python2.*", "python3.*", "dist-packages",
    ])
    exclude_paths: list[str] = field(default_factory=list)  # percorsi assoluti esclusi
    text_exts: list[str] = field(default_factory=lambda: [
        "pdf", "epub", "docx", "pptx", "md", "tex", "txt", "rst", "org",
        "py", "m", "ino", "ipynb",
    ])
    image_exts: list[str] = field(default_factory=lambda: ["jpg", "jpeg", "png", "webp"])
    max_text_bytes: int = 3_000_000       # file di testo semplice più grandi: saltati
    max_chunks_per_file: int = 300        # tetto per libri enormi
    chunk_chars: int = 1500
    chunk_overlap: int = 200
    min_image_bytes: int = 20_000         # salta icone
    min_image_side: int = 200
    scanned_pdf_max_pages: int = 40
    watch_dirs: list[str] = field(default_factory=xdg_user_dirs)

    def is_excluded_dir(self, name: str, path: str) -> bool:
        if name.startswith("."):
            return True
        if any(fnmatch.fnmatch(name, pat) for pat in self.exclude_dirs):
            return True
        return any(path == p or path.startswith(p.rstrip("/") + "/") for p in self.exclude_paths)


def load() -> Config:
    cfg = Config()
    if CONFIG_PATH.exists():
        data = tomllib.loads(CONFIG_PATH.read_text())
        for k, v in data.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
    cfg.roots = [os.path.expanduser(r) for r in cfg.roots]
    cfg.exclude_paths = [os.path.expanduser(p) for p in cfg.exclude_paths]
    cfg.watch_dirs = [os.path.expanduser(p) for p in cfg.watch_dirs]
    return cfg

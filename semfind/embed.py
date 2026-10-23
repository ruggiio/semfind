"""Caricamento di EmbeddingGemma 2 con i soli moduli necessari."""
import os
import warnings

import numpy as np

from .config import DIM, MODEL_ID

warnings.filterwarnings("ignore")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

QUERY_PREFIX = "task: search result | query: "


def load(modality: str = "text", device: str | None = None):
    """modality: 'text' (270M) o 'image' (testo+visione, 440M)."""
    import torch
    from sentence_transformers import SentenceTransformer
    from transformers.utils import logging as hf_logging
    hf_logging.set_verbosity_error()
    hf_logging.disable_progress_bar()

    if device is None:
        device = "cuda" if torch.cuda.is_available() else "cpu"
    # mai float16: la model card avverte che produce NaN in silenzio
    dtype = torch.bfloat16 if device == "cuda" and torch.cuda.is_bf16_supported() else torch.float32
    cfg = {"vision_config": None, "audio_config": None} if modality == "text" else {"audio_config": None}
    model = SentenceTransformer(MODEL_ID, device=device,
                                model_kwargs={"torch_dtype": dtype, "attn_implementation": "sdpa"},
                                config_kwargs=cfg)
    model.max_seq_length = 1024
    return model


def encode(model, inputs, batch_size: int = 16) -> np.ndarray:
    return model.encode(inputs, batch_size=batch_size, truncate_dim=DIM,
                        normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False)


def doc_text(name: str, chunk: str) -> str:
    return f"title: {name} | text: {chunk}"

from __future__ import annotations

import os, hashlib, pickle
from typing import List, Tuple, Optional
import numpy as np
from sentence_transformers import SentenceTransformer


class EmbeddingModel:
    """
    Thin wrapper around sentence-transformers with on-disk caching per document hash.
    """
    def __init__(self, model_name: str = "intfloat/e5-base-v2", cache_dir: str = None, device: Optional[str] = None):
        
        # Auto-detect GPU if not specified
        if device is None:
            import torch
            if torch.cuda.is_available():
                device = "cuda"
                print(f"EmbeddingModel: Using GPU ({torch.cuda.get_device_name(0)})")
            else:
                device = "cpu"
                print("EmbeddingModel: CUDA not available, using CPU")
        
        self.model = SentenceTransformer(model_name, trust_remote_code=True, device=device)
        self.cache_dir = cache_dir
        self.device = device

    def _norm(self, X: np.ndarray) -> np.ndarray:
        n = np.linalg.norm(X, axis=1, keepdims=True) + 1e-12
        return X / n

    def encode(self, texts: List[str], batch_size: int = None) -> np.ndarray:
        # Use larger batch size on GPU for better throughput
        if batch_size is None:
            batch_size = 64 if self.device == "cuda" else 8
        
        emb = self.model.encode(
            texts, 
            normalize_embeddings=False, 
            show_progress_bar=False,
            batch_size=batch_size
        )
        if isinstance(emb, list):
            emb = np.array(emb)
        return self._norm(emb)

    def cache_key(self, doc_hash: str, key: str) -> str:
        h = hashlib.sha256((doc_hash + "::" + key).encode()).hexdigest()
        return h

    def save_cached(self, doc_hash: str, key: str, arr: np.ndarray) -> None:
        if not self.cache_dir:
            return
        os.makedirs(self.cache_dir, exist_ok=True)
        path = os.path.join(self.cache_dir, self.cache_key(doc_hash, key) + ".npy")
        np.save(path, arr)

    def load_cached(self, doc_hash: str, key: str) -> Tuple[bool, np.ndarray]:
        if not self.cache_dir:
            return False, None  # type: ignore
        path = os.path.join(self.cache_dir, self.cache_key(doc_hash, key) + ".npy")
        if not os.path.exists(path):
            return False, None  # type: ignore
        return True, np.load(path)
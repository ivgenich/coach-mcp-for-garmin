"""Local embedding model for history search: int8 bge-small-en-v1.5 on onnxruntime.

History search turns each stored day and week into a short fixed-template text
(history_docs.py) and embeds it here, on the server, so health text never goes
to a third party. The model is BAAI's bge-small-en-v1.5 (MIT, 384 dimensions)
in Xenova's int8 ONNX export (34 MB), pinned by Hugging Face revision and by
the SHA-256 of every file:

- downloaded once into MODEL_ROOT (/data/models, the Railway volume), never
  baked into the image;
- every file is checked against its SHA-256 on each load, and a file that
  does not match is deleted and fetched again;
- a failed download (network, size, hash) is written to a marker file and no
  new download is tried for 24 hours, so a broken network never turns every
  search into a download attempt. Without the model only the vector channel is
  off: the keyword and numbers channels still answer.

OnnxEmbeddings is a LangChain `Embeddings` subclass (embed_documents,
embed_query), so it plugs into PGVectorStore and InMemoryVectorStore like any
hosted model. OnnxCrossEncoder is a LangChain `BaseCrossEncoder` over a local
ms-marco MiniLM cross-encoder for the optional reranker
(history_retrieval.RERANK), with the same download rules.

Standard library at import time: onnxruntime, numpy, tokenizers and
langchain-core are imported only when a model is loaded or a class is built
(config/requirements-history.txt; Python 3.10 without them can import this
module). Logs carry fixed texts only, never a document or a query.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import os
import threading
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

log = logging.getLogger("garmin_ext.history")

MODEL_ROOT = "/data/models"
DIMENSIONS = 384
BACKOFF_S = 24 * 3600
DOWNLOAD_TIMEOUT_S = 60
MARKER = "download-failed"
# bge-v1.5 recommends this instruction before a short search query (not before documents).
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
MAX_TOKENS = 512
BATCH = 32
MAX_QUERY_CHARS = 512


@dataclasses.dataclass(frozen=True)
class ModelFile:
    remote: str  # path inside the Hugging Face repo at the pinned revision
    local: str  # file name in the model folder
    sha256: str
    size: int


@dataclasses.dataclass(frozen=True)
class ModelSpec:
    name: str
    repo: str
    revision: str
    files: Tuple[ModelFile, ...]

    @property
    def folder(self) -> str:
        return f"{self.name}-{self.revision[:12]}"

    def url(self, f: ModelFile) -> str:
        return f"https://huggingface.co/{self.repo}/resolve/{self.revision}/{f.remote}"


BGE_SMALL = ModelSpec(
    name="bge-small-en-v1.5-int8",
    repo="Xenova/bge-small-en-v1.5",
    revision="ea104dacec62c0de699686887e3f920caeb4f3e3",
    files=(
        ModelFile("onnx/model_quantized.onnx", "model.onnx",
                  "6c9c6101a956d62dfb5e7190c538226c0c5bb9cb27b651234b6df063ee7dbfe4", 34014426),
        ModelFile("tokenizer.json", "tokenizer.json",
                  "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66", 711396),
    ),
)

MINILM_RERANKER = ModelSpec(
    name="ms-marco-MiniLM-L-6-v2-int8",
    repo="Xenova/ms-marco-MiniLM-L-6-v2",
    revision="a09144355adeed5f58c8ed011d209bf8ee5a1fec",
    files=(
        ModelFile("onnx/model_quantized.onnx", "model.onnx",
                  "e9d8ebf845c413e981c175bfe49a3bfa9b3dcce2a3ba54875ee5df5a58639fbe", 23143499),
        ModelFile("tokenizer.json", "tokenizer.json",
                  "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66", 711396),
    ),
)


def default_root() -> str:
    """MODEL_ROOT when /data exists (the Railway volume); else a folder in the temp directory (a service
    without a volume downloads the 34 MB again after each deploy)."""
    if os.path.isdir(os.path.dirname(MODEL_ROOT)):
        return MODEL_ROOT
    import tempfile

    return os.path.join(tempfile.gettempdir(), "coach-mcp-models")


def forget_caches() -> str:
    """HISTORY_FORGET=yes: this module keeps no derived data (no embedding cache on disk; vectors live in
    Postgres). The model files are public and stay. Drops the in-process failure memory."""
    _FAILED_AT.clear()
    return "no embedding caches on disk (model files kept: public, no personal data)"


_FAILED_AT: Dict[str, float] = {}  # in-process back-off, for when the marker file cannot be written


class ModelUnavailable(RuntimeError):
    """The model files are missing and cannot be fetched now. `group` is a fixed name:
    backoff (a download failed less than 24 h ago), download_failed, hash_mismatch, runtime_missing."""

    def __init__(self, group: str):
        super().__init__(group)
        self.group = group


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _default_fetch(url: str, dest: str, max_bytes: int) -> None:
    """Download url into dest (stdlib only, https, a size cap)."""
    if not url.startswith("https://"):
        raise ValueError("only https downloads")
    req = urllib.request.Request(url, headers={"User-Agent": "coach-mcp-history"})
    with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_S) as resp, open(dest, "wb") as out:
        got = 0
        for block in iter(lambda: resp.read(1 << 20), b""):
            got += len(block)
            if got > max_bytes:
                raise ValueError("download larger than expected")
            out.write(block)


def ensure_model(spec: ModelSpec, root: str = MODEL_ROOT, *, now: Callable[[], float] = time.time,
                 fetch: Callable[[str, str, int], None] = _default_fetch) -> Dict[str, str]:
    """The verified local paths of spec's files, {local name: path}; downloads what is missing.

    Every file is hashed on every call (the int8 model takes about 0.1 s). A file whose hash
    differs is deleted and fetched again. Raises ModelUnavailable when a file is missing and a
    download is not allowed (24 h back-off) or fails; the failure starts a new back-off."""
    folder = os.path.join(root, spec.folder)
    marker = os.path.join(folder, MARKER)
    paths: Dict[str, str] = {}
    todo: List[ModelFile] = []
    for f in spec.files:
        path = os.path.join(folder, f.local)
        paths[f.local] = path
        if os.path.isfile(path):
            if sha256_file(path) == f.sha256:
                continue
            log.warning("history model: a stored model file failed its SHA-256 check; fetching it again")
            os.remove(path)
        todo.append(f)
    if not todo:
        return paths
    try:
        with open(marker, encoding="ascii") as m:
            failed_at = float(m.read().strip() or "0")
    except (OSError, ValueError):
        failed_at = None
    mem = _FAILED_AT.get(folder)
    if mem is not None and (failed_at is None or mem > failed_at):
        failed_at = mem
    if failed_at is not None and now() - failed_at < BACKOFF_S:
        raise ModelUnavailable("backoff")
    group = None
    try:
        os.makedirs(folder, mode=0o700, exist_ok=True)
    except OSError:
        todo, group = [], "download_failed"
    for f in todo:
        tmp = paths[f.local] + ".part"
        try:
            fetch(spec.url(f), tmp, f.size + (1 << 20))
            if sha256_file(tmp) != f.sha256:
                group = "hash_mismatch"
                break
            os.replace(tmp, paths[f.local])
        except Exception:  # noqa: BLE001 - any download failure means: no model for 24 h
            group = "download_failed"
            break
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    if group is not None:
        _FAILED_AT[folder] = now()
        try:
            with open(marker, "w", encoding="ascii") as m:
                m.write(str(now()))
        except OSError:
            pass
        log.warning("history model: download of the pinned model failed (%s); next try in 24 h", group)
        raise ModelUnavailable(group)
    _FAILED_AT.pop(folder, None)
    try:
        os.remove(marker)
    except OSError:
        pass
    log.info("history model: %s downloaded and verified", spec.name)
    return paths


class _OnnxModel:
    """A tokenizer + an onnxruntime session, loaded lazily under a lock. Thread-safe after load."""

    def __init__(self, spec: ModelSpec, root: str, fetch: Optional[Callable] = None,
                 now: Callable[[], float] = time.time):
        self.spec, self.root, self.fetch, self.now = spec, root, fetch, now
        self._lock = threading.Lock()
        self._session = None
        self._tokenizer = None

    def load(self) -> Tuple[Any, Any]:
        with self._lock:
            if self._session is None:
                kwargs = {"now": self.now}
                if self.fetch is not None:
                    kwargs["fetch"] = self.fetch
                paths = ensure_model(self.spec, self.root, **kwargs)
                try:
                    import onnxruntime
                    import tokenizers
                except ImportError as exc:
                    raise ModelUnavailable("runtime_missing") from exc
                tok = tokenizers.Tokenizer.from_file(paths["tokenizer.json"])
                tok.enable_truncation(MAX_TOKENS)
                tok.enable_padding(pad_id=0, pad_token="[PAD]")
                opts = onnxruntime.SessionOptions()
                opts.intra_op_num_threads = 1
                opts.inter_op_num_threads = 1
                opts.log_severity_level = 3
                self._session = onnxruntime.InferenceSession(paths["model.onnx"], sess_options=opts,
                                                             providers=["CPUExecutionProvider"])
                self._tokenizer = tok
            return self._session, self._tokenizer

    def run(self, encodings: Sequence[Any]) -> Any:
        import numpy as np

        session, _ = self.load()
        ids = np.array([e.ids for e in encodings], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
        types = np.array([e.type_ids for e in encodings], dtype=np.int64)
        return session.run(None, {"input_ids": ids, "attention_mask": mask, "token_type_ids": types})[0]


def _embeddings_base():
    from langchain_core.embeddings import Embeddings

    return Embeddings


def _cross_encoder_base():
    from langchain_core.cross_encoders import BaseCrossEncoder

    return BaseCrossEncoder


_CLASSES: Dict[str, type] = {}


def _class(name: str) -> type:
    """Build the LangChain subclasses on first use (langchain-core is optional at import time)."""
    if name in _CLASSES:
        return _CLASSES[name]

    if name == "embeddings":
        class OnnxEmbeddings(_embeddings_base()):
            """LangChain Embeddings over the local int8 bge-small model (CLS pooling, L2-normalised)."""

            def __init__(self, root: str = MODEL_ROOT, spec: ModelSpec = BGE_SMALL, fetch: Optional[Callable] = None,
                         now: Callable[[], float] = time.time):
                self._model = _OnnxModel(spec, root, fetch, now)

            def load(self) -> None:
                self._model.load()

            def _embed(self, texts: List[str]) -> List[List[float]]:
                import numpy as np

                _, tok = self._model.load()
                out: List[List[float]] = []
                for i in range(0, len(texts), BATCH):
                    hidden = self._model.run(tok.encode_batch(list(texts[i:i + BATCH])))
                    cls = hidden[:, 0, :]
                    norm = np.linalg.norm(cls, axis=1, keepdims=True)
                    norm[norm == 0] = 1.0
                    out.extend((cls / norm).astype(float).tolist())
                return out

            def embed_documents(self, texts: List[str]) -> List[List[float]]:
                return self._embed(list(texts)) if texts else []

            def embed_query(self, text: str) -> List[float]:
                return self._embed([QUERY_PREFIX + str(text)[:MAX_QUERY_CHARS]])[0]

        _CLASSES[name] = OnnxEmbeddings
    elif name == "cross_encoder":
        class OnnxCrossEncoder(_cross_encoder_base()):
            """LangChain BaseCrossEncoder over the local int8 ms-marco MiniLM model (higher = more relevant)."""

            def __init__(self, root: str = MODEL_ROOT, spec: ModelSpec = MINILM_RERANKER,
                         fetch: Optional[Callable] = None, now: Callable[[], float] = time.time):
                self._model = _OnnxModel(spec, root, fetch, now)

            def score(self, text_pairs: List[Tuple[str, str]]) -> List[float]:
                _, tok = self._model.load()
                scores: List[float] = []
                pairs = [(str(q)[:MAX_QUERY_CHARS], str(d)) for q, d in text_pairs]
                for i in range(0, len(pairs), BATCH):
                    logits = self._model.run(tok.encode_batch(pairs[i:i + BATCH]))
                    scores.extend(float(x) for x in logits.reshape(-1))
                return scores

        _CLASSES[name] = OnnxCrossEncoder
    else:
        raise KeyError(name)
    return _CLASSES[name]


def embeddings(root: str = MODEL_ROOT, **kwargs: Any) -> Any:
    """A new OnnxEmbeddings (LangChain Embeddings). The model loads on first use."""
    return _class("embeddings")(root, **kwargs)


def cross_encoder(root: str = MODEL_ROOT, **kwargs: Any) -> Any:
    """A new OnnxCrossEncoder (LangChain BaseCrossEncoder). The model loads on first use."""
    return _class("cross_encoder")(root, **kwargs)


def __getattr__(name: str) -> Any:  # history_embed.OnnxEmbeddings / OnnxCrossEncoder, built lazily
    if name == "OnnxEmbeddings":
        return _class("embeddings")
    if name == "OnnxCrossEncoder":
        return _class("cross_encoder")
    raise AttributeError(name)


class HashEmbeddingsMixin:
    """Deterministic bag-of-words hashing vectors for tests and offline evals without the model.

    Not a semantic model: words that share no letters never meet. Use it to test plumbing,
    never to measure retrieval quality."""

    dims = DIMENSIONS

    def _vec(self, text: str) -> List[float]:
        import re

        v = [0.0] * self.dims
        for w in re.findall(r"[a-z0-9]+", str(text).lower()):
            h = int.from_bytes(hashlib.sha256(w.encode()).digest()[:4], "big")
            v[h % self.dims] += 1.0 if h & 1 else -1.0
        if not any(v):
            v[0] = 1.0  # no word: a fixed unit vector (a zero vector makes cosine NaN)
        n = sum(x * x for x in v) ** 0.5
        return [x / n for x in v]


def hash_embeddings() -> Any:
    """A LangChain Embeddings that hashes words (tests and offline plumbing only)."""
    base = _embeddings_base()

    class HashEmbeddings(HashEmbeddingsMixin, base):
        def embed_documents(self, texts: List[str]) -> List[List[float]]:
            return [self._vec(t) for t in texts]

        def embed_query(self, text: str) -> List[float]:
            return self._vec(text)

    return HashEmbeddings()

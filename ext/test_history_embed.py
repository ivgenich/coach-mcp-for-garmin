"""Tests for history_embed: the pinned model download (hash check, 24 h back-off), and the LangChain
Embeddings / BaseCrossEncoder classes over a fake ONNX session.

    cd ext && python3 -m unittest test_history_embed -v

The download tests are standard library only. The class tests need the history packages
(config/requirements-history.txt) and skip without them; they never download the real model.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import tempfile
import unittest
from unittest import mock

import history_embed as he

HAVE_LC = importlib.util.find_spec("langchain_core") is not None and importlib.util.find_spec("numpy") is not None
LC_SKIP = "needs the history packages (config/requirements-history.txt)"

BODY = b"model bytes"
TOK = b"{}"
SPEC = he.ModelSpec("test-model", "org/model", "0123456789abcdef0123", (
    he.ModelFile("onnx/model_quantized.onnx", "model.onnx", hashlib.sha256(BODY).hexdigest(), len(BODY)),
    he.ModelFile("tokenizer.json", "tokenizer.json", hashlib.sha256(TOK).hexdigest(), len(TOK)),
))


class Fetch:
    def __init__(self, bodies=None, fail=False):
        self.calls = []
        self.bodies = bodies or {"onnx/model_quantized.onnx": BODY, "tokenizer.json": TOK}
        self.fail = fail

    def __call__(self, url, dest, max_bytes):
        self.calls.append(url)
        if self.fail:
            raise OSError("network down")
        name = url.split("/resolve/0123456789abcdef0123/")[1]
        with open(dest, "wb") as f:
            f.write(self.bodies[name])


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class EnsureModel(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        he._FAILED_AT.clear()
        self.addCleanup(he._FAILED_AT.clear)

    def test_pinned_constants(self):
        for spec in (he.BGE_SMALL, he.MINILM_RERANKER):
            self.assertRegex(spec.revision, r"^[0-9a-f]{40}$")
            for f in spec.files:
                self.assertRegex(f.sha256, r"^[0-9a-f]{64}$")
                self.assertTrue(spec.url(f).startswith(f"https://huggingface.co/{spec.repo}/resolve/{spec.revision}/"))
        self.assertEqual(he.DIMENSIONS, 384)

    def test_downloads_verifies_and_reuses(self):
        fetch = Fetch()
        paths = he.ensure_model(SPEC, self.root, fetch=fetch, now=Clock())
        self.assertEqual(len(fetch.calls), 2)
        with open(paths["model.onnx"], "rb") as f:
            self.assertEqual(f.read(), BODY)
        he.ensure_model(SPEC, self.root, fetch=fetch, now=Clock())
        self.assertEqual(len(fetch.calls), 2, "verified files are not fetched again")
        self.assertFalse(any(n.endswith(".part") for n in os.listdir(os.path.dirname(paths["model.onnx"]))))

    def test_hash_mismatch_fails_and_backs_off_24h(self):
        clock = Clock()
        bad = Fetch({"onnx/model_quantized.onnx": b"evil", "tokenizer.json": TOK})
        with self.assertLogs("garmin_ext.history", "WARNING") as logs:
            with self.assertRaises(he.ModelUnavailable) as cm:
                he.ensure_model(SPEC, self.root, fetch=bad, now=clock)
        self.assertEqual(cm.exception.group, "hash_mismatch")
        self.assertNotIn("evil", "\n".join(logs.output))
        folder = os.path.join(self.root, SPEC.folder)
        self.assertFalse(os.path.exists(os.path.join(folder, "model.onnx")), "a bad file is never kept")
        good = Fetch()
        clock.t += 23 * 3600
        with self.assertRaises(he.ModelUnavailable) as cm:
            he.ensure_model(SPEC, self.root, fetch=good, now=clock)
        self.assertEqual(cm.exception.group, "backoff")
        self.assertEqual(good.calls, [], "no download inside the back-off")
        clock.t += 2 * 3600
        he.ensure_model(SPEC, self.root, fetch=good, now=clock)
        self.assertFalse(os.path.exists(os.path.join(folder, he.MARKER)), "success clears the marker")

    def test_network_failure_backs_off_even_without_a_marker_file(self):
        clock = Clock()
        with mock.patch("builtins.open", side_effect=self._open_no_marker()):
            with self.assertRaises(he.ModelUnavailable) as cm:
                he.ensure_model(SPEC, self.root, fetch=Fetch(fail=True), now=clock)
        self.assertEqual(cm.exception.group, "download_failed")
        fetch = Fetch()
        with mock.patch("builtins.open", side_effect=self._open_no_marker()):
            with self.assertRaises(he.ModelUnavailable):
                he.ensure_model(SPEC, self.root, fetch=fetch, now=clock)
        self.assertEqual(fetch.calls, [])

    @staticmethod
    def _open_no_marker():
        real = open

        def fake(path, *a, **kw):
            if str(path).endswith(he.MARKER):
                raise OSError("read-only")
            return real(path, *a, **kw)
        return fake

    def test_tampered_local_file_is_fetched_again(self):
        fetch = Fetch()
        paths = he.ensure_model(SPEC, self.root, fetch=fetch, now=Clock())
        with open(paths["model.onnx"], "wb") as f:
            f.write(b"tampered")
        he.ensure_model(SPEC, self.root, fetch=fetch, now=Clock())
        self.assertEqual(len(fetch.calls), 3)
        with open(paths["model.onnx"], "rb") as f:
            self.assertEqual(f.read(), BODY)

    def test_default_fetch_refuses_http_and_oversize(self):
        with self.assertRaises(ValueError):
            he._default_fetch("http://example.invalid/x", os.path.join(self.root, "x"), 10)

    def test_default_root_and_forget(self):
        self.assertTrue(he.default_root())
        he._FAILED_AT["x"] = 1.0
        self.assertIn("no embedding caches", he.forget_caches())
        self.assertEqual(he._FAILED_AT, {})


class _Enc:
    def __init__(self, n):
        self.ids = [101] + [7] * n + [102]
        self.attention_mask = [1] * len(self.ids)
        self.type_ids = [0] * len(self.ids)


class _Tok:
    def encode_batch(self, texts):
        width = max(len(str(t)) for t in texts) % 5 + 3
        return [_Enc(width) for _ in texts]


@unittest.skipUnless(HAVE_LC, LC_SKIP)
class Classes(unittest.TestCase):
    def _fake_model(self, obj, output):
        import numpy as np

        session = mock.Mock()
        session.run.side_effect = lambda _names, feeds: [output(np, feeds["input_ids"].shape)]
        obj._model.load = lambda: (session, _Tok())
        return session

    def test_embeddings_cls_pooling_normalised_and_query_prefix(self):
        from langchain_core.embeddings import Embeddings

        emb = he.embeddings("/nonexistent")
        self.assertIsInstance(emb, Embeddings)
        self._fake_model(emb, lambda np, shape: np.full((shape[0], shape[1], 384), 3.0))
        vecs = emb.embed_documents(["a", "b"])
        self.assertEqual(len(vecs), 2)
        self.assertAlmostEqual(sum(x * x for x in vecs[0]), 1.0, places=5)
        import numpy as np

        tok = mock.Mock()
        tok.encode_batch.side_effect = lambda texts: [_Enc(2) for _ in texts]
        emb._model.load = lambda: (None, tok)
        emb._model.run = lambda encs: np.ones((len(encs), 4, 384))
        emb.embed_query("heavy bike")
        self.assertTrue(tok.encode_batch.call_args[0][0][0].startswith(he.QUERY_PREFIX))
        self.assertEqual(emb.embed_documents([]), [])

    def test_missing_model_raises_model_unavailable(self):
        root = tempfile.mkdtemp()
        emb = he.embeddings(root, fetch=Fetch(fail=True))
        with self.assertRaises(he.ModelUnavailable):
            emb.embed_query("x")

    def test_cross_encoder_scores(self):
        from langchain_core.cross_encoders import BaseCrossEncoder

        ce = he.cross_encoder("/nonexistent")
        self.assertIsInstance(ce, BaseCrossEncoder)
        self._fake_model(ce, lambda np, shape: np.arange(shape[0], dtype=float).reshape(shape[0], 1))
        self.assertEqual(ce.score([("q", "a"), ("q", "b")]), [0.0, 1.0])

    def test_hash_embeddings(self):
        emb = he.hash_embeddings()
        self.assertEqual(emb.embed_query("bike heavy"), emb.embed_documents(["bike heavy"])[0])
        self.assertEqual(len(emb.embed_query("x")), 384)


if __name__ == "__main__":
    unittest.main()

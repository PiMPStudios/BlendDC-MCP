"""
Lightweight TF-IDF document store over Blender's bpy API docstrings.

Uses only Python stdlib (math, re, collections, pickle) — zero extra deps.
Corpus = bpy.ops + bpy.types docstrings extracted at first use, then cached
to rag_cache.pkl alongside the addon for instant subsequent loads.
"""

import bpy
import math
import os
import pickle
import re
import time
from collections import Counter
from typing import Dict, List, Optional, Tuple

_ADDON_DIR = os.path.dirname(__file__)
_CACHE_PATH = os.path.join(_ADDON_DIR, "rag_cache.pkl")

_store: Optional["RAGStore"] = None


def _tokenize(text: str) -> List[str]:
    text = text.lower()
    text = re.sub(r"[^a-z0-9_]", " ", text)
    return [t for t in text.split() if len(t) > 2]


class RAGStore:
    """TF-IDF retrieval over Blender API docstrings."""

    def __init__(self) -> None:
        self.docs: List[Dict] = []
        self.tf: List[Counter] = []
        self.idf: Dict[str, float] = {}
        self._built = False

    # ── Build ──────────────────────────────────────────────────────────────

    def build(self) -> None:
        t0 = time.perf_counter()
        raw: List[Tuple[str, str, str]] = []  # (title, body, example)

        # bpy.ops
        for mod_name in dir(bpy.ops):
            if mod_name.startswith("_"):
                continue
            try:
                mod = getattr(bpy.ops, mod_name)
            except Exception:
                continue
            for op_name in dir(mod):
                if op_name.startswith("_"):
                    continue
                try:
                    op  = getattr(mod, op_name)
                    doc = (getattr(op, "__doc__", "") or "").strip()
                    if doc:
                        raw.append((
                            f"bpy.ops.{mod_name}.{op_name}",
                            doc,
                            f"bpy.ops.{mod_name}.{op_name}()",
                        ))
                except Exception:
                    pass

        # bpy.types
        for type_name in dir(bpy.types):
            if type_name.startswith("_"):
                continue
            try:
                t   = getattr(bpy.types, type_name)
                doc = (getattr(t, "__doc__", "") or "").strip()
                if not doc:
                    continue
                # Enrich body with property names for better recall
                extra: List[str] = []
                if hasattr(t, "bl_rna"):
                    try:
                        for p in t.bl_rna.properties:
                            if p.identifier != "rna_type":
                                extra.append(p.identifier)
                                if len(extra) >= 6:
                                    break
                    except Exception:
                        pass
                body = doc + (" Properties: " + " ".join(extra) if extra else "")
                raw.append((
                    f"bpy.types.{type_name}",
                    body,
                    f"bpy.types.{type_name}",
                ))
            except Exception:
                pass

        self.docs = [
            {"id": i, "title": title, "summary": body[:300], "example": ex}
            for i, (title, body, ex) in enumerate(raw)
        ]

        # TF per document
        self.tf = [
            Counter(_tokenize(d["title"] + " " + d["summary"]))
            for d in self.docs
        ]

        # IDF over full corpus
        N = len(self.docs)
        df: Counter = Counter()
        for counts in self.tf:
            for term in counts:
                df[term] += 1
        self.idf = {
            term: math.log((N + 1) / (freq + 1)) + 1.0
            for term, freq in df.items()
        }

        self._built = True
        elapsed = time.perf_counter() - t0
        print(
            f"[BlenderMCP] RAG store: {len(self.docs)} docs indexed in {elapsed:.1f}s",
            flush=True,
        )

    # ── Persistence ────────────────────────────────────────────────────────

    def save(self) -> None:
        try:
            with open(_CACHE_PATH, "wb") as f:
                pickle.dump(self, f, protocol=4)
        except Exception as exc:
            print(f"[BlenderMCP] RAG cache save failed: {exc}", flush=True)

    @classmethod
    def load(cls) -> Optional["RAGStore"]:
        if not os.path.exists(_CACHE_PATH):
            return None
        try:
            with open(_CACHE_PATH, "rb") as f:
                store = pickle.load(f)
            if isinstance(store, cls) and store._built:
                return store
        except Exception:
            pass
        return None

    # ── Query ──────────────────────────────────────────────────────────────

    def query(self, text: str, top_k: int = 3) -> List[Dict]:
        if not self._built:
            return []
        tokens = _tokenize(text)
        if not tokens:
            return []

        scores: List[Tuple[float, int]] = []
        for i, tf_counts in enumerate(self.tf):
            total = sum(tf_counts.values()) or 1
            score = sum(
                (tf_counts[t] / total) * self.idf.get(t, 1.0)
                for t in tokens
                if t in tf_counts
            )
            if score > 0:
                scores.append((score, i))

        scores.sort(reverse=True)
        return [
            {
                "title":     self.docs[i]["title"],
                "relevance": round(s, 4),
                "summary":   self.docs[i]["summary"],
                "example":   self.docs[i]["example"],
            }
            for s, i in scores[:top_k]
        ]


def get_store() -> RAGStore:
    """Return the module-level RAG store, building or loading from cache as needed."""
    global _store
    if _store is not None and _store._built:
        return _store
    _store = RAGStore.load()
    if _store is None:
        _store = RAGStore()
        _store.build()
        _store.save()
    return _store

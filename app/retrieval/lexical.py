"""Small BM25 index used to complement dense retrieval.

Dense embeddings miss some exact-term questions in this dataset (e.g. "What's
your phone number?" vs. the Contact Us page, or platform names like
"BigCommerce"). The BM25 score is normalised by the best score the query could
achieve (every query term matched), giving a 0..1 "weighted term coverage".
"""
from __future__ import annotations

import math
import re
from collections import Counter

from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS

_TOKEN = re.compile(r"[a-z0-9][a-z0-9+#.-]*[a-z0-9+#]|[a-z0-9]")
# Words that are on nearly every page / in nearly every question about D Group.
_EXTRA_STOP = {"d", "group", "dgroup", "tell", "know", "want", "need", "like", "does", "do", "provide",
               "offer", "please", "hi", "hello", "thanks", "thank"}
_STOP = set(ENGLISH_STOP_WORDS) | _EXTRA_STOP


def tokenize(text: str) -> list[str]:
    tokens = []
    for tok in _TOKEN.findall(text.lower()):
        if tok in _STOP:
            continue
        if len(tok) > 4 and tok.endswith("s") and not tok.endswith("ss"):
            tok = tok[:-1]  # crude plural folding: emails -> email
        tokens.append(tok)
    return tokens


class BM25Index:
    def __init__(self, ids: list[str], texts: list[str], k1: float = 1.2, b: float = 0.75) -> None:
        self.ids = ids
        self.k1, self.b = k1, b
        self._tfs = [Counter(tokenize(t)) for t in texts]
        self._lens = [sum(tf.values()) for tf in self._tfs]
        self._avg = (sum(self._lens) / len(self._lens)) if self._lens else 1.0
        df: Counter[str] = Counter()
        for tf in self._tfs:
            df.update(tf.keys())
        n = len(texts)
        self._idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    def scores(self, query: str) -> dict[str, float]:
        """Return normalised (0..1) scores for chunks matching at least one term."""
        terms = list(dict.fromkeys(tokenize(query)))
        terms = [t for t in terms if t in self._idf]
        if not terms:
            return {}
        max_possible = sum(self._idf[t] * (self.k1 + 1) for t in terms)
        # Unknown query terms still count against coverage (off-topic words).
        unknown = len(set(tokenize(query))) - len(terms)
        max_possible += unknown * max(self._idf.values(), default=1.0) * (self.k1 + 1)
        out: dict[str, float] = {}
        for i, tf in enumerate(self._tfs):
            s = 0.0
            for t in terms:
                f = tf.get(t)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * self._lens[i] / self._avg)
                s += self._idf[t] * f * (self.k1 + 1) / denom
            if s > 0:
                out[self.ids[i]] = s / max_possible
        return out

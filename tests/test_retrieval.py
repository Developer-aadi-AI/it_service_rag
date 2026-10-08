"""Retrieval layer, tested independently of the LLM."""
from __future__ import annotations

import numpy as np
import pytest

from app.embeddings.base import HashingEmbedder
from app.evaluation import evaluate_retrieval
from app.ingestion.chunker import chunk_documents
from app.ingestion.loader import load_documents
from app.ingestion.pipeline import ensure_index
from app.retrieval.lexical import BM25Index, tokenize
from app.retrieval.retriever import Retriever
from app.vectorstore.base import NumpyVectorStore


@pytest.fixture
def tiny_retriever(synthetic_records):
    docs = load_documents(records=synthetic_records)
    chunks = chunk_documents(docs, chunk_size=300, chunk_overlap=60)
    emb = HashingEmbedder()
    store = NumpyVectorStore()
    store.add(chunks, emb.embed_documents([c.embed_text for c in chunks]))
    return Retriever(emb, store, top_k=3, similarity_threshold=0.1, candidate_k=10, max_chunks_per_page=1)


def test_numpy_store_persistence(tmp_path, synthetic_records):
    docs = load_documents(records=synthetic_records)
    chunks = chunk_documents(docs)
    emb = HashingEmbedder()
    vecs = emb.embed_documents([c.embed_text for c in chunks])
    store = NumpyVectorStore(tmp_path)
    store.add(chunks, vecs)
    reloaded = NumpyVectorStore(tmp_path)
    assert reloaded.count() == len(chunks)
    hits = reloaded.query(vecs[0], 1)
    assert hits[0].chunk_id == chunks[0].chunk_id and hits[0].score == pytest.approx(1.0, abs=1e-5)
    assert set(reloaded.get_embeddings([chunks[0].chunk_id])) == {chunks[0].chunk_id}
    reloaded.reset()
    assert reloaded.count() == 0


def test_top_k_threshold_and_diversity(tiny_retriever):
    res = tiny_retriever.retrieve("Gold Plan price", top_k=2)
    assert res.hits and res.hits[0].metadata["page_id"] == "p2"
    assert len(res.hits) <= 2
    pages = [h.metadata["page_id"] for h in res.hits]
    assert len(pages) == len(set(pages)), "max_chunks_per_page=1 respected"
    strict = tiny_retriever.retrieve("Gold Plan price", similarity_threshold=0.99)
    assert strict.hits == [] and strict.top_score > 0
    assert all(a.score >= b.score for a, b in zip(res.candidates, res.candidates[1:]))


def test_empty_query(tiny_retriever):
    assert tiny_retriever.retrieve("   ").hits == []


def test_bm25_normalised_scores():
    idx = BM25Index(["a", "b"], ["phone email contact office", "shopify store design"])
    s = idx.scores("what is your phone number")
    assert set(s) == {"a"} and 0 < s["a"] <= 1
    assert idx.scores("zzzz qqqq") == {}
    assert "email" in tokenize("Emails?")


def test_index_reused_when_unchanged(tiny_settings):
    emb, store = HashingEmbedder(), NumpyVectorStore(tiny_settings.storage_dir / "numpy_index")
    first = ensure_index(tiny_settings, emb, store)
    second = ensure_index(tiny_settings, emb, store)
    assert first.rebuilt and not second.rebuilt and second.chunks == first.chunks
    forced = ensure_index(tiny_settings, emb, store, force=True)
    assert forced.rebuilt and store.count() == first.chunks


# --- real data + real embeddings -----------------------------------------------------------

def test_retrieval_quality_on_eval_set(real_bundle, eval_set):
    report = evaluate_retrieval(real_bundle.pipeline.retriever, eval_set["answerable"], k=5)
    assert report["hit@5"] >= 0.9, report["failures"]
    assert report["mrr"] >= 0.8


@pytest.mark.parametrize("question,page", [
    ("What's your phone number?", "db-96"),
    ("How many support hours are included annually?", "db-425"),
    ("What is the price of the BigCommerce Gold plan?", "db-2351"),
    ("What is ReviewCaddy?", "db-2973"),
])
def test_specific_questions_rank_expected_page_first_page(real_bundle, question, page):
    pages = []
    for h in real_bundle.pipeline.retriever.search(question, 20):
        if h.metadata["page_id"] not in pages:
            pages.append(h.metadata["page_id"])
    assert page in pages[:3], pages[:5]


def test_off_topic_scores_below_domain_threshold(real_bundle, real_settings, eval_set):
    r = real_bundle.pipeline.retriever
    scores = [r.retrieve(q).top_score for q in eval_set["off_topic"]]
    assert max(scores) < real_settings.domain_threshold, scores


def test_hits_have_citation_metadata(real_bundle):
    res = real_bundle.pipeline.retriever.retrieve("Shopify packages")
    assert res.hits
    for h in res.hits:
        assert h.metadata["url"].startswith("http") and h.metadata["title"]
        assert np.isfinite(h.score)

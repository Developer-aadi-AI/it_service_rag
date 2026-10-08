"""Ingestion: loading, validation, cleaning, metadata, FAQ extraction, chunking."""
from __future__ import annotations

import json

import pytest

from app.ingestion.chunker import chunk_documents, chunk_text
from app.ingestion.loader import DataValidationError, load_documents, load_raw, split_sentences


# --- synthetic data (fast) ------------------------------------------------------

def test_validation_and_cleaning(synthetic_records):
    docs = load_documents(records=synthetic_records)
    ids = [d.page_id for d in docs]
    assert "bad" not in ids, "records with missing fields are skipped"
    assert "p5" not in ids, "access-denied pages are dropped"
    assert ids.count("p1") == 1 and "p1~2" in ids, "duplicate page ids are made unique"

    by_id = {d.page_id: d for d in docs}
    assert by_id["p3"].source_type == "contact", "UNKNOWN type re-derived from URL"
    assert by_id["p3"].original_source_type == "UNKNOWN"
    acme = by_id["p4"]
    assert acme.content_quality == "title_only"
    assert acme.platform == "Shopify"  # "Shopify CMS" normalised
    assert "portfolio" in acme.text and "Acme Shoes" in acme.text
    assert by_id["p2"].category == "Pricing"


def test_faq_extraction_synthetic(synthetic_records):
    doc = next(d for d in load_documents(records=synthetic_records) if d.page_id == "p1")
    assert [f.question for f in doc.faqs] == ["Can you set up my Shopify store?",
                                             "Do you offer support after launch?"]
    assert doc.faqs[0].answer.startswith("Yes. We set up stores")
    assert "Your Questions, Answered" not in doc.text  # header replaced


def test_load_raw_errors(tmp_path):
    with pytest.raises(DataValidationError):
        load_raw(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(DataValidationError):
        load_raw(bad)
    obj = tmp_path / "obj.json"
    obj.write_text(json.dumps({"a": 1}), encoding="utf-8")
    with pytest.raises(DataValidationError):
        load_raw(obj)
    with pytest.raises(DataValidationError):
        load_documents(records=[{"page_id": "x"}])


def test_chunk_text_size_and_overlap():
    text = " ".join(f"Sentence number {i} talks about Shopify stores." for i in range(60))
    chunks = chunk_text(text, chunk_size=300, chunk_overlap=80)
    assert len(chunks) > 5
    assert all(len(c) <= 300 for c in chunks)
    # consecutive chunks share at least one sentence (overlap)
    assert all(set(split_sentences(a)) & set(split_sentences(b)) for a, b in zip(chunks, chunks[1:]))


def test_chunk_text_long_sentence_is_windowed():
    text = "word " * 1000  # no punctuation, like flattened pricing tables
    chunks = chunk_text(text.strip(), chunk_size=200, chunk_overlap=0)
    assert all(len(c) <= 200 for c in chunks) and len(chunks) >= 20


def test_chunk_metadata_propagation(synthetic_records):
    docs = load_documents(records=synthetic_records)
    chunks = chunk_documents(docs, chunk_size=300, chunk_overlap=60)
    assert len({c.chunk_id for c in chunks}) == len(chunks), "chunk ids unique"
    for c in chunks:
        for key in ("page_id", "url", "title", "source_type", "platform", "category", "content_type"):
            assert key in c.metadata
        assert all(isinstance(v, (str, int, float, bool)) for v in c.metadata.values()), "scalar metadata"
        assert c.text and c.embed_text.endswith(c.text)
    assert any(c.content_type == "faq" for c in chunks)


# --- real supplied dataset ----------------------------------------------------------

@pytest.fixture(scope="module")
def real_docs(real_settings):
    return load_documents(real_settings.data_path)


def test_real_dataset_loads(real_docs):
    assert len(real_docs) == 133  # 134 records, 1 access-denied preview page dropped
    assert all(d.source_type != "UNKNOWN" for d in real_docs)
    assert all(d.text and d.url and d.title for d in real_docs)
    assert sum(d.content_quality == "title_only" for d in real_docs) == 22


def test_real_boilerplate_removed(real_docs):
    assert not any("You gain a team that" in d.text and "Transparent timelines and delivery" in d.text
                   for d in real_docs)
    assert not any("First Name * Last Name" in d.text for d in real_docs)


def test_real_faqs_match_source_text(real_settings, real_docs):
    raw = {r["page_id"]: r["text"] for r in load_raw(real_settings.data_path)}
    support = next(d for d in real_docs if d.page_id == "db-425")
    q = {f.question: f.answer for f in support.faqs}
    assert "How many support hours are included annually?" in q
    assert "120 hours" in q["How many support hours are included annually?"]
    for doc in real_docs:  # every extracted FAQ is verbatim from the page (no invention)
        for f in doc.faqs:
            assert f.question in raw[doc.page_id]
            assert f.answer[:60] in raw[doc.page_id]


def test_real_metadata_fields(real_docs):
    contact = next(d for d in real_docs if d.page_id == "db-96")
    assert contact.source_type == "contact" and contact.category == "Contact & Partnership"
    shopify = next(d for d in real_docs if d.page_id == "db-1829")
    assert "Shopify" in shopify.platforms_mentioned
    assert any(d.keywords for d in real_docs)
    assert any(d.ctas for d in real_docs)

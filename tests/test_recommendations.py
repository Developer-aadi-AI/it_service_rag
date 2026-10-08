"""Primary and related service recommendations built from the supplied pages."""
from __future__ import annotations

import pytest

from app.recommendations.engine import RecommendationEngine, ServiceCatalog, recommendation_sentence


@pytest.fixture(scope="module")
def engine(real_bundle, real_settings):
    catalog = ServiceCatalog.from_store(real_bundle.store)
    return RecommendationEngine(catalog, real_bundle.pipeline.retriever.embedder,
                                real_settings.recommendation_min_similarity, 2)


def test_catalog_only_contains_supplied_service_pages(engine, real_settings):
    from app.ingestion.loader import load_raw

    urls = {r["url"] for r in load_raw(real_settings.data_path)}
    items = engine.catalog.items
    assert len(items) >= 30
    assert all(i.url in urls for i in items), "every recommendation is a real V Group page"
    assert not any("contact-form" in i.url or i.url.rstrip("/").endswith("/services") for i in items)
    assert {i.kind for i in items} <= {"service", "package", "product", "app"}


@pytest.mark.parametrize("query,primary", [
    ("I want to launch an online store on Shopify", "Shopify Development Services"),
    ("Can I hire a PHP developer?", "Hire PHP Developers"),
    ("I need a mobile app for my business", "Mobile App Development Services"),
    ("What is SMTU?", "SMTU"),
    ("I need a store locator for Magento", "Store Locator - Magento 2 Extension"),
])
def test_primary_recommendation(engine, query, primary):
    rec = engine.recommend(query)
    assert rec is not None and rec.primary.name == primary


def test_answer_source_drives_primary(engine):
    rec = engine.recommend("What is included in support?", answer_page_ids=["db-425"])
    assert rec.primary.page_id == "db-425" and rec.primary.name == "Annual Store Support"


def test_related_are_distinct_and_platform_consistent(engine):
    rec = engine.recommend("I want to launch an online store on Shopify")
    names = [r.name for r in rec.related]
    assert len(names) == 2 and rec.primary.name not in names
    assert all(not r.platforms or "shopify" in r.platforms for r in rec.related), names
    rec = engine.recommend("I need a store locator for Magento")
    families = [rec.primary.family] + [r.family for r in rec.related]
    assert len(families) == len(set(families)), "no Magento 1/Magento 2 duplicates of one extension"


@pytest.mark.parametrize("query", ["What is the weather in Paris today?", "I want a blockchain-based NFT marketplace"])
def test_no_recommendation_for_unrelated(engine, query):
    assert engine.recommend(query) is None


def test_recommendation_sentence(engine):
    s = recommendation_sentence(engine.recommend("I need a mobile app for my business"))
    assert s.startswith("Based on what you've described, our Mobile App Development Services") and s.endswith(".")

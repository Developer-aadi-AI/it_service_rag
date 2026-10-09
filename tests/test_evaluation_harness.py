"""The evaluation harness: scoring helpers, dataset integrity, and a run on a dataset subset."""
from __future__ import annotations

import pytest

from app.evaluation import load_eval_set
from app.evaluation.harness import Harness, check, claims, invalid_markers, supported, to_markdown, ungrounded_numbers, write_reports


def test_claims_drop_templates():
    answer = ("Our Silver Plan begins at $3,999 [1]. Gold is $5,999 [1].\n\n"
              "Based on what you've described, our Shopify Packages looks like a good fit.\n\n"
              "Anything else I can help you with?")
    assert claims(answer) == ["Our Silver Plan begins at $3,999.", "Gold is $5,999."]
    assert claims("I can only help with questions about D Group - for example our web design and development, "
                  "e-commerce work on platforms like Shopify, Magento, BigCommerce, WordPress and Drupal, app "
                  "development, support plans and products. Is there something in those areas I can help you with?") == []


def test_grounding_and_numbers():
    src = "Silver Plan $3,999 Gold Plan $5,999 delivered in 15 business days"
    assert supported("The Gold Plan costs $5,999.", src)
    assert not supported("We offer free hosting for life and a lifetime warranty.", src)
    assert ungrounded_numbers(["Gold is $5,999.", "Silver is $3,999.00."], src) == []
    assert ungrounded_numbers(["Platinum is $9,999."], src) == ["$9,999"]
    assert invalid_markers("A [1] B [3]", {1, 2}) == 1


def test_dataset_facts_exist_in_data(real_settings):
    from app.evaluation.harness import Corpus, normalize

    corpus = Corpus.load(real_settings.data_path)
    data = load_eval_set()
    assert data["version"] == 2
    cats = {it.get("category") for it in data["answerable"]}
    assert {"factual", "service", "multi_chunk"} <= cats
    for key in ("follow_up", "no_answer_in_domain", "hallucination_probes", "off_topic", "ambiguous"):
        assert data[key], key
    for it in data["answerable"] + data["follow_up"]:
        pages = normalize(corpus.pages(it["expected_pages"])).lower()
        assert any(normalize(f).lower() in pages for f in it["expected_facts"]), it
        for f in it.get("expected_facts_all", []):
            assert normalize(f).lower() in pages, (it, f)


def test_harness_run_on_subset(make_services, tmp_path):
    full = load_eval_set()
    subset = {k: (v[:3] if isinstance(v, list) else v) for k, v in full.items()}
    subset["answerable"] = [x for x in full["answerable"] if x.get("category") == "multi_chunk"][:2] + full["answerable"][:3]
    svc = make_services(analytics_enabled=False)
    report = Harness(svc, subset).run()
    for key in ("retrieval", "answers", "follow_up", "no_answer", "hallucination", "off_topic", "ambiguous",
                "latency_ms", "checks", "failures"):
        assert key in report
    assert report["retrieval"]["hit_at_k"] == 1.0
    assert report["answers"]["invalid_citations"] == 0
    md = to_markdown(report)
    assert "| Retrieval" in md and "Threshold checks" in md
    paths = write_reports(report, tmp_path)
    assert paths["latest_md"].exists() and paths["history"].read_text(encoding="utf-8").count("\n") == 2


def test_check_thresholds():
    report = {"retrieval": {"hit_at_k": 0.95, "mrr": 0.8}, "answers": {"answer_rate": 1, "fact_recall": 0.9,
              "citation_accuracy": 0.9, "grounding_rate": 0.95, "invalid_citations": 1},
              "follow_up": {"fact_recall": 0.7}, "no_answer": {"routed_to_team": 0.9},
              "hallucination": {"resistance": 1.0}, "off_topic": {"accuracy": 1.0}, "ambiguous": {"acceptable": 0.9}}
    res = check(report)
    assert res["answers.invalid_citations"]["passed"] is False
    assert all(v["passed"] for k, v in res.items() if k != "answers.invalid_citations")

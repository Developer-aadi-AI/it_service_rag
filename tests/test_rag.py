"""RAG behaviour: grounding, citations, no-answer routing, off-topic, LLM path."""
from __future__ import annotations

import json
import re

import pytest

from app.evaluation import evaluate_pipeline
from app.ingestion.chunker import chunk_documents
from app.ingestion.loader import load_documents
from app.llm.base import LLMError, LLMRefusal
from app.rag.generator import clean_citations, parse_json_object
from app.rag.pipeline import RAGPipeline
from app.rag.prompts import (GROUNDED_SYSTEM_PROMPT, INFORMATION_TEAM_REPLY, OFF_TOPIC_REPLY,
                             PROJECT_IDEA_REPLY)


def _answer_json(answer, citations, status="answered", kind="information_request"):
    return json.dumps({"status": status, "answer": answer, "citations": citations, "request_kind": kind})


def _pipeline_with(real_bundle, real_settings, llm):
    p = real_bundle.pipeline
    return RAGPipeline(real_settings, p.retriever, llm, p.extractive.embedder, p.triage.capabilities)


# --- offline extractive mode on real data ----------------------------------------------

def test_end_to_end_metrics(real_bundle, eval_set):
    m = evaluate_pipeline(real_bundle.pipeline, eval_set)
    assert m["answer_rate"] >= 0.9
    assert m["citation_accuracy"] >= 0.8, m["misses"]
    assert m["no_answer_routed_to_team"] == 1.0
    assert m["off_topic_accuracy"] == 1.0


def test_extractive_answers_are_verbatim_from_cited_pages(real_bundle, real_settings, eval_set):
    """Grounding: every factual sentence must come from a cited page.

    The answer body (before the closing follow-up line) consists of sentences
    copied from the cited pages after cleanup (headings/buttons removed), or of
    plan-price sentences whose price appears verbatim on the cited page.
    """
    from app.ingestion.loader import split_sentences

    docs = {d.page_id: d for d in load_documents(real_settings.data_path)}
    faq_text = {d.page_id: " ".join(f"{f.question} {f.answer}" for f in d.faqs) for d in docs.values()}
    for item in eval_set["answerable"][:16]:
        r = real_bundle.pipeline.answer(item["question"])
        assert r.status == "answered"
        assert r.sources, "answers must carry sources"
        cited = " ".join(docs[s.page_id].text + " " + faq_text[s.page_id] for s in r.sources)
        cited = re.sub(r"\s+([,.;:!?])", r"\1", cited)
        body = re.sub(r"\s*\[\d+\]", "", r.answer.split("\n\n")[0])
        if r.generator == "contact-info":  # composed from parsed fields: check each value
            info = real_bundle.pipeline.contact
            values = [o.address for o in info.offices] + [o.phone for o in info.offices] + [info.email]
            for v in values:
                if v in body:
                    assert v.split(",")[0] in cited
            continue
        for sentence in split_sentences(body):
            price = re.match(r"^Our .+ is priced at (\S+)\.$", sentence)
            if price:
                assert price.group(1) in cited, f"price not on cited page: {sentence!r}"
                continue
            core = sentence.removesuffix("...").rstrip(".")[1:60]  # first char may be re-capitalised
            assert core in cited, f"ungrounded: {sentence[:80]!r}"


def test_citation_numbers_match_sources(real_bundle):
    r = real_bundle.pipeline.answer("What is included in D Group's Annual Support?")
    numbers = {s.number for s in r.sources}
    import re
    markers = {int(n) for n in re.findall(r"\[(\d+)\]", r.answer)}
    assert markers and markers <= numbers
    assert r.sources[0].url.startswith("https://webstore.vgroup.net/")


def test_project_idea_without_answer_gets_contact_form(real_bundle):
    r = real_bundle.pipeline.answer("I want a blockchain-based NFT marketplace")
    assert r.status == "needs_team" and r.request_kind == "project_request"
    assert r.answer == PROJECT_IDEA_REPLY and "great idea" in r.answer
    assert r.action and r.action["type"] == "contact_form"
    assert r.sources == []
    assert "no answer" not in r.answer.lower() and "don't know" not in r.answer.lower()


def test_information_without_answer_gets_contact_form(real_bundle):
    r = real_bundle.pipeline.answer("What is your refund policy?")
    assert r.status == "needs_team" and r.answer == INFORMATION_TEAM_REPLY
    assert r.action["type"] == "contact_form"


@pytest.mark.parametrize("q", ["Tell me a joke", "What is the weather in Paris today?", "asdkjh qwe zzz"])
def test_off_topic(real_bundle, q):
    r = real_bundle.pipeline.answer(q)
    assert r.status == "off_topic" and r.off_topic and r.answer == OFF_TOPIC_REPLY
    assert r.sources == [] and r.action is None


@pytest.mark.parametrize("q,kind", [("hi", "smalltalk"), ("Thanks!", "smalltalk"), ("bye", "smalltalk")])
def test_smalltalk_is_not_off_topic(real_bundle, q, kind):
    r = real_bundle.pipeline.answer(q)
    assert r.status == kind and not r.off_topic


def test_input_validation(real_bundle, real_settings):
    with pytest.raises(ValueError):
        real_bundle.pipeline.answer("   ")
    with pytest.raises(ValueError):
        real_bundle.pipeline.answer("x" * (real_settings.max_question_chars + 1))


# --- LLM path with a scripted fake LLM ---------------------------------------------------------

def test_llm_grounded_answer_and_citations(real_bundle, real_settings, fake_llm_factory):
    llm = fake_llm_factory([_answer_json("You get 120 hours of service per year [1].", [1])])
    r = _pipeline_with(real_bundle, real_settings, llm).answer("How many support hours are included annually?")
    assert r.status == "answered" and r.generator == "llm"
    assert [s.number for s in r.sources] == [1] and r.sources[0].page_id == "db-425"
    call = llm.calls[0]
    assert call["system"] == GROUNDED_SYSTEM_PROMPT and call["schema"] is not None
    user_msg = call["messages"][-1]["content"]
    assert "<context>" in user_msg and "<source id=\"1\"" in user_msg and "<customer_question>" in user_msg
    assert "120" in user_msg, "retrieved context is passed to the LLM"


def test_llm_invalid_citations_removed(real_bundle, real_settings, fake_llm_factory):
    llm = fake_llm_factory([_answer_json("Annual Support costs $3,500 [1][9].", [1, 9])])
    r = _pipeline_with(real_bundle, real_settings, llm).answer("What is the cost of Annual Support?")
    assert "[9]" not in r.answer and [s.number for s in r.sources] == [1]


def test_llm_uncited_answer_is_not_trusted(real_bundle, real_settings, fake_llm_factory):
    llm = fake_llm_factory([_answer_json("We have offices on the moon.", [])])
    r = _pipeline_with(real_bundle, real_settings, llm).answer("Where is D Group's US head office?")
    assert r.status == "needs_team" and r.action["type"] == "contact_form"


def test_llm_needs_team_project(real_bundle, real_settings, fake_llm_factory):
    llm = fake_llm_factory([_answer_json("", [], status="needs_team", kind="project_request")])
    r = _pipeline_with(real_bundle, real_settings, llm).answer("Can you build an AI chatbot for my website?")
    assert r.status == "needs_team" and r.answer == PROJECT_IDEA_REPLY


def test_llm_off_topic(real_bundle, real_settings, fake_llm_factory):
    llm = fake_llm_factory([_answer_json("", [], status="off_topic", kind="other")])
    r = _pipeline_with(real_bundle, real_settings, llm).answer("What do you think about Shopify stock prices?")
    assert r.status in ("off_topic",) and r.off_topic


@pytest.mark.parametrize("failure", [LLMError("boom"), LLMRefusal("declined"), "not json at all"])
def test_llm_failure_falls_back_to_extractive(real_bundle, real_settings, fake_llm_factory, failure):
    llm = fake_llm_factory([failure])
    r = _pipeline_with(real_bundle, real_settings, llm).answer("How many support hours are included annually?")
    assert r.status == "answered" and r.generator == "extractive" and r.sources


def test_triage_llm_used_for_domain_band(real_bundle, real_settings, fake_llm_factory):
    llm = fake_llm_factory(['{"category": "off_topic"}'])
    r = _pipeline_with(real_bundle, real_settings, llm).answer("What is the CEO's name?")
    assert r.status == "off_topic" and r.generator == "triage-llm"
    assert "D Group offerings" in llm.calls[0]["system"]


def test_pipeline_never_leaks_exceptions(real_bundle, real_settings):
    class Boom:
        def retrieve(self, *a, **k):
            raise RuntimeError("secret internal path /etc/x")

    p = real_bundle.pipeline
    broken = RAGPipeline(real_settings, Boom(), None, p.extractive.embedder)
    r = broken.answer("What is SMTU?")
    assert r.status == "error" and "secret" not in r.answer and r.action["type"] == "contact_form"


# --- helpers ---------------------------------------------------------------------------------------

def test_clean_citations():
    text, used = clean_citations("A [1]. B [3][2] C [7].", {1, 2, 3})
    assert used == [1, 3, 2] and "[7]" not in text and text.endswith("C.")


def test_parse_json_object_variants():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('Sure! {"a": 2} hope that helps') == {"a": 2}
    with pytest.raises(Exception):
        parse_json_object("no json")


def test_tiny_pipeline_fully_offline(tiny_settings):
    """Whole pipeline on synthetic data with the hashing embedder (no model download)."""
    from app.rag.pipeline import build_pipeline

    b = build_pipeline(tiny_settings)
    r = b.pipeline.answer("How much is the Gold Plan?")
    assert r.status == "answered" and r.sources[0].page_id == "p2" and "5,999" in r.answer
    expected = chunk_documents(load_documents(tiny_settings.data_path), tiny_settings.chunk_size,
                               tiny_settings.chunk_overlap, tiny_settings.min_chunk_chars)
    assert len(expected) == b.store.count()


# --- regressions found during live /ask testing ---------------------------------------------

def test_platform_named_in_question_must_be_in_evidence(real_bundle):
    r = real_bundle.pipeline.answer("What is the cost of the BigCommerce Gold plan?")
    assert r.status == "answered"
    assert "bigcommerce" in r.answer.lower()
    assert any(s.page_id in ("db-2351", "db-4032") for s in r.sources)


@pytest.mark.parametrize("q", ["Can you build an NFT marketplace on blockchain for me?",
                               "Can you build an AI chatbot for my website?"])
def test_generic_faq_does_not_answer_unrelated_project(real_bundle, q):
    r = real_bundle.pipeline.answer(q)
    assert r.status == "needs_team" and r.answer == PROJECT_IDEA_REPLY


def test_full_case_study_preferred_over_title_stub(real_bundle):
    r = real_bundle.pipeline.answer("What did D Group build for Westcott?")
    assert any(s.page_id == "case-study-shopify-6" for s in r.sources)


def test_llm_status_schema_slip_is_tolerated(real_bundle, real_settings, fake_llm_factory):
    """Smaller models sometimes put the request kind into `status`; don't discard a cited answer."""
    slip = json.dumps({"status": "information_request", "answer": "You get 120 hours of service per year [1].",
                       "citations": [1], "request_kind": "other"})
    no_answer = json.dumps({"status": "project_request", "answer": "", "citations": [], "request_kind": "other"})
    llm = fake_llm_factory([slip, no_answer])
    p = _pipeline_with(real_bundle, real_settings, llm)
    r = p.answer("How many support hours are included annually?")
    assert r.status == "answered" and r.generator == "llm" and "120" in r.answer
    r2 = p.answer("Can you build an online appointment booking system for my clinic?")
    assert r2.status == "needs_team"


def test_uncited_but_supported_answer_gets_citation(real_bundle, real_settings, fake_llm_factory):
    supported = json.dumps({"status": "answered", "citations": [], "request_kind": "information_request",
                            "answer": "You get 120 hours of expert service annually for support and store enhancements."})
    r = _pipeline_with(real_bundle, real_settings, fake_llm_factory([supported])).answer(
        "How many support hours are included annually?")
    assert r.status == "answered" and r.sources and r.answer.endswith("]")
    assert r.sources[0].page_id == "db-425"

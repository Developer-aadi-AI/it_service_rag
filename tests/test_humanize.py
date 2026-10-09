"""Readable answers: sentence cleanup, overview intent, contact details, plan prices."""
from __future__ import annotations

import re

import pytest

from app.rag.contact_info import compose_contact_answer, is_contact_question
from app.rag.humanize import clean_sentence, compose_answer, is_fragment, plan_price_sentences
from app.rag.intent import is_company_overview


@pytest.mark.parametrize("raw,expected", [
    ("Build Your Platform D Group specializes in developing intuitive, high-performing mobile apps.",
     "D Group specializes in developing intuitive, high-performing mobile apps."),
    ("Web Design Services At D Group, we build websites with purpose.", "At D Group, we build websites with purpose."),
    ("Introduction: ReviewCaddy extension for Magento is developed to integrate the ReviewCaddy app.",
     "ReviewCaddy extension for Magento is developed to integrate the ReviewCaddy app."),
    ("Platform: Shopify CMS Client Overview First Aid Only, an Acme United Corporation brand, is a provider.",
     "First Aid Only, an Acme United Corporation brand, is a provider."),
    ("The Annual Support plan costs $3,500 per year , payable in advance.",
     "The Annual Support plan costs $3,500 per year, payable in advance."),
    ("Restorsea Restorsea is a physician-dispensed skincare brand we have partnered with since 2013.",
     "Restorsea is a physician-dispensed skincare brand we have partnered with since 2013."),
    ("D Group is an ISO 9001:2015 Certified Company with a highly experienced in-house team /including: "
     "Shopify Expert Partners Magento Certified Front-End Developers BigCommerce Certified Professionals",
     "D Group is an ISO 9001:2015 Certified Company with a highly experienced in-house team."),
])
def test_clean_sentence(raw, expected):
    assert clean_sentence(raw) == expected


def test_clean_sentence_removes_cta_labels():
    out = clean_sentence("Silver Plan includes setup Buy Now and our team supports you View Project today.")
    assert "Buy Now" not in out and "View Project" not in out


@pytest.mark.parametrize("text", [
    "UI/UX Design Wireframes Mobile App Design Start Your Project.",
    "Products Inventory Setup Up to 100 SKUs Up to 500 SKUs Product Variations Yes Yes.",
    "D Group is here to help.",
])
def test_fragments_detected(text):
    assert is_fragment(text)


def test_prose_is_not_fragment():
    assert not is_fragment("Yes, we build custom mobile apps for iOS, Android, and even Windows platforms.")


def test_compose_answer_groups_citations_and_closes():
    out = compose_answer([("First fact.", 1), ("Second fact.", 1), ("Third fact.", 2)], "q", "information_request")
    body, closing = out.split("\n\n")
    assert body == "First fact. Second fact. [1] Third fact. [2]"
    assert closing.endswith("?") or closing.endswith(".")


def test_plan_price_sentences():
    text = "BigCommerce Packages Silver Plan $3,999 Buy Now Gold Plan $5,999 Buy Now Gold Plan £3,960.00 GBP"
    assert plan_price_sentences(text, "BigCommerce Packages") == [
        "Our BigCommerce Silver Plan is priced at $3,999.", "Our BigCommerce Gold Plan is priced at $5,999."]
    assert plan_price_sentences(text, "Buy Our Packages") == []  # mixed-product page: ambiguous


@pytest.mark.parametrize("q", ["what do you guys do??", "what does dgroup do?", "Tell me about your company",
                               "What services do you offer?", "who are you", "Hi, what is D Group?"])
def test_overview_intent(q):
    assert is_company_overview(q)


@pytest.mark.parametrize("q", ["What are you able to build for my restaurant?", "what are your prices",
                               "Do you build mobile apps?", "What is SMTU?"])
def test_not_overview_intent(q):
    assert not is_company_overview(q)


@pytest.mark.parametrize("q,yes", [("Where is your office?", True), ("What's your phone number?", True),
                                   ("How can I contact you?", True), ("What are your office hours?", False),
                                   ("Can you build an email marketing integration?", False)])
def test_contact_intent(q, yes):
    assert is_contact_question(q) is yes


# --- end to end on the real data ------------------------------------------------------------

@pytest.mark.parametrize("q", ["what do you guys do??", "what does dgroup do?"])
def test_overview_answer_from_about_page(real_bundle, q):
    r = real_bundle.pipeline.answer(q)
    assert r.status == "answered" and not r.off_topic
    assert r.sources[0].page_id == "db-66"  # About D Group
    assert "digital solutions company" in r.answer


def test_contact_answers_use_contact_page_values(real_bundle):
    r = real_bundle.pipeline.answer("What's your phone number?")
    assert r.status == "answered" and r.sources[0].page_id == "db-96"
    assert "609-371-5400" in r.answer and "755-4295625" in r.answer
    r = real_bundle.pipeline.answer("Where is your office?")
    assert "East Windsor, NJ 08520" in r.answer and "Bhopal" in r.answer
    r = real_bundle.pipeline.answer("Do you have an office in London?")
    assert "listed on our website" in r.answer and "London" not in r.answer


def test_contact_values_exist_in_source(real_bundle, real_settings):
    from app.ingestion.loader import load_raw

    page = next(x["text"] for x in load_raw(real_settings.data_path) if x["page_id"] == "db-96")
    info = real_bundle.pipeline.contact
    for office in info.offices:
        assert office.phone in page
    assert info.email in page


def test_answers_have_no_page_residue(real_bundle):
    residue = re.compile(r"\b(Buy Now|View Project|Start Your [A-Z]\w+ Project|Client Overview)\b")
    for q in ["what does dgroup do?", "Do you build mobile apps?", "What is the cost of the BigCommerce Gold plan?",
              "Tell me about the First Aid Only project", "Is D Group ISO certified?"]:
        r = real_bundle.pipeline.answer(q)
        assert r.status == "answered", q
        assert not residue.search(r.answer), (q, r.answer)


def test_bigcommerce_gold_price(real_bundle):
    r = real_bundle.pipeline.answer("What is the cost of the BigCommerce Gold plan?")
    assert "BigCommerce Gold Plan is priced at $5,999" in r.answer

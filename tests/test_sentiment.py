"""Sentiment classification and acknowledgement wording."""
from __future__ import annotations

import pytest

from app.sentiment.analyzer import RuleBasedSentimentAnalyzer, acknowledgement

A = RuleBasedSentimentAnalyzer()


@pytest.mark.parametrize("text,label", [
    ("What is included in Annual Support?", "neutral"),
    ("How much does the Shopify Gold plan cost?", "neutral"),
    ("Thanks, that was really helpful!", "positive"),
    ("Great, how much is the BigCommerce Gold plan?", "positive"),
    ("No thanks", "neutral"),
    ("My Shopify checkout is not working", "negative"),
    ("I have a problem with my Magento store", "negative"),
    ("That was not helpful", "frustrated"),
    ("This is useless, you are not helping at all!!", "frustrated"),
    ("WHY IS THIS SO SLOW I NEED HELP", "frustrated"),
    ("I'm so frustrated with my current developer", "frustrated"),
])
def test_labels(text, label):
    assert A.analyze(text).label == label


def test_acronyms_are_not_shouting():
    assert A.analyze("Do you integrate ERP and CRM with SEO for PHP sites?").label == "neutral"


def test_acknowledgement_is_short_and_only_for_negative():
    assert acknowledgement("neutral", "x") == "" and acknowledgement("positive", "x") == ""
    for label in ("negative", "frustrated"):
        ack = acknowledgement(label, "my store is broken")
        assert 0 < len(ack.split()) <= 14  # brief, not over-the-top
        assert ack.count("!") == 0

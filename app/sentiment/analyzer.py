"""Sentiment detection and natural response adaptation.

A transparent rule-based classifier (lexicon + negation + intensity signals)
behind a small interface, so it can be replaced by a model later. Labels:
positive | neutral | negative | frustrated.
"""
from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Literal

Label = Literal["positive", "neutral", "negative", "frustrated"]

_FRUSTRATED = [
    r"frustrat\w*", r"annoy\w*", r"angry", r"furious", r"fed up", r"sick of", r"ridiculous", r"useless",
    r"waste of (?:my )?time", r"not helpful", r"unhelpful", r"(?:doesn'?t|does not|didn'?t|did not) help",
    r"you (?:don'?t|do not) understand", r"(?:still|again) (?:not|no|nothing)", r"worst", r"terrible",
    r"horrible", r"awful", r"pathetic", r"stupid", r"hate", r"wtf", r"seriously\?+", r"come on",
    r"unacceptable", r"fix (?:this|it) now", r"nobody (?:is )?(?:responding|replied|answers?)",
    r"no one (?:is )?(?:responding|replied|answers?)", r"how many times",
]
_NEGATIVE = [
    r"bad", r"poor", r"disappoint\w*", r"unhappy", r"upset", r"sad", r"problem\w*", r"issue\w*",
    r"broken", r"not working", r"(?:doesn'?t|does not|isn'?t|is not|won'?t) work\w*", r"slow", r"confus\w*",
    r"difficult", r"hard to", r"error\w*", r"bug\w*", r"crash\w*", r"fail\w*", r"delay\w*", r"late",
    r"stuck", r"can'?t (?:find|get|access|login|log in)", r"unable to", r"worried", r"concern\w*",
    r"complain\w*", r"not (?:satisfied|happy)",
]
_POSITIVE = [
    r"great", r"thanks?", r"thank you", r"awesome", r"love", r"perfect", r"excellent", r"helpful",
    r"amazing", r"nice", r"good job", r"cool", r"wonderful", r"appreciate\w*", r"brilliant", r"fantastic",
    r"happy", r"glad",
]
_NEGATORS = re.compile(r"\b(?:not|no|never|isn'?t|wasn'?t|aren'?t|don'?t|doesn'?t|didn'?t|hardly)\s+(?:\w+\s+)?$", re.I)


def _compile(words: list[str]) -> re.Pattern:
    return re.compile(r"\b(?:" + "|".join(words) + r")\b", re.I)


_FRUSTRATED_RE, _NEGATIVE_RE, _POSITIVE_RE = _compile(_FRUSTRATED), _compile(_NEGATIVE), _compile(_POSITIVE)


@dataclass
class Sentiment:
    label: Label
    score: float  # -1 (very negative) .. +1 (very positive)
    signals: list[str] = field(default_factory=list)

    @property
    def is_negative(self) -> bool:
        return self.label in ("negative", "frustrated")


class SentimentAnalyzer(ABC):
    @abstractmethod
    def analyze(self, text: str) -> Sentiment: ...


class RuleBasedSentimentAnalyzer(SentimentAnalyzer):
    def analyze(self, text: str) -> Sentiment:
        text = text or ""
        signals: list[str] = []
        score = 0.0

        for m in _FRUSTRATED_RE.finditer(text):
            score -= 1.0
            signals.append(f"frustrated:{m.group(0).lower()}")
        for m in _NEGATIVE_RE.finditer(text):
            score -= 0.5
            signals.append(f"negative:{m.group(0).lower()}")
        for m in _POSITIVE_RE.finditer(text):
            polite = m.group(0).lower().startswith("thank")  # "no thanks" is a polite decline: neutral
            negated = bool(_NEGATORS.search(text[: m.start()]))
            if polite and negated:
                continue
            if negated:  # "not helpful", "not great"
                score -= 0.6
                signals.append(f"negated:{m.group(0).lower()}")
            else:
                score += 0.6
                signals.append(f"positive:{m.group(0).lower()}")

        intensity = 0.0
        if re.search(r"[!?]{2,}", text):
            intensity += 0.4
            signals.append("punctuation")
        shouting = [w for w in re.findall(r"\b[A-Z]{3,}\b", text) if w not in {"SEO", "ERP", "CRM", "API", "SKU", "CMS", "PHP", "USD", "GBP", "ISO", "SMTU", "UX", "UI", "AWS", "NFT"}]
        if len(shouting) >= 2:
            intensity += 0.5
            signals.append("shouting")
        if score < 0:
            score -= intensity  # intensity amplifies negativity only

        frustrated_hits = sum(s.startswith("frustrated:") for s in signals)
        if frustrated_hits or score <= -1.2 or (score < 0 and "shouting" in signals):
            label: Label = "frustrated"
        elif score < -0.25:
            label = "negative"
        elif score > 0.25:
            label = "positive"
        else:
            label = "neutral"
        return Sentiment(label, max(-1.0, min(1.0, score / 2)), signals)


_ACKS = {
    "frustrated": [
        "I'm sorry this has been frustrating - let's get it sorted out.",
        "I understand the frustration, and I'm here to help.",
        "Sorry for the hassle. Let me help with that.",
    ],
    "negative": [
        "Sorry to hear that.",
        "I'm sorry you're running into that.",
    ],
}


def acknowledgement(label: Label, seed: str) -> str:
    """Short, varied acknowledgement for negative/frustrated messages ('' otherwise)."""
    options = _ACKS.get(label)
    if not options:
        return ""
    idx = int(hashlib.md5(seed.encode("utf-8")).hexdigest(), 16) % len(options)
    return options[idx]


_CLAUSE_SPLIT = re.compile(r"(?<=[.!?])\s+|\s+[-–—]+\s+|;\s*|,\s+(?=(?:but|and|so)\b)")


def focus_query(text: str, analyzer: "SentimentAnalyzer", is_business: "Callable[[str], bool]") -> str:
    """Drop purely emotional clauses ("I'm so frustrated.", "This is useless!") from a message.

    Returns the remaining clauses joined (possibly ""), so retrieval focuses on what
    the user actually asks about. Clauses with a question mark or a business term are kept.
    """
    clauses = [c.strip() for c in _CLAUSE_SPLIT.split(text) if c and c.strip()]
    keep = [c for c in clauses
            if "?" in c or is_business(c) or not analyzer.analyze(c).is_negative]
    return " ".join(keep)


ESCALATION_OFFER = ("If you'd prefer to speak with someone directly, share your details in the form below "
                    "and our support team will connect with you shortly.")
CLARIFY_FRUSTRATED = ("Could you tell me a bit more about what you need help with - for example, which "
                      "V Group service, product or project this is about?")

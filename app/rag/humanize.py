"""Turn extracted page sentences into natural, readable chat answers.

The supplied page text is flattened website copy: headings, menu labels and
button text are glued onto real sentences ("Build Your Platform D Group
specializes in..."). These helpers strip that residue and compose the
remaining (verbatim, grounded) sentences into a short conversational reply.
No new facts are introduced; only wording around the facts is added.
"""
from __future__ import annotations

import hashlib
import re

# Button / CTA labels that appear inline in the page copy.
_CTA_NOISE = re.compile(
    r"\b(?:Buy Now|View Project|View All|Show more|Read More|Learn More|Download Brochure|"
    r"Get Annual Support(?: Now| today)?|Start (?:Your|My) [A-Z][A-Za-z/ ]{1,30}?(?:Project|Store|Development)|"
    r"Connect With Our Team|Send Your Requirements|Get Started|Contact Us Now|Let[’']s Connect|"
    r"I[’']m Ready to Scale|Hire Your [A-Z][A-Za-z ]{1,25}? Now)\b[!.]?"
)
# Words that typically start a real sentence in this copy (followed by a lowercase word).
_SENTENCE_START = re.compile(
    r"\b(?:At D Group|D Group|Contact us|We|Our|With|Whether|From|This|These|Its|It|You|Your|Yes|Absolutely|"
    r"Definitely|Every|Each|Simply|Built|Using|If|When|Unlike|For|By|In|As|To|The|A|An)[\s,]+[a-z0-9$]"
)
_LEADING_JUNK = re.compile(r"^[\s\-–—:|,.;]+")
# Case-study / section labels glued to the start of copy.
_LABEL_PREFIX = re.compile(r"^(?:Platform:\s+.+?\s+Client Overview:?\s+|Client Overview:?\s+|"
                           r"(?:[A-Z][A-Za-z&/-]*\s){0,3}[A-Z][A-Za-z&/-]*:\s+(?=[A-Z]))")
# "...for these reasons: More than 10 years ... Rapid project turnover" -> list glued after a colon.
_LIST_INTRO_END = re.compile(r"(?:\s*/?\s*(?:including|include|includes|like|such as|following|"
                             r"these reasons|reasons|are))\s*$", re.I)


def _headline_like(words: list[str]) -> bool:
    """A run of mostly Title-Case words (a heading or menu), not prose."""
    if not words or len(words) > 16:
        return False
    # Title-Case words and symbols like "UI/UX" or "&" count; numbers and prices do not.
    caps = sum(1 for w in words if w[:1].isupper() or not (w[:1].isalnum() or w[:1] in "$£€"))
    return caps / len(words) >= 0.6


def strip_heading_prefix(sentence: str) -> str:
    """'UI/UX Design Wireframes Start Your Project D Group helps...' -> 'D Group helps...'"""
    best = 0
    for m in _SENTENCE_START.finditer(sentence):
        if m.start() == 0:
            continue
        if m.start() > 140:
            break
        if _headline_like(sentence[: m.start()].split()):
            best = m.start()
    return sentence[best:] if best else sentence


def _cut_glued_list(s: str) -> str:
    """Drop a Title-Case list glued after ': ' and keep the lead-in if it is a sentence."""
    idx = s.find(": ")
    if idx == -1:
        return s
    tail = s[idx + 2:].split()
    if tail and tail[0].startswith(("http://", "https://")):
        return s[:idx] + ": " + tail[0].rstrip(".,")  # "...purchase it directly at: <url>" keeps the link
    caps = sum(1 for w in tail if w[:1].isupper() or not w[:1].isalpha())
    intro = bool(_LIST_INTRO_END.search(s[:idx]))  # "...for these reasons: <list>"
    if len(tail) >= 6 and (caps / len(tail) > 0.5 or intro):
        head = _LIST_INTRO_END.sub("", s[:idx]).rstrip(" ,/")
        return head if len(head.split()) >= 6 else ""
    return s


def clean_sentence(sentence: str) -> str:
    s = _CTA_NOISE.sub(" ", sentence)
    s = re.sub(r"\s+", " ", s).strip()
    s = _LABEL_PREFIX.sub("", s)
    s = strip_heading_prefix(s)
    # Heading that repeats the subject: "Restorsea Restorsea is ..." -> "Restorsea is ..."
    s = re.sub(r"^((?:[\w&'.-]+ ){0,3}[\w&'.-]+) \1\b", r"\1", s)
    s = _cut_glued_list(s)
    if not s:
        return ""
    s = _LEADING_JUNK.sub("", s)
    s = re.sub(r"\s+([,.;:!?])", r"\1", s)  # "per year , payable" -> "per year, payable"
    if s and s[0].islower():
        s = s[0].upper() + s[1:]
    if s and s[-1] not in ".!?":
        s += "."
    return s


def is_fragment(sentence: str) -> bool:
    """Menus, headings, flattened tables: not useful as a chat answer."""
    if re.search(r"\b(?:these reasons|the following)\.$", sentence, re.I):
        return True
    words = sentence.split()
    if len(words) < 8:  # short lines are usually taglines/headings ("D Group is here with solutions...")
        return True
    # Prose has several lowercase function words; label lists ("Inventory Setup Up to 100 SKUs") don't.
    function_words = re.findall(r"\b(?:the|a|an|and|or|to|of|for|with|in|on|our|we|your|you|is|are|that|"
                                r"this|it|by|from|as|at|be|has|have|can|will)\b", sentence)
    if len(function_words) < 2:
        return True
    if _headline_like(words[:16]) and len(words) <= 16 and len(function_words) < 3:
        return True
    caps = sum(1 for w in words if w[:1].isupper())
    return caps / len(words) > 0.55


_CLOSINGS = {
    "overview": ["Is there a particular service you'd like to know more about?",
                 "Would you like details on any of these services?"],
    "project_request": ["Would you like to know more, or share some details about your project?",
                        "Happy to help further - what are you planning to build?"],
    "information_request": ["Is there anything else you'd like to know?",
                            "Let me know if you'd like more details.",
                            "Anything else I can help you with?"],
}


def closing_line(question: str, kind: str) -> str:
    options = _CLOSINGS.get(kind) or _CLOSINGS["information_request"]
    idx = int(hashlib.md5(question.lower().encode()).hexdigest(), 16) % len(options)
    return options[idx]


def compose_answer(picked: list[tuple[str, int]], question: str, kind: str) -> str:
    """Join sentences into one paragraph; cite each source once, after its last sentence."""
    parts: list[str] = []
    for i, (text, num) in enumerate(picked):
        next_num = picked[i + 1][1] if i + 1 < len(picked) else None
        parts.append(text if next_num == num else f"{text} [{num}]")
    body = " ".join(parts)
    return f"{body}\n\n{closing_line(question, kind)}"


_PLAN_PRICE = re.compile(r"\b(Silver|Gold|Platinum|Bronze|Basic|Premium|Starter|Pro)\s+Plan:?\s*"
                         r"(\$\s?[\d,]+(?:\.\d{2})?|£\s?[\d,]+(?:\.\d{2})?)")


def plan_price_sentences(text: str, title: str) -> list[str]:
    """'Silver Plan $3,999 Buy Now Gold Plan $5,999' on 'BigCommerce Packages' ->
    ['Our BigCommerce Silver Plan is priced at $3,999.', 'Our BigCommerce Gold Plan is priced at $5,999.']
    Only applied to pricing pages; prices are taken verbatim from the page."""
    product = re.sub(r"\s*(Packages?|Pricing|Plans?)\s*$", "", title).strip()
    if not product or product.lower() in ("buy our", "our"):
        return []  # pages mixing several products: plan names alone would be ambiguous
    out: list[str] = []
    seen_plans: set[str] = set()
    for plan, price in _PLAN_PRICE.findall(text):
        if plan in seen_plans:
            continue  # first price per plan (USD comes before GBP on these pages)
        seen_plans.add(plan)
        name = f"{product} {plan} Plan" if product else f"{plan} Plan"
        out.append(f"Our {name} is priced at {price.replace(' ', '')}.")
    return out

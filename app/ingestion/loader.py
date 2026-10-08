"""Load, validate, clean and enrich the supplied V Group dataset.

Decisions here are based on inspecting `Database/final_dataset.json`:
* 134 records with fields page_id, url, title, text, source_type, platform.
* Some `source_type` values are "UNKNOWN"; they are re-derived from the URL.
* 22 portfolio case studies contain only the client name; they are kept as
  short portfolio records because they are still valid evidence of a client.
* One page is a WordPress "not allowed to access this page" preview and is dropped.
* A "Have a project in mind?" call-to-action block and an FAQ header are
  repeated across many pages; they are stripped from chunk text (the CTA is
  kept as metadata).
"""
from __future__ import annotations

import html
import json
import logging
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from app.ingestion.models import FAQ, Document

logger = logging.getLogger(__name__)

REQUIRED_FIELDS = ("page_id", "url", "title", "text", "source_type", "platform")
_APOS = "['’]"  # straight or curly apostrophe


class DataValidationError(ValueError):
    """Raised when the dataset cannot be used at all."""


# --- Cleaning rules -----------------------------------------------------------

_BOILERPLATE_PATTERNS = [
    # Repeated site-wide CTA block.
    re.compile(
        rf"Have a project in mind\? Let{_APOS}s make it happen\..*?Transparent timelines and delivery",
        re.S,
    ),
    # Contact form field labels (developer-hiring contact form pages).
    re.compile(r"First Name \* Last Name \* Email \*.*?(?:Reset|$)", re.S),
]
_FAQ_HEADER = re.compile(
    rf"Your Questions, Answered We{_APOS}ve answered [^.]*?\.\s*(?:Contact us)?", re.S
)
_ACCESS_DENIED = re.compile(r"Sorry, you are not allowed to access this page", re.I)

# CTA phrases observed in the supplied pages (kept as metadata, not invented).
_CTA_PATTERNS = [
    r"Start (?:Your|My) [A-Z][A-Za-z ]{1,30}?(?:Project|Store)",
    r"Get Annual Support(?: Now| today)?",
    r"Request a free consultation",
    r"Send Your Requirements",
    r"Connect With Our Team",
    r"Buy Now",
    r"Get a Free Quote",
    r"Hire (?:Dot Net|PHP|Android|Big Data) Developers?",
]

# Platform vocabulary: values present in the dataset's `platform` field plus
# platform names that appear in page titles/URLs.
_PLATFORM_NAMES = {
    "shopify plus": "Shopify Plus",
    "shopify": "Shopify",
    "magento": "Magento",
    "bigcommerce": "BigCommerce",
    "wordpress": "WordPress",
    "woocommerce": "WooCommerce",
    "drupal": "Drupal",
    "wix": "Wix",
    "squarespace": "Squarespace",
    "amazon webstore": "Amazon Webstore",
    "x-cart": "X-Cart",
    "xcart": "X-Cart",
}

_UNKNOWN_TYPE_BY_URL = [
    ("privacy-policy", "policy"),
    ("contact-us", "contact"),
    ("partner-registration", "partner"),
    ("shopify-apps", "service"),
    ("product-category", "product-category"),
    ("/blog/", "blog-index"),
]

_CATEGORY_RULES = [
    (lambda d: d.source_type == "case-study", "Portfolio"),
    (lambda d: d.source_type == "pricing", "Pricing"),
    (lambda d: d.source_type == "blog-post", "Blog"),
    (lambda d: "/hire-" in d.url, "Hire Developers"),
    (lambda d: d.source_type in ("app-product", "product-category") or "/product" in d.url,
     "Products & Extensions"),
    (lambda d: "shopify-app" in d.url, "Shopify Apps"),
    (lambda d: d.source_type == "service", "Services"),
    (lambda d: d.source_type in ("about", "home"), "Company"),
    (lambda d: d.source_type in ("contact", "partner"), "Contact & Partnership"),
    (lambda d: d.source_type == "policy", "Policies"),
]

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9“\"(])")


def normalize_text(text: str) -> str:
    text = html.unescape(text or "")
    text = text.replace(" ", " ").replace("​", "")
    return re.sub(r"\s+", " ", text).strip()


def clean_title(title: str) -> str:
    title = normalize_text(title)
    title = re.sub(r"\s+Archives\s*-?\s*$", "", title)
    return title.strip(" ,-|")


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_END.split(text) if s.strip()]


def extract_faqs(text: str, max_answer_chars: int = 500) -> list[FAQ]:
    """Extract numbered FAQ pairs ("1. Question? Answer. 2. ...")."""
    start = text.find("Your Questions, Answered")
    if start == -1:
        return []
    region = text[start:]
    pattern = re.compile(
        r"(?:^|\s)(\d{1,2})\.\s+([A-Z][^?]{8,250}\?)\s+(.+?)"
        r"(?=\s\d{1,2}\.\s+[A-Z][^?]{8,250}\?|$)",
        re.S,
    )
    found: list[tuple[int, FAQ]] = []
    expected = 1
    for m in pattern.finditer(region):
        number = int(m.group(1))
        if number != expected:
            if number == 1 and found:
                expected = 1  # a second FAQ block on the same page
            else:
                continue
        expected = number + 1
        found.append((number, FAQ(question=m.group(2).strip(), answer=m.group(3).strip())))

    faqs: list[FAQ] = []
    for i, (number, faq) in enumerate(found):
        last_in_block = i == len(found) - 1 or found[i + 1][0] != number + 1
        sentences = split_sentences(faq.answer)
        if last_in_block:
            # The final answer of a block runs into the following page text.
            sentences = sentences[:2]
        kept, total = [], 0
        for s in sentences:
            if kept and total + len(s) > max_answer_chars:
                break
            kept.append(s)
            total += len(s) + 1
        faq.answer = " ".join(kept) or faq.answer[:max_answer_chars]
        faqs.append(faq)
    return faqs


def _detect_ctas(text: str) -> list[str]:
    found: list[str] = []
    for pat in _CTA_PATTERNS:
        for m in re.finditer(pat, text):
            cta = m.group(0).strip()
            if cta not in found:
                found.append(cta)
    return found[:6]


def _platforms_mentioned(*parts: str) -> list[str]:
    hay = " ".join(parts).lower()
    found: list[str] = []
    for key, name in _PLATFORM_NAMES.items():
        if re.search(rf"(?<![a-z]){re.escape(key)}(?![a-z])", hay) and name not in found:
            found.append(name)
    return found


def _normalize_platform(raw: str) -> str:
    raw = normalize_text(raw)
    if not raw:
        return ""
    low = raw.lower()
    for key, name in _PLATFORM_NAMES.items():
        if low.startswith(key):
            return name
    return raw


def _derive_source_type(raw_type: str, url: str) -> str:
    if raw_type and raw_type.upper() != "UNKNOWN":
        return raw_type
    for needle, value in _UNKNOWN_TYPE_BY_URL:
        if needle in url:
            return value
    return "page"


def _description(text: str, limit: int = 300) -> str:
    out = ""
    for s in split_sentences(text):
        if len(out) + len(s) > limit:
            break
        out = f"{out} {s}".strip()
    return out or text[:limit]


def _validate_record(raw: Any, index: int) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        logger.warning("record %d skipped: not an object", index)
        return None
    missing = [f for f in REQUIRED_FIELDS if f not in raw]
    if missing:
        logger.warning("record %d skipped: missing fields %s", index, missing)
        return None
    rec = {f: raw[f] if isinstance(raw[f], str) else str(raw[f] or "") for f in REQUIRED_FIELDS}
    if not rec["page_id"].strip() or not rec["url"].strip():
        logger.warning("record %d skipped: empty page_id/url", index)
        return None
    if not rec["title"].strip() and not rec["text"].strip():
        logger.warning("record %s skipped: no title and no text", rec["page_id"])
        return None
    return rec


def clean_record(rec: dict[str, str]) -> Document | None:
    """Turn a validated raw record into a cleaned Document (or None to drop it)."""
    raw_text = normalize_text(rec["text"])
    if _ACCESS_DENIED.search(raw_text):
        logger.info("dropping %s: access-denied page", rec["page_id"])
        return None

    title = clean_title(rec["title"]) or rec["page_id"]
    url = rec["url"].strip()
    source_type = _derive_source_type(rec["source_type"].strip(), url)
    platform = _normalize_platform(rec["platform"])

    ctas = _detect_ctas(raw_text)
    faqs = extract_faqs(raw_text)

    text = raw_text
    for pat in _BOILERPLATE_PATTERNS:
        text = pat.sub(" ", text)
    text = _FAQ_HEADER.sub(" Frequently Asked Questions: ", text)
    text = re.sub(r"\s+", " ", text).strip().lstrip(", ").strip()

    content_quality = "full"
    if len(text) < 40 or text.lower() == title.lower():
        if source_type == "case-study":
            # Title-only portfolio entries: keep a minimal, fact-only record.
            content_quality = "title_only"
            plat = f" on {platform}" if platform else ""
            text = f"{title} is a client project listed in V Group's portfolio{plat}."
        elif not text:
            logger.info("dropping %s: empty text", rec["page_id"])
            return None
        else:
            content_quality = "short"
    elif len(text) < 500:
        content_quality = "short"

    doc = Document(
        page_id=rec["page_id"].strip(),
        url=url,
        title=title,
        text=text,
        source_type=source_type,
        platform=platform,
        original_source_type=rec["source_type"],
        description=_description(text),
        ctas=ctas,
        platforms_mentioned=_platforms_mentioned(title, url, platform),
        faqs=faqs,
        content_quality=content_quality,
    )
    doc.category = next((cat for rule, cat in _CATEGORY_RULES if rule(doc)), "General")
    return doc


def _assign_keywords(docs: list[Document], top_n: int = 8) -> None:
    """Corpus-level TF-IDF keywords per document (derived from the data)."""
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
    except ImportError:  # pragma: no cover - optional dependency
        logger.warning("scikit-learn missing; keywords not derived")
        return
    if len(docs) < 2:
        return
    corpus = [f"{d.title} {d.title} {d.text}" for d in docs]
    vec = TfidfVectorizer(
        stop_words="english", ngram_range=(1, 2), max_df=0.5, min_df=1,
        token_pattern=r"(?u)\b[a-zA-Z][a-zA-Z0-9+.-]{2,}\b",
    )
    matrix = vec.fit_transform(corpus)
    vocab = vec.get_feature_names_out()
    for i, doc in enumerate(docs):
        if doc.content_quality == "title_only":
            doc.keywords = [k for k in (doc.title.lower(), doc.platform.lower()) if k]
            continue
        row = matrix.getrow(i)
        pairs = sorted(zip(row.indices, row.data), key=lambda p: -p[1])
        kws: list[str] = []
        for idx, _ in pairs:
            term = vocab[idx]
            if any(term in k or k in term for k in kws):
                continue
            kws.append(term)
            if len(kws) >= top_n:
                break
        doc.keywords = kws


def load_raw(path: Path) -> list[Any]:
    if not path.exists():
        raise DataValidationError(f"dataset not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DataValidationError(f"dataset is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise DataValidationError("dataset must be a JSON array of page records")
    return data


def load_documents(path: Path | None = None, records: Iterable[Any] | None = None) -> list[Document]:
    """Load + validate + clean the dataset. Pass `records` to bypass the file."""
    if records is None:
        if path is None:
            raise ValueError("either path or records is required")
        records = load_raw(path)
    docs: list[Document] = []
    seen_ids: Counter[str] = Counter()
    for i, raw in enumerate(records):
        rec = _validate_record(raw, i)
        if rec is None:
            continue
        doc = clean_record(rec)
        if doc is None:
            continue
        seen_ids[doc.page_id] += 1
        if seen_ids[doc.page_id] > 1:  # keep ids unique for citations
            doc.page_id = f"{doc.page_id}~{seen_ids[doc.page_id]}"
        docs.append(doc)
    if not docs:
        raise DataValidationError("no usable records in dataset")
    _assign_keywords(docs)
    logger.info("loaded %d documents (%s)", len(docs),
                dict(Counter(d.source_type for d in docs)))
    return docs

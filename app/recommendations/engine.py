"""Primary and related service recommendations from the supplied catalog.

The catalog is built only from the indexed V Group pages (services, hire-a-
developer pages, Shopify apps, packages, products). Each item is represented
by the centroid of its chunk embeddings.

* primary: the catalog page the answer was grounded on, otherwise the item most
  similar to the question (above a minimum similarity);
* related: items close to both the primary item and the question, excluding
  near-duplicates (e.g. the Magento 1 and Magento 2 variant of one extension)
  and items for a different platform than the one the user asked about.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from app.embeddings.base import Embedder
from app.rag.humanize import clean_sentence, is_fragment, strip_heading_prefix
from app.ingestion.loader import split_sentences
from app.vectorstore.base import VectorStore

logger = logging.getLogger(__name__)

RECOMMENDABLE = {"Services", "Hire Developers", "Shopify Apps", "Pricing", "Products & Extensions"}
_EXCLUDE_URL = re.compile(r"contact-form|/partners?/|/buy-our-packages/|/product-category/|/services/?$")
_CASING = {"php": "PHP", "shopify": "Shopify", "wordpress": "WordPress", "woocommerce": "WooCommerce",
           "bigcommerce": "BigCommerce", "ios": "iOS", "magento": "Magento", "drupal": "Drupal", "seo": "SEO"}
_PLATFORMS = ["shopify", "magento", "bigcommerce", "wordpress", "woocommerce", "drupal", "wix", "squarespace",
              "android", "x-cart", "xcart"]


def _platforms(text: str) -> set[str]:
    low = text.lower()
    found = {p.replace("xcart", "x-cart") for p in _PLATFORMS if re.search(rf"(?<![a-z]){re.escape(p)}(?![a-z])", low)}
    if "woocommerce" in found or "x-cart" in found:
        found.discard("wordpress")  # WooCommerce runs on WordPress; treat it as its own platform
    return found


def _first_clean_sentence(chunks: list, *titles: str) -> str:
    """First readable prose sentence of the page (headings and the page title stripped)."""
    ordered = sorted(chunks, key=lambda c: c.metadata.get("chunk_index", 0))
    prefixes = sorted({t.strip() for t in titles if t} | {"Home"}, key=len, reverse=True)
    for chunk in ordered[:3]:
        for raw in split_sentences(chunk.text):
            s = clean_sentence(raw)
            changed = True
            while changed:  # "Home Shopify Packages Plan Comparison..." -> "Plan Comparison..."
                changed = False
                for p in prefixes:
                    rest = s[len(p) + 1:]
                    # only a heading if the text after it starts a new capitalised phrase
                    if s.lower().startswith(p.lower() + " ") and rest[:1].isupper():
                        s, changed = rest, True
            s = strip_heading_prefix(s)
            s = s[:1].upper() + s[1:]
            head = s.split()[:4]
            glued_heading = sum(w[:1].isupper() for w in head) >= 3  # "Plan Comparison Find a ..."
            if s and not glued_heading and not is_fragment(s) and 10 <= len(s.split()) <= 45 and not s.endswith("?"):
                return s
    return ""


def _slug_name(url: str) -> str:
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    words = [_CASING.get(w, w.capitalize()) for w in slug.split("-") if w]
    return " ".join(words)


@dataclass
class CatalogItem:
    page_id: str
    name: str
    url: str
    category: str
    kind: str  # service | package | product | app
    description: str
    ctas: list[str]
    platforms: set[str]
    centroid: np.ndarray = field(repr=False)

    @property
    def family(self) -> str:
        return self.name.split(" - ")[0].split("- ")[0].strip().lower()

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("centroid")
        d["platforms"] = sorted(self.platforms)
        return d


class ServiceCatalog:
    def __init__(self, items: list[CatalogItem]) -> None:
        self.items = items
        self.by_page = {i.page_id: i for i in items}
        self.matrix = np.vstack([i.centroid for i in items]) if items else np.zeros((0, 1), dtype=np.float32)

    @classmethod
    def from_store(cls, store: VectorStore) -> "ServiceCatalog":
        pages: dict[str, list] = defaultdict(list)
        for chunk in store.all_chunks():
            m = chunk.metadata
            if (m.get("category") in RECOMMENDABLE and m.get("content_quality") == "full"
                    and m.get("content_type") == "page" and not _EXCLUDE_URL.search(m.get("url", ""))):
                pages[m["page_id"]].append(chunk)
        items: list[CatalogItem] = []
        for page_id, chunks in pages.items():
            vecs = store.get_embeddings([c.chunk_id for c in chunks])
            if not vecs:
                continue
            centroid = np.mean(np.vstack(list(vecs.values())), axis=0)
            centroid = centroid / (np.linalg.norm(centroid) or 1.0)
            m = chunks[0].metadata
            category, url = m["category"], m["url"]
            if category in ("Services", "Hire Developers") or url.rstrip("/").endswith(("shopify-apps", "shopify-app-development")):
                name, kind = _slug_name(url), "service"
            elif category == "Shopify Apps":
                name, kind = m["title"], "app"
            elif category == "Pricing":
                name, kind = m["title"], "package"
            else:
                name, kind = m["title"], "product"
            desc = _first_clean_sentence(chunks, m["title"], name)
            ctas = [c for c in m.get("ctas", "").split(" | ") if c]
            items.append(CatalogItem(page_id, name, url, category, kind, desc, ctas,
                                     _platforms(f"{name} {url}"), centroid.astype(np.float32)))
        logger.info("service catalog: %d items", len(items))
        return cls(items)


@dataclass
class Recommendation:
    primary: CatalogItem
    related: list[CatalogItem]
    primary_score: float

    def public(self) -> dict[str, Any]:
        def card(i: CatalogItem) -> dict[str, Any]:
            return {"name": i.name, "url": i.url, "kind": i.kind, "category": i.category,
                    "description": i.description, "cta": i.ctas[0] if i.ctas else None}
        return {"primary": card(self.primary), "related": [card(r) for r in self.related]}


class RecommendationEngine:
    def __init__(self, catalog: ServiceCatalog, embedder: Embedder, min_similarity: float = 0.55,
                 related_count: int = 2) -> None:
        self.catalog = catalog
        self.embedder = embedder
        self.min_similarity = min_similarity
        self.related_count = related_count

    def _allowed(self, item: CatalogItem, platforms: set[str]) -> bool:
        return not platforms or not item.platforms or bool(item.platforms & platforms)

    def recommend(self, query: str, answer_page_ids: list[str] | None = None) -> Recommendation | None:
        if not self.catalog.items or not query.strip():
            return None
        q = self.embedder.embed_query(query)
        sims = self.catalog.matrix @ q
        platforms = _platforms(query)

        primary_idx = None
        low_query = query.lower()
        for idx, item in enumerate(self.catalog.items):  # a product named in the question wins ("What is SMTU?")
            if len(item.family) >= 4 and re.search(rf"(?<![a-z]){re.escape(item.family)}(?![a-z])", low_query):
                primary_idx = idx
                break
        for pid in ([] if primary_idx is not None else answer_page_ids or []):
            item = self.catalog.by_page.get(pid)
            if item is not None and self._allowed(item, platforms):
                primary_idx = self.catalog.items.index(item)
                break
        if primary_idx is None:
            order = np.argsort(-sims)
            for i in order:
                if sims[i] < self.min_similarity:
                    break
                if self._allowed(self.catalog.items[i], platforms):
                    primary_idx = int(i)
                    break
        if primary_idx is None:
            return None
        primary = self.catalog.items[primary_idx]

        affinity = self.catalog.matrix @ primary.centroid
        scores = 0.5 * affinity + 0.5 * sims
        related: list[CatalogItem] = []
        families = {primary.family}
        for i in np.argsort(-scores):
            item = self.catalog.items[int(i)]
            if len(related) >= self.related_count:
                break
            if (int(i) == primary_idx or item.family in families or affinity[i] > 0.97
                    or not self._allowed(item, platforms | primary.platforms) or scores[i] < self.min_similarity):
                continue
            related.append(item)
            families.add(item.family)
        return Recommendation(primary, related, float(sims[primary_idx]))


_SEEKING = re.compile(r"\b(recommend\w*|suggest\w*|which (?:service|package|plan|option)|best (?:option|fit|service|package)|"
                      r"what should i|what do i need|right (?:service|package|platform)|help me (?:choose|decide|pick))\b", re.I)


def is_recommendation_seeking(text: str) -> bool:
    return bool(_SEEKING.search(text))


def recommendation_sentence(rec: Recommendation) -> str:
    primary = f"our {rec.primary.name}" if rec.primary.kind != "product" else rec.primary.name
    sentence = f"Based on what you've described, {primary} looks like a good fit"
    if rec.related:
        names = [r.name for r in rec.related]
        joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        sentence += f", and you may also find {joined} useful"
    return sentence + "."

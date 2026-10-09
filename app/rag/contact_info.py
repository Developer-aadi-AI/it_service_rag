"""Contact details parsed from the supplied Contact Us page.

The Contact Us page is a block of labels ("US Head Office ... Phone: ...
Email: ...") that does not read well when quoted. This module extracts the
fields with regexes and states them in plain sentences. Every value comes
from the page text; if a field is not found it is simply not mentioned.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.vectorstore.base import SearchHit

_OFFICE = re.compile(
    r"(?P<label>[A-Z][A-Za-z]+) Head Office\s+(?P<addr>.+?)\s+Phone:\s*(?P<phone>\(?\+?[\d()]+\)?[\d\s-]*\d(?:\s*Ext\.\s*\d+)?)"
    r"(?:\s+Toll-Free:\s*(?P<tollfree>[\d-]+(?:\s*Ext\.\s*\d+)?))?"
    r"(?:\s+Email:\s*(?P<email>[\w.+-]+@[\w-]+\.[\w.]+))?")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", re.I)

_CONTACT_Q = re.compile(
    r"\b(office|offices|address|located|location|where are you|where is (?:d ?group|dgroup|your)|"
    r"headquarter\w*|hq|phone|telephone|call you|call d ?group|number|email|e-mail|mail id|"
    r"contact (?:details|info\w*|you|d ?group|number)|how (?:can|do) i (?:contact|reach)|reach you|"
    r"get in touch)\b", re.I)
_NOT_CONTACT = re.compile(r"\b(hours|timing|timings|open|opening|weekend|form|policy|privacy|"
                          r"employees?|staff|team size|careers?|jobs?|marketing|newsletter|template|"
                          r"integrat\w*|build|develop\w*|app|store|website)\b", re.I)


@dataclass
class Office:
    label: str
    address: str
    phone: str = ""
    tollfree: str = ""


@dataclass
class ContactInfo:
    offices: list[Office] = field(default_factory=list)
    email: str = ""
    page_id: str = ""
    title: str = ""
    url: str = ""
    hit: SearchHit | None = None

    @property
    def available(self) -> bool:
        return bool(self.offices or self.email)


def is_contact_question(text: str) -> bool:
    return bool(_CONTACT_Q.search(text)) and not _NOT_CONTACT.search(text)


def parse_contact_page(chunks: list[SearchHit]) -> ContactInfo:
    """Find the contact-type page among the indexed chunks and parse it."""
    for hit in chunks:
        if hit.metadata.get("source_type") != "contact" or hit.metadata.get("content_type") != "page":
            continue
        text = hit.text
        info = ContactInfo(page_id=hit.metadata.get("page_id", ""), title=hit.metadata.get("title", ""),
                           url=hit.metadata.get("url", ""), hit=hit)
        for m in _OFFICE.finditer(text):
            addr = re.sub(r"\s*[–—-]\s*(\d{6})", r" \1", m.group("addr").strip(" ,"))
            info.offices.append(Office(m.group("label"), addr, m.group("phone").strip(),
                                       (m.group("tollfree") or "").strip()))
            if m.group("email") and not info.email:
                info.email = m.group("email")
        if not info.email:
            em = _EMAIL.search(text)
            info.email = em.group(0) if em else ""
        if info.available:
            return info
    return ContactInfo()


def compose_contact_answer(question: str, info: ContactInfo) -> str:
    q = question.lower()
    wants_phone = bool(re.search(r"\b(phone|telephone|call|number)\b", q))
    wants_email = bool(re.search(r"\b(email|e-mail|mail)\b", q))
    wants_address = bool(re.search(r"\b(office|address|located|location|where|headquarter\w*|hq)\b", q))
    if not (wants_phone or wants_email or wants_address):  # general "how can I contact you?"
        wants_phone = wants_email = wants_address = True

    parts: list[str] = []
    if wants_address and info.offices:
        descs = [f"our {o.label} head office is at {o.address}" for o in info.offices]
        sentence = descs[0] if len(descs) == 1 else ", and ".join([", ".join(descs[:-1]), descs[-1]])
        # A place the user named that is not in any listed address: do not claim
        # anything about it, just state what the website lists.
        named = re.findall(r"\bin ([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)?)", question)
        known = " ".join(o.address + " " + o.label for o in info.offices).lower()
        if any(n.lower() not in known for n in named):
            parts.append("Here are the offices listed on our website: " + sentence + ".")
        else:
            parts.append(sentence[0].upper() + sentence[1:] + ".")
    if wants_phone and info.offices:
        calls = []
        for o in info.offices:
            if o.phone:
                extra = f" (toll-free {o.tollfree})" if o.tollfree else ""
                calls.append(f"our {o.label} office at {o.phone}{extra}")
        if calls:
            parts.append("You can call " + " or ".join(calls) + ".")
    if wants_email and info.email:
        parts.append(f"You can also email us at {info.email}." if parts else f"You can email us at {info.email}.")
    if not parts:
        return ""
    return " ".join(parts) + " [1]\n\nIs there anything else I can help you with?"

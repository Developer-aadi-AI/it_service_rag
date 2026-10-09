"""Lightweight, deterministic intent signals used around retrieval.

These do not replace the LLM's judgement; they cover small talk (which should
not be treated as off-topic) and give a fallback for "is this something V
Group could help with?" when no LLM is configured.
"""
from __future__ import annotations

import re

_GREETING = re.compile(
    r"^\s*(hi+|hello+|hey+|hiya|good (morning|afternoon|evening)|greetings|namaste|yo)"
    r"(\s+(there|team|d ?group))?\s*[!.?]*\s*$", re.I)
_THANKS = re.compile(r"^\s*(thanks?( you)?( so much| a lot)?|thank u|thx|ty|great,? thanks?|cool,? thanks?)\s*[!.]*\s*$", re.I)
_GOODBYE = re.compile(r"^\s*(bye+|goodbye|see you|see ya|that'?s all|no,? that'?s (all|it)|nothing else)\s*[!.]*\s*$", re.I)

# The user addresses D Group itself (company, staff, offices, policies, prices...).
_ADDRESSES_COMPANY = re.compile(
    r"\b(you|your|yours|d\s?group|dgroup|d group inc|company|agency|firm|team|staff)\b", re.I)
_BUSINESS_TERMS = re.compile(
    r"\b(ceo|founder|owner|office|offices|location|address|headquarter\w*|branch|contact|email|phone|"
    r"call|price|prices|pricing|cost|costs|quote|budget|refund|policy|policies|terms|warranty|"
    r"guarantee|payment|invoice|discount|support|maintenance|service|services|project|projects|"
    r"portfolio|client|clients|career|careers|job|jobs|hiring|hire|intern\w*|partner\w*|"
    r"website|web|site|store|shop|ecommerce|e-commerce|app|apps|application|software|platform|"
    r"plugin|extension|developer|developers|development|design|seo|marketing|hosting|cloud|"
    r"integration|migration|crm|erp|api|timeline|deadline|delivery|team size|experience|"
    r"certified|certification|iso)\b", re.I)

# The user wants something done/built (a potential project / lead).
_PROJECT_REQUEST = re.compile(
    r"\b(can|could|would|will) (you|your team|d ?group)\b.*\b(build|develop|create|design|make|set ?up|"
    r"integrate|migrate|implement|customi[sz]e|redesign|launch|automate|add|connect|help)\b"
    r"|\b(i|we)\s*(?:'d|would)?\s*(want|need|like|wish|plan|am planning|are planning|'m looking|"
    r"am looking|are looking)\b.*\b(build|develop|create|design|make|app|website|site|store|"
    r"platform|system|tool|bot|chatbot|integration|marketplace|solution|software|portal)\b"
    r"|\b(looking for|in need of)\b.*\b(developer|agency|team|partner|help|someone)\b"
    r"|\bdo you (build|develop|create|design|make|offer|do|provide|work on|handle)\b",
    re.I)


def smalltalk_kind(text: str) -> str | None:
    if _GREETING.match(text):
        return "greeting"
    if _THANKS.match(text):
        return "thanks"
    if _GOODBYE.match(text):
        return "goodbye"
    return None


def is_business_related(text: str) -> bool:
    """True when the message plausibly concerns D Group or its kind of work."""
    has_business = bool(_BUSINESS_TERMS.search(text))
    return has_business and (bool(_ADDRESSES_COMPANY.search(text)) or bool(_PROJECT_REQUEST.search(text)))


def is_project_request(text: str) -> bool:
    return bool(_PROJECT_REQUEST.search(text))


_YOU = r"(?:you guys|you all|you|u|ya|y'all|d ?group inc|d ?group|dgroup|your company|your team|your agency)"
_OVERVIEW = re.compile(
    r"^\s*(?:so|ok|okay|hey|hi|hello)?[\s,]*(?:"
    rf"(?:what|wat|wht)\s+(?:do|does|exactly do|exactly does)\s+{_YOU}\s+(?:guys\s+)?(?:do|offer|provide|sell|make|work on)"
    rf"|(?:who|what)\s+(?:are|r)\s+{_YOU}"
    r"|(?:what is|what's|whats|who is)\s+(?:d ?group|dgroup|d group inc|your company)"
    r"|(?:tell me|can you tell me|could you tell me)\s+(?:more\s+)?about\s+(?:you|yourself|yourselves|your company|your services|d ?group|dgroup)"
    rf"|(?:what|which)\s+(?:kind of\s+|type of\s+)?services\s+(?:do|does)\s+{_YOU}\s+(?:offer|provide|have|do)"
    rf"|(?:what can|how can)\s+{_YOU}\s+(?:do|help)"
    r"|(?:about|services of)\s+(?:your company|d ?group|dgroup)"
    r")\b(?:\s+(?:guys|exactly|again|here|then|actually|really|in short|for me|for businesses|for a living))*"
    r"[\s?!.]*$", re.I)

OVERVIEW_QUERY = "About D Group: what services does D Group offer and what kind of company is it?"


def is_company_overview(text: str) -> bool:
    """'What do you guys do?', 'What does DGroup do?', 'Tell me about your company'..."""
    return len(text) <= 120 and bool(_OVERVIEW.match(text))


_PRODUCT_WORDS = re.compile(r"\b(shopify|magento|bigcommerce|wordpress|woocommerce|drupal|wix|squarespace|x-?cart|"
                            r"reviewcaddy|smtu|carts|android|ios|plan|package|gold|silver)\b", re.I)


def mentions_business(text: str) -> bool:
    """True when the text names a D Group-related subject (service, platform, product, policy...)."""
    return bool(_BUSINESS_TERMS.search(text) or _PRODUCT_WORDS.search(text))


_COMPANY_FACT = re.compile(
    r"\b(?:how many|how long|since when|what year|when|how old)\b.{0,40}\b(?:clients?|customers?|years?|industr\w*|"
    r"projects?|web ?stores?|stores|in business|founded|started|establish\w*|experience|portfolio)\b"
    r"|\b(?:founded|established|in business since)\b", re.I)


def is_company_fact(text: str) -> bool:
    """Questions about D Group itself: client counts, years in business, founding date..."""
    return bool(_COMPANY_FACT.search(text)) and not re.search(r"\b(plan|package|support|deliver|take)\b", text, re.I)

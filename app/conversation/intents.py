"""Conversation-level intents (on top of the RAG intents in app.rag.intent)."""
from __future__ import annotations

import re

_CONTACT_REQUEST = re.compile(
    r"\b(?:talk|speak|chat|connect|get in touch)\s+(?:to|with)\s+(?:someone|somebody|a human|a person|a real person|"
    r"an agent|a representative|your (?:team|sales|support|marketing)|(?:the\s+)?(?:sales|support|marketing)(?: team)?|"
    r"an expert|a specialist|a developer)\b"
    r"|\b(?:contact|call|email|reach)\s+me\b"
    r"|\b(?:call\s?back|callback)\b"
    r"|\b(?:request|book|schedule|get|want|need)\s+(?:a\s+)?(?:free\s+)?(?:quote|consultation|demo|call)\b"
    r"|\bhave (?:someone|somebody|your team) (?:contact|call|email|reach)\b"
    r"|\b(?:human|live) (?:agent|support|person)\b"
    r"|\bcontact (?:sales|support|marketing|your team)\b",
    re.I)

_CLOSING = (r"(?:thanks?|thank you|that'?s (?:all|it|everything)|(?:it'?s|that'?s) fine|i'?m (?:good|fine|done|all set)|"
            r"all good|all set|we'?re good|nothing(?: else)?|not really|not now|that will be all|bye|goodbye)")
_NEGATIVE_REPLY = re.compile(
    rf"^\s*(?:(?:no+|nope|nah)\b(?:[\s,.!]+{_CLOSING})*|{_CLOSING}(?:[\s,.!]+{_CLOSING})*)[\s,.!]*$", re.I)

_AFFIRMATIVE_ONLY = re.compile(
    r"^\s*(?:yes+|yeah|yep|yup|sure|ok(?:ay)?|please|yes please|i do|i have (?:one|another) (?:more )?question|"
    r"one more (?:thing|question)|actually,? yes)\s*[.!]*\s*$", re.I)


_INFO_QUESTION = re.compile(r"^\s*(?:how|what|when|where|why|which|who|is|are|does|do)\b", re.I)


def is_contact_request(text: str) -> bool:
    """Explicit request to be contacted ("call me back", "I want a quote").

    Informational questions that merely mention a quote/consultation
    ("How fast can I get a quote?") are answered from the knowledge base instead.
    """
    if _INFO_QUESTION.match(text) and not re.search(r"\b(?:talk|speak|connect)\s+(?:to|with)\b", text, re.I):
        return False
    return bool(_CONTACT_REQUEST.search(text))


def is_negative_reply(text: str) -> bool:
    return bool(_NEGATIVE_REPLY.match(text))


def is_affirmative_only(text: str) -> bool:
    return bool(_AFFIRMATIVE_ONLY.match(text))

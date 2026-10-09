"""Phase 1 evaluation helpers (kept for the existing tests; the full harness is app/evaluation/harness.py).


Metrics go beyond "HTTP 200": retrieval hit@k / MRR against pages known to
contain the answer, citation correctness, fact presence, no-answer routing and
off-topic detection.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.config import PROJECT_ROOT
from app.rag.pipeline import RAGPipeline
from app.retrieval.retriever import Retriever

EVAL_PATH = PROJECT_ROOT / "tests" / "eval" / "eval_set.json"


def load_eval_set(path: Path = EVAL_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def evaluate_retrieval(retriever: Retriever, items: list[dict], k: int = 5) -> dict[str, Any]:
    hits, rr, failures = 0, 0.0, []
    for item in items:
        expected = set(item["expected_pages"])
        ranked_pages: list[str] = []
        for h in retriever.search(item["question"], max(retriever.candidate_k, k)):
            page = h.metadata.get("page_id")
            if page not in ranked_pages:
                ranked_pages.append(page)
        top = ranked_pages[:k]
        rank = next((i + 1 for i, p in enumerate(top) if p in expected), None)
        if rank:
            hits += 1
            rr += 1.0 / rank
        else:
            failures.append({"question": item["question"], "got": top})
    n = max(1, len(items))
    return {f"hit@{k}": round(hits / n, 3), "mrr": round(rr / n, 3), "n": len(items), "failures": failures}


def evaluate_pipeline(pipeline: RAGPipeline, data: dict[str, Any]) -> dict[str, Any]:
    ans = data["answerable"]
    answered = cited_ok = fact_ok = 0
    details = []
    for item in ans:
        r = pipeline.answer(item["question"])
        cited_pages = {s.page_id for s in r.sources}
        ok_cite = bool(cited_pages & set(item["expected_pages"]))
        ok_fact = any(f.lower() in r.answer.lower() for f in item.get("expected_facts", []))
        answered += r.status == "answered"
        cited_ok += r.status == "answered" and ok_cite
        fact_ok += r.status == "answered" and ok_fact
        if not (r.status == "answered" and ok_cite):
            details.append({"question": item["question"], "status": r.status,
                            "cited": sorted(cited_pages)})
    no_ans = data["no_answer_in_domain"]
    routed = sum(pipeline.answer(i["question"]).status == "needs_team" for i in no_ans)
    off = data["off_topic"]
    off_ok = sum(pipeline.answer(q).status == "off_topic" for q in off)
    return {
        "mode": pipeline.mode,
        "answer_rate": round(answered / len(ans), 3),
        "citation_accuracy": round(cited_ok / len(ans), 3),
        "fact_recall": round(fact_ok / len(ans), 3),
        "no_answer_routed_to_team": round(routed / len(no_ans), 3),
        "off_topic_accuracy": round(off_ok / len(off), 3),
        "misses": details,
    }

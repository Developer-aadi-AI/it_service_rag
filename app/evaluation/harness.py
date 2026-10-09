"""Repeatable evaluation harness for the D Group assistant.

Runs every category of tests/eval/eval_set.json through the real system (the
configured answer mode: LLM when a key is set, offline extractive otherwise)
and measures:

* retrieval relevance        hit@k, MRR, recall of all expected pages (multi-chunk), precision@k
* answer correctness         expected fact present / all facts present (multi-chunk)
* grounding                  share of answer sentences supported by the cited pages
* citation correctness       cites an expected page; no citation markers to unknown sources
* no-answer behaviour        in-domain questions without an answer are handed to the team
* hallucination resistance   false-premise / injection probes: no forbidden claims, no ungrounded numbers
* off-topic / ambiguous      correct routing; ambiguous questions handled within allowed statuses
* follow-ups                 multi-turn conversations through the ConversationManager
* latency                    per question

Results go to reports/evaluation/<timestamp>.json + latest.json + latest.md, and
one summary line is appended to history.csv so runs can be compared over time.
"""
from __future__ import annotations

import csv
import json
import re
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import PROJECT_ROOT
from app.evaluation.legacy import load_eval_set
from app.ingestion.loader import load_documents, split_sentences
from app.rag import humanize
from app.rag.prompts import (ERROR_REPLY, INFORMATION_TEAM_REPLY, OFF_TOPIC_REPLY, PROJECT_IDEA_REPLY,
                             SMALLTALK_REPLIES)
from app.retrieval.lexical import tokenize

REPORT_DIR = PROJECT_ROOT / "reports" / "evaluation"

# Minimum acceptable values for `--check` (tuned to the offline mode; an LLM should exceed them).
THRESHOLDS = {
    "retrieval.hit_at_k": 0.90,
    "retrieval.mrr": 0.75,
    "answers.answer_rate": 0.85,
    "answers.fact_recall": 0.75,
    "answers.citation_accuracy": 0.80,
    "answers.grounding_rate": 0.90,
    "answers.invalid_citations": 0,  # maximum
    "follow_up.fact_recall": 0.60,
    "no_answer.routed_to_team": 0.80,
    "hallucination.resistance": 0.90,
    "off_topic.accuracy": 0.90,
    "ambiguous.acceptable": 0.80,
}

_FIXED = {PROJECT_IDEA_REPLY, INFORMATION_TEAM_REPLY, OFF_TOPIC_REPLY, ERROR_REPLY, *SMALLTALK_REPLIES.values()}
_TEMPLATE_PATTERNS = [re.compile(p, re.I) for p in (
    r"^Based on what you've described", r"^(I'm sorry|Sorry|I understand)\b", r"^If you'd prefer to speak",
    r"^You have \d+ more attempts?", r"^Could you tell me a bit more", r"anything else I can help you with\?$")]
_CLOSINGS = {c for options in humanize._CLOSINGS.values() for c in options}
_NUMBER = re.compile(r"[$£]?\d+(?:,\d{3})*(?:\.\d+)?%?")


@dataclass
class Corpus:
    """Page texts (incl. FAQs) for grounding checks."""

    text: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, data_path: Path) -> "Corpus":
        docs = load_documents(data_path)
        return cls({d.page_id: re.sub(r"\s+([,.;:!?])", r"\1", d.text + " " + " ".join(
            f"{f.question} {f.answer}" for f in d.faqs)) for d in docs})

    def pages(self, ids) -> str:
        return " ".join(self.text.get(i, "") for i in ids)


def claims(answer: str) -> list[str]:
    """Factual sentences of an answer (drops fixed replies, closings, acknowledgements, markers)."""
    answer = normalize(answer)
    if answer.strip() in _FIXED:
        return []
    out = []
    for para in answer.split("\n\n"):
        para = para.strip()
        if not para or para in _CLOSINGS or para in _FIXED:
            continue
        for s in split_sentences(re.sub(r"\s*\[\d{1,2}\]", "", para)):
            if s in _CLOSINGS or any(p.search(s) for p in _TEMPLATE_PATTERNS):
                continue
            out.append(s)
    return out


def supported(sentence: str, source_text: str, min_overlap: float = 0.6) -> bool:
    """A sentence is supported when most of its content words appear in the cited pages."""
    words = set(tokenize(normalize(sentence)))
    if not words:
        return True
    src = set(tokenize(normalize(source_text)))
    stems = {w[:5] for w in src}
    hit = sum(1 for w in words if w in src or w[:5] in stems)
    return hit / len(words) >= min_overlap


def ungrounded_numbers(sentences: list[str], source_text: str) -> list[str]:
    norm_src = source_text.replace(" ", "")
    bad = []
    for s in sentences:
        for n in _NUMBER.findall(s):
            core = n.strip("$£%").rstrip(".,").removesuffix(".00")
            if len(core) >= 2 and core not in norm_src:
                bad.append(n)
    return bad


def invalid_markers(answer: str, source_numbers: set[int]) -> int:
    return sum(1 for n in re.findall(r"\[(\d{1,2})\]", answer) if int(n) not in source_numbers)


def _rate(values: list[bool]) -> float:
    return round(sum(values) / len(values), 3) if values else 0.0


_SPACES = str.maketrans({"\u202f": " ", "\u00a0": " ", "\u2009": " ", "\u2011": "-", "\u2010": "-",
                         "\u2013": "-", "\u2014": "-", "\u2019": "'"})


def normalize(text: str) -> str:
    """Unicode spaces/dashes/quotes -> ASCII so '8\u202fhours' matches '8 hours'."""
    return (text or "").translate(_SPACES)


def _contains(answer: str, facts: list[str]) -> list[bool]:
    low = normalize(answer).lower()
    return [normalize(f).lower() in low for f in facts]


class Harness:
    def __init__(self, services, data: dict[str, Any] | None = None, k: int = 5, pace_s: float = 0.0) -> None:
        self.svc = services
        self.pipeline = services.manager.pipeline
        self.retriever = self.pipeline.retriever
        self.data = data or load_eval_set()
        self.k = k
        self.corpus = Corpus.load(services.settings.data_path)
        self.latencies: list[float] = []
        self.failures: list[dict[str, Any]] = []
        self.generators: dict[str, int] = {}
        self.pace_s = pace_s  # pause between questions (stay under provider rate limits)

    # -- helpers -----------------------------------------------------------------------
    def _ranked_pages(self, query: str) -> list[str]:
        pages: list[str] = []
        for h in self.retriever.search(query, max(self.retriever.candidate_k, self.k)):
            p = h.metadata.get("page_id")
            if p not in pages:
                pages.append(p)
        return pages[: self.k]

    def _answer(self, question: str):
        if self.pace_s:
            time.sleep(self.pace_s)
        start = time.perf_counter()
        r = self.pipeline.answer(question)
        self.latencies.append((time.perf_counter() - start) * 1000)
        self.generators[r.generator or "none"] = self.generators.get(r.generator or "none", 0) + 1
        return r

    def _fail(self, category: str, question: str, reason: str, **extra) -> None:
        if "answer" in extra:
            extra["answer"] = normalize(extra["answer"])[:200]
        self.failures.append({"category": category, "question": question, "reason": reason, **extra})

    def _judge_answer(self, category: str, question: str, r, expected_pages, facts, facts_all=None) -> dict:
        cited = [s.page_id for s in r.sources]
        src_text = self.corpus.pages(cited)
        sents = claims(r.answer) if r.status == "answered" else []
        support = [supported(s, src_text) for s in sents]
        res = {
            "answered": r.status == "answered",
            "fact": r.status == "answered" and any(_contains(r.answer, facts)),
            "all_facts": r.status == "answered" and all(_contains(r.answer, facts_all or facts[:1])),
            "citation": r.status == "answered" and bool(set(cited) & set(expected_pages)),
            "grounded": support,
            "invalid": invalid_markers(r.answer, {s.number for s in r.sources}),
        }
        if not res["answered"]:
            self._fail(category, question, f"not answered (status={r.status})", answer=r.answer)
        elif not res["citation"]:
            self._fail(category, question, "cited page does not contain the answer", cited=cited, answer=r.answer)
        elif not res["fact"]:
            self._fail(category, question, "expected fact missing from answer", expected=facts, answer=r.answer)
        if support and not all(support):
            self._fail(category, question, "answer sentence not supported by cited pages",
                       unsupported=[s for s, ok in zip(sents, support) if not ok][:2])
        return res

    # -- categories ----------------------------------------------------------------------
    def retrieval(self) -> dict[str, Any]:
        hits, rr, recall_all, precision = [], [], [], []
        items = list(self.data["answerable"])
        for it in items:
            ranked = self._ranked_pages(it["question"])
            exp = set(it["expected_pages"])
            rank = next((i + 1 for i, p in enumerate(ranked) if p in exp), None)
            hits.append(rank is not None)
            rr.append(1 / rank if rank else 0.0)
            precision.append(sum(p in exp for p in ranked) / max(1, len(ranked)))
            if it.get("category") == "multi_chunk":
                recall_all.append(any(p in ranked for p in exp))
            if rank is None:
                self._fail("retrieval", it["question"], "no expected page in top-k", got=ranked)
        return {"hit_at_k": _rate(hits), "mrr": round(statistics.mean(rr), 3), "k": self.k,
                "precision_at_k": round(statistics.mean(precision), 3), "n": len(items),
                "multi_chunk_page_recall": _rate(recall_all)}

    def answers(self) -> dict[str, Any]:
        per_cat: dict[str, list[dict]] = {}
        for it in self.data["answerable"]:
            r = self._answer(it["question"])
            res = self._judge_answer(it.get("category", "factual"), it["question"], r, it["expected_pages"],
                                     it["expected_facts"], it.get("expected_facts_all"))
            per_cat.setdefault(it.get("category", "factual"), []).append(res)
        all_res = [x for v in per_cat.values() for x in v]
        grounding = [g for x in all_res for g in x["grounded"]]

        def block(rs):
            return {"n": len(rs), "answer_rate": _rate([x["answered"] for x in rs]),
                    "fact_recall": _rate([x["fact"] for x in rs]),
                    "citation_accuracy": _rate([x["citation"] for x in rs]),
                    "all_facts": _rate([x["all_facts"] for x in rs])}

        return {**block(all_res), "grounding_rate": _rate(grounding),
                "fully_grounded_answers": _rate([all(x["grounded"]) for x in all_res if x["answered"]]),
                "invalid_citations": sum(x["invalid"] for x in all_res),
                "by_category": {c: block(rs) for c, rs in per_cat.items()}}

    def follow_ups(self) -> dict[str, Any]:
        results, rewritten = [], []
        for it in self.data.get("follow_up", []):
            sid = self.svc.manager.start_session().session.session_id
            turn = None
            for text in it["turns"]:
                if self.pace_s:
                    time.sleep(self.pace_s)
                turn = self.svc.manager.handle_message(sid, text)
                gen = turn.rag.generator if turn.rag else "rules"
                self.generators[gen or "none"] = self.generators.get(gen or "none", 0) + 1
            r = turn.rag
            rewritten.append(bool(r and r.search_query != it["turns"][-1]))
            if r is None:
                self._fail("follow_up", " -> ".join(it["turns"]), f"no answer (status={turn.status})")
                results.append({"answered": False, "fact": False, "citation": False, "grounded": [], "invalid": 0,
                                "all_facts": False})
                continue
            results.append(self._judge_answer("follow_up", " -> ".join(it["turns"]), r, it["expected_pages"],
                                              it["expected_facts"]))
            self.svc.manager.close_by_user(sid)
        return {"n": len(results), "fact_recall": _rate([x["fact"] for x in results]),
                "citation_accuracy": _rate([x["citation"] for x in results]),
                "rewrite_rate": _rate(rewritten),
                "grounding_rate": _rate([g for x in results for g in x["grounded"]])}

    def no_answer(self) -> dict[str, Any]:
        routed, kind_ok = [], []
        for it in self.data["no_answer_in_domain"]:
            r = self._answer(it["question"])
            routed.append(r.status == "needs_team")
            kind_ok.append(r.status == "needs_team" and r.request_kind == it.get("kind"))
            if r.status != "needs_team":
                self._fail("no_answer", it["question"], f"expected team hand-off, got {r.status}",
                           answer=r.answer[:160])
        return {"n": len(routed), "routed_to_team": _rate(routed), "request_kind_accuracy": _rate(kind_ok)}

    def hallucination(self) -> dict[str, Any]:
        safe = []
        for it in self.data.get("hallucination_probes", []):
            r = self._answer(it["question"])
            sents = claims(r.answer) if r.status == "answered" else []
            src = self.corpus.pages(s.page_id for s in r.sources)
            forbidden = [p for p, hit in zip(it.get("must_not_contain", []),
                                             _contains(r.answer, it.get("must_not_contain", []))) if hit]
            numbers = ungrounded_numbers(sents, src)
            unsupported = [s for s in sents if not supported(s, src)]
            ok = not forbidden and not numbers and not unsupported
            safe.append(ok)
            if not ok:
                self._fail("hallucination", it["question"], "possible hallucination",
                           forbidden=forbidden, numbers=numbers, unsupported=unsupported[:2], answer=r.answer)
        return {"n": len(safe), "resistance": _rate(safe)}

    def off_topic(self) -> dict[str, Any]:
        ok = []
        for q in self.data["off_topic"]:
            r = self._answer(q)
            ok.append(r.status == "off_topic")
            if r.status != "off_topic":
                self._fail("off_topic", q, f"expected off_topic, got {r.status}")
        return {"n": len(ok), "accuracy": _rate(ok)}

    def ambiguous(self) -> dict[str, Any]:
        ok = []
        for it in self.data.get("ambiguous", []):
            r = self._answer(it["question"])
            sents = claims(r.answer) if r.status == "answered" else []
            src = self.corpus.pages(s.page_id for s in r.sources)
            good = (r.status in it["allowed_status"] and not ungrounded_numbers(sents, src)
                    and all(supported(s, src) for s in sents))
            if good and r.status == "answered" and it.get("expected_facts"):
                good = any(_contains(r.answer, it["expected_facts"]))
            ok.append(good)
            if not good:
                self._fail("ambiguous", it["question"], f"status={r.status}", answer=r.answer[:160])
        return {"n": len(ok), "acceptable": _rate(ok)}

    # -- run ------------------------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        started = time.perf_counter()
        report = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "mode": self.pipeline.mode,
            "dataset_version": self.data.get("version", 1),
            "retrieval": self.retrieval(),
            "answers": self.answers(),
            "follow_up": self.follow_ups(),
            "no_answer": self.no_answer(),
            "hallucination": self.hallucination(),
            "off_topic": self.off_topic(),
            "ambiguous": self.ambiguous(),
        }
        lat = sorted(self.latencies)
        report["latency_ms"] = {"mean": round(statistics.mean(lat), 1) if lat else 0,
                                "p95": round(lat[int(0.95 * (len(lat) - 1))], 1) if lat else 0,
                                "questions": len(lat)}
        report["duration_s"] = round(time.perf_counter() - started, 1)
        # Which component produced the answers: with an LLM configured, a low "llm" share means the
        # provider failed (e.g. rate limits) and the offline fallback answered instead.
        gen_total = sum(v for k, v in self.generators.items() if k in ("llm", "extractive"))
        report["generators"] = {**self.generators,
                                "llm_answer_share": round(self.generators.get("llm", 0) / gen_total, 3) if gen_total else 0.0}
        report["checks"] = check(report)
        report["failures"] = self.failures
        return report


def _get(report: dict, dotted: str):
    cur: Any = report
    for part in dotted.split("."):
        cur = cur[part]
    return cur


def check(report: dict, thresholds: dict[str, float] = THRESHOLDS) -> dict[str, dict]:
    out = {}
    for key, limit in thresholds.items():
        value = _get(report, key)
        passed = value <= limit if key.endswith("invalid_citations") else value >= limit
        out[key] = {"value": value, "threshold": limit, "passed": passed}
    return out


def to_markdown(report: dict) -> str:
    r, a, f, n, h, o, amb = (report[k] for k in ("retrieval", "answers", "follow_up", "no_answer", "hallucination",
                                                  "off_topic", "ambiguous"))
    lines = [
        f"# Evaluation report - {report['timestamp']}",
        "",
        f"Mode: `{report['mode']}` | dataset v{report['dataset_version']} | {report['latency_ms']['questions']} questions "
        f"| mean latency {report['latency_ms']['mean']} ms (p95 {report['latency_ms']['p95']} ms) | {report['duration_s']} s",
        "",
        f"Answer generators: {report.get('generators', {})}",
        "",
        "| Area | Metric | Value |",
        "|---|---|---|",
        f"| Retrieval ({r['n']}) | hit@{r['k']} / MRR / precision@{r['k']} | {r['hit_at_k']} / {r['mrr']} / {r['precision_at_k']} |",
        f"| Answers ({a['n']}) | answer rate / fact recall / citation accuracy | {a['answer_rate']} / {a['fact_recall']} / {a['citation_accuracy']} |",
        f"| Grounding | supported sentences / fully grounded answers / invalid citations | {a['grounding_rate']} / {a['fully_grounded_answers']} / {a['invalid_citations']} |",
        *[f"| - {c} ({b['n']}) | answer / fact / citation / all facts | {b['answer_rate']} / {b['fact_recall']} / {b['citation_accuracy']} / {b['all_facts']} |"
          for c, b in a["by_category"].items()],
        f"| Follow-ups ({f['n']}) | fact recall / citation / rewritten | {f['fact_recall']} / {f['citation_accuracy']} / {f['rewrite_rate']} |",
        f"| No-answer ({n['n']}) | routed to team / correct request kind | {n['routed_to_team']} / {n['request_kind_accuracy']} |",
        f"| Hallucination probes ({h['n']}) | resistance | {h['resistance']} |",
        f"| Off-topic ({o['n']}) | accuracy | {o['accuracy']} |",
        f"| Ambiguous ({amb['n']}) | acceptable handling | {amb['acceptable']} |",
        "",
        "## Threshold checks",
        "",
        *[f"- {'PASS' if c['passed'] else 'FAIL'} `{k}` = {c['value']} (threshold {c['threshold']})"
          for k, c in report["checks"].items()],
        "",
        f"## Failures ({len(report['failures'])})",
        "",
        *[f"- **{x['category']}** - {x['question']}: {x['reason']}"
          + (f" ({', '.join(f'{k}={v}' for k, v in x.items() if k not in ('category', 'question', 'reason'))})"
             if len(x) > 3 else "") for x in report["failures"]],
    ]
    return "\n".join(lines) + "\n"


def write_reports(report: dict, directory: Path = REPORT_DIR) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = report["timestamp"].replace(":", "").replace("-", "")[:15]
    paths = {"json": directory / f"eval-{stamp}.json", "latest_json": directory / "latest.json",
             "latest_md": directory / "latest.md", "history": directory / "history.csv"}
    mode = "llm" if str(report.get("mode", "")).startswith("llm") else "offline"
    paths["mode_md"] = directory / f"latest-{mode}.md"
    paths["mode_json"] = directory / f"latest-{mode}.json"
    payload = json.dumps(report, indent=2, ensure_ascii=False)
    markdown = to_markdown(report)
    paths["json"].write_text(payload, encoding="utf-8")
    for key in ("latest_json", "mode_json"):
        paths[key].write_text(payload, encoding="utf-8")
    for key in ("latest_md", "mode_md"):
        paths[key].write_text(markdown, encoding="utf-8")
    new = not paths["history"].exists()
    with paths["history"].open("a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        cols = ["timestamp", "mode", *THRESHOLDS.keys(), "latency_mean_ms", "all_passed"]
        if new:
            w.writerow(cols)
        w.writerow([report["timestamp"], report["mode"], *[_get(report, k) for k in THRESHOLDS],
                    report["latency_ms"]["mean"], all(c["passed"] for c in report["checks"].values())])
    return paths

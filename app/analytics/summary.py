"""Analytics summaries from recorded events, leads, feedback and the email log.

    python -m app.analytics --days 7            # text report
    python -m app.analytics --days 30 --json    # machine-readable
    GET /admin/analytics?days=7                 # API (X-Admin-Key header)
"""
from __future__ import annotations

import json
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any


def _pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))], 1)


def summarize(db, since: datetime | None = None, until: datetime | None = None, top_n: int = 10) -> dict[str, Any]:
    until = until or datetime.now(timezone.utc)
    since = since or until - timedelta(days=7)
    window = (since.isoformat(), until.isoformat())

    rows = db.query("SELECT event_type, session_id, data, created_at FROM events "
                    "WHERE created_at >= ? AND created_at <= ? ORDER BY id", window)
    events: dict[str, list[dict]] = {}
    for r in rows:
        d = json.loads(r["data"] or "{}")
        d["_session"], d["_at"] = r["session_id"], r["created_at"]
        events.setdefault(r["event_type"], []).append(d)

    # -- messages (chat turns + stateless /ask) --------------------------------------
    turns = events.get("message", []) + events.get("ask", [])
    statuses = Counter(t.get("status", "unknown") for t in turns)
    questions = [t for t in turns if t.get("status") in ("answered", "needs_team", "off_topic")]
    in_domain = statuses["answered"] + statuses["needs_team"]
    latencies = [float(t["latency_ms"]) for t in turns if t.get("latency_ms") is not None]
    top_questions = Counter(t["question"] for t in questions if t.get("question"))
    sentiments = Counter(t.get("sentiment") for t in events.get("message", []) if t.get("sentiment"))

    # -- sessions ------------------------------------------------------------------------
    closed = events.get("session_closed", [])
    durations = [float(c["duration_s"]) for c in closed if c.get("duration_s") is not None]
    reasons = Counter(c.get("reason") for c in closed)
    irrelevant_sessions = {t["_session"] for t in events.get("message", []) if t.get("status") == "off_topic"}

    # -- recommendations --------------------------------------------------------------------
    recs = events.get("recommendation", [])
    primary = Counter(r.get("primary") for r in recs if r.get("primary"))
    related = Counter(name for r in recs for name in r.get("related", []))

    # -- leads / feedback / email / errors ----------------------------------------------------
    leads = db.query("SELECT interest, source FROM leads WHERE created_at >= ? AND created_at <= ?", window)
    feedback = db.query("SELECT rating, channel FROM feedback WHERE created_at >= ? AND created_at <= ?", window)
    ratings = Counter(f["rating"] for f in feedback)
    emails = db.query("SELECT email_type, status FROM email_log WHERE created_at >= ? AND created_at <= ?", window)
    errors = events.get("error", [])

    return {
        "period": {"from": window[0], "to": window[1]},
        "sessions": {
            "started": len(events.get("session_started", [])),
            "closed": len(closed),
            "avg_duration_s": round(statistics.mean(durations), 1) if durations else 0.0,
            "median_duration_s": round(statistics.median(durations), 1) if durations else 0.0,
            "avg_messages_per_session": round(statistics.mean([c.get("user_messages", 0) for c in closed]), 1) if closed else 0.0,
            "close_reasons": dict(reasons),
            "sessions_with_irrelevant_input": len(irrelevant_sessions),
            "ended_by_irrelevant_limit": reasons.get("irrelevant_limit", 0),
            "ended_by_inactivity": reasons.get("idle_timeout", 0),
        },
        "questions": {
            "total": len(turns),
            "by_status": dict(statuses),
            "answer_rate_pct": _pct(statuses["answered"], len(questions)),
            "no_answer_rate_pct": _pct(statuses["needs_team"], in_domain),
            "off_topic_rate_pct": _pct(statuses["off_topic"], len(questions)),
            "avg_latency_ms": round(statistics.mean(latencies), 1) if latencies else 0.0,
            "p95_latency_ms": _p95(latencies),
            "top_questions": [{"question": q, "count": n} for q, n in top_questions.most_common(top_n)],
            "sentiment": dict(sentiments),
        },
        "recommendations": {
            "shown": len(recs),
            "top_primary": [{"service": s, "count": n} for s, n in primary.most_common(top_n)],
            "top_related": [{"service": s, "count": n} for s, n in related.most_common(top_n)],
        },
        "leads": {
            "submitted": len(leads),
            "by_source": dict(Counter(l["source"] for l in leads)),
            "by_interest": [{"service": s or "(not specified)", "count": n}
                            for s, n in Counter(l["interest"] for l in leads).most_common(top_n)],
        },
        "feedback": {
            "responses": len(feedback),
            "ratings": {r: ratings.get(r, 0) for r in ("Great", "OK", "Poor")},
            "satisfaction_pct": _pct(ratings.get("Great", 0), len(feedback)),
            "by_channel": dict(Counter(f["channel"] for f in feedback)),
        },
        "emails": {
            "sent": sum(1 for e in emails if e["status"] == "sent"),
            "failed": sum(1 for e in emails if e["status"] != "sent"),
            "by_type": dict(Counter(e["email_type"] for e in emails)),
        },
        "errors": {
            "total": len(errors),
            "by_component": dict(Counter(e.get("component") for e in errors)),
            "by_type": dict(Counter(e.get("error_type") for e in errors)),
        },
    }


def format_text(summary: dict[str, Any]) -> str:
    s, q, r, l, f, e, err = (summary[k] for k in ("sessions", "questions", "recommendations", "leads",
                                                   "feedback", "emails", "errors"))
    lines = [
        "D Group assistant - analytics summary",
        f"Period: {summary['period']['from'][:19]} -> {summary['period']['to'][:19]} (UTC)",
        "",
        "Sessions",
        f"  started {s['started']} | closed {s['closed']} | avg duration {s['avg_duration_s']}s "
        f"(median {s['median_duration_s']}s) | avg user messages {s['avg_messages_per_session']}",
        f"  close reasons: {s['close_reasons'] or '-'}",
        f"  sessions with irrelevant input: {s['sessions_with_irrelevant_input']} "
        f"(ended by the limit: {s['ended_by_irrelevant_limit']}, by inactivity: {s['ended_by_inactivity']})",
        "",
        "Questions",
        f"  total {q['total']} | answered {q['answer_rate_pct']}% | no-answer (team hand-off) {q['no_answer_rate_pct']}% "
        f"| off-topic {q['off_topic_rate_pct']}%",
        f"  latency avg {q['avg_latency_ms']} ms | p95 {q['p95_latency_ms']} ms | sentiment {q['sentiment'] or '-'}",
        "  top questions:",
        *[f"    {i + 1:>2}. ({t['count']}) {t['question']}" for i, t in enumerate(q["top_questions"])],
        "",
        "Recommendations",
        f"  shown {r['shown']} | top: " + (", ".join(f"{x['service']} ({x['count']})" for x in r["top_primary"][:5]) or "-"),
        "",
        "Leads",
        f"  submitted {l['submitted']} | by interest: "
        + (", ".join(f"{x['service']} ({x['count']})" for x in l["by_interest"][:5]) or "-"),
        "",
        "Feedback",
        f"  responses {f['responses']} | Great {f['ratings']['Great']} | OK {f['ratings']['OK']} | "
        f"Poor {f['ratings']['Poor']} | satisfaction {f['satisfaction_pct']}%",
        "",
        "Email",
        f"  sent {e['sent']} | failed {e['failed']} | by type {e['by_type'] or '-'}",
        "",
        "Errors",
        f"  total {err['total']} | by component {err['by_component'] or '-'}",
    ]
    return "\n".join(lines)

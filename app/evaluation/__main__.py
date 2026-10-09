"""CLI: python -m app.evaluation [--check] [--no-write] [--k 5]

Uses the configured answer mode (set an LLM key in .env to evaluate LLM answers).
Emails go to an in-memory mailbox and storage to a temporary folder, so running the
evaluation never sends mail or touches production data.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from app.config import get_settings
from app.email.senders import MemoryEmailSender
from app.evaluation.harness import Harness, to_markdown, write_reports
from app.logging_config import setup_logging
from app.rag.pipeline import build_pipeline
from app.services import build_services


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the D Group RAG assistant")
    parser.add_argument("--check", action="store_true", help="exit 1 if any metric is below its threshold")
    parser.add_argument("--no-write", action="store_true", help="do not write reports/evaluation/*")
    parser.add_argument("--k", type=int, default=5, help="retrieval cut-off for hit@k / MRR")
    parser.add_argument("--json", action="store_true", help="print the JSON report instead of markdown")
    parser.add_argument("--pace", type=float, default=0.0,
                        help="seconds to wait between questions (e.g. 15 for rate-limited LLM tiers)")
    args = parser.parse_args()

    settings = get_settings()
    setup_logging("WARNING")
    bundle = build_pipeline(settings)
    tmp = Path(tempfile.mkdtemp(prefix="dgroup-eval-"))
    eval_settings = settings.model_copy(update={"storage_dir": tmp, "analytics_enabled": False,
                                                "idle_monitor_enabled": False, "save_transcripts_on_close": False})
    services = build_services(eval_settings, bundle, email_sender=MemoryEmailSender(), background_email=False)
    try:
        report = Harness(services, k=args.k, pace_s=args.pace).run()
    finally:
        services.shutdown()
    print(json.dumps(report, indent=2, ensure_ascii=False) if args.json else to_markdown(report))
    if not args.no_write:
        paths = write_reports(report)
        print(f"\nReports: {paths['latest_md']} (history: {paths['history']})", file=sys.stderr)
    if args.check and not all(c["passed"] for c in report["checks"].values()):
        sys.exit(1)


if __name__ == "__main__":
    main()

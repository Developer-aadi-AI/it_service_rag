"""CLI: python -m app.analytics [--days 7] [--json]"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone

from app.analytics.summary import format_text, summarize
from app.config import get_settings
from app.storage.database import Database


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarise D Group assistant analytics")
    parser.add_argument("--days", type=float, default=7.0, help="look-back window in days (default 7)")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    parser.add_argument("--db", help="path to the SQLite database (default: DATABASE_PATH / storage/dgroup.db)")
    args = parser.parse_args()
    settings = get_settings()
    from pathlib import Path

    db_path = Path(args.db) if args.db else settings.db_path
    if not db_path.exists():
        raise SystemExit(f"no analytics database at {db_path} (start the API and chat first)")
    db = Database(db_path)
    until = datetime.now(timezone.utc)
    summary = summarize(db, until - timedelta(days=args.days), until)
    print(json.dumps(summary, indent=2, ensure_ascii=False) if args.json else format_text(summary))


if __name__ == "__main__":
    main()

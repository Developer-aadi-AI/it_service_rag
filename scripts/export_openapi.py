"""Write the OpenAPI schema to docs/openapi.json:  python scripts/export_openapi.py"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

spec = create_app(Settings(_env_file=None)).openapi()
out = Path(__file__).resolve().parent.parent / "docs" / "openapi.json"
out.write_text(json.dumps(spec, indent=2), encoding="utf-8")
print(f"wrote {out} ({len(spec['paths'])} paths)")

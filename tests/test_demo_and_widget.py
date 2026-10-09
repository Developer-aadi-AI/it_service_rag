"""Demo scenarios (pre-demo smoke test) and the web widget contract."""
from __future__ import annotations

import re

from app.config import PROJECT_ROOT
from tests.conftest import make_client

WIDGET = PROJECT_ROOT / "app" / "static" / "widget"


def test_all_demo_scenarios_pass(real_bundle, real_settings):
    from app.demo import SCENARIOS, run

    lines, failed = run(bundle=real_bundle, settings=real_settings)
    text = "\n".join(lines)
    assert failed == 0, [l for l in lines if l.startswith("**Check:** FAIL")]
    assert text.count("**Check:** PASS") == len(SCENARIOS) == 10


def test_widget_is_served_and_csp_safe(make_services):
    svc = make_services(idle_monitor_enabled=False)
    with make_client(svc) as c:
        assert c.get("/", follow_redirects=False).headers["location"] == "/widget/"
        for name in ("", "widget.js", "widget.css", "notification-sounds.js"):
            assert c.get(f"/widget/{name}").status_code == 200
    html = (WIDGET / "index.html").read_text(encoding="utf-8")
    assert "<script>" not in html and "style=" not in html and "onclick" not in html  # CSP: no inline code
    js = (WIDGET / "widget.js").read_text(encoding="utf-8")
    assert "innerHTML" not in js  # user/assistant text rendered with textContent only


def test_widget_only_calls_existing_endpoints(make_services):
    js = (WIDGET / "widget.js").read_text(encoding="utf-8")
    called = set(re.findall(r'api\("(GET|POST|PATCH)",\s*"(/[a-z/]+)', js))
    svc = make_services(idle_monitor_enabled=False)
    with make_client(svc) as c:
        paths = c.get("/openapi.json").json()["paths"]
    templates = {re.sub(r"\{[^}]+\}", "", p).rstrip("/") for p in paths}
    for method, prefix in called:
        assert any(t.startswith(prefix.rstrip("/")) for t in templates), (method, prefix)
    assert {"/sessions", "/sessions/"} & {p for _, p in called}

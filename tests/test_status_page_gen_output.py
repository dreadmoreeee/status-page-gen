import json
import os
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone

import pytest

from status_page_gen import cli
from status_page_gen.build import build, collect, record
from status_page_gen.checks import Result
from status_page_gen.config import parse_config
from status_page_gen.history import DayStat, History
from status_page_gen.incidents import IncidentError, load_incidents, parse_incident, render_markdown
from status_page_gen.render import (
    overall_status, render_atom, render_html, render_json, uptime_cells, window_uptime,
)

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


def res(cid, ts, status="up", latency=100, detail="HTTP 200"):
    return Result(cid, ts, status, latency, detail)


# --- history -------------------------------------------------------------------

def test_history_append_and_read(tmp_path):
    with History(str(tmp_path / "sub" / "h.db")) as h:
        h.append([res("a", "2026-09-29T10:00:00Z"), res("b", "2026-09-29T10:00:01Z", "down", None, "boom")])
        h.append([res("a", "2026-09-29T10:05:00Z", "degraded", 4000, "slow")])
        rows = h.read("a")
        assert [r.ts for r in rows] == ["2026-09-29T10:05:00Z", "2026-09-29T10:00:00Z"]
        assert h.read("a", limit=1)[0].status == "degraded"
        latest = h.latest()
        assert latest["a"].status == "degraded" and latest["b"].latency_ms is None
        assert latest["b"].detail == "boom"
    with History(str(tmp_path / "sub" / "h.db")) as h2:  # persisted
        assert len(h2.read("a")) == 2


def test_history_daily_and_prune():
    with History(":memory:") as h:
        h.append([
            res("a", "2026-09-27T23:59:59Z"),
            res("a", "2026-09-28T00:00:00Z"),
            res("a", "2026-09-28T01:00:00Z", "down", None),
            res("a", "2026-09-28T02:00:00Z", "degraded"),
            res("a", "2026-09-28T03:00:00Z"),
        ])
        daily = h.daily("2026-09-28")
        assert list(daily["a"]) == ["2026-09-28"]
        assert daily["a"]["2026-09-28"] == DayStat(total=4, down=1, degraded=1)
        assert daily["a"]["2026-09-28"].uptime_pct == 75.0
        assert h.prune("2026-09-28T00:00:00Z") == 1
        assert len(h.read("a")) == 4


# --- uptime bars ---------------------------------------------------------------

def test_uptime_cells_one_per_day_oldest_first_with_neutral_gaps():
    stats = {"2026-09-29": DayStat(10, 0, 0), "2026-09-27": DayStat(200, 2, 0), "2026-09-26": DayStat(10, 5, 1)}
    cells = uptime_cells(stats, date(2026, 9, 29), 5)
    assert [c.day for c in cells] == ["2026-09-25", "2026-09-26", "2026-09-27", "2026-09-28", "2026-09-29"]
    assert [c.css for c in cells] == ["nd", "bad", "warn", "nd", "ok"]
    assert cells[0].title == "2026-09-25: no data" and cells[0].uptime_pct is None
    assert cells[2].title == "2026-09-27: 99.00% uptime (200 checks, 2 down, 0 degraded)"


def test_window_uptime_ignores_days_outside_window():
    stats = {"2026-09-29": DayStat(10, 1, 0), "2026-09-20": DayStat(10, 0, 0), "2026-01-01": DayStat(100, 100, 0)}
    assert window_uptime(stats, date(2026, 9, 29), 10) == pytest.approx(95.0)
    assert window_uptime({}, date(2026, 9, 29), 10) is None


def test_overall_status():
    assert overall_status(["up", "up"])[1] == "All systems operational"
    assert overall_status(["up", "degraded"]) == ("degraded", "Degraded performance")
    assert overall_status(["up", "down"]) == ("down", "Partial outage")
    assert overall_status(["down", "down"]) == ("down", "Major outage")
    assert overall_status(["unknown", "unknown"])[0] == "unknown"
    assert overall_status(["unknown", "up"])[0] == "up"


# --- markdown / incidents ------------------------------------------------------

def test_markdown_escapes_html_and_supports_subset():
    out = render_markdown(
        "# Title\n\nHello <b>x</b> **bold** and *it* and `a<b`.\n\n- one\n- two [ok](https://example.com/?a=1&b=2)\n"
    )
    assert "<b>" not in out
    assert "&lt;b&gt;x&lt;/b&gt;" in out
    assert "<h3>Title</h3>" in out
    assert "<strong>bold</strong>" in out and "<em>it</em>" in out
    assert "<code>a&lt;b</code>" in out
    assert out.count("<li>") == 2
    assert 'href="https://example.com/?a=1&amp;b=2"' in out


def test_markdown_blocks_dangerous_links():
    out = render_markdown('[x](javascript:alert(1)) [y](https://ok.example/"onmouseover="z)')
    assert 'href="javascript' not in out
    assert "onmouseover" not in out.replace("&quot;onmouseover", "")
    assert '<a href="https://ok.example/&quot;' in out


def test_markdown_nul_bytes_do_not_forge_link_tokens():
    assert "<a" not in render_markdown("x \x000\x00 y")


def test_parse_incident_variants():
    inc = parse_incident(
        '---\ntitle: "Db down"\ndate: 2026-09-20 14:30\nstatus: Resolved\naffected: [a, "b"]\n---\nBody here\n', "x")
    assert (inc.id, inc.title, inc.status, inc.affected) == ("x", "Db down", "resolved", ["a", "b"])
    assert inc.date == datetime(2026, 9, 20, 14, 30, tzinfo=timezone.utc)
    assert inc.body == "Body here"
    inc2 = parse_incident("---\ntitle: T\ndate: 2026-09-20T10:00:00+02:00\naffected: a, b\n---\n", "y")
    assert inc2.date == datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)
    assert inc2.affected == ["a", "b"] and inc2.status == "investigating"


@pytest.mark.parametrize("text", ["no front matter", "---\ntitle: T\n", "---\ndate: 2026-01-01\n---\n",
                                  "---\ntitle: T\ndate: nope\n---\n"])
def test_parse_incident_errors(text):
    with pytest.raises(IncidentError):
        parse_incident(text, "x")


def test_load_incidents_sorted_and_bad_files_skipped(tmp_path):
    (tmp_path / "a.md").write_text("---\ntitle: Old\ndate: 2026-01-01\n---\nx", encoding="utf-8")
    (tmp_path / "b.md").write_text("---\ntitle: New\ndate: 2026-03-01\n---\nx", encoding="utf-8")
    (tmp_path / "bad.md").write_text("garbage", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")
    warnings = []
    incs = load_incidents(str(tmp_path), warnings.append)
    assert [i.title for i in incs] == ["New", "Old"]
    assert len(warnings) == 1 and "bad.md" in warnings[0]
    assert load_incidents(str(tmp_path / "missing")) == []


# --- full build ----------------------------------------------------------------

@pytest.fixture()
def site(tmp_path):
    inc = tmp_path / "incidents"
    inc.mkdir()
    (inc / "evil.md").write_text(
        "---\ntitle: '<script>alert(1)</script> & co'\ndate: 2026-09-28 08:00\nstatus: investigating\n"
        "affected: web, <img src=x>\n---\nText <script>alert(2)</script> and **bold**.\n",
        encoding="utf-8")
    (inc / "ok.md").write_text(
        "---\ntitle: Fixed\ndate: 2026-09-10\nstatus: resolved\naffected: web\n---\nAll good.\n", encoding="utf-8")
    cfg = parse_config({
        "title": "Acme <Status> & co",
        "description": "desc \"quoted\" <b>",
        "output_dir": "out", "history": "h.db", "incidents_dir": "incidents", "days": 90,
        "base_url": "https://status.example.com/",
        "checks": [
            {"id": "web", "name": "Web <site>", "type": "http", "url": "https://example.com"},
            {"id": "db", "name": "Database", "type": "tcp", "host": "db.example.com:5432"},
            {"id": "new", "name": "Not yet checked", "type": "tcp", "host": "x.example.com:1"},
        ],
    }, str(tmp_path))
    record(cfg, [
        res("web", "2026-09-28T11:00:00Z", "down", None, "<script>x</script>"),
        res("web", "2026-09-29T11:00:00Z", "up", 123, "HTTP 200"),
        res("db", "2026-09-29T11:00:01Z", "down", None, "connection refused"),
    ], now=NOW)
    return cfg


def test_html_has_no_script_and_escapes_everything(site):
    paths = build(site, now=NOW)
    page = open(paths[0], encoding="utf-8").read()
    assert "<script" not in page.lower()
    assert "javascript:" not in page.lower()
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; co" in page
    assert "Acme &lt;Status&gt; &amp; co" in page
    assert "Web &lt;site&gt;" in page
    assert "<img" not in page
    assert "prefers-color-scheme:dark" in page
    assert "Partial outage" in page
    assert "Last updated 2026-09-29 12:00 UTC" in page
    assert "not checked yet" in page


def test_html_has_one_bar_per_day_per_check(site):
    page = open(build(site, now=NOW)[0], encoding="utf-8").read()
    assert page.count('<span class="d ') == 3 * 90
    assert 'title="2026-09-29: 100.00% uptime (1 checks, 0 down, 0 degraded)"' in page
    assert 'title="2026-09-28: 0.00% uptime (1 checks, 1 down, 0 degraded)"' in page
    assert 'title="2026-09-27: no data"' in page
    assert 'class="d bad"' in page and 'class="d nd"' in page and 'class="d ok"' in page


def test_json_feed_wellformed(site):
    text = open(build(site, now=NOW)[1], encoding="utf-8").read()
    doc = json.loads(text)
    assert doc["generated_at"] == "2026-09-29T12:00:00Z"
    assert doc["overall"] == {"status": "down", "label": "Partial outage"}
    by_id = {c["id"]: c for c in doc["checks"]}
    assert by_id["web"]["status"] == "up" and by_id["web"]["latency_ms"] == 123
    assert by_id["db"]["status"] == "down" and by_id["new"]["status"] == "unknown"
    assert by_id["web"]["uptime_pct"] == 50.0
    assert [i["id"] for i in doc["incidents"]] == ["evil", "ok"]
    assert doc["incidents"][0]["resolved"] is False and doc["incidents"][1]["resolved"] is True
    assert doc["incidents"][1]["affected"] == [{"id": "web", "name": "Web <site>"}]


def test_atom_feed_wellformed(site):
    text = open(build(site, now=NOW)[2], encoding="utf-8").read()
    root = ET.fromstring(text)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    assert root.tag == "{http://www.w3.org/2005/Atom}feed"
    assert root.find("a:updated", ns).text == "2026-09-29T12:00:00Z"
    entries = root.findall("a:entry", ns)
    assert len(entries) == 2
    first = entries[0]
    assert first.find("a:title", ns).text == "<script>alert(1)</script> & co"
    content = first.find("a:content", ns)
    assert content.get("type") == "html"
    assert "<script>" not in content.text  # the embedded HTML is already escaped
    ids = [e.find("a:id", ns).text for e in entries]
    assert len(set(ids)) == 2
    links = {l.get("rel"): l.get("href") for l in root.findall("a:link", ns)}
    assert links["self"] == "https://status.example.com/feed.xml"


def test_atom_without_incidents_is_still_valid(tmp_path):
    cfg = parse_config({"checks": [{"id": "a", "type": "tcp", "host": "h:1"}]}, str(tmp_path))
    record(cfg, [res("a", "2026-09-29T11:00:00Z")], now=NOW)
    paths = build(cfg, now=NOW)
    ET.fromstring(open(paths[2], encoding="utf-8").read())
    assert "No incidents reported." in open(paths[0], encoding="utf-8").read()


def test_build_without_history_shows_no_data(tmp_path):
    cfg = parse_config({"checks": [{"id": "a", "type": "tcp", "host": "h:1"}]}, str(tmp_path))
    page = open(build(cfg, now=NOW)[0], encoding="utf-8").read()
    assert "No data yet" in page
    assert page.count("no data") >= 90


def test_collect_limits_incidents(site):
    site.max_incidents = 1
    assert [i.id for i in collect(site, NOW).incidents] == ["evil"]


def test_render_functions_are_pure(site):
    data = collect(site, NOW)
    assert render_html(data) == render_html(data)
    assert render_json(data) == render_json(data)
    assert render_atom(data) == render_atom(data)


# --- CLI -----------------------------------------------------------------------

def _write_cfg(tmp_path, port):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({
        "title": "CLI", "output_dir": "out", "history": "h.db", "incidents_dir": "none",
        "checks": [{"id": "x", "name": "X", "type": "tcp", "host": f"127.0.0.1:{port}", "timeout": 1}],
    }), encoding="utf-8")
    return str(p)


def _free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_cli_check_exit_codes_and_build(tmp_path, capsys):
    import socket
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    try:
        cfg = _write_cfg(tmp_path, listener.getsockname()[1])
        assert cli.main(["check", "-c", cfg]) == 0
        assert "UP" in capsys.readouterr().out
    finally:
        listener.close()
    cfg = _write_cfg(tmp_path, _free_port())
    assert cli.main(["check", "-c", cfg, "--build"]) == 1
    assert "DOWN" in capsys.readouterr().out
    assert os.path.exists(tmp_path / "out" / "index.html")
    assert cli.main(["build", "-c", cfg]) == 0
    assert os.path.exists(tmp_path / "out" / "status.json")
    with History(str(tmp_path / "h.db")) as h:
        assert len(h.read("x")) == 2


def test_cli_bad_config_exit_2(tmp_path, capsys):
    p = tmp_path / "bad.json"
    p.write_text("{}", encoding="utf-8")
    assert cli.main(["check", "-c", str(p)]) == 2
    assert "error:" in capsys.readouterr().err
    assert cli.main(["build", "-c", str(tmp_path / "missing.yml")]) == 2


def test_module_entry_point():
    import subprocess
    import sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = subprocess.run([sys.executable, "-m", "status_page_gen", "--version"], cwd=root,
                         capture_output=True, text=True)
    assert out.returncode == 0 and "status-page-gen" in out.stdout


def test_sources_are_ascii():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for sub in ("status_page_gen", "tests", "examples"):
        for base, _dirs, files in os.walk(os.path.join(root, sub)):
            for f in files:
                if f.endswith((".py", ".yml", ".md")):
                    with open(os.path.join(base, f), "rb") as fh:
                        fh.read().decode("ascii")

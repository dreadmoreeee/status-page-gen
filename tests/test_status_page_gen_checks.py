import http.server
import shutil
import socket
import ssl
import subprocess
import threading
import time
from datetime import datetime, timezone

import pytest

from status_page_gen import checks
from status_page_gen.config import Check, parse_config


class Handler(http.server.BaseHTTPRequestHandler):
    seen_agents = []
    seen_methods = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        Handler.seen_agents.append(self.headers.get("User-Agent"))
        Handler.seen_methods.append(self.command)
        if self.path == "/ok":
            body = b"<html>Welcome to the pub. All good.</html>"
            self.send_response(200)
        elif self.path == "/missing":
            body = b"nope"
            self.send_response(404)
        elif self.path == "/slow":
            time.sleep(1.5)
            body = b"late"
            self.send_response(200)
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        else:
            body = b"other"
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def http_base():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http_check(url, **kw):
    return Check(id="c", name="c", type="http", url=url, timeout=kw.pop("timeout", 3.0), **kw)


def test_http_up_with_default_status(http_base):
    status, latency, detail = checks.run_http(http_check(http_base + "/ok"))
    assert status == "up"
    assert latency is not None and latency >= 0
    assert detail == "HTTP 200"


def test_http_sends_user_agent_and_get_only(http_base):
    Handler.seen_agents.clear()
    Handler.seen_methods.clear()
    checks.run_http(http_check(http_base + "/ok"))
    assert Handler.seen_agents == [checks.USER_AGENT]
    assert checks.USER_AGENT == "status-page-gen/1.0 (+https://github.com/dreadmoreeee/status-page-gen)"
    assert set(Handler.seen_methods) == {"GET"}


def test_http_wrong_status_is_down(http_base):
    status, latency, detail = checks.run_http(http_check(http_base + "/missing"))
    assert status == "down"
    assert latency is not None
    assert "404" in detail and "expected 200" in detail


def test_http_expected_status_can_be_404(http_base):
    status, _, _ = checks.run_http(http_check(http_base + "/missing", expect_status=404))
    assert status == "up"


def test_http_redirect_is_followed(http_base):
    status, _, detail = checks.run_http(http_check(http_base + "/redirect"))
    assert status == "up"
    assert detail == "HTTP 200"


def test_http_keyword_present(http_base):
    status, _, detail = checks.run_http(http_check(http_base + "/ok", keyword="Welcome to the pub"))
    assert status == "up"
    assert "keyword found" in detail


def test_http_keyword_missing_is_down(http_base):
    status, _, detail = checks.run_http(http_check(http_base + "/ok", keyword="Closed for renovation"))
    assert status == "down"
    assert "not found" in detail


def test_http_timeout_is_down(http_base):
    started = time.perf_counter()
    status, latency, detail = checks.run_http(http_check(http_base + "/slow", timeout=0.3))
    assert status == "down"
    assert latency is None
    assert "timed out" in detail
    assert time.perf_counter() - started < 1.4


def test_http_connection_refused_is_down():
    status, latency, detail = checks.run_http(http_check(f"http://127.0.0.1:{closed_port()}/"))
    assert status == "down"
    assert latency is None
    assert detail


def test_http_slow_is_degraded(http_base):
    status, latency, detail = checks.run_http(http_check(http_base + "/slow", degraded_ms=200.0))
    assert status == "degraded"
    assert latency >= 1400
    assert "slow" in detail


def test_tcp_up_and_down():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    port = listener.getsockname()[1]
    try:
        up = Check(id="t", name="t", type="tcp", host="127.0.0.1", port=port, timeout=2.0)
        status, latency, detail = checks.run_tcp(up)
        assert status == "up" and latency is not None
        assert f"127.0.0.1:{port}" in detail
    finally:
        listener.close()
    down = Check(id="t", name="t", type="tcp", host="127.0.0.1", port=closed_port(), timeout=2.0)
    status, latency, _ = checks.run_tcp(down)
    assert status == "down" and latency is None


def test_run_check_never_raises_and_returns_result():
    bad = Check(id="x", name="x", type="tcp", host="127.0.0.1", port=closed_port(), timeout=1.0)
    res = checks.run_check(bad)
    assert res.check_id == "x" and res.status == "down"
    assert res.ts.endswith("Z") and len(res.ts) == 20


def test_run_all_keeps_config_order(http_base):
    cs = [
        http_check(http_base + "/ok"),
        Check(id="b", name="b", type="tcp", host="127.0.0.1", port=closed_port(), timeout=1.0),
        http_check(http_base + "/missing"),
    ]
    cs[0].id, cs[2].id = "a", "c"
    for conc in (1, 3):
        res = checks.run_all(cs, conc)
        assert [r.check_id for r in res] == ["a", "b", "c"]
        assert [r.status for r in res] == ["up", "down", "down"]


# --- TLS -----------------------------------------------------------------------

def test_parse_not_after_and_days_left():
    not_after = checks.parse_not_after("Jun  1 12:00:00 2027 GMT")
    assert not_after == datetime(2027, 6, 1, 12, 0, tzinfo=timezone.utc)
    now = datetime(2027, 5, 2, 12, 0, tzinfo=timezone.utc)
    assert checks.days_left(not_after, now) == pytest.approx(30.0)


@pytest.mark.parametrize(
    "days,expected",
    [(90, "up"), (21.0, "up"), (20.9, "degraded"), (7.0, "degraded"), (6.99, "down"), (0.5, "down"), (-1, "down")],
)
def test_classify_tls_thresholds(days, expected):
    assert checks.classify_tls(days, 21, 7)[0] == expected


def test_classify_tls_details():
    assert "expired" in checks.classify_tls(-2.5, 21, 7)[1]
    assert "12 day(s)" in checks.classify_tls(12.7, 21, 7)[1]


@pytest.fixture(scope="module")
def tls_server(tmp_path_factory):
    if not shutil.which("openssl"):
        pytest.skip("openssl not available")
    d = tmp_path_factory.mktemp("tls")
    cert, key = str(d / "cert.pem"), str(d / "key.pem")
    proc = subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert,
         "-days", "30", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"],
        capture_output=True,
    )
    if proc.returncode != 0:
        pytest.skip("could not create a test certificate")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    stop = threading.Event()

    def serve():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except (socket.timeout, OSError):
                continue
            try:
                with ctx.wrap_socket(conn, server_side=True):
                    pass
            except (ssl.SSLError, OSError):
                pass

    threading.Thread(target=serve, daemon=True).start()
    yield srv.getsockname()[1], cert
    stop.set()
    srv.close()


def tls_check(port, cert, **kw):
    return Check(id="t", name="t", type="tls", host="localhost", port=port, timeout=3.0,
                 ca_file=cert, **kw)


def test_tls_local_cert_up(tls_server):
    port, cert = tls_server
    status, latency, detail = checks.run_tls(tls_check(port, cert, warn_days=21, fail_days=7))
    assert status == "up", detail
    assert latency is not None
    assert "29 day(s) left" in detail or "30 day(s) left" in detail


def test_tls_local_cert_warn_and_fail(tls_server):
    port, cert = tls_server
    assert checks.run_tls(tls_check(port, cert, warn_days=40, fail_days=7))[0] == "degraded"
    assert checks.run_tls(tls_check(port, cert, warn_days=60, fail_days=40))[0] == "down"


def test_tls_untrusted_cert_is_down(tls_server):
    port, _ = tls_server  # no ca_file: the self-signed cert must not verify
    status, _, detail = checks.run_tls(tls_check(port, ""))
    assert status == "down"
    assert "certificate" in detail.lower()


def test_tls_closed_port_is_down():
    status, latency, _ = checks.run_tls(tls_check(closed_port(), ""))
    assert status == "down" and latency is None


def test_tls_frozen_now_makes_cert_expired(tls_server):
    port, cert = tls_server
    future = datetime(2040, 1, 1, tzinfo=timezone.utc)
    status, _, detail = checks.run_tls(tls_check(port, cert), now=future)
    assert status == "down"
    assert "expired" in detail


# --- config --------------------------------------------------------------------

def test_config_defaults_and_paths(tmp_path):
    cfg = parse_config(
        {"title": "T", "checks": [
            {"id": "a", "name": "A", "type": "http", "url": "https://example.com"},
            {"id": "b", "type": "tls", "host": "example.com"},
            {"id": "c", "type": "tcp", "host": "example.com:22"},
        ]},
        str(tmp_path),
    )
    a, b, c = cfg.checks
    assert a.expect_status == 200 and a.timeout == 10.0
    assert (b.host, b.port, b.warn_days, b.fail_days) == ("example.com", 443, 21.0, 7.0)
    assert (c.host, c.port) == ("example.com", 22)
    assert cfg.output_dir == str(tmp_path / "public")


@pytest.mark.parametrize(
    "bad",
    [
        {"checks": []},
        {"checks": [{"id": "a b", "type": "http", "url": "https://x.y"}]},
        {"checks": [{"id": "a", "type": "ftp", "url": "https://x.y"}]},
        {"checks": [{"id": "a", "type": "http", "url": "ftp://x.y"}]},
        {"checks": [{"id": "a", "type": "tcp", "host": "example.com"}]},
        {"checks": [{"id": "a", "type": "tls", "host": "x.y", "warn_days": 3, "fail_days": 9}]},
        {"checks": [{"id": "a", "type": "tcp", "host": "x:1"}, {"id": "a", "type": "tcp", "host": "y:1"}]},
    ],
)
def test_config_rejects_bad_input(bad):
    from status_page_gen.config import ConfigError
    with pytest.raises(ConfigError):
        parse_config(bad)


def test_load_yaml_and_json(tmp_path):
    from status_page_gen.config import load_config
    (tmp_path / "c.yml").write_text(
        "title: Y\nchecks:\n  - {id: a, type: tcp, host: 'h:80'}\n", encoding="utf-8")
    (tmp_path / "c.json").write_text(
        '{"title": "J", "checks": [{"id": "a", "type": "tcp", "host": "h:80"}]}', encoding="utf-8")
    assert load_config(str(tmp_path / "c.yml")).title == "Y"
    assert load_config(str(tmp_path / "c.json")).title == "J"

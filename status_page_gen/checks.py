"""The three check types: http, tcp, tls. Every check is a single, polite probe."""

from __future__ import annotations

import http.client
import math
import socket
import ssl
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Check

USER_AGENT = "status-page-gen/1.0 (+https://github.com/dreadmoreeee/status-page-gen)"
UP, DEGRADED, DOWN = "up", "degraded", "down"
MAX_BODY = 1024 * 1024  # a keyword is only searched in the first MiB


@dataclass(frozen=True)
class Result:
    check_id: str
    ts: str  # UTC, e.g. 2026-09-29T14:03:11Z
    status: str
    latency_ms: int | None
    detail: str


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ms(start: float) -> int:
    return int(round((time.perf_counter() - start) * 1000))


def _slow(check: Check, latency: int, detail: str) -> tuple[str, str]:
    if check.degraded_ms and latency > check.degraded_ms:
        return DEGRADED, f"{detail}, slow ({latency} ms > {int(check.degraded_ms)} ms)"
    return UP, detail


def _err(exc: BaseException) -> str:
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "timed out"
    if isinstance(reason, ssl.SSLCertVerificationError):
        return f"certificate error: {reason.verify_message or reason}"
    text = str(reason) or reason.__class__.__name__
    return text[:200]


def run_http(check: Check) -> tuple[str, int | None, str]:
    req = urllib.request.Request(
        check.url, method="GET", headers={"User-Agent": USER_AGENT, "Accept": "*/*"}
    )
    start = time.perf_counter()
    body = b""
    try:
        try:
            with urllib.request.urlopen(req, timeout=check.timeout) as resp:
                code = resp.status
                if check.keyword:
                    body = resp.read(MAX_BODY)
        except urllib.error.HTTPError as exc:
            code = exc.code
            try:
                if check.keyword:
                    body = exc.read(MAX_BODY)
            finally:
                exc.close()
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
        return DOWN, None, _err(exc)
    latency = _ms(start)
    if code != check.expect_status:
        return DOWN, latency, f"HTTP {code}, expected {check.expect_status}"
    detail = f"HTTP {code}"
    if check.keyword:
        if check.keyword not in body.decode("utf-8", "replace"):
            return DOWN, latency, f"HTTP {code}, keyword {check.keyword!r} not found"
        detail += ", keyword found"
    status, detail = _slow(check, latency, detail)
    return status, latency, detail


def run_tcp(check: Check) -> tuple[str, int | None, str]:
    start = time.perf_counter()
    try:
        with socket.create_connection((check.host, check.port), timeout=check.timeout):
            pass
    except OSError as exc:
        return DOWN, None, _err(exc)
    latency = _ms(start)
    status, detail = _slow(check, latency, f"connected to {check.host}:{check.port}")
    return status, latency, detail


def parse_not_after(value: str) -> datetime:
    """Parse the notAfter string of ssl.getpeercert(), e.g. 'Jun  1 12:00:00 2027 GMT'."""
    return datetime.fromtimestamp(ssl.cert_time_to_seconds(value), tz=timezone.utc)


def days_left(not_after: datetime, now: datetime) -> float:
    return (not_after - now).total_seconds() / 86400.0


def classify_tls(days: float, warn_days: float, fail_days: float) -> tuple[str, str]:
    """Status and detail for a certificate with `days` left (negative = expired)."""
    if days < 0:
        return DOWN, f"certificate expired {math.ceil(-days)} day(s) ago"
    shown = math.floor(days)
    if days < fail_days:
        return DOWN, f"certificate expires in {shown} day(s) (fail below {fail_days:g})"
    if days < warn_days:
        return DEGRADED, f"certificate expires in {shown} day(s) (warn below {warn_days:g})"
    return UP, f"certificate valid, {shown} day(s) left"


def run_tls(check: Check, now: datetime | None = None) -> tuple[str, int | None, str]:
    ctx = ssl.create_default_context(cafile=check.ca_file or None)
    start = time.perf_counter()
    try:
        with socket.create_connection((check.host, check.port), timeout=check.timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=check.host) as tls:
                cert = tls.getpeercert()
    except (OSError, ssl.SSLError) as exc:
        return DOWN, None, _err(exc)
    latency = _ms(start)
    if not cert or "notAfter" not in cert:
        return DOWN, latency, "no certificate presented"
    left = days_left(parse_not_after(cert["notAfter"]), now or utc_now())
    status, detail = classify_tls(left, check.warn_days, check.fail_days)
    return status, latency, detail


_RUNNERS = {"http": run_http, "tcp": run_tcp, "tls": run_tls}


def run_check(check: Check) -> Result:
    """Run one check; never raises."""
    ts = iso(utc_now())
    try:
        status, latency, detail = _RUNNERS[check.type](check)
    except Exception as exc:  # a probe must never take the whole run down
        status, latency, detail = DOWN, None, f"internal error: {exc.__class__.__name__}"
    return Result(check.id, ts, status, latency, detail)


def run_all(checks: list[Check], concurrency: int = 1) -> list[Result]:
    """Run all checks (sequentially by default); results keep the config order."""
    if concurrency <= 1 or len(checks) <= 1:
        return [run_check(c) for c in checks]
    with ThreadPoolExecutor(max_workers=min(concurrency, len(checks))) as pool:
        return list(pool.map(run_check, checks))

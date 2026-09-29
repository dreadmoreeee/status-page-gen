"""Configuration loading (YAML or JSON) and validation."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

CHECK_TYPES = ("http", "tcp", "tls")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class ConfigError(ValueError):
    """Raised for an invalid or unreadable configuration."""


@dataclass
class Check:
    id: str
    name: str
    type: str
    timeout: float = 10.0
    degraded_ms: float | None = 3000.0
    # http
    url: str = ""
    expect_status: int = 200
    keyword: str = ""
    # tcp / tls
    host: str = ""
    port: int = 0
    # tls
    warn_days: float = 21.0
    fail_days: float = 7.0
    ca_file: str = ""


@dataclass
class Config:
    title: str
    output_dir: str
    history: str
    incidents_dir: str
    checks: list[Check]
    description: str = ""
    base_url: str = ""
    days: int = 90
    max_incidents: int = 20
    retention_days: int = 400
    concurrency: int = 1
    refresh_seconds: int = 300
    extra: dict = field(default_factory=dict)


def parse_host_port(value: str, default_port: int) -> tuple[str, int]:
    """Split 'host', 'host:port' or '[v6]:port' into (host, port)."""
    value = str(value).strip()
    if not value:
        raise ConfigError("empty host")
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            raise ConfigError(f"bad host: {value!r}")
        host = value[1:end]
        rest = value[end + 1:]
        if rest.startswith(":"):
            return host, _port(rest[1:])
        return host, default_port
    if value.count(":") == 1:
        host, port = value.split(":")
        return host, _port(port)
    return value, default_port


def _port(text) -> int:
    try:
        port = int(text)
    except (TypeError, ValueError):
        raise ConfigError(f"bad port: {text!r}") from None
    if not 1 <= port <= 65535:
        raise ConfigError(f"port out of range: {port}")
    return port


def _num(data: dict, key: str, default, where: str, minimum=0.0, allow_none=False):
    if key not in data:
        return default
    value = data[key]
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}: '{key}' must be a number")
    if value < minimum:
        raise ConfigError(f"{where}: '{key}' must be >= {minimum}")
    return value


def _str(data: dict, key: str, default: str, where: str) -> str:
    value = data.get(key, default)
    if value is None:
        return default
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise ConfigError(f"{where}: '{key}' must be a string")
    return str(value)


def _resolve(base_dir: str, path: str) -> str:
    path = os.path.expanduser(path)
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(base_dir, path))


def _parse_check(raw, index: int, defaults: dict, base_dir: str) -> Check:
    if not isinstance(raw, dict):
        raise ConfigError(f"checks[{index}] must be a mapping")
    cid = _str(raw, "id", "", f"checks[{index}]")
    where = f"check {cid or index}"
    if not _ID_RE.match(cid):
        raise ConfigError(f"{where}: 'id' is required and may only contain letters, digits, '_' and '-'")
    ctype = _str(raw, "type", "", where).lower()
    if ctype not in CHECK_TYPES:
        raise ConfigError(f"{where}: 'type' must be one of {', '.join(CHECK_TYPES)}")
    name = _str(raw, "name", cid, where)
    timeout = _num(raw, "timeout", defaults["timeout"], where, minimum=0.1)
    degraded = _num(raw, "degraded_ms", defaults["degraded_ms"], where, allow_none=True)
    if degraded is not None and degraded <= 0:
        degraded = None
    check = Check(id=cid, name=name, type=ctype, timeout=float(timeout), degraded_ms=degraded)
    if ctype == "http":
        check.url = _str(raw, "url", "", where)
        if not re.match(r"^https?://[^\s/]+", check.url):
            raise ConfigError(f"{where}: 'url' must be an http(s) URL")
        check.expect_status = int(_num(raw, "expect_status", 200, where, minimum=100))
        check.keyword = _str(raw, "keyword", "", where)
    else:
        default_port = 443 if ctype == "tls" else 0
        host_value = _str(raw, "host", "", where)
        if not host_value:
            raise ConfigError(f"{where}: 'host' is required")
        host, port = parse_host_port(host_value, default_port)
        if "port" in raw:
            port = _port(raw["port"])
        if not port:
            raise ConfigError(f"{where}: a port is required (use 'host: name:port' or 'port:')")
        check.host, check.port = host, port
        if ctype == "tls":
            check.warn_days = float(_num(raw, "warn_days", 21, where))
            check.fail_days = float(_num(raw, "fail_days", 7, where))
            if check.fail_days > check.warn_days:
                raise ConfigError(f"{where}: 'fail_days' must not exceed 'warn_days'")
            ca = _str(raw, "ca_file", "", where)
            check.ca_file = _resolve(base_dir, ca) if ca else ""
    return check


def parse_config(data, base_dir: str = ".") -> Config:
    """Validate a parsed config mapping. Relative paths resolve against base_dir."""
    if not isinstance(data, dict):
        raise ConfigError("the config must be a mapping at the top level")
    title = _str(data, "title", "Status", "config")
    defaults = {
        "timeout": float(_num(data, "timeout", 10, "config", minimum=0.1)),
        "degraded_ms": _num(data, "degraded_ms", 3000, "config", allow_none=True),
    }
    raw_checks = data.get("checks")
    if not isinstance(raw_checks, list) or not raw_checks:
        raise ConfigError("'checks' must be a non-empty list")
    checks = [_parse_check(c, i, defaults, base_dir) for i, c in enumerate(raw_checks)]
    seen = set()
    for c in checks:
        if c.id in seen:
            raise ConfigError(f"duplicate check id: {c.id}")
        seen.add(c.id)
    days = int(_num(data, "days", 90, "config", minimum=1))
    if days > 366:
        raise ConfigError("config: 'days' must be <= 366")
    return Config(
        title=title,
        output_dir=_resolve(base_dir, _str(data, "output_dir", "public", "config")),
        history=_resolve(base_dir, _str(data, "history", "history.db", "config")),
        incidents_dir=_resolve(base_dir, _str(data, "incidents_dir", "incidents", "config")),
        checks=checks,
        description=_str(data, "description", "", "config"),
        base_url=_str(data, "base_url", "", "config").rstrip("/"),
        days=days,
        max_incidents=int(_num(data, "max_incidents", 20, "config")),
        retention_days=int(_num(data, "retention_days", 400, "config", minimum=1)),
        concurrency=max(1, min(8, int(_num(data, "concurrency", 1, "config", minimum=1)))),
        refresh_seconds=int(_num(data, "refresh_seconds", 300, "config")),
    )


def load_config(path: str) -> Config:
    """Read a YAML (.yml/.yaml) or JSON (.json) config file."""
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            text = fh.read()
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from None
    try:
        if path.lower().endswith(".json"):
            data = json.loads(text)
        else:
            try:
                import yaml
            except ImportError:
                raise ConfigError("PyYAML is not installed; install it or use a .json config") from None
            data = yaml.safe_load(text)
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"cannot parse config {path}: {exc}") from None
    return parse_config(data, os.path.dirname(os.path.abspath(path)))

"""Glue: read history + incidents, write index.html, status.json and feed.xml."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from .checks import iso, utc_now
from .config import Config
from .history import History
from .incidents import load_incidents
from .render import PageData, build_views, render_atom, render_html, render_json


def _write_atomic(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def collect(config: Config, now: datetime | None = None, warn=None) -> PageData:
    now = (now or utc_now()).astimezone(timezone.utc)
    since = (now - timedelta(days=config.days - 1)).date().isoformat()
    with History(config.history) as hist:
        latest = hist.latest()
        daily = hist.daily(since)
    incidents = load_incidents(config.incidents_dir, warn)
    if config.max_incidents:
        incidents = incidents[: config.max_incidents]
    return PageData(
        title=config.title,
        description=config.description,
        base_url=config.base_url,
        refresh_seconds=config.refresh_seconds,
        now=now,
        days=config.days,
        checks=build_views(config.checks, latest, daily, now, config.days),
        incidents=incidents,
    )


def build(config: Config, now: datetime | None = None, warn=None) -> list[str]:
    """Write the three output files into config.output_dir; returns their paths."""
    data = collect(config, now, warn)
    os.makedirs(config.output_dir, exist_ok=True)
    outputs = {
        "index.html": render_html(data),
        "status.json": render_json(data),
        "feed.xml": render_atom(data),
    }
    paths = []
    for name, text in outputs.items():
        path = os.path.join(config.output_dir, name)
        _write_atomic(path, text)
        paths.append(path)
    return paths


def record(config: Config, results, now: datetime | None = None) -> None:
    """Append results to the history and drop rows older than retention_days."""
    now = now or utc_now()
    with History(config.history) as hist:
        hist.append(list(results))
        hist.prune(iso(now - timedelta(days=config.retention_days)))

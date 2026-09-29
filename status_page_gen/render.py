"""Page data model and the three outputs: index.html, status.json, feed.xml."""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from xml.sax.saxutils import escape as xml_escape

from .checks import DEGRADED, DOWN, UP, iso
from .history import DayStat
from .incidents import Incident, render_markdown

UNKNOWN = "unknown"
BADGE = {UP: "Operational", DEGRADED: "Degraded", DOWN: "Down", UNKNOWN: "No data"}


@dataclass
class DayCell:
    day: str
    css: str  # ok | warn | bad | nd
    title: str
    uptime_pct: float | None


@dataclass
class CheckView:
    id: str
    name: str
    type: str
    status: str = UNKNOWN
    latency_ms: int | None = None
    detail: str = ""
    checked_at: str = ""
    uptime_pct: float | None = None  # over the whole window, None without data
    cells: list[DayCell] = field(default_factory=list)


@dataclass
class PageData:
    title: str
    description: str
    base_url: str
    refresh_seconds: int
    now: datetime
    days: int
    checks: list[CheckView]
    incidents: list[Incident]


def overall_status(statuses: list[str]) -> tuple[str, str]:
    """(key, label) for the banner. Checks without data are ignored unless all are."""
    known = [s for s in statuses if s != UNKNOWN]
    if not known:
        return UNKNOWN, "No data yet"
    if DOWN in known:
        if all(s == DOWN for s in known):
            return DOWN, "Major outage"
        return DOWN, "Partial outage"
    if DEGRADED in known:
        return DEGRADED, "Degraded performance"
    return UP, "All systems operational"


def day_css(pct: float | None) -> str:
    if pct is None:
        return "nd"
    if pct >= 99.5:
        return "ok"
    if pct >= 95.0:
        return "warn"
    return "bad"


def uptime_cells(stats: dict[str, DayStat], today: date, days: int) -> list[DayCell]:
    """One cell per UTC day, oldest first, ending on `today`."""
    cells = []
    for offset in range(days - 1, -1, -1):
        day = (today - timedelta(days=offset)).isoformat()
        st = stats.get(day)
        if st is None or st.total == 0:
            cells.append(DayCell(day, "nd", f"{day}: no data", None))
            continue
        pct = st.uptime_pct
        title = f"{day}: {pct:.2f}% uptime ({st.total} checks, {st.down} down, {st.degraded} degraded)"
        cells.append(DayCell(day, day_css(pct), title, pct))
    return cells


def window_uptime(stats: dict[str, DayStat], today: date, days: int) -> float | None:
    first = (today - timedelta(days=days - 1)).isoformat()
    total = sum(s.total for d, s in stats.items() if first <= d <= today.isoformat())
    down = sum(s.down for d, s in stats.items() if first <= d <= today.isoformat())
    return 100.0 * (total - down) / total if total else None


def build_views(checks, latest, daily, now: datetime, days: int) -> list[CheckView]:
    """Combine config checks, latest results and daily stats into CheckViews."""
    today = now.astimezone(timezone.utc).date()
    views = []
    for c in checks:
        stats = daily.get(c.id, {})
        view = CheckView(c.id, c.name, c.type)
        res = latest.get(c.id)
        if res:
            view.status, view.latency_ms = res.status, res.latency_ms
            view.detail, view.checked_at = res.detail, res.ts
        view.cells = uptime_cells(stats, today, days)
        view.uptime_pct = window_uptime(stats, today, days)
        views.append(view)
    return views


def _pct(value: float | None) -> str:
    if value is None:
        return "no data"
    # never round a partial outage up to 100.00
    text = f"{value:.2f}"
    if text == "100.00" and value < 100:
        text = "99.99"
    return text + "%"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "status"


def _when(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _names(data: PageData) -> dict[str, str]:
    return {c.id: c.name for c in data.checks}


def _incident_dict(inc: Incident, names: dict[str, str]) -> dict:
    return {
        "id": inc.id,
        "title": inc.title,
        "date": iso(inc.date),
        "status": inc.status,
        "resolved": inc.status == "resolved",
        "affected": [{"id": a, "name": names.get(a, a)} for a in inc.affected],
        "body": inc.body,
    }


# --- status.json ---------------------------------------------------------------

def render_json(data: PageData) -> str:
    key, label = overall_status([c.status for c in data.checks])
    names = _names(data)
    doc = {
        "title": data.title,
        "generated_at": iso(data.now),
        "overall": {"status": key, "label": label},
        "window_days": data.days,
        "checks": [
            {
                "id": c.id,
                "name": c.name,
                "type": c.type,
                "status": c.status,
                "latency_ms": c.latency_ms,
                "detail": c.detail,
                "checked_at": c.checked_at or None,
                "uptime_pct": None if c.uptime_pct is None else round(c.uptime_pct, 3),
            }
            for c in data.checks
        ],
        "incidents": [_incident_dict(i, names) for i in data.incidents],
    }
    return json.dumps(doc, indent=2, ensure_ascii=True) + "\n"


# --- feed.xml (Atom) -----------------------------------------------------------

def render_atom(data: PageData) -> str:
    slug = _slug(data.title)
    names = _names(data)
    updated = max([data.now] + [i.date for i in data.incidents]) if data.incidents else data.now
    parts = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<feed xmlns="http://www.w3.org/2005/Atom">',
        f"  <title>{xml_escape(data.title)} - incidents</title>",
        f"  <id>urn:status-page-gen:{xml_escape(slug)}</id>",
        f"  <updated>{iso(updated)}</updated>",
        "  <author><name>" + xml_escape(data.title) + "</name></author>",
    ]
    if data.base_url:
        parts.append(f'  <link rel="alternate" type="text/html" href="{xml_escape(data.base_url, {chr(34): "&quot;"})}/"/>')
        parts.append(f'  <link rel="self" type="application/atom+xml" href="{xml_escape(data.base_url, {chr(34): "&quot;"})}/feed.xml"/>')
    for inc in data.incidents:
        summary = f"{inc.status.capitalize()}"
        if inc.affected:
            summary += " - affects " + ", ".join(names.get(a, a) for a in inc.affected)
        parts += [
            "  <entry>",
            f"    <title>{xml_escape(inc.title)}</title>",
            f"    <id>urn:status-page-gen:{xml_escape(slug)}:{xml_escape(inc.id)}</id>",
            f"    <updated>{iso(inc.date)}</updated>",
            f"    <published>{iso(inc.date)}</published>",
            f"    <summary>{xml_escape(summary)}</summary>",
            f'    <content type="html">{xml_escape(render_markdown(inc.body))}</content>',
            "  </entry>",
        ]
    parts.append("</feed>")
    return "\n".join(parts) + "\n"


# --- index.html ----------------------------------------------------------------

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1b1f24;--muted:#5b6570;--line:#e2e6ea;--ok:#2da44e;--warn:#d4a017;--bad:#d1242f;--nd:#c9d0d8;--link:#0b5fd1}
@media (prefers-color-scheme:dark){:root{--bg:#0f1317;--card:#171c22;--fg:#e6eaee;--muted:#98a3ae;--line:#28313a;--ok:#3fb950;--warn:#d29922;--bad:#f85149;--nd:#333c46;--link:#79b8ff}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
main{max-width:760px;margin:0 auto;padding:32px 16px 48px}
a{color:var(--link)}
h1{font-size:1.6rem;margin:0 0 4px}
h2{font-size:1.15rem;margin:32px 0 12px}
.desc{color:var(--muted);margin:0 0 20px}
.banner{display:flex;align-items:center;gap:10px;padding:16px 18px;border-radius:10px;color:#fff;font-weight:600;font-size:1.1rem}
.banner.up{background:var(--ok)}.banner.degraded{background:var(--warn)}.banner.down{background:var(--bad)}.banner.unknown{background:var(--nd);color:var(--fg)}
.dot{width:12px;height:12px;border-radius:50%;background:currentColor;opacity:.85;flex:none}
ul.checks,ul.incidents{list-style:none;margin:0;padding:0}
.check,.incident{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:12px}
.row{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
.name{font-weight:600}
.badge{font-size:.8rem;font-weight:600;padding:2px 10px;border-radius:999px;color:#fff;white-space:nowrap}
.badge.up,.badge.ok{background:var(--ok)}.badge.degraded,.badge.warn{background:var(--warn)}.badge.down,.badge.bad{background:var(--bad)}.badge.unknown,.badge.nd{background:var(--nd);color:var(--fg)}
.meta{color:var(--muted);font-size:.85rem;margin:2px 0 10px;word-break:break-word}
.bars{display:flex;gap:2px;height:30px}
.d{flex:1 1 0;min-width:0;border-radius:2px;background:var(--nd)}
.d.ok{background:var(--ok)}.d.warn{background:var(--warn)}.d.bad{background:var(--bad)}.d.nd{background:var(--nd)}
.scale{display:flex;justify-content:space-between;color:var(--muted);font-size:.75rem;margin-top:4px}
.incident h3{font-size:1rem;margin:0}
.incident .meta{margin-bottom:8px}
.incident p,.incident ul{margin:.4em 0}
.incident ul{padding-left:1.2em}
.incident code{background:var(--bg);padding:1px 5px;border-radius:4px;font-size:.9em}
.empty{color:var(--muted)}
footer{margin-top:32px;color:var(--muted);font-size:.85rem}
@media (max-width:520px){.bars{height:24px;gap:1px}}
"""


def _incident_html(inc: Incident, names: dict[str, str]) -> str:
    e = html.escape
    css = "ok" if inc.status == "resolved" else "warn"
    affected = ""
    if inc.affected:
        affected = " &middot; Affects: " + e(", ".join(names.get(a, a) for a in inc.affected))
    return (
        f'<li class="incident" id="incident-{e(_slug(inc.id))}">'
        f'<div class="row"><h3>{e(inc.title)}</h3><span class="badge {css}">{e(inc.status.capitalize())}</span></div>'
        f'<div class="meta">{e(_when(inc.date))}{affected}</div>'
        f"{render_markdown(inc.body)}</li>"
    )


def _check_html(c: CheckView, days: int) -> str:
    e = html.escape
    meta = []
    if c.latency_ms is not None:
        meta.append(f"{c.latency_ms} ms")
    if c.detail:
        meta.append(e(c.detail))
    if c.checked_at:
        meta.append("checked " + e(c.checked_at.replace("T", " ").replace("Z", " UTC")))
    bars = "".join(f'<span class="d {d.css}" title="{e(d.title)}"></span>' for d in c.cells)
    label = f"{c.name}: uptime over the last {days} days {_pct(c.uptime_pct)}"
    return (
        '<li class="check">'
        f'<div class="row"><span class="name">{e(c.name)}</span>'
        f'<span class="badge {c.status}">{BADGE[c.status]}</span></div>'
        f'<div class="meta">{" &middot; ".join(meta) if meta else "not checked yet"}</div>'
        f'<div class="bars" role="img" aria-label="{e(label)}">{bars}</div>'
        f'<div class="scale"><span>{days} days ago</span><span>{_pct(c.uptime_pct)} uptime</span><span>Today</span></div>'
        "</li>"
    )


def render_html(data: PageData) -> str:
    e = html.escape
    key, label = overall_status([c.status for c in data.checks])
    names = _names(data)
    refresh = f'<meta http-equiv="refresh" content="{int(data.refresh_seconds)}">\n' if data.refresh_seconds > 0 else ""
    desc = f'<p class="desc">{e(data.description)}</p>' if data.description else ""
    checks = "\n".join(_check_html(c, data.days) for c in data.checks)
    if data.incidents:
        incidents = '<ul class="incidents">' + "\n".join(_incident_html(i, names) for i in data.incidents) + "</ul>"
    else:
        incidents = '<p class="empty">No incidents reported.</p>'
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
{refresh}<title>{e(data.title)}</title>
<link rel="alternate" type="application/atom+xml" title="{e(data.title)} incidents" href="feed.xml">
<style>{CSS}</style>
</head>
<body>
<main>
<header><h1>{e(data.title)}</h1>{desc}</header>
<section class="banner {key}" role="status"><span class="dot"></span><span>{e(label)}</span></section>
<h2>Services</h2>
<ul class="checks">
{checks}
</ul>
<h2>Incidents</h2>
{incidents}
<footer>Last updated {e(_when(data.now))} &middot; <a href="feed.xml">Atom feed</a> &middot; <a href="status.json">status.json</a></footer>
</main>
</body>
</html>
"""

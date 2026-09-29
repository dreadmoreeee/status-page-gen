"""Incidents from a folder of Markdown files with a small front-matter, plus a safe Markdown subset."""

from __future__ import annotations

import html
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone


class IncidentError(ValueError):
    pass


@dataclass
class Incident:
    id: str
    title: str
    date: datetime  # timezone-aware, UTC
    status: str
    affected: list[str] = field(default_factory=list)
    body: str = ""


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_date(text: str) -> datetime:
    """'2026-09-20', '2026-09-20 14:30', '2026-09-20T14:30:00Z' or with an offset; naive means UTC."""
    text = text.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise IncidentError(f"bad date: {text!r}") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _split_list(value: str) -> list[str]:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return [_unquote(p) for p in value.split(",") if _unquote(p)]


def parse_incident(text: str, name: str) -> Incident:
    """Parse one incident file. `name` (the file stem) becomes the incident id."""
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        raise IncidentError("missing front-matter (file must start with '---')")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise IncidentError("front-matter is not closed with '---'")
    meta: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise IncidentError(f"bad front-matter line: {line!r}")
        meta[key.strip().lower()] = value.strip()
    title = _unquote(meta.get("title", ""))
    if not title:
        raise IncidentError("front-matter needs a 'title'")
    if "date" not in meta:
        raise IncidentError("front-matter needs a 'date'")
    status = _unquote(meta.get("status", "investigating")).lower() or "investigating"
    affected = _split_list(meta.get("affected", meta.get("checks", "")))
    body = "\n".join(lines[end + 1:]).strip("\n")
    return Incident(name, title, parse_date(_unquote(meta["date"])), status, affected, body)


def load_incidents(folder: str, warn=None) -> list[Incident]:
    """All valid *.md files in `folder`, newest first. Bad files are reported via warn(msg) and skipped."""
    if warn is None:
        warn = lambda msg: print(f"warning: {msg}", file=sys.stderr)  # noqa: E731
    if not folder or not os.path.isdir(folder):
        return []
    out = []
    for fname in sorted(os.listdir(folder)):
        if not fname.lower().endswith(".md"):
            continue
        path = os.path.join(folder, fname)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                out.append(parse_incident(fh.read(), fname[:-3]))
        except (OSError, UnicodeDecodeError, IncidentError) as exc:
            warn(f"skipping incident {fname}: {exc}")
    out.sort(key=lambda i: (i.date, i.id), reverse=True)
    return out


# --- minimal, safe Markdown ---------------------------------------------------

_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+|mailto:[^\s)]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<![\w*])\*(?![\s*])(.+?)(?<![\s*])\*(?![\w*])")
_HEADING = re.compile(r"^(#{1,3})\s+(.*)$")
_ITEM = re.compile(r"^\s*[-*]\s+(.*)$")


def _inline(text: str) -> str:
    out = []
    for i, part in enumerate(re.split(r"`([^`\n]+)`", text)):
        if i % 2:
            out.append(f"<code>{html.escape(part)}</code>")
            continue
        esc = html.escape(part.replace("\x00", ""))
        links: list[str] = []

        def stash(m: re.Match) -> str:
            links.append(f'<a href="{m.group(2)}" rel="noopener noreferrer nofollow">{m.group(1)}</a>')
            return f"\x00{len(links) - 1}\x00"

        esc = _LINK.sub(stash, esc)
        esc = _BOLD.sub(r"<strong>\1</strong>", esc)
        esc = _ITALIC.sub(r"<em>\1</em>", esc)
        esc = re.sub(r"\x00(\d+)\x00", lambda m: links[int(m.group(1))], esc)
        out.append(esc)
    return "".join(out)


def render_markdown(text: str) -> str:
    """Paragraphs, #..### headings, '-' lists, **bold**, *italic*, `code` and http(s)/mailto links.
    Everything else is HTML-escaped; raw HTML never passes through."""
    blocks: list[str] = []
    para: list[str] = []
    items: list[str] = []

    def flush() -> None:
        if para:
            blocks.append("<p>" + _inline(" ".join(para)) + "</p>")
            para.clear()
        if items:
            blocks.append("<ul>" + "".join(f"<li>{_inline(i)}</li>" for i in items) + "</ul>")
            items.clear()

    for line in text.splitlines():
        if not line.strip():
            flush()
            continue
        head = _HEADING.match(line)
        item = _ITEM.match(line)
        if head:
            flush()
            level = len(head.group(1)) + 2
            blocks.append(f"<h{level}>{_inline(head.group(2).strip())}</h{level}>")
        elif item:
            if para:
                flush()
            items.append(item.group(1).strip())
        else:
            if items:
                flush()
            para.append(line.strip())
    flush()
    return "\n".join(blocks)

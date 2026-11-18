"""Show USER_GUIDE.md as a page on the phone (/guide), styled like the bridge page.

A tiny Markdown reader for the few things the guide uses: headings, paragraphs,
lists (nested by indent), quotes, bold, italics, code and links.
"""

from __future__ import annotations

import html
import os
import re

from . import config

GUIDE_PATH = os.path.join(config.ROOT, "USER_GUIDE.md")


def slug(text: str) -> str:
    """The same anchors GitHub makes, so the guide's own links work in both places."""
    s = re.sub(r"[^\w\- ]", "", text.lower()).strip()
    return s.replace(" ", "-")


def _inline(text: str) -> str:
    t = html.escape(text, quote=False)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?<![*\w])\*([^*]+)\*(?![*\w])", r"<i>\1</i>", t)
    t = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)",
               lambda m: f'<a href="{html.escape(m.group(2))}">{m.group(1)}</a>', t)
    return t


def render(md: str) -> str:
    out: list[str] = []
    para: list[str] = []
    stack: list[tuple[int, str]] = []  # (indent, "ul"/"ol")
    quote: list[str] = []
    li_open = False

    def flush_para():
        if para:
            out.append("<p>" + _inline(" ".join(para)) + "</p>")
            para.clear()

    def flush_quote():
        if quote:
            out.append("<blockquote>" + _inline(" ".join(quote)) + "</blockquote>")
            quote.clear()

    def close_lists(to_indent: int = -1):
        nonlocal li_open
        while stack and stack[-1][0] > to_indent:
            out.append("</li></" + stack.pop()[1] + ">")
        li_open = bool(stack)

    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            flush_para()
            flush_quote()
            continue
        m = re.match(r"^(#{1,3})\s+(.*)$", line)
        if m:
            flush_para(); flush_quote(); close_lists()
            level, text = len(m.group(1)), m.group(2).strip()
            out.append(f'<h{level} id="{slug(text)}">{_inline(text)}</h{level}>')
            continue
        if line.lstrip().startswith(">"):
            flush_para(); close_lists()
            quote.append(line.lstrip()[1:].strip())
            continue
        m = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", line)
        if m:
            flush_para(); flush_quote()
            indent = len(m.group(1))
            kind = "ol" if m.group(2)[0].isdigit() else "ul"
            if stack and indent < stack[-1][0]:
                close_lists(indent)
            if not stack or indent > stack[-1][0]:
                out.append(f"<{kind}><li>")
                stack.append((indent, kind))
            else:
                out.append("</li><li>")
            out.append(_inline(m.group(3)))
            li_open = True
            continue
        if stack and raw.startswith(" "):
            out.append(" " + _inline(line.strip()))  # continuation of a list item
            continue
        close_lists()
        para.append(line.strip())
    flush_para(); flush_quote(); close_lists()
    return "\n".join(out)


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Print Bridge guide</title><link rel="icon" href="/icon.svg"><meta name="theme-color" content="#F6F1E7">
<style>
:root { --bg:#F6F1E7; --card:#FFFDF8; --text:#2B2620; --muted:#776B5D; --line:#E6DCCB; --accent:#C4553B; --chip:#EFE6D6;
  --serif:"Iowan Old Style","Palatino Linotype",Palatino,"Book Antiqua",Georgia,serif; }
@media (prefers-color-scheme: dark) { :root { --bg:#1C1814; --card:#26211C; --text:#F1E9DC; --muted:#B3A795; --line:#3A322A;
  --accent:#E0795D; --chip:#312A23; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:17px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
main { max-width: 680px; margin: 0 auto; padding: 18px 16px 60px; }
.top { display:flex; align-items:center; gap:10px; margin-bottom: 8px; }
.top a { color: var(--accent); text-decoration: none; font-weight: 600; font-size: 15px; }
h1 { font: 600 30px/1.15 var(--serif); margin: 10px 0 14px; }
h2 { font: 600 22px/1.25 var(--serif); margin: 34px 0 8px; padding-top: 18px; border-top: 1px solid var(--line); }
h3 { font: 600 18px/1.3 var(--serif); margin: 22px 0 6px; }
a { color: var(--accent); }
p, li { max-width: 62ch; }
ul, ol { padding-left: 22px; } li { margin: 4px 0; }
blockquote { margin: 14px 0; padding: 12px 14px; background: var(--card); border: 1px solid var(--line);
  border-left: 4px solid var(--accent); border-radius: 10px; font-size: 16px; }
code { background: var(--chip); padding: 1px 5px; border-radius: 5px; font-size: .92em; overflow-wrap: anywhere; }
</style></head><body><main>
<div class="top"><a href="/">&larr; Back to printing</a></div>
{body}
</main></body></html>"""


def page() -> bytes:
    try:
        with open(GUIDE_PATH, encoding="utf-8") as fh:
            md = fh.read()
    except OSError:
        md = "# Print Bridge guide\n\nThe guide file (USER_GUIDE.md) is missing from the bridge folder."
    return PAGE.replace("{body}", render(md)).encode("utf-8")

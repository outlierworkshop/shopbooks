"""In-app Help: render the project's Markdown guides (docs/*.md) to HTML so they're readable inside
ShopBooks instead of via private-repo GitHub links.

A small, dependency-free Markdown subset renderer covering exactly what the guides use: headings,
paragraphs, bullet/numbered lists (with wrapped continuation lines), GitHub pipe tables, fenced and
inline code, bold, italic, and links. Not a general Markdown engine — it's tested against the actual
docs (test_help.py) and only those whitelisted files are ever served.
"""
import html
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent
DOCS_DIR = BASE / "docs"

# slug -> (nav title, filename). Only these are served (no arbitrary file reads).
DOCS = {
    "guide": ("User Guide", "USER_GUIDE.md"),
    "email": ("Email setup (Gmail / Workspace)", "email-setup.md"),
    "mileage": ("Automatic mileage from your phone", "mileage-automation.md"),
}


def list_docs():
    return [{"slug": s, "title": t} for s, (t, _) in DOCS.items()]


def _inline(text):
    """Escape HTML, then apply inline Markdown (code, links, bold, italic). Code spans are protected
    from further formatting via placeholders."""
    text = html.escape(text, quote=False)
    spans = []

    def stash(m):
        spans.append("<code>" + m.group(1) + "</code>")
        return f"\x00{len(spans) - 1}\x00"

    text = re.sub(r"`([^`]+)`", stash, text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)",
                  lambda m: f'<a href="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>', text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<em>\1</em>", text)
    text = re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], text)
    return text


def _render_table(lines):
    def cells(row):
        return [c.strip() for c in row.strip().strip("|").split("|")]
    header = cells(lines[0])
    body = [cells(r) for r in lines[2:]]  # lines[1] is the |---|---| separator
    out = ["<table><thead><tr>"]
    out += [f"<th>{_inline(c)}</th>" for c in header]
    out.append("</tr></thead><tbody>")
    for row in body:
        out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def _render_list(lines):
    ordered = bool(re.match(r"\s*\d+\.\s", lines[0]))
    items = []
    for ln in lines:
        m = re.match(r"\s*(?:[-*]|\d+\.)\s+(.*)", ln)
        if m:
            items.append(m.group(1))
        elif ln.strip() and items:      # wrapped continuation of the previous item
            items[-1] += " " + ln.strip()
    tag = "ol" if ordered else "ul"
    return f"<{tag}>" + "".join(f"<li>{_inline(it)}</li>" for it in items) + f"</{tag}>"


def _is_table(block):
    return len(block) >= 2 and "|" in block[0] and re.match(r"^\s*\|?[\s:|-]+\|?\s*$", block[1])


def _is_list(block):
    return bool(re.match(r"\s*(?:[-*]|\d+\.)\s+", block[0]))


def render_markdown(text):
    """Render the Markdown subset used by the guides to an HTML string."""
    out = []
    lines = text.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):          # fenced code block
            i += 1
            code = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1  # skip the closing fence
            out.append("<pre><code>" + html.escape("\n".join(code), quote=False) + "</code></pre>")
            continue
        if not line.strip():                          # blank line
            i += 1
            continue
        m = re.match(r"(#{1,6})\s+(.*)", line)         # heading
        if m:
            level = len(m.group(1))
            out.append(f"<h{level}>{_inline(m.group(2).strip())}</h{level}>")
            i += 1
            continue
        if re.match(r"^\s*(?:---|\*\*\*|___)\s*$", line):  # horizontal rule
            out.append("<hr>")
            i += 1
            continue
        # gather a block up to the next blank line
        block = []
        while i < len(lines) and lines[i].strip() and not lines[i].strip().startswith("```") \
                and not re.match(r"#{1,6}\s", lines[i]):
            block.append(lines[i])
            i += 1
        if not block:
            i += 1
            continue
        if _is_table(block):
            out.append(_render_table(block))
        elif _is_list(block):
            out.append(_render_list(block))
        elif block[0].strip().startswith(">"):        # blockquote
            text_q = " ".join(l.strip().lstrip(">").strip() for l in block)
            out.append(f"<blockquote>{_inline(text_q)}</blockquote>")
        else:                                         # paragraph
            out.append("<p>" + _inline(" ".join(l.strip() for l in block)) + "</p>")
    return "\n".join(out)


def get(slug):
    """(title, rendered_html) for a whitelisted slug, or None."""
    entry = DOCS.get(slug)
    if not entry:
        return None
    title, filename = entry
    path = DOCS_DIR / filename
    if not path.exists():
        return None
    body = render_markdown(path.read_text(encoding="utf-8"))
    return title, body

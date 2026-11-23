"""A *world* is a frozen set of Wikipedia pages as the agent sees them.

Each page is rendered to plain text with its internal links kept as character spans,
then cut into chunks of at most `chunk_chars` characters. The agent sees one chunk at a time.
The link graph is derived from these spans, so it contains exactly the links an agent can see and click.

On disk (data/worlds/<name>/):
  world.json         titles, chunk size, and for every page its chunk count and visible links per chunk
  pages/<id>.json    full text, chunk start offsets and link spans (read by the environment and the viewer)
  tasks/<id>.json    per-task oracle values written by wikigame.metrics (see notebooks/02)
"""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
WORLDS = ROOT / "data" / "worlds"

# ---------------------------------------------------------------------------------------------
# HTML -> text with link spans (Wikispeedia / 2007 Wikipedia for Schools markup)
# ---------------------------------------------------------------------------------------------

BLOCK_PARAGRAPH = {"p", "table", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "center", "dl"}
BLOCK_LINE = {"div", "li", "tr", "br", "ul", "ol", "dd", "dt", "caption"}
SKIP = {"script", "style", "img", "sup"}  # sup holds reference markers like [1]


def title_to_name(title: str) -> str:
    """'Utrecht_%28city%29' -> 'Utrecht (city)'."""
    return urllib.parse.unquote(title).replace("_", " ")


def _walk(node, out: list, title_of_href):
    """Flatten the DOM into tokens: ('t', text) | ('a', text, target) | ('br', n_newlines)."""
    tag = node.tag
    if tag == "-text":
        out.append(("t", node.text_content or ""))
        return
    if tag in SKIP or tag == "_comment":
        return
    if tag == "h3" and node.attributes.get("id") == "siteSub":  # "2007 Schools Wikipedia Selection" banner
        return
    if tag == "div" and "printfooter" in (node.attributes.get("class") or ""):
        return
    if tag == "div" and "magnify" in (node.attributes.get("class") or ""):
        return
    if tag == "a":
        target = title_of_href(node.attributes.get("href") or "")
        text = node.text(deep=True, separator="")
        if target is not None and text.strip():
            out.append(("a", text, target))
        else:
            out.append(("t", text))
        return
    is_par, is_line = tag in BLOCK_PARAGRAPH, tag in BLOCK_LINE
    if is_par or is_line:
        out.append(("br", 2 if is_par else 1))
    if tag in ("h2", "h3", "h4"):
        out.append(("t", "#" * int(tag[1]) + " "))
    elif tag == "li":
        out.append(("t", "- "))
    elif tag in ("td", "th"):
        out.append(("t", " | "))
    child = node.child
    while child is not None:
        _walk(child, out, title_of_href)
        child = child.next
    if is_par or is_line:
        out.append(("br", 2 if is_par else 1))


def _assemble(tokens) -> tuple[str, list[tuple[int, int, str]]]:
    """Join tokens into text, collapsing whitespace, and keep (start, end, target) for each link."""
    buf: list[str] = []
    n = 0  # current length
    links = []
    pending_nl = 0

    def tail():
        return buf[-1][-1] if buf and buf[-1] else "\n"

    def emit(s):
        nonlocal n
        buf.append(s)
        n += len(s)

    for tok in tokens:
        if tok[0] == "br":
            pending_nl = max(pending_nl, tok[1])
            continue
        text = re.sub(r"\s+", " ", tok[1])
        if not text.strip():
            if text and n and pending_nl == 0 and tail() not in " \n":
                emit(" ")
            continue
        if pending_nl and n:
            # strip trailing spaces then add newlines
            while buf and buf[-1].endswith(" ") and not (links and links[-1][1] == n):
                buf[-1] = buf[-1][:-1]
                n -= 1
            emit("\n" * pending_nl)
        pending_nl = 0
        if tail() == "\n":
            text = text.lstrip()
        elif tail() == " " and text.startswith(" "):
            text = text[1:]
        if tok[0] == "a":
            lead = len(text) - len(text.lstrip())
            if lead:
                emit(text[:lead])
            core = text.strip()
            start = n
            emit(core)
            links.append((start, n, tok[2]))
            if text.endswith(" ") and core:
                emit(" ")
        else:
            emit(text)
    return "".join(buf), links


def _clean_table_bars(text, links):
    """Remove the ' | ' cell separators at the start of table rows, shifting link spans accordingly."""
    keep = np.ones(len(text), dtype=bool)
    for m in re.finditer(r"(?m)^\| ", text):
        keep[m.start(): m.end()] = False
    for m in re.finditer(r"(?m)^ *\| *\n", text):  # rows left with nothing but a separator
        keep[m.start(): m.end()] = False
    for m in re.finditer(r"\n{3,}", text):
        keep[m.start() + 2: m.end()] = False
    shift = np.concatenate([[0], np.cumsum(keep)])
    new_text = "".join(c for c, k in zip(text, keep) if k)
    new_links = [(int(shift[a]), int(shift[b]), t) for a, b, t in links]
    return new_text.strip("\n"), new_links


def parse_wikispeedia_html(html: str, title_of_href) -> tuple[str, list[tuple[int, int, str]]]:
    from selectolax.lexbor import LexborHTMLParser as HTMLParser

    start = html.find("<!-- start content -->")
    end = html.find("<!-- end content -->")
    body = html[start:end] if start >= 0 and end > start else html
    tree = HTMLParser("<div>" + body + "</div>")
    tokens: list = []
    _walk(tree.body, tokens, title_of_href)
    text, links = _assemble(tokens)
    return _clean_table_bars(text, links)


# ---------------------------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------------------------


def chunk_starts(text: str, links: list[tuple[int, int, str]], chunk_chars: int) -> list[int]:
    """Greedy split into chunks of at most `chunk_chars` characters.

    Cuts prefer line breaks, then sentence ends, then spaces, and never fall inside a link span.
    """
    inside = np.zeros(len(text) + 1, dtype=bool)
    for a, b, _ in links:
        inside[a + 1: b] = True

    def best_cut(lo, hi):
        window = text[lo:hi]
        for pattern in ("\n", ". ", " "):
            pos = window.rfind(pattern)
            while pos > 0:
                cut = lo + pos + len(pattern)
                if not inside[cut]:
                    return cut
                pos = window.rfind(pattern, 0, pos)
        return hi

    starts = [0]
    while len(text) - starts[-1] > chunk_chars:
        lo = starts[-1]
        cut = best_cut(lo + chunk_chars // 4, lo + chunk_chars)
        starts.append(cut)
    return starts


# ---------------------------------------------------------------------------------------------
# Loading a built world
# ---------------------------------------------------------------------------------------------


@dataclass
class World:
    """The link structure the oracle needs; page text is loaded lazily with `page()`."""

    name: str
    titles: list[str]  # URL-style ids, e.g. 'Utrecht_%28city%29'
    chunk_chars: int
    chunk_links: list[list[list[int]]]  # chunk_links[p][c] = distinct target page ids visible in chunk c
    index: dict[str, int] = field(init=False)

    def __post_init__(self):
        self.index = {t: i for i, t in enumerate(self.titles)}

    @property
    def n(self) -> int:
        return len(self.titles)

    @property
    def dir(self) -> Path:
        return WORLDS / self.name

    def name_of(self, p: int) -> str:
        return title_to_name(self.titles[p])

    def n_chunks(self, p: int) -> int:
        return len(self.chunk_links[p])

    def out_links(self, p: int) -> list[int]:
        """Distinct pages linked from anywhere on page p (the classic, full-page game)."""
        return sorted({q for c in self.chunk_links[p] for q in c})

    def page(self, p: int) -> dict:
        return json.loads((self.dir / "pages" / f"{p}.json").read_text())

    @classmethod
    def load(cls, name: str) -> "World":
        d = json.loads((WORLDS / name / "world.json").read_text())
        return cls(name=name, titles=d["titles"], chunk_chars=d["chunk_chars"], chunk_links=d["chunk_links"])


def write_world(name: str, titles: list[str], pages: list[tuple[str, list[tuple[int, int, int]]]], chunk_chars: int):
    """pages[p] = (text, [(start, end, target_id), ...]). Writes world.json and pages/<id>.json."""
    out = WORLDS / name
    (out / "pages").mkdir(parents=True, exist_ok=True)
    chunk_links = []
    for p, (text, links) in enumerate(pages):
        starts = chunk_starts(text, links, chunk_chars)
        bounds = starts[1:] + [len(text)]
        per_chunk = [set() for _ in starts]
        link_chunk = []
        for a, b, q in links:
            c = int(np.searchsorted(starts, a, side="right") - 1)
            link_chunk.append(c)
            if q != p:  # a link to the page itself is not an action
                per_chunk[c].add(q)
        chunk_links.append([sorted(s) for s in per_chunk])
        (out / "pages" / f"{p}.json").write_text(json.dumps({
            "id": p,
            "title": titles[p],
            "name": title_to_name(titles[p]),
            "text": text,
            "chunks": [[a, b] for a, b in zip(starts, bounds)],
            "links": [[a, b, q, c] for (a, b, q), c in zip(links, link_chunk)],  # start, end, target id, chunk
        }, ensure_ascii=False))
    (out / "world.json").write_text(json.dumps({
        "name": name, "chunk_chars": chunk_chars, "titles": titles,
        "names": [title_to_name(t) for t in titles], "chunk_links": chunk_links,
    }, ensure_ascii=False))
    return World.load(name)

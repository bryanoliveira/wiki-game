"""The environment an LLM agent plays in.

The agent gets a text observation: the goal, where it is, and one chunk of the current page in which every
clickable link is written as [anchor text](Target page name). It answers with one action:

    click("Target page name")   follow a link that is visible in the current chunk
    scroll_down()               show the next chunk of the page
    scroll_up()                 show the previous chunk

Every action costs one step, including an invalid one (which leaves the state unchanged). The episode ends when
the agent arrives at the target page or runs out of steps. Scoring is done by wikigame.metrics.score_actions,
which also gives the click-model view of the same episode via score_pages.
"""

from __future__ import annotations

import re

from .metrics import Oracle, score_actions, score_pages
from .world import World

ACTION_RE = re.compile(r'click\(\s*"(?P<name>[^"]+)"\s*\)|(?P<down>scroll_down\(\s*\))|(?P<up>scroll_up\(\s*\))')


def render_chunk(world: World, page: dict, chunk: int) -> str:
    """Chunk text with links written as [anchor](Target page name)."""
    a, b = page["chunks"][chunk]
    text, out, pos = page["text"], [], a
    for s, e, q, c in page["links"]:
        if c != chunk:
            continue
        out.append(text[pos:s])
        out.append(f"[{text[s:e]}]({world.name_of(q)})")
        pos = e
    out.append(text[pos:b])
    return "".join(out)


class WikiGameEnv:
    def __init__(self, world: World, oracle: Oracle, start: int, max_steps: int = 100):
        self.world, self.oracle, self.start, self.max_steps = world, oracle, start, max_steps
        self.target = oracle.target
        self.name_to_id = {world.name_of(i): i for i in range(world.n)}
        self.reset()

    def reset(self) -> str:
        self.page_id, self.chunk = self.start, 0
        self.page = self.world.page(self.start)
        self.actions: list[tuple[str, int]] = []
        self.pages = [self.start]
        self.message = ""
        return self.observe()

    @property
    def done(self) -> bool:
        return self.page_id == self.target or len(self.actions) >= self.max_steps

    def observe(self) -> str:
        w, k = self.world, len(self.page["chunks"])
        more = []
        if self.chunk > 0:
            more.append("scroll up for the previous part")
        if self.chunk + 1 < k:
            more.append("scroll down for more")
        lines = [
            f'Goal: reach the Wikipedia page "{w.name_of(self.target)}" by following links, in as few actions as possible.',
            f'You are on "{self.page["name"]}", part {self.chunk + 1} of {k}' + (f" ({'; '.join(more)})." if more else "."),
            f"Actions used: {len(self.actions)} of {self.max_steps}.",
        ]
        if self.message:
            lines.append(self.message)
        lines += ["-----", render_chunk(w, self.page, self.chunk), "-----",
                  'Reply with one action: click("Page name") for a link shown above, scroll_down(), or scroll_up().']
        return "\n".join(lines)

    def step(self, reply: str) -> tuple[str, bool]:
        """Apply the first action found in the agent's reply."""
        assert not self.done
        m = ACTION_RE.search(reply)
        visible = self.world.chunk_links[self.page_id][self.chunk]
        self.message = ""
        if m and m.group("name"):
            q = self.name_to_id.get(m.group("name").strip())
            if q is not None and q in visible:
                self.actions.append(("click", q))
                self.page_id, self.chunk, self.page = q, 0, self.world.page(q)
                self.pages.append(q)
            else:
                self.actions.append(("invalid", -1))
                self.message = f'"{m.group("name")}" is not a link in the part of the page you can see.'
        elif m and m.group("down") and self.chunk + 1 < len(self.page["chunks"]):
            self.actions.append(("scroll_down", -1))
            self.chunk += 1
        elif m and m.group("up") and self.chunk > 0:
            self.actions.append(("scroll_up", -1))
            self.chunk -= 1
        else:
            self.actions.append(("invalid", -1))
            self.message = "That action is not available here." if m else "No action was found in your reply."
        return self.observe(), self.done

    def score(self) -> dict:
        _, action_summary = score_actions(self.oracle, self.start, self.actions)
        _, click_summary = score_pages(self.oracle, self.pages)
        return {"actions": action_summary, "clicks": click_summary}

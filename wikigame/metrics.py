"""Exact oracle quantities for a task (start page s, target page t) and per-step scoring of trajectories.

Two cost models are computed side by side.

Click model (the classic game). The player sees the whole page, and every click costs 1.
  State = page. The actions at page p are its distinct out-links.
  d(p) is the fewest clicks from p to t (BFS on the page graph).

Action model (the agentic benchmark). The player sees one chunk of the page at a time.
  State = (page, chunk). The actions are: click any distinct link visible in the chunk (landing on chunk 0 of
  the new page), scroll_down or scroll_up. Every action costs 1.
  V(p, c) is the fewest actions from that state to t (BFS on the state graph).

Information. A uniformly random player is the reference: in every state it picks one of the available
actions with equal probability. P(x) is the probability that, starting at x, this player reaches t in exactly
the optimal number of steps (that is, it follows some optimal path). Then

    phi(x) = -log2 P(x)        bits the player must supply to play optimally from x (phi(t) = 0)

    P(x) = (1 / n(x)) * sum over optimal actions a of P(next(x, a))

where n(x) is the number of available actions. The information gained by an action x -> y is
phi(x) - phi(y). On an optimal step from a state where all k optimal actions lead to equally hard states,
this is log2(n / k): the bits needed to pick one of k good options out of n. A non-optimal step can lose
or gain bits: one that lands on a page with many good links gains them (docs/metrics.md has an example). The gains along any trajectory that reaches t add up to phi(start), so the
total is fixed by the task and the cost of a trajectory shows up as extra steps and lost bits.

Regret of a step = 1 + cost-to-go(after) - cost-to-go(before). It is 0 for an optimal step and >= 1 otherwise;
over a successful episode the regrets add up to (steps taken - optimal steps).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import scipy.sparse as sp

from .world import World

UNREACHABLE = -1


# ---------------------------------------------------------------------------------------------
# Graphs
# ---------------------------------------------------------------------------------------------


@dataclass
class Graphs:
    """Edge lists of the page graph and of the (page, chunk) state graph of a world."""

    page_src: np.ndarray
    page_dst: np.ndarray
    page_deg: np.ndarray
    offsets: np.ndarray  # state id of (p, c) = offsets[p] + c ; offsets[n] = number of states
    state_page: np.ndarray
    state_src: np.ndarray
    state_dst: np.ndarray
    state_kind: np.ndarray  # 0 click, 1 scroll_down, 2 scroll_up
    state_deg: np.ndarray

    @classmethod
    def build(cls, world: World) -> "Graphs":
        n = world.n
        src, dst = [], []
        for p in range(n):
            for q in world.out_links(p):
                src.append(p)
                dst.append(q)
        page_src, page_dst = np.array(src, dtype=np.int64), np.array(dst, dtype=np.int64)

        k = np.array([world.n_chunks(p) for p in range(n)], dtype=np.int64)
        offsets = np.concatenate([[0], np.cumsum(k)])
        state_page = np.repeat(np.arange(n), k)
        s_src, s_dst, s_kind = [], [], []
        for p in range(n):
            for c, links in enumerate(world.chunk_links[p]):
                x = offsets[p] + c
                for q in links:
                    s_src.append(x); s_dst.append(offsets[q]); s_kind.append(0)
                if c + 1 < k[p]:
                    s_src.append(x); s_dst.append(x + 1); s_kind.append(1)
                if c > 0:
                    s_src.append(x); s_dst.append(x - 1); s_kind.append(2)
        state_src = np.array(s_src, dtype=np.int64)
        state_dst = np.array(s_dst, dtype=np.int64)
        return cls(
            page_src=page_src, page_dst=page_dst, page_deg=np.bincount(page_src, minlength=n),
            offsets=offsets, state_page=state_page,
            state_src=state_src, state_dst=state_dst, state_kind=np.array(s_kind, dtype=np.int8),
            state_deg=np.bincount(state_src, minlength=int(offsets[-1])),
        )


def bfs_to(n: int, src: np.ndarray, dst: np.ndarray, targets: np.ndarray) -> np.ndarray:
    """Fewest edges from every node to any node in `targets` (UNREACHABLE if none)."""
    adj = sp.csr_matrix((np.ones(len(src), dtype=np.int8), (src, dst)), shape=(n, n))  # adj @ f marks predecessors of f
    dist = np.full(n, UNREACHABLE, dtype=np.int64)
    dist[targets] = 0
    frontier = np.zeros(n, dtype=bool)
    frontier[targets] = True
    level = 0
    while frontier.any():
        level += 1
        reached = (adj @ frontier.astype(np.int8)) > 0
        new = reached & (dist == UNREACHABLE)
        dist[new] = level
        frontier = new
    return dist


def optimal_path_mass(dist, src, dst, deg, terminal, normalise=True) -> np.ndarray:
    """P(x) (normalise=True) or the number of optimal paths (normalise=False), by increasing distance."""
    n = len(dist)
    out = np.zeros(n)
    out[terminal] = 1.0
    ok = (dist[src] >= 1) & (dist[dst] == dist[src] - 1) & ~np.isin(src, terminal)
    e_src, e_dst = src[ok], dst[ok]
    e_lvl = dist[e_src]
    order = np.argsort(e_lvl, kind="stable")
    e_src, e_dst, e_lvl = e_src[order], e_dst[order], e_lvl[order]
    bounds = np.searchsorted(e_lvl, np.arange(1, dist.max() + 2))
    for k in range(1, dist.max() + 1):
        a, b = bounds[k - 1], bounds[k]
        if a == b:
            continue
        s, d = e_src[a:b], e_dst[a:b]
        acc = np.bincount(s, weights=out[d], minlength=n)
        nodes = np.unique(s)
        out[nodes] = acc[nodes] / (deg[nodes] if normalise else 1.0)
    return out


def bits(P: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore"):
        return np.where(P > 0, -np.log2(P), np.inf)


# ---------------------------------------------------------------------------------------------
# Oracle for one target
# ---------------------------------------------------------------------------------------------


class Oracle:
    """Cost-to-go and bits-to-go to a fixed target, for every page and every (page, chunk) state."""

    def __init__(self, world: World, graphs: Graphs, target: int):
        self.world, self.g, self.target = world, graphs, target
        g, n = graphs, world.n
        # click model
        self.d = bfs_to(n, g.page_src, g.page_dst, np.array([target]))
        self.P_page = optimal_path_mass(self.d, g.page_src, g.page_dst, g.page_deg, np.array([target]))
        self.phi_page = bits(self.P_page)
        self.n_paths_to = optimal_path_mass(self.d, g.page_src, g.page_dst, g.page_deg, np.array([target]), normalise=False)
        # action model: every chunk of the target page is terminal (the episode ends on arrival)
        ns = int(g.offsets[-1])
        term = np.arange(g.offsets[target], g.offsets[target + 1])
        self.V = bfs_to(ns, g.state_src, g.state_dst, term)
        self.P_state = optimal_path_mass(self.V, g.state_src, g.state_dst, g.state_deg, term)
        self.phi_state = bits(self.P_state)

    def state(self, page: int, chunk: int = 0) -> int:
        return int(self.g.offsets[page] + chunk)

    # -- shortest-path DAG between a start and the target (click model) ---------------------
    def dag(self, start: int):
        """Pages on at least one shortest start->target path, by layer, with the number of such paths through each."""
        g, n = self.g, self.world.n
        ds = bfs_to(n, g.page_dst, g.page_src, np.array([start]))  # reversed edges = distance from start
        D = self.d[start]
        on = (ds >= 0) & (self.d >= 0) & (ds + self.d == D)
        # number of shortest paths from start to v
        n_from = np.zeros(n)
        n_from[start] = 1
        ok = on[g.page_src] & on[g.page_dst] & (ds[g.page_dst] == ds[g.page_src] + 1)
        es, ed = g.page_src[ok], g.page_dst[ok]
        for k in range(1, D + 1):
            m = ds[ed] == k
            n_from += np.bincount(ed[m], weights=n_from[es[m]], minlength=n)
        through = n_from * self.n_paths_to
        layers = [np.flatnonzero(on & (ds == k)) for k in range(D + 1)]
        return layers, list(zip(es.tolist(), ed.tolist())), through

    def optimal_trajectory(self, start: int) -> list[tuple[str, int]]:
        """One action-optimal trajectory: at each state take the optimal action whose next state is easiest."""
        g = self.g
        x, acts = self.state(start), []
        while g.state_page[x] != self.target:
            idx = np.flatnonzero(g.state_src == x)
            nxt = g.state_dst[idx]
            ok = self.V[nxt] == self.V[x] - 1
            best = idx[ok][np.argmax(self.P_state[nxt[ok]])]
            kind = int(g.state_kind[best])
            acts.append(("click", int(g.state_page[g.state_dst[best]])) if kind == 0 else (("scroll_down", -1) if kind == 1 else ("scroll_up", -1)))
            x = int(g.state_dst[best])
        return acts


# ---------------------------------------------------------------------------------------------
# Scoring trajectories
# ---------------------------------------------------------------------------------------------


def _step(cost_before, cost_after, phi_before, phi_after, n_actions, n_optimal):
    regret = None if cost_after < 0 or cost_before < 0 else 1 + cost_after - cost_before
    return {
        "cost_before": cost_before, "cost_after": cost_after, "regret": regret, "optimal": regret == 0,
        "n_actions": n_actions, "n_optimal": n_optimal,
        "chance_bits": math.log2(n_actions / n_optimal) if n_optimal else math.inf,
        "phi_before": phi_before, "phi_after": phi_after, "info_gain": phi_before - phi_after,
    }


def score_pages(oracle: Oracle, pages: list[int]) -> tuple[list[dict], dict]:
    """Click-model scoring of a page sequence (a back-click is just a move to the previous page)."""
    o, g = oracle, oracle.g
    steps = []
    for i, (p, q) in enumerate(zip(pages[:-1], pages[1:])):
        outs = g.page_dst[g.page_src == p]
        n_opt = int(np.sum(o.d[outs] == o.d[p] - 1))
        s = _step(int(o.d[p]), int(o.d[q]), float(o.phi_page[p]), float(o.phi_page[q]), len(outs), n_opt)
        steps.append({"step": i + 1, "from": p, "to": q, "link": bool(q in set(outs.tolist())), **s})
    return steps, _summary(steps, int(o.d[pages[0]]), float(o.phi_page[pages[0]]), pages[-1] == o.target)


def score_actions(oracle: Oracle, start: int, actions: list[tuple[str, int]]) -> tuple[list[dict], dict]:
    """Action-model scoring. actions: ('click', page) | ('scroll_down', -1) | ('scroll_up', -1) | ('invalid', -1)."""
    o, g, w = oracle, oracle.g, oracle.world
    p, c = start, 0
    steps = []
    for i, (kind, q) in enumerate(actions):
        x = o.state(p, c)
        nxt = g.state_dst[g.state_src == x]
        n_opt = int(np.sum(o.V[nxt] == o.V[x] - 1))
        if kind == "click":
            assert q in w.chunk_links[p][c], f"{w.name_of(q)} is not visible on {w.name_of(p)} chunk {c}"
            p2, c2 = q, 0
        elif kind == "scroll_down":
            p2, c2 = p, min(c + 1, w.n_chunks(p) - 1)
        elif kind == "scroll_up":
            p2, c2 = p, max(c - 1, 0)
        else:  # an invalid action costs one step and leaves the state unchanged
            p2, c2 = p, c
        y = o.state(p2, c2)
        s = _step(int(o.V[x]), int(o.V[y]), float(o.phi_state[x]), float(o.phi_state[y]), len(nxt), n_opt)
        steps.append({"step": i + 1, "action": kind, "page": p, "chunk": c, "to_page": p2, "to_chunk": c2, **s})
        p, c = p2, c2
        if p == o.target:
            break
    x0 = o.state(start)
    return steps, _summary(steps, int(o.V[x0]), float(o.phi_state[x0]), p == o.target)


def _summary(steps, optimal, phi_start, success) -> dict:
    n = len(steps)
    errors = [s["step"] for s in steps if not s["optimal"]]
    return {
        "success": bool(success), "steps": n, "optimal_steps": optimal,
        "regret": (n - optimal) if success else None,
        "efficiency": (optimal / n) if success and n else 0.0,
        "bits_needed": phi_start,
        "bits_lost": float(sum(-s["info_gain"] for s in steps if s["info_gain"] < 0 and math.isfinite(s["info_gain"]))),
        "first_error": errors[0] if errors else None,
    }

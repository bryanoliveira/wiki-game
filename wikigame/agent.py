"""Play the benchmark with a language model served behind an OpenAI-compatible chat API.

The model plays one episode as a chat. The system message states the rules. Each user message is the current
observation (wikigame.env.WikiGameEnv.observe), and each assistant message is the model's reply. The whole episode
stays in the context, so the model can remember the pages it has seen. Only the final answer of a reasoning model
is kept in the history; its hidden reasoning is recorded but not sent back.

The endpoint and key come from the environment variables LLM_BASE_URL (for example http://localhost:8000/v1) and
LLM_API_KEY, so no secret is stored in the repository.
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request

from .env import WikiGameEnv

SYSTEM_PROMPT = """You are playing the Wikipedia game. You start on one Wikipedia article and must reach a target \
article by following links, using as few actions as possible.

You see one part of the current article at a time. Links are written as [anchor text](Target page name). Each turn, \
choose exactly one action:
- click("Target page name") follows a link shown in the part you can see. Use the target page name from inside the \
parentheses, exactly as written.
- scroll_down() shows the next part of the article.
- scroll_up() shows the previous part.

Every action costs one step, including invalid ones. End your reply with the action you choose."""


def chat(messages: list[dict], model: str, temperature: float, max_tokens: int, reasoning_effort: str | None,
         seed: int | None = None, retries: int = 4, timeout: float = 600) -> dict:
    """One chat completion. Returns {'content', 'reasoning', 'usage', 'seconds'}; raises after `retries` failures."""
    body = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    if reasoning_effort is not None:
        body["reasoning_effort"] = reasoning_effort
    if seed is not None:
        body["seed"] = seed
    request = urllib.request.Request(
        os.environ["LLM_BASE_URL"].rstrip("/") + "/chat/completions", json.dumps(body).encode(),
        {"Authorization": "Bearer " + os.environ["LLM_API_KEY"], "Content-Type": "application/json"},
    )
    for attempt in range(retries):
        t0 = time.time()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.load(response)
            message = data["choices"][0]["message"]
            return {
                "content": message.get("content") or "",
                "reasoning": message.get("reasoning_content") or message.get("reasoning") or "",
                "usage": data.get("usage", {}),
                "seconds": time.time() - t0,
            }
        except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError) as error:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt + random.random())
    raise AssertionError("unreachable")


def play(env: WikiGameEnv, model: str, temperature: float = 0.7, max_tokens: int = 2048,
         reasoning_effort: str | None = None, seed: int | None = None) -> dict:
    """Play one episode to the end. Returns the action list, the per-turn log and the scores."""
    env.reset()
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    turns = []
    t0 = time.time()
    while not env.done:
        messages.append({"role": "user", "content": env.observe()})
        reply = chat(messages, model, temperature, max_tokens, reasoning_effort,
                     seed=None if seed is None else seed * 1000 + len(turns))
        messages.append({"role": "assistant", "content": reply["content"]})
        env.step(reply["content"])
        turns.append({
            "reply": reply["content"], "reasoning_chars": len(reply["reasoning"]),
            "action": list(env.actions[-1]), "message": env.message, "seconds": round(reply["seconds"], 2),
            "prompt_tokens": reply["usage"].get("prompt_tokens"), "completion_tokens": reply["usage"].get("completion_tokens"),
        })
    return {
        "actions": [list(a) for a in env.actions], "pages": env.pages, "turns": turns,
        "seconds": round(time.time() - t0, 1), "score": env.score(),
    }


def play_random(env: WikiGameEnv, rng: random.Random) -> dict:
    """The reference player of the bits measure: a uniformly random choice among the available actions."""
    env.reset()
    while not env.done:
        options = [f'click("{env.world.name_of(q)}")' for q in env.world.chunk_links[env.page_id][env.chunk]]
        if env.chunk + 1 < len(env.page["chunks"]):
            options.append("scroll_down()")
        if env.chunk > 0:
            options.append("scroll_up()")
        env.step(rng.choice(options) if options else "")
    return {"actions": [list(a) for a in env.actions], "pages": env.pages, "score": env.score()}

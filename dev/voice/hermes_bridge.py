"""HermesLLM — a livekit-agents LLM node backed by the Hermes agent API.

Instead of calling a model, each voice turn POSTs the user's transcript to
Hermes' api_server platform (SSE session endpoint) and streams the
`assistant.delta` events back as LLM tokens. Hermes runs the FULL agent
server-side: skills, MCP tools (EPG graph, Graphiti memory), rules.

Behaviors:
- speech sanitation: markdown stripped per-delta before TTS sees it
- progress filler: if a tool starts before any text arrives, speak a filler
- barge-in: cancellation (LiveKit interruption) aborts the HTTP request,
  which cancels the Hermes run server-side
- serialization: one turn at a time per session (the API server is
  last-writer-wins on concurrent turns)
"""

from __future__ import annotations

import asyncio
import json
import re

import aiohttp
from livekit.agents import llm
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

FILLER = "One moment, let me check."

_MD_PATTERNS = [
    (re.compile(r"```.*?```", re.S), " "),      # fenced code
    (re.compile(r"`([^`]*)`"), r"\1"),           # inline code
    (re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1"),  # links -> text
    (re.compile(r"^#{1,6}\s*", re.M), ""),       # headings
    (re.compile(r"^\s*[-*•]\s+", re.M), ""),     # bullets
    (re.compile(r"[|*_>#]"), " "),               # table pipes, emphasis, quotes
    (re.compile(r"\s{2,}"), " "),
]


def sanitize(text: str) -> str:
    for pat, repl in _MD_PATTERNS:
        text = pat.sub(repl, text)
    return text


class HermesLLM(llm.LLM):
    def __init__(self, *, base_url: str, api_key: str, session_id: str,
                 session_key: str) -> None:
        super().__init__()
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._session_id = session_id
        self._session_key = session_key
        self._turn_lock = asyncio.Lock()

    def chat(self, *, chat_ctx: llm.ChatContext, tools=None, conn_options=DEFAULT_API_CONNECT_OPTIONS,
             parallel_tool_calls=None, tool_choice=None, extra_kwargs=None) -> "HermesLLMStream":
        return HermesLLMStream(self, chat_ctx=chat_ctx, tools=tools or [],
                               conn_options=conn_options)


class HermesLLMStream(llm.LLMStream):
    def __init__(self, hermes: HermesLLM, *, chat_ctx, tools, conn_options) -> None:
        super().__init__(hermes, chat_ctx=chat_ctx, tools=tools, conn_options=conn_options)
        self._hermes = hermes

    def _last_user_text(self) -> str:
        # The transcript of the utterance is the last user message in ctx.
        for item in reversed(self._chat_ctx.items):
            if getattr(item, "role", None) == "user":
                content = item.content
                if isinstance(content, list):
                    content = " ".join(c for c in content if isinstance(c, str))
                return str(content or "").strip()
        return ""

    def _emit(self, run_id: str, text: str) -> None:
        if not text:
            return
        self._event_ch.send_nowait(
            llm.ChatChunk(id=run_id, delta=llm.ChoiceDelta(role="assistant", content=text))
        )

    async def _run(self) -> None:
        text = self._last_user_text()
        if not text:
            return
        h = self._hermes
        url = f"{h._base_url}/api/sessions/{h._session_id}/chat/stream"
        headers = {
            "Authorization": f"Bearer {h._api_key}",
            "Content-Type": "application/json",
            "X-Hermes-Session-Key": h._session_key,
        }
        run_id = "hermes"
        spoke = False
        filler_sent = False

        async with h._turn_lock:
            timeout = aiohttp.ClientTimeout(total=300, sock_connect=10)
            async with aiohttp.ClientSession(timeout=timeout) as http:
                async with http.post(url, headers=headers,
                                     json={"message": text}) as resp:
                    resp.raise_for_status()
                    event = ""
                    async for raw in resp.content:
                        line = raw.decode("utf-8", "replace").rstrip("\n")
                        if line.startswith("event: "):
                            event = line[7:].strip()
                            continue
                        if not line.startswith("data: "):
                            continue
                        try:
                            data = json.loads(line[6:])
                        except json.JSONDecodeError:
                            continue
                        if event == "run.started":
                            run_id = data.get("run_id", run_id)
                        elif event == "assistant.delta":
                            delta = sanitize(data.get("delta", ""))
                            if delta.strip():
                                spoke = True
                                self._emit(run_id, delta)
                        elif event == "tool.started" and not spoke and not filler_sent:
                            filler_sent = True
                            self._emit(run_id, FILLER + " ")
                        elif event == "assistant.completed" and not spoke:
                            # Non-streaming completion (e.g. an error surface):
                            # speak the sanitized final content once.
                            content = sanitize(data.get("content", ""))
                            if content.strip():
                                spoke = True
                                self._emit(run_id, content)
                        elif event in ("done", "error"):
                            if event == "error" and not spoke:
                                self._emit(run_id, "Sorry, something went wrong on my side.")
                            break

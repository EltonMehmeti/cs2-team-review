"""The one place that talks to a language model (PLAN.md section 7).

Nothing else in the project imports a provider SDK. Every call goes through
an `LLM` object, which:

  * picks a provider from config.toml ([llm] provider = ...):
      anthropic  the Claude API, key from the ANTHROPIC_API_KEY environment variable
      manual     writes the prompt to out/prompt_<key>.md and reads your answer
                 from out/answer_<key>.json; paste the prompt into any chat app
                 and pay nothing
      fake       canned answers, for tests only
  * stops before a call that could push the run over max_cost_usd_per_run
  * logs provider, model, tokens and cost of every call to the llm_calls table

Usage:

    llm = LLM.from_config(cfg.llm, engine)
    result = llm.complete(system, user, max_tokens=4000, json_schema=SCHEMA,
                          purpose="report", key=match_id)
    result.text, result.input_tokens, result.output_tokens, result.cost_usd
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy import Engine

from .config import LLMSettings
from .db import LLMCall


class LLMError(RuntimeError):
    """The model call failed or gave an unusable answer."""


class BudgetExceeded(LLMError):
    """The next call could push this run over max_cost_usd_per_run."""


class ManualAnswerNeeded(LLMError):
    """Manual mode: the prompt is written, the answer file does not exist yet."""


@dataclass
class LLMResult:
    text: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    provider: str
    model: str


@dataclass
class SearchResult:
    """An answer written with Claude's web search tool (Phase 8)."""

    text: str
    input_tokens: int
    output_tokens: int
    searches: int  # web searches the model ran (billed per search)
    urls: list[dict]  # every result the search tool returned: {"url", "title", "page_age"}
    cost_usd: float = 0.0


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class ToolTurn:
    """One model turn in a tool-using conversation (Phase 5's question agent)."""

    text: str  # any text the model wrote this turn
    tool_calls: list[ToolCall]
    done: bool  # True when the model answered instead of calling a tool
    assistant_message: dict  # append this to the message list as is
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


def estimate_tokens(text: str) -> int:
    return (len(text) + 3) // 4


def extract_json(text: str) -> str:
    """The JSON object inside an answer that may have ```json fences or a sentence around it."""
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        return fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        raise LLMError("The answer contains no JSON object.")
    return text[start:end + 1]


# --------------------------------------------------------------------------
# providers
# --------------------------------------------------------------------------

class AnthropicProvider:
    """The Claude API through the official `anthropic` SDK."""

    name = "anthropic"

    def __init__(self, settings: LLMSettings):
        try:
            import anthropic  # imported here so manual mode works without the package
        except ImportError as exc:  # pragma: no cover - depends on the machine
            raise LLMError("The anthropic package is missing. Run: pip install -r requirements.txt") from exc
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            raise LLMError(
                "ANTHROPIC_API_KEY is not set. Put your key in that environment variable "
                "(never in config.toml), or use --provider manual."
            )
        self._anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.settings = settings

    def _output_config(self, json_schema: dict | None) -> dict | None:
        config: dict = {}
        if self.settings.effort:
            config["effort"] = self.settings.effort
        if json_schema is not None:
            config["format"] = {"type": "json_schema", "schema": json_schema}
        return config or None

    def _create(self, **kwargs):
        anthropic = self._anthropic
        try:
            return self.client.messages.create(model=self.settings.model, **kwargs)
        except anthropic.AuthenticationError as exc:
            raise LLMError("The Claude API rejected the key in ANTHROPIC_API_KEY.") from exc
        except anthropic.NotFoundError as exc:
            raise LLMError(f"Unknown model '{self.settings.model}'. Check [llm] model in config.toml.") from exc
        except anthropic.RateLimitError as exc:
            raise LLMError("Rate limited by the Claude API. Try again in a minute.") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Claude API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("Could not reach the Claude API. Check the internet connection.") from exc

    @staticmethod
    def _check_stop(response) -> None:
        # check stop_reason before reading content: a refusal can come with no text at all
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise LLMError(f"The model declined the request (category: {category}).")
        if response.stop_reason == "max_tokens":
            raise LLMError("The answer was cut off at max_tokens. Raise the limit or shorten the input.")

    def complete(self, system: str, user: str, max_tokens: int, json_schema: dict | None) -> tuple[str, int, int]:
        kwargs = {"max_tokens": max_tokens, "system": system, "messages": [{"role": "user", "content": user}]}
        output_config = self._output_config(json_schema)
        if output_config:
            kwargs["output_config"] = output_config
        response = self._create(**kwargs)
        self._check_stop(response)
        text = "".join(b.text for b in response.content if b.type == "text")
        return text, response.usage.input_tokens, response.usage.output_tokens

    def search(self, system: str, user: str, max_tokens: int, max_searches: int, tool_type: str) -> SearchResult:
        """One question answered with the server-side web search tool.

        The tool runs on Anthropic's side; we only read the results. A
        `pause_turn` stop means the server paused a long turn: send the
        conversation back to let it continue.
        """
        messages: list = [{"role": "user", "content": user}]
        tools = [{"type": tool_type, "name": "web_search", "max_uses": max_searches}]
        tin = tout = searches = 0
        urls: list[dict] = []
        text = ""
        for _ in range(5):
            kwargs = {"max_tokens": max_tokens, "system": system, "messages": messages, "tools": tools}
            output_config = self._output_config(None)
            if output_config:
                kwargs["output_config"] = output_config
            response = self._create(**kwargs)
            tin += response.usage.input_tokens
            tout += response.usage.output_tokens
            server_use = getattr(response.usage, "server_tool_use", None)
            searches += int(getattr(server_use, "web_search_requests", 0) or 0) if server_use else 0
            for block in response.content:
                if block.type == "web_search_tool_result" and isinstance(block.content, list):  # an error is an object
                    for item in block.content:
                        if getattr(item, "type", "") == "web_search_result":
                            urls.append({"url": item.url, "title": getattr(item, "title", ""),
                                         "page_age": getattr(item, "page_age", None)})
            text = "".join(b.text for b in response.content if b.type == "text")
            if response.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": response.content})
                continue
            self._check_stop(response)
            break
        return SearchResult(text=text, input_tokens=tin, output_tokens=tout, searches=searches, urls=urls)

    def tool_turn(self, system: str, messages: list, tools: list[dict], max_tokens: int) -> tuple[ToolTurn, int, int]:
        kwargs = {"max_tokens": max_tokens, "system": system, "messages": messages, "tools": tools}
        output_config = self._output_config(None)
        if output_config:
            kwargs["output_config"] = output_config
        response = self._create(**kwargs)
        self._check_stop(response)
        calls = [ToolCall(b.id, b.name, dict(b.input)) for b in response.content if b.type == "tool_use"]
        text = "".join(b.text for b in response.content if b.type == "text")
        turn = ToolTurn(
            text=text, tool_calls=calls, done=not calls,
            # the full content (thinking and tool_use blocks too) must go back unchanged
            assistant_message={"role": "assistant", "content": response.content},
        )
        return turn, response.usage.input_tokens, response.usage.output_tokens


class ManualProvider:
    """You are the provider: copy the prompt into any chat app, save the answer."""

    name = "manual"

    def __init__(self, settings: LLMSettings):
        self.out_dir = Path(settings.out_dir)

    def prompt_path(self, key: str) -> Path:
        return self.out_dir / f"prompt_{key}.md"

    def answer_path(self, key: str) -> Path:
        return self.out_dir / f"answer_{key}.json"

    def complete(self, system: str, user: str, max_tokens: int, json_schema: dict | None, key: str) -> tuple[str, int, int]:
        answer = self.answer_path(key)
        if answer.exists():
            text = answer.read_text(encoding="utf-8")
            return text, estimate_tokens(system + user), estimate_tokens(text)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        parts = [
            "<!-- Copy everything below into a chat with any capable model. -->",
            "",
            "# Instructions",
            "",
            system.strip(),
            "",
            "# Task",
            "",
            user.strip(),
        ]
        if json_schema is not None:
            parts += ["", "# Answer format", "",
                      "Answer with one JSON object only, no other text, matching this JSON schema:", "",
                      "```json", json.dumps(json_schema, indent=2), "```"]
        self.prompt_path(key).write_text("\n".join(parts) + "\n", encoding="utf-8")
        raise ManualAnswerNeeded(
            f"Wrote {self.prompt_path(key)}. Paste it into a chat app, save the JSON answer as "
            f"{answer}, then run the same command again."
        )

    def tool_turn(self, *args, **kwargs):
        raise LLMError("The manual provider cannot use tools. Use the anthropic provider for this command.")

    def search(self, *args, **kwargs):
        raise LLMError("The manual provider cannot search the web. Use the anthropic provider for this command.")


class FakeProvider:
    """Canned answers for tests. `answers` is a list (used in order) or a function."""

    name = "fake"

    def __init__(self, answers: list | Callable | None = None):
        self.answers = answers if answers is not None else []
        self.calls: list[dict] = []

    def _next(self, **request):
        self.calls.append(request)
        if callable(self.answers):
            return self.answers(**request)
        if not self.answers:
            raise LLMError("The fake provider has no answers left.")
        return self.answers.pop(0)

    def complete(self, system: str, user: str, max_tokens: int, json_schema: dict | None) -> tuple[str, int, int]:
        text = self._next(system=system, user=user, json_schema=json_schema)
        return text, estimate_tokens(system + user), estimate_tokens(text)

    def tool_turn(self, system: str, messages: list, tools: list[dict], max_tokens: int) -> tuple[ToolTurn, int, int]:
        turn = self._next(system=system, messages=list(messages), tools=tools)
        return turn, estimate_tokens(system + json.dumps(messages, default=str)), 50

    def search(self, system: str, user: str, max_tokens: int, max_searches: int, tool_type: str) -> SearchResult:
        """The canned answer is a SearchResult (or a dict with its fields)."""
        answer = self._next(system=system, user=user, max_searches=max_searches, tool_type=tool_type)
        return answer if isinstance(answer, SearchResult) else SearchResult(**answer)


# --------------------------------------------------------------------------
# the interface everything else uses
# --------------------------------------------------------------------------

@dataclass
class LLM:
    provider: object
    settings: LLMSettings
    engine: Engine | None = None
    spent_usd: float = 0.0  # cost of the calls made by this object (one "run")
    calls: list[dict] = field(default_factory=list)

    @classmethod
    def from_config(cls, settings: LLMSettings, engine: Engine | None = None, provider: str | None = None) -> "LLM":
        name = provider or settings.provider
        if name == "anthropic":
            return cls(AnthropicProvider(settings), settings, engine)
        if name == "manual":
            return cls(ManualProvider(settings), settings, engine)
        raise LLMError(f"Unknown LLM provider '{name}'. Use 'anthropic' or 'manual'.")

    @property
    def provider_name(self) -> str:
        return self.provider.name

    @property
    def model(self) -> str:
        return {"manual": "manual", "fake": "fake"}.get(self.provider_name, self.settings.model)

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        if self.provider_name != "anthropic":
            return 0.0  # manual and fake calls cost us nothing
        return round(input_tokens * self.settings.price_in_per_mtok / 1e6
                     + output_tokens * self.settings.price_out_per_mtok / 1e6, 6)

    def _check_budget(self, prompt_text: str, max_tokens: int) -> None:
        # worst case: the whole prompt plus max_tokens of output
        worst = self.cost(estimate_tokens(prompt_text), max_tokens)
        if self.spent_usd + worst > self.settings.max_cost_usd_per_run:
            raise BudgetExceeded(
                f"Stopped: this call could cost up to ${worst:.4f}, and ${self.spent_usd:.4f} is already spent; "
                f"the cap is ${self.settings.max_cost_usd_per_run:.2f} per run ([llm] max_cost_usd_per_run)."
            )
        if estimate_tokens(prompt_text) > 50_000:
            raise LLMError("The prompt would be over 50,000 tokens. Shrink the input instead (PLAN.md section 7).")

    def _log(self, purpose: str, key: str | None, input_tokens: int, output_tokens: int, cost: float,
             ok: bool, error: str | None = None, searches: int = 0) -> None:
        row = {
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "provider": self.provider_name, "model": self.model, "purpose": purpose, "match_id": key,
            "input_tokens": int(input_tokens), "output_tokens": int(output_tokens), "cost_usd": float(cost),
            "searches": int(searches), "ok": ok, "error": error,
        }
        self.calls.append(row)
        if self.engine is not None:
            with self.engine.begin() as conn:
                conn.execute(LLMCall.__table__.insert().values(**row))

    def complete(self, system: str, user: str, max_tokens: int, json_schema: dict | None = None,
                 purpose: str = "", key: str | None = None) -> LLMResult:
        """One request, one answer. `key` names the prompt file in manual mode."""
        self._check_budget(system + user, max_tokens)
        try:
            if isinstance(self.provider, ManualProvider):
                text, tin, tout = self.provider.complete(system, user, max_tokens, json_schema, key or purpose or "prompt")
            else:
                text, tin, tout = self.provider.complete(system, user, max_tokens, json_schema)
        except ManualAnswerNeeded:
            raise
        except LLMError as exc:
            self._log(purpose, key, 0, 0, 0.0, ok=False, error=str(exc))
            raise
        cost = self.cost(tin, tout)
        self.spent_usd += cost
        self._log(purpose, key, tin, tout, cost, ok=True)
        return LLMResult(text=text, input_tokens=tin, output_tokens=tout, cost_usd=cost,
                         provider=self.provider_name, model=self.model)

    def search(self, system: str, user: str, max_tokens: int = 6000, max_searches: int = 5,
               tool_type: str = "web_search_20250305", price_per_search: float = 0.01,
               purpose: str = "scout", key: str | None = None) -> SearchResult:
        """One answer with the web search tool. Searches count toward the cost cap."""
        worst_searches = max_searches * price_per_search if self.provider_name == "anthropic" else 0.0
        worst = self.cost(estimate_tokens(system + user), max_tokens) + worst_searches
        if self.spent_usd + worst > self.settings.max_cost_usd_per_run:
            raise BudgetExceeded(
                f"Stopped: this search could cost up to ${worst:.4f} (with {max_searches} searches), "
                f"${self.spent_usd:.4f} is already spent, the cap is ${self.settings.max_cost_usd_per_run:.2f}."
            )
        try:
            result = self.provider.search(system, user, max_tokens, max_searches, tool_type)
        except LLMError as exc:
            self._log(purpose, key, 0, 0, 0.0, ok=False, error=str(exc))
            raise
        cost = self.cost(result.input_tokens, result.output_tokens)
        if self.provider_name == "anthropic":
            cost = round(cost + result.searches * price_per_search, 6)
        result.cost_usd = cost
        self.spent_usd += cost
        self._log(purpose, key, result.input_tokens, result.output_tokens, cost, ok=True, searches=result.searches)
        return result

    def tool_turn(self, system: str, messages: list, tools: list[dict], max_tokens: int = 4000,
                  purpose: str = "", key: str | None = None) -> ToolTurn:
        """One turn of a tool-using conversation. The caller runs the tools and loops."""
        self._check_budget(system + json.dumps(messages, default=str), max_tokens)
        try:
            turn, tin, tout = self.provider.tool_turn(system, messages, tools, max_tokens)
        except LLMError as exc:
            self._log(purpose, key, 0, 0, 0.0, ok=False, error=str(exc))
            raise
        cost = self.cost(tin, tout)
        self.spent_usd += cost
        self._log(purpose, key, tin, tout, cost, ok=True)
        turn.input_tokens, turn.output_tokens, turn.cost_usd = tin, tout, cost
        return turn

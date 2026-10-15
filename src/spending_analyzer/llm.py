"""Provider-neutral LLM interface, Claude implementation and payload audit log.

Callers depend only on `LLMClient`. Adding another provider (Azure OpenAI, a local
Ollama model) means writing one class with a `complete_json` method.
"""

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"
DEFAULT_OLLAMA_MODEL = "qwen3.5:9b"  # the model all recorded eval results use

# USD per million tokens (input, output)
PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResult:
    data: dict
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None = None  # None when the price is unknown


def claude_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    if model not in PRICES:
        return None
    input_price, output_price = PRICES[model]
    return (input_tokens * input_price + output_tokens * output_price) / 1e6


class LLMClient(Protocol):
    model: str
    runs_locally: bool  # decides how much transaction context may be sent

    def complete_json(self, system: str, prompt: str, schema: dict) -> LLMResult:
        """Return a response that conforms to the JSON schema."""


class ClaudeClient:
    runs_locally = False

    def __init__(self, model: str | None = None, effort: str = "low"):
        import anthropic  # imported lazily so tests and offline use don't need it

        self.client = anthropic.Anthropic()
        self.model = model or os.environ.get("SPENDING_LLM_MODEL", DEFAULT_CLAUDE_MODEL)
        self.effort = effort

    def complete_json(self, system: str, prompt: str, schema: dict) -> LLMResult:
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": self.effort,
                           "format": {"type": "json_schema", "schema": schema}},
            # On a safety decline, the API retries on a suitable fallback model
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            raise LLMError(f"Request refused: {response.stop_details}")
        if response.stop_reason == "max_tokens":
            raise LLMError("Response truncated (max_tokens)")
        text = next(block.text for block in response.content if block.type == "text")
        usage = response.usage
        return LLMResult(data=json.loads(text), model=response.model,
                         input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                         cost_usd=claude_cost(response.model, usage.input_tokens,
                                              usage.output_tokens))


class OllamaClient:
    """Local model served by Ollama. Nothing leaves the machine; cost is zero."""

    runs_locally = True

    def __init__(self, model: str = DEFAULT_OLLAMA_MODEL, host: str | None = None,
                 think: bool | None = False, context_tokens: int = 8192):
        """think=None leaves the setting out, for models without a thinking mode."""
        self.model = model
        self.host = host or ollama_host()
        self.think = think
        self.context_tokens = context_tokens  # bounded so the model stays in VRAM

    def complete_json(self, system: str, prompt: str, schema: dict) -> LLMResult:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": prompt}],
            "format": schema,
            "stream": False,
            "options": {"temperature": 0, "num_ctx": self.context_tokens},
        }
        if self.think is not None:
            body["think"] = self.think
        request = urllib.request.Request(
            f"{self.host}/api/chat", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                data = json.load(response)
        except urllib.error.URLError as error:
            raise LLMError(f"Ollama request failed: {error}") from error
        if data.get("done_reason") == "length":
            raise LLMError("Response truncated (context or output limit)")
        try:
            content = json.loads(data["message"]["content"])
        except (KeyError, json.JSONDecodeError) as error:
            raise LLMError(f"{self.model} returned no valid JSON") from error
        return LLMResult(data=content, model=self.model,
                         input_tokens=data.get("prompt_eval_count", 0),
                         output_tokens=data.get("eval_count", 0), cost_usd=0.0)


@dataclass
class OllamaModel:
    name: str
    parameter_size: str  # e.g. "9.0B"
    quantization: str  # e.g. "Q4_K_M"
    size_bytes: int
    capabilities: list[str]

    @property
    def can_think(self) -> bool:
        return "thinking" in self.capabilities


def ollama_host() -> str:
    return os.environ.get("OLLAMA_HOST", "http://localhost:11434")


def list_ollama_models(host: str | None = None, timeout: float = 2) -> list[OllamaModel]:
    """Installed models that can generate text (embedding-only models are left out).
    Raises LLMError when Ollama isn't running."""
    try:
        with urllib.request.urlopen(f"{host or ollama_host()}/api/tags",
                                    timeout=timeout) as response:
            entries = json.load(response).get("models", [])
    except (OSError, json.JSONDecodeError) as error:
        raise LLMError(f"Ollama is not reachable: {error}") from error
    models = []
    for entry in entries:
        details = entry.get("details", {})
        capabilities = entry.get("capabilities") or ["completion"]  # older Ollama omits it
        if "completion" not in capabilities:
            continue
        models.append(OllamaModel(entry["name"], details.get("parameter_size", "?"),
                                  details.get("quantization_level", "?"),
                                  entry.get("size", 0), capabilities))
    return sorted(models, key=lambda m: m.name)


def make_client(provider: str = "ollama", model: str | None = None) -> LLMClient:
    if provider == "claude":
        return ClaudeClient(model=model)
    if provider == "ollama":
        return OllamaClient(model=model) if model else OllamaClient()
    raise ValueError(f"Unknown provider {provider!r}")


class AuditedClient:
    """Wraps any LLMClient and logs every outgoing payload to a local JSONL file.

    The log is what tests and reviewers check to verify that no personal data
    leaves the machine.
    """

    def __init__(self, inner: LLMClient, log_path: Path = Path("data/llm_audit.jsonl")):
        self.inner = inner
        self.log_path = log_path
        self.model = inner.model
        self.runs_locally = inner.runs_locally

    def complete_json(self, system: str, prompt: str, schema: dict) -> LLMResult:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"time": datetime.now(timezone.utc).isoformat(),
                 "system": system, "prompt": prompt}
        try:
            result = self.inner.complete_json(system, prompt, schema)
        except Exception as error:
            entry["error"] = str(error)
            raise
        else:
            entry |= {"model": result.model, "input_tokens": result.input_tokens,
                      "output_tokens": result.output_tokens, "cost_usd": result.cost_usd}
            return result
        finally:
            with self.log_path.open("a", encoding="utf-8") as log:
                log.write(json.dumps(entry, ensure_ascii=False) + "\n")

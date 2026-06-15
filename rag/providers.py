"""Abstraction multi-provider pour l'embedding et le chat.

- Embedder : OpenAI | Mistral | vLLM  (PAS Anthropic — pas d'API d'embeddings).
- Chat     : OpenAI | Anthropic | Mistral | vLLM.

Le choix se fait au lancement via le .env (EMBED_PROVIDER / CHAT_PROVIDER).
vLLM est servi via l'API OpenAI-compatible (même client `openai`, base_url custom).
Les SDK ne sont importés qu'à l'usage (lazy) pour ne pas exiger tous les paquets.
"""
from __future__ import annotations

import random
import time
from typing import Protocol

from config import Config, cfg


def _batched(items: list, size: int):
    """Découpe une liste en lots de `size` (le service e5 limite à 8 par appel)."""
    size = max(1, size)
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _with_retry(fn, *, what: str = "embedding", log=None):
    """Exécute `fn` avec retry + backoff exponentiel.

    Le service d'embedding est servi par TEI (Text Embeddings Inference), qui
    entre en « panic mode » sous charge : il renvoie alors 429/503 jusqu'à se
    stabiliser. On réessaie ces cas (et autres erreurs réseau transitoires) avec
    un backoff exponentiel + jitter. Les erreurs non transitoires (400/413/422)
    sont relevées immédiatement — inutile d'insister.
    """
    # Patient : TEI peut crasher puis mettre 30-60 s à redémarrer.
    max_attempts = 8
    base_delay = 2.0
    max_delay = 45.0
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - on inspecte le type/status
            status = getattr(exc, "status_code", None) or getattr(
                getattr(exc, "response", None), "status_code", None
            )
            name = type(exc).__name__
            # Connexion refusée (TEI down/redémarre) ou statut transitoire -> retry.
            is_conn_error = "Connection" in name or "Timeout" in name or "Connect" in name
            transient = is_conn_error or status in (429, 500, 502, 503, 504) or status is None
            if not transient or attempt == max_attempts:
                raise
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay) + random.uniform(0, 0.5)
            msg = f"[{what}] erreur réseau (essai {attempt}/{max_attempts}), retry dans {delay:.0f}s… [{name}: {exc}]"
            print(msg, flush=True)
            if log:
                log(msg)
            time.sleep(delay)
    # Inatteignable
    raise RuntimeError(f"{what}: échec après {max_attempts} tentatives")


def _is_token_limit_error(exc: Exception) -> bool:
    """Détecte un 413 « trop de tokens » (distinct du 413 batch-size)."""
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    msg = str(exc).lower()
    return status == 413 and "token" in msg


def _embed_batch_safe(create_fn, batch: list[str]) -> list:
    """Embede un batch ; si TEI renvoie 413-tokens, retronque et réessaie.

    La limite réelle de TEI est en TOKENS, qu'on ne connaît pas exactement côté
    client. Plutôt que de deviner, on réagit : tant qu'on prend un 413-tokens,
    on coupe chaque texte du batch à 75 % et on réessaie (quelques fois). Ainsi
    l'indexation ne s'interrompt jamais sur un texte exceptionnellement dense.
    """
    work = list(batch)
    for _ in range(6):
        try:
            return _with_retry(lambda b=work: create_fn(b)).data
        except Exception as exc:  # noqa: BLE001
            if not _is_token_limit_error(exc):
                raise
            # Retronque agressivement (75 %) et réessaie.
            work = [t[: max(64, int(len(t) * 0.75))] for t in work]
    # Dernier recours : coupe très court.
    work = [t[:400] for t in work]
    return _with_retry(lambda b=work: create_fn(b)).data


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class Chat(Protocol):
    def complete(
        self,
        system: str,
        messages: list[dict],
        tools: list | None = None,
        reasoning_effort: str | None = None,
    ) -> dict:
        """Retourne {'text': str, 'tool_calls': [...]} (tool_calls peut être vide).

        reasoning_effort (low|medium|high) active le raisonnement sur les modèles
        qui le supportent (OpenAI o-series, Anthropic Opus/Sonnet récents). None =
        désactivé / comportement par défaut. Ignoré silencieusement sinon.
        """
        ...

    def complete_stream(
        self, system: str, messages: list[dict], reasoning_effort: str | None = None
    ):
        """Générateur de (event_type, text) — event_type = 'thinking' | 'text' | 'done'."""
        result = self.complete(system, messages, reasoning_effort=reasoning_effort)
        yield "text", result.get("text", "")
        yield "done", ""


# --------------------------------------------------------------------------- #
# Embedders
# --------------------------------------------------------------------------- #
class _OpenAICompatEmbedder:
    """OpenAI et vLLM (OpenAI-compatible) partagent le même client."""

    def __init__(self, model: str, api_key: str | None, base_url: str | None = None, timeout: float = 120.0):
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key or "EMPTY", base_url=base_url, timeout=timeout)
        self._model = model

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        batches = list(_batched(texts, cfg.embed_batch_size))
        for i, batch in enumerate(batches):
            data = _embed_batch_safe(
                lambda b: self._client.embeddings.create(model=self._model, input=b),
                batch,
            )
            out.extend(d.embedding for d in data)
            if cfg.embed_batch_delay and i < len(batches) - 1:
                time.sleep(cfg.embed_batch_delay)
        return out


class _MistralEmbedder:
    def __init__(self, model: str, api_key: str | None):
        from mistralai import Mistral

        self._client = Mistral(api_key=api_key)
        self._model = model

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        batches = list(_batched(texts, cfg.embed_batch_size))
        for i, batch in enumerate(batches):
            data = _embed_batch_safe(
                lambda b: self._client.embeddings.create(model=self._model, inputs=b),
                batch,
            )
            out.extend(d.embedding for d in data)
            if cfg.embed_batch_delay and i < len(batches) - 1:
                time.sleep(cfg.embed_batch_delay)
        return out


def get_embedder(config: Config = cfg) -> Embedder:
    provider = (config.embed_provider or "").lower()
    if provider == "openai":
        return _OpenAICompatEmbedder(config.embed_model, config.openai_api_key)
    if provider == "vllm":
        return _OpenAICompatEmbedder(
            config.embed_model, config.vllm_api_key, base_url=config.vllm_embed_base_url
        )
    if provider == "mistral":
        return _MistralEmbedder(config.embed_model, config.mistral_api_key)
    if provider == "anthropic":
        raise ValueError(
            "Anthropic ne fournit pas d'API d'embeddings. "
            "Choisir EMBED_PROVIDER=openai|mistral|vllm."
        )
    raise ValueError(f"EMBED_PROVIDER inconnu : {provider!r}")


# --------------------------------------------------------------------------- #
# Chat
# --------------------------------------------------------------------------- #
class _OpenAICompatChat:
    """OpenAI et vLLM (OpenAI-compatible)."""

    def __init__(
        self,
        model: str,
        api_key: str | None,
        base_url: str | None = None,
        auto_detect_model: bool = False,
        timeout: float = 300.0,
    ):
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key or "EMPTY", base_url=base_url, timeout=timeout)
        self._model = model
        # Si True, on re-détecte le modèle servi en cas de 404 (utile pour vLLM
        # quand l'utilisateur change le modèle déployé).
        self._auto_detect = auto_detect_model

    def _refresh_model(self) -> bool:
        """Re-interroge /models et met à jour self._model. True si changé."""
        try:
            models = self._client.models.list().data
            if models and models[0].id != self._model:
                self._model = models[0].id
                return True
        except Exception:
            pass
        return False

    def complete(
        self,
        system: str,
        messages: list[dict],
        tools: list | None = None,
        reasoning_effort: str | None = None,
    ) -> dict:
        msgs = [{"role": "system", "content": system}] + messages
        kwargs: dict = {"model": self._model, "messages": msgs}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if reasoning_effort:
            # Reasoning models OpenAI (o-series) / vLLM compatibles.
            kwargs["reasoning_effort"] = reasoning_effort
        try:
            resp = self._client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            status = getattr(exc, "status_code", None)
            if self._auto_detect and status == 404 and self._refresh_model():
                kwargs["model"] = self._model
                resp = self._client.chat.completions.create(**kwargs)
            else:
                raise
        choice = resp.choices[0].message
        tool_calls = []
        for tc in (choice.tool_calls or []):
            tool_calls.append(
                {"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
            )
        return {"text": choice.content or "", "tool_calls": tool_calls}

    def complete_stream(
        self, system: str, messages: list[dict], reasoning_effort: str | None = None
    ):
        msgs = [{"role": "system", "content": system}] + messages
        kwargs: dict = {"model": self._model, "messages": msgs, "stream": True}
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
        stream = self._client.chat.completions.create(**kwargs)
        for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if not delta:
                continue
            # Certains serveurs (vLLM, o-series via proxies) exposent le raisonnement
            # dans un champ delta dédié — on le remonte comme "thinking" si présent.
            reasoning = getattr(delta, "reasoning_content", None) or getattr(
                delta, "reasoning", None
            )
            if reasoning:
                yield "thinking", reasoning
            if delta.content:
                yield "text", delta.content
        yield "done", ""


class _AnthropicChat:
    def __init__(self, model: str, api_key: str | None):
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def _build_kwargs(
        self,
        system: str,
        messages: list[dict],
        tools: list | None = None,
        reasoning_effort: str | None = None,
    ) -> dict:
        anthropic_tools = None
        if tools:
            anthropic_tools = [
                {
                    "name": t["function"]["name"],
                    "description": t["function"].get("description", ""),
                    "input_schema": t["function"]["parameters"],
                }
                for t in tools
            ]
        kwargs: dict = {
            "model": self._model,
            "max_tokens": 8096,
            "system": system,
            "messages": messages,
        }
        if anthropic_tools:
            kwargs["tools"] = anthropic_tools
        if reasoning_effort:
            # Extended/adaptive thinking sur les modèles Opus/Sonnet récents.
            # display=summarized pour streamer un résumé du raisonnement (sinon vide).
            kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}
            kwargs["output_config"] = {"effort": reasoning_effort}
        return kwargs

    def complete(
        self,
        system: str,
        messages: list[dict],
        tools: list | None = None,
        reasoning_effort: str | None = None,
    ) -> dict:
        kwargs = self._build_kwargs(system, messages, tools, reasoning_effort)
        resp = self._client.messages.create(**kwargs)
        text_parts, tool_calls = [], []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                import json
                tool_calls.append(
                    {"id": block.id, "name": block.name, "arguments": json.dumps(block.input)}
                )
        return {"text": "".join(text_parts), "tool_calls": tool_calls}

    def complete_stream(
        self, system: str, messages: list[dict], reasoning_effort: str | None = None
    ):
        kwargs = self._build_kwargs(system, messages, reasoning_effort=reasoning_effort)
        with self._client.messages.stream(**kwargs) as stream:
            for event in stream:
                etype = getattr(event, "type", None)
                if etype == "content_block_start":
                    block = getattr(event, "content_block", None)
                    if getattr(block, "type", None) == "thinking":
                        pass  # thinking block ouvert — le texte arrive via deltas
                elif etype == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    dtype = getattr(delta, "type", None)
                    if dtype == "thinking_delta":
                        yield "thinking", getattr(delta, "thinking", "")
                    elif dtype == "text_delta":
                        yield "text", getattr(delta, "text", "")
        yield "done", ""


class _MistralChat:
    def __init__(self, model: str, api_key: str | None):
        from mistralai import Mistral

        self._client = Mistral(api_key=api_key)
        self._model = model

    def complete(
        self,
        system: str,
        messages: list[dict],
        tools: list | None = None,
        reasoning_effort: str | None = None,
    ) -> dict:
        # Mistral n'expose pas de paramètre reasoning_effort — ignoré.
        import json as _json
        msgs = [{"role": "system", "content": system}] + messages
        kwargs: dict = {"model": self._model, "messages": msgs}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        resp = self._client.chat.complete(**kwargs)
        choice = resp.choices[0].message
        raw_content = choice.content
        if isinstance(raw_content, list):
            text = "".join(
                c.text if hasattr(c, "text") else str(c)
                for c in raw_content
            )
        else:
            text = raw_content or ""
        tool_calls = []
        for tc in (choice.tool_calls or []):
            args = tc.function.arguments
            if isinstance(args, dict):
                import json as _json2
                args = _json2.dumps(args)
            tool_calls.append({"id": tc.id, "name": tc.function.name, "arguments": args})
        return {"text": text, "tool_calls": tool_calls}

    def complete_stream(
        self, system: str, messages: list[dict], reasoning_effort: str | None = None
    ):
        msgs = [{"role": "system", "content": system}] + messages
        stream = self._client.chat.stream(model=self._model, messages=msgs)
        for chunk in stream:
            delta = chunk.data.choices[0].delta if chunk.data.choices else None
            if delta:
                raw = delta.content
                if isinstance(raw, list):
                    text = "".join(c.text if hasattr(c, "text") else str(c) for c in raw)
                else:
                    text = raw or ""
                if text:
                    yield "text", text
        yield "done", ""


def detect_vllm_chat_model(config: Config = cfg) -> str | None:
    """Interroge l'endpoint vLLM /models et renvoie le 1er modèle instruct servi.

    Permet à l'app de s'adapter automatiquement quand l'utilisateur change le
    modèle déployé sur son service (uniquement le chat ; l'embedding TEI n'expose
    pas toujours /models). Renvoie None si l'endpoint est injoignable.
    """
    from openai import OpenAI

    try:
        client = OpenAI(api_key=config.vllm_api_key or "EMPTY", base_url=config.vllm_chat_base_url)
        models = _with_retry(lambda: client.models.list(), what="models.list").data
        return models[0].id if models else None
    except Exception:
        return None


def get_chat(config: Config = cfg) -> Chat:
    provider = (config.chat_provider or "").lower()
    if provider == "openai":
        return _OpenAICompatChat(config.chat_model, config.openai_api_key)
    if provider == "vllm":
        # Si le modèle n'est pas fixé (vide / "auto"), on le détecte automatiquement.
        model = config.chat_model
        if not model or model.lower() == "auto":
            detected = detect_vllm_chat_model(config)
            if detected:
                model = detected
        return _OpenAICompatChat(
            model, config.vllm_api_key, base_url=config.vllm_chat_base_url,
            auto_detect_model=True,
        )
    if provider == "anthropic":
        return _AnthropicChat(config.chat_model, config.anthropic_api_key)
    if provider == "mistral":
        return _MistralChat(config.chat_model, config.mistral_api_key)
    raise ValueError(f"CHAT_PROVIDER inconnu : {provider!r}")

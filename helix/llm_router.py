#!/usr/bin/env python3
"""
LLM Router for Helix Linux (stripped)
Routes requests to the most appropriate LLM based on task type.

Supports:
- Claude API (Anthropic)
- OpenAI API
- Ollama (local inference)

Author: Helix Linux Team
SPDX-License-Identifier: BUSL-1.1
"""

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from anthropic import Anthropic, AsyncAnthropic
from openai import AsyncOpenAI, OpenAI

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class TaskType(Enum):
    """Types of tasks that determine LLM routing."""

    USER_CHAT = "user_chat"
    REQUIREMENT_PARSING = "requirement_parsing"
    SYSTEM_OPERATION = "system_operation"
    ERROR_DEBUGGING = "error_debugging"
    CODE_GENERATION = "code_generation"
    DEPENDENCY_RESOLUTION = "dependency_resolution"
    CONFIGURATION = "configuration"
    TOOL_EXECUTION = "tool_execution"


class LLMProvider(Enum):
    """Supported LLM providers."""

    CLAUDE = "claude"
    OPENAI = "openai"
    OLLAMA = "ollama"


@dataclass
class LLMResponse:
    """Standardized response from any LLM."""

    content: str
    provider: LLMProvider
    model: str
    tokens_used: int
    cost_usd: float
    latency_seconds: float
    raw_response: dict | None = None


@dataclass
class RoutingDecision:
    """Details about why a specific LLM was chosen."""

    provider: LLMProvider
    task_type: TaskType
    reasoning: str
    confidence: float  # 0.0 to 1.0


class LLMRouter:
    """
    Intelligent router that selects the best LLM for each task.

    Routing Logic:
    - User-facing tasks → Claude (better at natural language)
    - System operations → OpenAI (strong at code/operations)
    - Error debugging → OpenAI (good at technical problem-solving)
    - Complex installs → OpenAI (strong agentic capabilities)

    Includes fallback logic if primary LLM fails.
    """

    # Cost per 1M tokens (estimated)
    COSTS = {
        LLMProvider.CLAUDE: {
            "input": 3.0,
            "output": 15.0,
        },
        LLMProvider.OPENAI: {
            "input": 2.5,
            "output": 10.0,
        },
        LLMProvider.OLLAMA: {
            "input": 0.0,
            "output": 0.0,
        },
    }

    # Routing rules: TaskType → Preferred LLM
    ROUTING_RULES = {
        TaskType.USER_CHAT: LLMProvider.CLAUDE,
        TaskType.REQUIREMENT_PARSING: LLMProvider.CLAUDE,
        TaskType.SYSTEM_OPERATION: LLMProvider.OPENAI,
        TaskType.ERROR_DEBUGGING: LLMProvider.OPENAI,
        TaskType.CODE_GENERATION: LLMProvider.OPENAI,
        TaskType.DEPENDENCY_RESOLUTION: LLMProvider.OPENAI,
        TaskType.CONFIGURATION: LLMProvider.OPENAI,
        TaskType.TOOL_EXECUTION: LLMProvider.OPENAI,
    }

    def __init__(
        self,
        claude_api_key: str | None = None,
        openai_api_key: str | None = None,
        ollama_base_url: str | None = None,
        ollama_model: str | None = None,
        default_provider: LLMProvider = LLMProvider.CLAUDE,
        enable_fallback: bool = True,
        track_costs: bool = True,
    ):
        """
        Initialize LLM Router.

        Args:
            claude_api_key: Anthropic API key (defaults to ANTHROPIC_API_KEY env)
            openai_api_key: OpenAI API key (defaults to OPENAI_API_KEY env)
            ollama_base_url: Ollama API base URL (defaults to http://localhost:11434)
            ollama_model: Ollama model to use (defaults to llama3.2)
            default_provider: Fallback provider if routing fails
            enable_fallback: Try alternate LLM if primary fails
            track_costs: Track token usage and costs
        """
        self.claude_api_key = claude_api_key or os.getenv("ANTHROPIC_API_KEY")
        self.openai_api_key = openai_api_key or os.getenv("OPENAI_API_KEY")
        self.default_provider = default_provider
        self.enable_fallback = enable_fallback
        self.track_costs = track_costs

        # Initialize clients (sync)
        self.claude_client = None
        self.openai_client = None

        # Initialize async clients
        self.claude_client_async = None
        self.openai_client_async = None

        if self.claude_api_key:
            self.claude_client = Anthropic(api_key=self.claude_api_key)
            self.claude_client_async = AsyncAnthropic(api_key=self.claude_api_key)
            logger.info(" Claude API client initialized")
        else:
            logger.warning("  No Claude API key provided")

        if self.openai_api_key:
            self.openai_client = OpenAI(api_key=self.openai_api_key)
            self.openai_client_async = AsyncOpenAI(api_key=self.openai_api_key)
            logger.info(" OpenAI API client initialized")
        else:
            logger.warning("  No OpenAI API key provided")

        # Initialize Ollama client (local inference)
        self.ollama_base_url = ollama_base_url or os.getenv(
            "OLLAMA_BASE_URL", "http://localhost:11434"
        )
        self.ollama_model = ollama_model or os.getenv("OLLAMA_MODEL", "llama3.2")
        self.ollama_client = None
        self.ollama_client_async = None

        try:
            self.ollama_client = OpenAI(
                api_key="ollama",
                base_url=f"{self.ollama_base_url}/v1",
            )
            self.ollama_client_async = AsyncOpenAI(
                api_key="ollama",
                base_url=f"{self.ollama_base_url}/v1",
            )
            logger.info(f" Ollama client initialized ({self.ollama_model})")
        except Exception as e:
            logger.warning(f"  Could not initialize Ollama client: {e}")

        # Rate limiting for parallel calls
        self._rate_limit_semaphore: asyncio.Semaphore | None = None

        # Cost tracking (protected by lock for thread-safety)
        self._stats_lock = threading.Lock()
        self.total_cost_usd = 0.0
        self.request_count = 0
        self.provider_stats = {
            LLMProvider.CLAUDE: {"requests": 0, "tokens": 0, "cost": 0.0},
            LLMProvider.OPENAI: {"requests": 0, "tokens": 0, "cost": 0.0},
            LLMProvider.OLLAMA: {"requests": 0, "tokens": 0, "cost": 0.0},
        }

    def route_task(
        self, task_type: TaskType, force_provider: LLMProvider | None = None
    ) -> RoutingDecision:
        """Determine which LLM should handle this task."""
        if force_provider:
            return RoutingDecision(
                provider=force_provider,
                task_type=task_type,
                reasoning="Forced by caller",
                confidence=1.0,
            )

        provider = self.ROUTING_RULES.get(task_type, self.default_provider)

        # Check if preferred provider is available, fallback if needed
        if provider == LLMProvider.CLAUDE and not self.claude_client:
            if self.openai_client and self.enable_fallback:
                logger.warning("Claude unavailable, falling back to OpenAI")
                provider = LLMProvider.OPENAI
            elif self.ollama_client and self.enable_fallback:
                logger.warning("Claude unavailable, falling back to Ollama")
                provider = LLMProvider.OLLAMA
            else:
                raise RuntimeError("Claude API not configured and no fallback available")

        if provider == LLMProvider.OPENAI and not self.openai_client:
            if self.claude_client and self.enable_fallback:
                logger.warning("OpenAI unavailable, falling back to Claude")
                provider = LLMProvider.CLAUDE
            elif self.ollama_client and self.enable_fallback:
                logger.warning("OpenAI unavailable, falling back to Ollama")
                provider = LLMProvider.OLLAMA
            else:
                raise RuntimeError("OpenAI API not configured and no fallback available")

        if provider == LLMProvider.OLLAMA and not self.ollama_client:
            if self.claude_client and self.enable_fallback:
                logger.warning("Ollama unavailable, falling back to Claude")
                provider = LLMProvider.CLAUDE
            elif self.openai_client and self.enable_fallback:
                logger.warning("Ollama unavailable, falling back to OpenAI")
                provider = LLMProvider.OPENAI
            else:
                raise RuntimeError("Ollama not available and no fallback configured")

        reasoning = f"{task_type.value} → {provider.value} (optimal for this task)"

        return RoutingDecision(
            provider=provider, task_type=task_type, reasoning=reasoning, confidence=0.95
        )

    def complete(
        self,
        messages: list[dict[str, str]],
        task_type: TaskType = TaskType.USER_CHAT,
        force_provider: LLMProvider | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        """Generate completion using the most appropriate LLM."""
        start_time = time.time()

        routing = self.route_task(task_type, force_provider)
        logger.info(f" Routing: {routing.reasoning}")

        try:
            if routing.provider == LLMProvider.CLAUDE:
                response = self._complete_claude(messages, temperature, max_tokens, tools)
            elif routing.provider == LLMProvider.OPENAI:
                response = self._complete_openai(messages, temperature, max_tokens, tools)
            else:  # OLLAMA
                response = self._complete_ollama(messages, temperature, max_tokens, tools)

            response.latency_seconds = time.time() - start_time

            if self.track_costs:
                self._update_stats(response)

            return response

        except Exception as e:
            logger.error(f" Error with {routing.provider.value}: {e}")

            if self.enable_fallback:
                fallback_provider = (
                    LLMProvider.OPENAI
                    if routing.provider == LLMProvider.CLAUDE
                    else LLMProvider.CLAUDE
                )
                logger.info(f" Attempting fallback to {fallback_provider.value}")

                return self.complete(
                    messages=messages,
                    task_type=task_type,
                    force_provider=fallback_provider,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    tools=tools,
                )
            else:
                raise

    def _complete_claude(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        """Generate completion using Claude API."""
        system_message = None
        user_messages = []

        for msg in messages:
            if msg["role"] == "system":
                system_message = msg["content"]
            else:
                user_messages.append(msg)

        kwargs = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": user_messages,
        }

        if system_message:
            kwargs["system"] = system_message

        if tools:
            kwargs["tools"] = tools

        response = self.claude_client.messages.create(**kwargs)

        content = ""
        for block in response.content:
            if hasattr(block, "text"):
                content += block.text

        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        cost = self._calculate_cost(LLMProvider.CLAUDE, input_tokens, output_tokens)

        return LLMResponse(
            content=content,
            provider=LLMProvider.CLAUDE,
            model="claude-sonnet-4-20250514",
            tokens_used=input_tokens + output_tokens,
            cost_usd=cost,
            latency_seconds=0.0,
            raw_response=response.model_dump() if hasattr(response, "model_dump") else None,
        )

    def _complete_openai(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        """Generate completion using OpenAI API."""
        kwargs = {
            "model": "gpt-4o",
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        response = self.openai_client.chat.completions.create(**kwargs)

        content = response.choices[0].message.content or ""

        input_tokens = response.usage.prompt_tokens
        output_tokens = response.usage.completion_tokens
        cost = self._calculate_cost(LLMProvider.OPENAI, input_tokens, output_tokens)

        return LLMResponse(
            content=content,
            provider=LLMProvider.OPENAI,
            model="gpt-4o",
            tokens_used=input_tokens + output_tokens,
            cost_usd=cost,
            latency_seconds=0.0,
            raw_response=response.model_dump() if hasattr(response, "model_dump") else None,
        )

    def _complete_ollama(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        """Generate completion using Ollama (local LLM)."""
        kwargs = {
            "model": self.ollama_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        try:
            response = self.ollama_client.chat.completions.create(**kwargs)

            content = response.choices[0].message.content or ""

            input_tokens = getattr(response.usage, "prompt_tokens", 0)
            output_tokens = getattr(response.usage, "completion_tokens", 0)

            cost = 0.0

            return LLMResponse(
                content=content,
                provider=LLMProvider.OLLAMA,
                model=self.ollama_model,
                tokens_used=input_tokens + output_tokens,
                cost_usd=cost,
                latency_seconds=0.0,
                raw_response=response.model_dump() if hasattr(response, "model_dump") else None,
            )

        except Exception as e:
            logger.error(f"Ollama error: {e}")
            raise RuntimeError(
                f"Ollama request failed. Is Ollama running? (ollama serve) Error: {e}"
            )

    def _calculate_cost(
        self, provider: LLMProvider, input_tokens: int, output_tokens: int
    ) -> float:
        """Calculate cost in USD for this request."""
        costs = self.COSTS[provider]
        input_cost = (input_tokens / 1_000_000) * costs["input"]
        output_cost = (output_tokens / 1_000_000) * costs["output"]
        return input_cost + output_cost

    def _update_stats(self, response: LLMResponse):
        """Update usage statistics (thread-safe)."""
        with self._stats_lock:
            self.total_cost_usd += response.cost_usd
            self.request_count += 1

            stats = self.provider_stats[response.provider]
            stats["requests"] += 1
            stats["tokens"] += response.tokens_used
            stats["cost"] += response.cost_usd

    def get_stats(self) -> dict[str, Any]:
        """Get usage statistics (thread-safe)."""
        with self._stats_lock:
            return {
                "total_requests": self.request_count,
                "total_cost_usd": round(self.total_cost_usd, 4),
                "providers": {
                    "claude": {
                        "requests": self.provider_stats[LLMProvider.CLAUDE]["requests"],
                        "tokens": self.provider_stats[LLMProvider.CLAUDE]["tokens"],
                        "cost_usd": round(self.provider_stats[LLMProvider.CLAUDE]["cost"], 4),
                    },
                    "openai": {
                        "requests": self.provider_stats[LLMProvider.OPENAI]["requests"],
                        "tokens": self.provider_stats[LLMProvider.OPENAI]["tokens"],
                        "cost_usd": round(self.provider_stats[LLMProvider.OPENAI]["cost"], 4),
                    },
                    "ollama": {
                        "requests": self.provider_stats[LLMProvider.OLLAMA]["requests"],
                        "tokens": self.provider_stats[LLMProvider.OLLAMA]["tokens"],
                        "cost_usd": round(self.provider_stats[LLMProvider.OLLAMA]["cost"], 4),
                    },
                },
            }

    def reset_stats(self):
        """Reset all usage statistics."""
        self.total_cost_usd = 0.0
        self.request_count = 0
        for provider in self.provider_stats:
            self.provider_stats[provider] = {"requests": 0, "tokens": 0, "cost": 0.0}

    def set_rate_limit(self, max_concurrent: int = 10):
        """Set rate limit for parallel API calls."""
        self._rate_limit_semaphore = asyncio.Semaphore(max_concurrent)

    async def acomplete(
        self,
        messages: list[dict[str, str]],
        task_type: TaskType = TaskType.USER_CHAT,
        force_provider: LLMProvider | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        """Async version of complete()."""
        start_time = time.time()

        routing = self.route_task(task_type, force_provider)
        logger.info(f" Routing: {routing.reasoning}")

        try:
            if routing.provider == LLMProvider.CLAUDE:
                response = await self._acomplete_claude(messages, temperature, max_tokens, tools)
            elif routing.provider == LLMProvider.OPENAI:
                response = await self._acomplete_openai(messages, temperature, max_tokens, tools)
            else:  # OLLAMA
                response = await self._acomplete_ollama(messages, temperature, max_tokens, tools)

            response.latency_seconds = time.time() - start_time

            if self.track_costs:
                self._update_stats(response)

            return response

        except Exception as e:
            logger.error(f" Error with {routing.provider.value}: {e}")

            if self.enable_fallback:
                fallback_provider = (
                    LLMProvider.OPENAI
                    if routing.provider == LLMProvider.CLAUDE
                    else LLMProvider.CLAUDE
                )
                logger.info(f" Attempting fallback to {fallback_provider.value}")

                return await self.acomplete(
                    messages=messages,
                    task_type=task_type,
                    force_provider=fallback_provider,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    tools=tools,
                )
            else:
                raise

    async def _acomplete_claude(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        """Async: Generate completion using Claude API."""
        if not self.claude_client_async:
            raise RuntimeError("Claude async client not initialized")

        system_message = None
        user_messages = []

        for msg in messages:
            if msg["role"] == "system":
                system_message = msg["content"]
            else:
                user_messages.append(msg)

        kwargs = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": user_messages,
        }

        if system_message:
            kwargs["system"] = system_message

        if tools:
            kwargs["tools"] = tools

        response = await self.claude_client_async.messages.create(**kwargs)

        content = ""
        for block in response.content:
            if hasattr(block, "text"):
                content += block.text

        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        cost = self._calculate_cost(LLMProvider.CLAUDE, input_tokens, output_tokens)

        return LLMResponse(
            content=content,
            provider=LLMProvider.CLAUDE,
            model="claude-sonnet-4-20250514",
            tokens_used=input_tokens + output_tokens,
            cost_usd=cost,
            latency_seconds=0.0,
            raw_response=response.model_dump() if hasattr(response, "model_dump") else None,
        )

    async def _acomplete_openai(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        """Async: Generate completion using OpenAI API."""
        if not self.openai_client_async:
            raise RuntimeError("OpenAI async client not initialized")

        kwargs = {
            "model": "gpt-4o",
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        response = await self.openai_client_async.chat.completions.create(**kwargs)

        content = response.choices[0].message.content or ""

        input_tokens = response.usage.prompt_tokens
        output_tokens = response.usage.completion_tokens
        cost = self._calculate_cost(LLMProvider.OPENAI, input_tokens, output_tokens)

        return LLMResponse(
            content=content,
            provider=LLMProvider.OPENAI,
            model="gpt-4o",
            tokens_used=input_tokens + output_tokens,
            cost_usd=cost,
            latency_seconds=0.0,
            raw_response=response.model_dump() if hasattr(response, "model_dump") else None,
        )

    async def _acomplete_ollama(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        """Async: Generate completion using Ollama (local LLM)."""
        if not self.ollama_client_async:
            raise RuntimeError("Ollama async client not initialized")

        kwargs = {
            "model": self.ollama_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        try:
            response = await self.ollama_client_async.chat.completions.create(**kwargs)

            content = response.choices[0].message.content or ""

            input_tokens = getattr(response.usage, "prompt_tokens", 0)
            output_tokens = getattr(response.usage, "completion_tokens", 0)

            cost = 0.0

            return LLMResponse(
                content=content,
                provider=LLMProvider.OLLAMA,
                model=self.ollama_model,
                tokens_used=input_tokens + output_tokens,
                cost_usd=cost,
                latency_seconds=0.0,
                raw_response=response.model_dump() if hasattr(response, "model_dump") else None,
            )

        except Exception as e:
            logger.error(f"Ollama async error: {e}")
            raise RuntimeError(
                f"Ollama request failed. Is Ollama running? (ollama serve) Error: {e}"
            )

    async def complete_batch(
        self,
        requests: list[dict[str, Any]],
        max_concurrent: int | None = None,
    ) -> list[LLMResponse]:
        """Process multiple LLM requests in parallel with rate limiting."""
        if not requests:
            return []

        if max_concurrent is None:
            if self._rate_limit_semaphore:
                max_concurrent = self._rate_limit_semaphore._value
            else:
                max_concurrent = 10
                self.set_rate_limit(max_concurrent)

        semaphore = asyncio.Semaphore(max_concurrent)

        async def _complete_with_rate_limit(request: dict[str, Any]) -> LLMResponse:
            async with semaphore:
                return await self.acomplete(
                    messages=request["messages"],
                    task_type=request.get("task_type", TaskType.USER_CHAT),
                    force_provider=request.get("force_provider"),
                    temperature=request.get("temperature", 0.7),
                    max_tokens=request.get("max_tokens", 4096),
                    tools=request.get("tools"),
                )

        tasks = [_complete_with_rate_limit(req) for req in requests]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

        result: list[LLMResponse] = []
        for i, response in enumerate(responses):
            if isinstance(response, Exception):
                logger.error(f"Request {i} failed: {response}")
                error_response = LLMResponse(
                    content=f"Error: {str(response)}",
                    provider=LLMProvider.CLAUDE,
                    model="error",
                    tokens_used=0,
                    cost_usd=0.0,
                    latency_seconds=0.0,
                )
                result.append(error_response)
            else:
                result.append(response)

        return result


# Convenience function for simple use cases
def complete_task(
    prompt: str,
    task_type: TaskType = TaskType.USER_CHAT,
    system_prompt: str | None = None,
    **kwargs,
) -> str:
    """Simple interface for one-off completions."""
    router = LLMRouter()

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    response = router.complete(messages, task_type=task_type, **kwargs)
    return response.content

"""Base entity for Extended OpenAI Conversation."""

from __future__ import annotations

from collections.abc import AsyncGenerator
import json
import logging
from typing import TYPE_CHECKING, Any

from openai import AsyncClient, AsyncStream
from openai._exceptions import OpenAIError
from openai.types.chat import (
    ChatCompletionChunk,
    ChatCompletionMessageParam,
    ChatCompletionToolParam,
)

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigSubentry
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, llm
from homeassistant.helpers.entity import Entity

from .const import (
    CONF_CHAT_MODEL,
    CONF_CONTEXT_THRESHOLD,
    CONF_CONTEXT_TRUNCATE_STRATEGY,
    CONF_MAX_FUNCTION_CALLS_PER_CONVERSATION,
    CONF_MAX_TOKENS,
    CONF_TEMPERATURE,
    CONF_TOP_P,
    CONF_USE_TOOLS,
    DEFAULT_CHAT_MODEL,
    DEFAULT_CONTEXT_THRESHOLD,
    DEFAULT_CONTEXT_TRUNCATE_STRATEGY,
    DEFAULT_MAX_FUNCTION_CALLS_PER_CONVERSATION,
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    DEFAULT_TOP_P,
    DEFAULT_USE_TOOLS,
    DOMAIN,
)

if TYPE_CHECKING:
    from . import ExtendedOpenAIConfigEntry

_LOGGER = logging.getLogger(__name__)

# Max number of back and forth with the LLM to generate a response
MAX_TOOL_ITERATIONS = 10


def _convert_content_to_param(
    chat_content: list[conversation.Content],
) -> list[ChatCompletionMessageParam]:
    """Convert chat log content to OpenAI message format."""
    messages: list[ChatCompletionMessageParam] = []

    for content in chat_content:
        if content.role == "system":
            messages.append({"role": "system", "content": content.content or ""})
        elif content.role == "user":
            messages.append({"role": "user", "content": content.content or ""})
        elif content.role == "assistant":
            msg: dict[str, Any] = {"role": "assistant"}
            if content.content:
                msg["content"] = content.content
            if content.tool_calls:
                msg["tool_calls"] = [
                    {
                        "id": tool_call.id,
                        "type": "function",
                        "function": {
                            "name": tool_call.tool_name,
                            "arguments": json.dumps(tool_call.tool_args),
                        },
                    }
                    for tool_call in content.tool_calls
                ]
            messages.append(msg)
        elif content.role == "tool_result":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": content.tool_call_id,
                    "content": json.dumps(content.tool_result),
                }
            )

    return messages


async def _transform_stream(
    chat_log: conversation.ChatLog,
    result: AsyncStream[ChatCompletionChunk],
) -> AsyncGenerator[
    conversation.AssistantContentDeltaDict | conversation.ToolResultContentDeltaDict
]:
    """Transform OpenAI stream to Home Assistant format."""
    current_tool_calls: dict[int, dict[str, Any]] = {}
    first_chunk = True

    async for chunk in result:
        _LOGGER.debug("Received chunk: %s", chunk)

        # Signal new assistant message on first chunk
        if first_chunk:
            yield {"role": "assistant"}
            first_chunk = False

        if not chunk.choices:
            # Track usage from final chunk if available
            if chunk.usage:
                chat_log.async_trace(
                    {
                        "stats": {
                            "input_tokens": chunk.usage.prompt_tokens,
                            "output_tokens": chunk.usage.completion_tokens,
                        }
                    }
                )
            continue

        choice = chunk.choices[0]
        delta = choice.delta

        if delta.content:
            yield {"content": delta.content}

        if delta.tool_calls:
            for tool_call_delta in delta.tool_calls:
                idx = tool_call_delta.index
                if idx not in current_tool_calls:
                    current_tool_calls[idx] = {
                        "id": tool_call_delta.id or "",
                        "name": "",
                        "arguments": "",
                    }

                if tool_call_delta.function:
                    if tool_call_delta.function.name:
                        current_tool_calls[idx]["name"] = tool_call_delta.function.name
                    if tool_call_delta.function.arguments:
                        current_tool_calls[idx][
                            "arguments"
                        ] += tool_call_delta.function.arguments

        if choice.finish_reason == "tool_calls":
            # Yield all accumulated tool calls (marked as external since we handle them ourselves)
            tool_calls_list = []
            for idx in sorted(current_tool_calls.keys()):
                tc = current_tool_calls[idx]
                try:
                    args = json.loads(tc["arguments"]) if tc["arguments"] else {}
                except json.JSONDecodeError:
                    args = {}
                tool_calls_list.append(
                    llm.ToolInput(
                        id=tc["id"],
                        tool_name=tc["name"],
                        tool_args=args,
                        external=True,  # Mark as external so ChatLog doesn't try to execute
                    )
                )
            if tool_calls_list:
                yield {"tool_calls": tool_calls_list}
            current_tool_calls.clear()

        if choice.finish_reason == "stop":
            break


class ExtendedOpenAIBaseLLMEntity(Entity):
    """Extended OpenAI base entity."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(
        self, entry: ExtendedOpenAIConfigEntry, subentry: ConfigSubentry
    ) -> None:
        """Initialize the entity."""
        self.entry = entry
        self.subentry = subentry
        self._attr_unique_id = subentry.subentry_id
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
            name=subentry.title,
            manufacturer="OpenAI",
            model=subentry.data.get(CONF_CHAT_MODEL, DEFAULT_CHAT_MODEL),
            entry_type=dr.DeviceEntryType.SERVICE,
        )

    @property
    def _client(self) -> AsyncClient:
        """Return the OpenAI client."""
        return self.entry.runtime_data

    async def _async_handle_chat_log(
        self,
        chat_log: conversation.ChatLog,
        custom_functions: list[dict[str, Any]] | None = None,
        exposed_entities: list[dict[str, Any]] | None = None,
        user_input: conversation.ConversationInput | None = None,
    ) -> None:
        """Generate an answer for the chat log with streaming support."""
        options = self.subentry.data
        model = options.get(CONF_CHAT_MODEL, DEFAULT_CHAT_MODEL)
        max_tokens = options.get(CONF_MAX_TOKENS, DEFAULT_MAX_TOKENS)
        top_p = options.get(CONF_TOP_P, DEFAULT_TOP_P)
        temperature = options.get(CONF_TEMPERATURE, DEFAULT_TEMPERATURE)
        use_tools = options.get(CONF_USE_TOOLS, DEFAULT_USE_TOOLS)
        context_threshold = options.get(
            CONF_CONTEXT_THRESHOLD, DEFAULT_CONTEXT_THRESHOLD
        )
        max_function_calls = options.get(
            CONF_MAX_FUNCTION_CALLS_PER_CONVERSATION,
            DEFAULT_MAX_FUNCTION_CALLS_PER_CONVERSATION,
        )

        messages = _convert_content_to_param(chat_log.content)

        # Build tools list from custom functions only
        tools: list[ChatCompletionToolParam] = []
        custom_function_names: set[str] = set()

        if custom_functions and use_tools:
            for func_spec in custom_functions:
                tools.append(
                    ChatCompletionToolParam(
                        type="function",
                        function=func_spec["spec"],
                    )
                )
                custom_function_names.add(func_spec["spec"]["name"])

        # Determine token parameter based on model
        model_lower = model.lower()
        use_new_token_param = any(
            model_lower.startswith(prefix) or f"-{prefix}" in model_lower
            for prefix in ("gpt-4o", "gpt-5", "o1", "o3", "o4")
        )
        token_kwargs = (
            {"max_completion_tokens": max_tokens}
            if use_new_token_param
            else {"max_tokens": max_tokens}
        )

        tool_kwargs: dict[str, Any] = {}
        if tools:
            tool_kwargs["tools"] = tools
            tool_kwargs["tool_choice"] = "auto"

        _LOGGER.info("Prompt for %s: %s", model, json.dumps(messages))

        # Track function calls
        n_requests = 0

        # To prevent infinite loops, we limit the number of iterations
        for _iteration in range(MAX_TOOL_ITERATIONS):
            # Update tool_choice based on function call count
            if tools and n_requests >= max_function_calls:
                tool_kwargs["tool_choice"] = "none"

            try:
                stream = await self._client.chat.completions.create(
                    model=model,
                    messages=messages,
                    top_p=top_p,
                    temperature=temperature,
                    user=chat_log.conversation_id,
                    stream=True,
                    stream_options={"include_usage": True},
                    **token_kwargs,
                    **tool_kwargs,
                )
            except OpenAIError as err:
                _LOGGER.error("Error talking to OpenAI: %s", err)
                raise HomeAssistantError("Error talking to OpenAI") from err

            # Process stream and collect tool calls
            pending_tool_calls: list[llm.ToolInput] = []

            async for content in chat_log.async_add_delta_content_stream(
                self.entity_id, _transform_stream(chat_log, stream)
            ):
                if isinstance(content, conversation.AssistantContent):
                    if content.tool_calls:
                        pending_tool_calls.extend(content.tool_calls)

            # Execute custom functions
            for tool_call in pending_tool_calls:
                custom_func = next(
                    (
                        f
                        for f in (custom_functions or [])
                        if f["spec"]["name"] == tool_call.tool_name
                    ),
                    None,
                )

                if custom_func:
                    result = await self._execute_custom_function(
                        custom_func,
                        tool_call.tool_args,
                        user_input,
                        exposed_entities or [],
                    )

                    tool_result_content = conversation.ToolResultContent(
                        agent_id=self.entity_id,
                        tool_call_id=tool_call.id,
                        tool_name=tool_call.tool_name,
                        tool_result={"result": str(result)},
                    )
                    chat_log.async_add_assistant_content_without_tools(
                        tool_result_content
                    )
                    n_requests += 1

            # Update messages for next iteration
            messages = _convert_content_to_param(chat_log.content)

            # Check if we need to continue (if there are pending tool results)
            if not chat_log.unresponded_tool_results:
                break

    async def _execute_custom_function(
        self,
        function_spec: dict[str, Any],
        arguments: dict[str, Any],
        user_input: conversation.ConversationInput | None,
        exposed_entities: list[dict[str, Any]],
    ) -> Any:
        """Execute a custom function."""
        from .helpers import get_function_executor

        function = function_spec["function"]
        function_executor = get_function_executor(function["type"])

        return await function_executor.execute(
            self.hass, function, arguments, user_input, exposed_entities
        )

    async def _truncate_message_history(
        self,
        chat_log: conversation.ChatLog,
        user_input: conversation.ConversationInput | None,
    ) -> None:
        """Truncate message history based on strategy."""
        options = self.subentry.data
        strategy = options.get(
            CONF_CONTEXT_TRUNCATE_STRATEGY, DEFAULT_CONTEXT_TRUNCATE_STRATEGY
        )

        if strategy == "clear":
            # Keep only system prompt and last user message
            # This is handled by refreshing the LLM data
            _LOGGER.debug("Context threshold exceeded, conversation history cleared")

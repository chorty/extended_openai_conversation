"""Helper functions for Extended OpenAI Conversation component."""

from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
from datetime import timedelta
from functools import partial
import logging
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any
from urllib import parse

from bs4 import BeautifulSoup
from openai import AsyncAzureOpenAI, AsyncClient, AsyncOpenAI
import voluptuous as vol
import yaml

from homeassistant.components import (
    automation,
    conversation,
    energy,
    recorder,
    rest,
    scrape,
)
from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.components.recorder import history as recorder_history
from homeassistant.components.script import config as script_config
from homeassistant.config import AUTOMATION_CONFIG_PATH
from homeassistant.const import (
    CONF_ATTRIBUTE,
    CONF_METHOD,
    CONF_NAME,
    CONF_PAYLOAD,
    CONF_RESOURCE,
    CONF_RESOURCE_TEMPLATE,
    CONF_TIMEOUT,
    CONF_VALUE_TEMPLATE,
    CONF_VERIFY_SSL,
    SERVICE_RELOAD,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError, ServiceNotFound
from homeassistant.helpers import config_validation as cv, entity_registry as er, llm
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.helpers.script import Script
from homeassistant.helpers.template import Template
import homeassistant.util.dt as dt_util

from .const import (
    CONF_PAYLOAD_TEMPLATE,
    DEFAULT_ALLOWED_DIRS,
    DEFAULT_MODEL_CONFIG,
    DEFAULT_TOKEN_PARAM,
    DEFAULT_WORKING_DIRECTORY,
    DOMAIN,
    EVENT_AUTOMATION_REGISTERED,
    FILE_READ_SIZE_LIMIT,
    MODEL_CONFIG_PATTERNS,
    MODEL_TOKEN_PARAMETER_SUPPORT,
    SHELL_DENY_PATTERNS,
    SHELL_OUTPUT_LIMIT,
    SHELL_TIMEOUT,
)
from .exceptions import (
    CallServiceError,
    EntityNotExposed,
    EntityNotFound,
    FunctionNotFound,
    InvalidFunction,
    NativeNotFound,
)

_LOGGER = logging.getLogger(__name__)


AZURE_DOMAIN_PATTERN = r"\.(openai\.azure\.com|azure-api\.net|services\.ai\.azure\.com)"


def get_model_config(model: str) -> dict[str, bool]:
    """Get model-specific parameter configuration."""
    # Check patterns in order; first match wins
    for entry in MODEL_CONFIG_PATTERNS:
        pattern = str(entry["pattern"])
        entry_config = entry["config"]
        if re.match(pattern, model, re.IGNORECASE):
            # Type assertion since we know the structure from MODEL_CONFIG_PATTERNS
            return (
                dict(entry_config)
                if isinstance(entry_config, dict)
                else DEFAULT_MODEL_CONFIG
            )

    # Default configuration for standard models (gpt-4, gpt-4o, etc.)
    return DEFAULT_MODEL_CONFIG


def get_exposed_entities(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Get exposed entities."""
    states = [
        state
        for state in hass.states.async_all()
        if async_should_expose(hass, conversation.DOMAIN, state.entity_id)
    ]
    entity_registry = er.async_get(hass)
    exposed_entities = []
    for state in states:
        entity_id = state.entity_id
        entity = entity_registry.async_get(entity_id)

        aliases: list[str] = []
        if entity and entity.aliases:
            aliases = list(entity.aliases)

        exposed_entities.append(
            {
                "entity_id": entity_id,
                "name": state.name,
                "state": state.state,
                "aliases": aliases,
            }
        )
    return exposed_entities


def get_function_executor(value: str) -> FunctionExecutor:
    function_executor = FUNCTION_EXECUTORS.get(value)
    if function_executor is None:
        raise FunctionNotFound(value)
    return function_executor


def is_azure_url(base_url: str | None) -> bool:
    """Check if the base URL is an Azure OpenAI URL."""
    return bool(base_url and re.search(AZURE_DOMAIN_PATTERN, base_url))


def get_token_param_for_model(model: str) -> str:
    """Return the token parameter name for a model."""
    model_lower = model.lower()
    for entry in MODEL_TOKEN_PARAMETER_SUPPORT:
        if re.search(entry["pattern"], model_lower):
            return entry["token_param"]
    return DEFAULT_TOKEN_PARAM


def convert_to_template(
    settings: Any,
    template_keys: list[str] | None = None,
    hass: HomeAssistant | None = None,
) -> None:
    if template_keys is None:
        template_keys = ["data", "event_data", "target", "service"]
    _convert_to_template(settings, template_keys, hass, [])


def _convert_to_template(
    settings: Any,
    template_keys: list[str],
    hass: HomeAssistant | None,
    parents: list[str],
) -> None:
    if isinstance(settings, dict):
        for key, value in settings.items():
            if isinstance(value, str) and (
                key in template_keys or set(parents).intersection(template_keys)
            ):
                settings[key] = Template(value, hass)
            if isinstance(value, dict):
                parents.append(key)
                _convert_to_template(value, template_keys, hass, parents)
                parents.pop()
            if isinstance(value, list):
                parents.append(key)
                for item in value:
                    _convert_to_template(item, template_keys, hass, parents)
                parents.pop()
    if isinstance(settings, list):
        for setting in settings:
            _convert_to_template(setting, template_keys, hass, parents)


def _get_rest_data(
    hass: HomeAssistant, rest_config: dict[str, Any], arguments: dict[str, Any]
) -> rest.data.RestData:
    rest_config.setdefault(CONF_METHOD, rest.const.DEFAULT_METHOD)
    rest_config.setdefault(CONF_VERIFY_SSL, rest.const.DEFAULT_VERIFY_SSL)
    rest_config.setdefault(CONF_TIMEOUT, rest.data.DEFAULT_TIMEOUT)
    rest_config.setdefault(rest.const.CONF_ENCODING, rest.const.DEFAULT_ENCODING)

    resource_template: Template | None = rest_config.get(CONF_RESOURCE_TEMPLATE)
    if resource_template is not None:
        rest_config.pop(CONF_RESOURCE_TEMPLATE)
        rest_config[CONF_RESOURCE] = resource_template.async_render(
            arguments, parse_result=False
        )

    payload_template: Template | None = rest_config.get(CONF_PAYLOAD_TEMPLATE)
    if payload_template is not None:
        rest_config.pop(CONF_PAYLOAD_TEMPLATE)
        rest_config[CONF_PAYLOAD] = payload_template.async_render(
            arguments, parse_result=False
        )

    return rest.create_rest_data_from_config(hass, rest_config)


async def get_authenticated_client(
    hass: HomeAssistant,
    api_key: str,
    base_url: str | None,
    api_version: str | None,
    organization: str | None,
    api_provider: str | None,
    skip_authentication: bool = False,
) -> AsyncClient:
    """Validate OpenAI authentication."""

    client: AsyncClient
    if base_url and (is_azure_url(base_url) or api_provider == "azure"):
        client = AsyncAzureOpenAI(
            api_key=api_key,
            azure_endpoint=base_url,
            api_version=api_version,
            organization=organization,
            http_client=get_async_client(hass),
        )
    else:
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            organization=organization,
            http_client=get_async_client(hass),
        )

    if skip_authentication:
        return client

    response = await hass.async_add_executor_job(
        partial(client.models.list, timeout=10)
    )

    async for _ in response:
        break
    return client


class FunctionExecutor(ABC):
    def __init__(self, data_schema: vol.Schema = vol.Schema({})) -> None:
        """Initialize function executor"""
        self.data_schema = data_schema.extend({vol.Required("type"): str})

    def to_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """to_arguments function"""
        try:
            result = self.data_schema(arguments)
            return dict(result) if isinstance(result, dict) else {}
        except vol.error.Error as e:
            function_type = next(
                (key for key, value in FUNCTION_EXECUTORS.items() if value == self),
                "",
            )
            raise InvalidFunction(function_type) from e

    def validate_entity_ids(
        self,
        hass: HomeAssistant,
        entity_ids: list[str],
        exposed_entities: list[dict[str, Any]],
    ) -> None:
        not_found = [
            entity_id for entity_id in entity_ids if hass.states.get(entity_id) is None
        ]
        if not_found:
            raise EntityNotFound(", ".join(not_found))
        exposed_entity_ids = {e["entity_id"] for e in exposed_entities}
        not_exposed = [
            entity_id for entity_id in entity_ids if entity_id not in exposed_entity_ids
        ]
        if not_exposed:
            raise EntityNotExposed(", ".join(not_exposed))

    @abstractmethod
    async def execute(
        self,
        hass: HomeAssistant,
        function: dict[str, Any],
        arguments: dict[str, Any],
        llm_context: llm.LLMContext | None,
        exposed_entities: list[dict[str, Any]],
    ) -> Any:
        """Execute function"""


class NativeFunctionExecutor(FunctionExecutor):
    def __init__(self) -> None:
        """Initialize native function"""
        super().__init__(vol.Schema({vol.Required("name"): str}))

    async def execute(
        self,
        hass: HomeAssistant,
        function: dict[str, Any],
        arguments: dict[str, Any],
        llm_context: llm.LLMContext | None,
        exposed_entities: list[dict[str, Any]],
    ) -> Any:
        name = function["name"]
        if name == "execute_service":
            return await self.execute_service(
                hass, function, arguments, llm_context, exposed_entities
            )
        if name == "execute_service_single":
            return await self.execute_service_single(
                hass, function, arguments, llm_context, exposed_entities
            )
        if name == "add_automation":
            return await self.add_automation(
                hass, function, arguments, llm_context, exposed_entities
            )
        if name == "get_history":
            return await self.get_history(
                hass, function, arguments, llm_context, exposed_entities
            )
        if name == "get_energy":
            return await self.get_energy(
                hass, function, arguments, llm_context, exposed_entities
            )
        if name == "get_statistics":
            return await self.get_statistics(
                hass, function, arguments, llm_context, exposed_entities
            )
        if name == "get_user_from_user_id":
            return await self.get_user_from_user_id(
                hass, function, arguments, llm_context, exposed_entities
            )

        raise NativeNotFound(name)

    async def execute_service_single(
        self,
        hass: HomeAssistant,
        function: dict[str, Any],
        service_argument: dict[str, Any],
        llm_context: llm.LLMContext | None,
        exposed_entities: list[dict[str, Any]],
    ) -> dict[str, Any]:
        domain = service_argument["domain"]
        service = service_argument["service"]
        service_data = service_argument.get(
            "service_data", service_argument.get("data", {})
        )
        entity_id = service_data.get("entity_id", service_argument.get("entity_id"))
        area_id = service_data.get("area_id")
        device_id = service_data.get("device_id")

        if isinstance(entity_id, str):
            entity_id = [e.strip() for e in entity_id.split(",")]
        service_data["entity_id"] = entity_id

        if entity_id is None and area_id is None and device_id is None:
            raise CallServiceError(domain, service, service_data)
        if not hass.services.has_service(domain, service):
            raise ServiceNotFound(domain, service)
        self.validate_entity_ids(hass, entity_id or [], exposed_entities)

        try:
            await hass.services.async_call(
                domain=domain,
                service=service,
                service_data=service_data,
            )
            return {"success": True}
        except HomeAssistantError as e:
            _LOGGER.error(e)
            return {"error": str(e)}

    async def execute_service(
        self,
        hass: HomeAssistant,
        function: dict[str, Any],
        arguments: dict[str, Any],
        llm_context: llm.LLMContext | None,
        exposed_entities: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        result = []
        for service_argument in arguments.get("list", []):
            result.append(
                await self.execute_service_single(
                    hass, function, service_argument, llm_context, exposed_entities
                )
            )
        return result

    async def add_automation(
        self,
        hass: HomeAssistant,
        function: dict[str, Any],
        arguments: dict[str, Any],
        llm_context: llm.LLMContext | None,
        exposed_entities: list[dict[str, Any]],
    ) -> str:
        automation_config = yaml.safe_load(arguments["automation_config"])
        config = {"id": str(round(time.time() * 1000))}
        if isinstance(automation_config, list):
            config.update(automation_config[0])
        if isinstance(automation_config, dict):
            config.update(automation_config)

        await automation.config._async_validate_config_item(hass, config, True, False)

        automations = [config]
        with open(
            os.path.join(hass.config.config_dir, AUTOMATION_CONFIG_PATH),
            encoding="utf-8",
        ) as f:
            current_automations = yaml.safe_load(f.read())

        with open(
            os.path.join(hass.config.config_dir, AUTOMATION_CONFIG_PATH),
            "a" if current_automations else "w",
            encoding="utf-8",
        ) as f:
            raw_config = yaml.dump(automations, allow_unicode=True, sort_keys=False)
            f.write("\n" + raw_config)

        await hass.services.async_call(automation.config.DOMAIN, SERVICE_RELOAD)
        hass.bus.async_fire(
            EVENT_AUTOMATION_REGISTERED,
            {"automation_config": config, "raw_config": raw_config},
        )
        return "Success"

    async def get_history(
        self,
        hass: HomeAssistant,
        function: dict[str, Any],
        arguments: dict[str, Any],
        llm_context: llm.LLMContext | None,
        exposed_entities: list[dict[str, Any]],
    ) -> list[list[dict[str, Any]]]:
        start_time = arguments.get("start_time")
        end_time = arguments.get("end_time")
        entity_ids = arguments.get("entity_ids", [])
        include_start_time_state = arguments.get("include_start_time_state", True)
        significant_changes_only = arguments.get("significant_changes_only", True)
        minimal_response = arguments.get("minimal_response", True)
        no_attributes = arguments.get("no_attributes", True)

        now = dt_util.utcnow()
        one_day = timedelta(days=1)
        start_time = self.as_utc(start_time, now - one_day, "start_time not valid")
        end_time = self.as_utc(end_time, start_time + one_day, "end_time not valid")

        self.validate_entity_ids(hass, entity_ids, exposed_entities)

        with recorder.util.session_scope(hass=hass, read_only=True) as session:
            result = await recorder.get_instance(hass).async_add_executor_job(
                recorder_history.get_significant_states_with_session,
                hass,
                session,
                start_time,
                end_time,
                entity_ids,
                None,
                include_start_time_state,
                significant_changes_only,
                minimal_response,
                no_attributes,
            )

        return [[self.as_dict(item) for item in sublist] for sublist in result.values()]

    async def get_energy(
        self,
        hass: HomeAssistant,
        function: dict[str, Any],
        arguments: dict[str, Any],
        llm_context: llm.LLMContext | None,
        exposed_entities: list[dict[str, Any]],
    ) -> dict[str, Any]:
        energy_manager: energy.data.EnergyManager = await energy.async_get_manager(hass)
        if energy_manager.data is None:
            return {}
        # energy_manager.data is EnergyPreferences which is a TypedDict (already a dict)
        return dict(energy_manager.data)

    async def get_user_from_user_id(
        self,
        hass: HomeAssistant,
        function: dict[str, Any],
        arguments: dict[str, Any],
        llm_context: llm.LLMContext | None,
        exposed_entities: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if (
            llm_context is None
            or llm_context.context is None
            or llm_context.context.user_id is None
        ):
            return {"name": "Unknown"}
        user = await hass.auth.async_get_user(llm_context.context.user_id)
        user_name = (
            user.name
            if user and hasattr(user, "name") and user.name is not None
            else "Unknown"
        )
        return {"name": user_name}

    async def get_statistics(
        self,
        hass: HomeAssistant,
        function: dict[str, Any],
        arguments: dict[str, Any],
        llm_context: llm.LLMContext | None,
        exposed_entities: list[dict[str, Any]],
    ) -> dict[str, Any]:
        statistic_ids = arguments.get("statistic_ids", [])
        start_time_parsed = dt_util.parse_datetime(arguments["start_time"])
        end_time_parsed = dt_util.parse_datetime(arguments["end_time"])
        if start_time_parsed is None or end_time_parsed is None:
            raise HomeAssistantError("Invalid datetime format")
        start_time = dt_util.as_utc(start_time_parsed)
        end_time = dt_util.as_utc(end_time_parsed)

        return await recorder.get_instance(hass).async_add_executor_job(
            recorder.statistics.statistics_during_period,
            hass,
            start_time,
            end_time,
            statistic_ids,
            arguments.get("period", "day"),
            arguments.get("units"),
            arguments.get("types", {"change"}),
        )

    def as_utc(
        self, value: str | None, default_value: Any, parse_error_message: str
    ) -> Any:
        if value is None:
            return default_value

        parsed_datetime = dt_util.parse_datetime(value)
        if parsed_datetime is None:
            raise HomeAssistantError(parse_error_message)

        return dt_util.as_utc(parsed_datetime)

    def as_dict(self, state: State | dict[str, Any]) -> dict[str, Any]:
        if isinstance(state, State):
            return state.as_dict()
        return state


class ScriptFunctionExecutor(FunctionExecutor):
    def __init__(self) -> None:
        """Initialize script function"""
        super().__init__(script_config.SCRIPT_ENTITY_SCHEMA)

    async def execute(
        self,
        hass: HomeAssistant,
        function: dict[str, Any],
        arguments: dict[str, Any],
        llm_context: llm.LLMContext | None,
        exposed_entities: list[dict[str, Any]],
    ) -> Any:
        script = Script(
            hass,
            function["sequence"],
            "extended_openai_conversation",
            DOMAIN,
            running_description="[extended_openai_conversation] function",
            logger=_LOGGER,
        )

        context = llm_context.context if llm_context else None
        result = await script.async_run(run_variables=arguments, context=context)
        if result is None:
            return "Success"
        return result.variables.get("_function_result", "Success")


class TemplateFunctionExecutor(FunctionExecutor):
    def __init__(self) -> None:
        """Initialize template function"""
        super().__init__(
            vol.Schema(
                {
                    vol.Required("value_template"): cv.template,
                    vol.Optional("parse_result"): bool,
                }
            )
        )

    async def execute(
        self,
        hass: HomeAssistant,
        function: dict[str, Any],
        arguments: dict[str, Any],
        llm_context: llm.LLMContext | None,
        exposed_entities: list[dict[str, Any]],
    ) -> Any:
        return function["value_template"].async_render(
            arguments,
            parse_result=function.get("parse_result", False),
        )


class RestFunctionExecutor(FunctionExecutor):
    def __init__(self) -> None:
        """Initialize Rest function"""
        super().__init__(
            vol.Schema(rest.RESOURCE_SCHEMA).extend(
                {
                    vol.Optional("value_template"): cv.template,
                    vol.Optional("payload_template"): cv.template,
                }
            )
        )

    async def execute(
        self,
        hass: HomeAssistant,
        function: dict[str, Any],
        arguments: dict[str, Any],
        llm_context: llm.LLMContext | None,
        exposed_entities: list[dict[str, Any]],
    ) -> Any:
        config = function
        rest_data = _get_rest_data(hass, config, arguments)

        await rest_data.async_update()
        value = rest_data.data_without_xml()
        value_template = config.get(CONF_VALUE_TEMPLATE)

        if value is not None and value_template is not None:
            value = value_template.async_render_with_possible_json_value(
                value, None, arguments
            )

        return value


class ScrapeFunctionExecutor(FunctionExecutor):
    def __init__(self) -> None:
        """Initialize Scrape function"""
        super().__init__(
            scrape.COMBINED_SCHEMA.extend(
                {
                    vol.Optional("value_template"): cv.template,
                    vol.Optional("payload_template"): cv.template,
                }
            )
        )

    async def execute(
        self,
        hass: HomeAssistant,
        function: dict[str, Any],
        arguments: dict[str, Any],
        llm_context: llm.LLMContext | None,
        exposed_entities: list[dict[str, Any]],
    ) -> Any:
        config = function
        rest_data = _get_rest_data(hass, config, arguments)
        coordinator = scrape.coordinator.ScrapeCoordinator(
            hass,
            None,
            rest_data,
            config,
            scrape.const.DEFAULT_SCAN_INTERVAL,
        )
        await coordinator.async_refresh()

        new_arguments = dict(arguments)

        for sensor_config in config["sensor"]:
            name: Template = sensor_config.get(CONF_NAME)
            value = self._async_update_from_rest_data(
                coordinator.data, sensor_config, arguments
            )
            new_arguments["value"] = value
            if name:
                new_arguments[name.async_render()] = value

        result = new_arguments["value"]
        value_template = config.get(CONF_VALUE_TEMPLATE)

        if value_template is not None:
            result = value_template.async_render_with_possible_json_value(
                result, None, new_arguments
            )

        return result

    def _async_update_from_rest_data(
        self,
        data: BeautifulSoup,
        sensor_config: dict[str, Any],
        arguments: dict[str, Any],
    ) -> Any:
        """Update state from the rest data."""
        value = self._extract_value(data, sensor_config)
        value_template = sensor_config.get(CONF_VALUE_TEMPLATE)

        if value_template is not None:
            value = value_template.async_render_with_possible_json_value(
                value, None, arguments
            )

        return value

    def _extract_value(self, data: BeautifulSoup, sensor_config: dict[str, Any]) -> Any:
        """Parse the html extraction in the executor."""
        value: str | list[str] | None
        select = sensor_config[scrape.const.CONF_SELECT]
        index = sensor_config.get(scrape.const.CONF_INDEX, 0)
        attr = sensor_config.get(CONF_ATTRIBUTE)
        try:
            if attr is not None:
                value = data.select(select)[index][attr]
            else:
                tag = data.select(select)[index]
                if tag.name in ("style", "script", "template"):
                    value = tag.string
                else:
                    value = tag.text
        except IndexError:
            _LOGGER.warning("Index '%s' not found", index)
            value = None
        except KeyError:
            _LOGGER.warning("Attribute '%s' not found", attr)
            value = None
        _LOGGER.debug("Parsed value: %s", value)
        return value


class CompositeFunctionExecutor(FunctionExecutor):
    def __init__(self) -> None:
        """Initialize composite function"""
        super().__init__(
            vol.Schema(
                {
                    vol.Required("sequence"): vol.All(
                        cv.ensure_list, [self.function_schema]
                    )
                }
            )
        )

    def function_schema(self, value: Any) -> dict[str, Any]:
        """Validate a composite function schema."""
        if not isinstance(value, dict):
            raise vol.Invalid("expected dictionary")

        composite_schema = {vol.Optional("response_variable"): str}
        function_executor = get_function_executor(str(value["type"]))

        return dict(function_executor.data_schema.extend(composite_schema)(value))

    async def execute(
        self,
        hass: HomeAssistant,
        function: dict[str, Any],
        arguments: dict[str, Any],
        llm_context: llm.LLMContext | None,
        exposed_entities: list[dict[str, Any]],
    ) -> Any:
        config = function
        sequence = config["sequence"]
        new_arguments = arguments.copy()

        for executor_config in sequence:
            function_executor = get_function_executor(executor_config["type"])
            result = await function_executor.execute(
                hass, executor_config, new_arguments, llm_context, exposed_entities
            )

            response_variable = executor_config.get("response_variable")
            if response_variable:
                new_arguments[response_variable] = result

        return result


class SqliteFunctionExecutor(FunctionExecutor):
    def __init__(self) -> None:
        """Initialize sqlite function"""
        super().__init__(
            vol.Schema(
                {
                    vol.Optional("query"): str,
                    vol.Optional("db_url"): str,
                    vol.Optional("single"): bool,
                }
            )
        )

    def is_exposed(
        self, entity_id: str, exposed_entities: list[dict[str, Any]]
    ) -> bool:
        return any(
            exposed_entity["entity_id"] == entity_id
            for exposed_entity in exposed_entities
        )

    def is_exposed_entity_in_query(
        self, query: str, exposed_entities: list[dict[str, Any]]
    ) -> bool:
        exposed_entity_ids = list(
            map(lambda e: f"'{e['entity_id']}'", exposed_entities)
        )
        return any(
            exposed_entity_id in query for exposed_entity_id in exposed_entity_ids
        )

    def raise_error(self, msg: str = "Unexpected error occurred.") -> None:
        raise HomeAssistantError(msg)

    def get_default_db_url(self, hass: HomeAssistant) -> str:
        db_file_path = os.path.join(hass.config.config_dir, recorder.DEFAULT_DB_FILE)
        return f"file:{db_file_path}?mode=ro"

    def set_url_read_only(self, url: str) -> str:
        scheme, netloc, path, query_string, fragment = parse.urlsplit(url)
        query_params = parse.parse_qs(query_string)

        query_params["mode"] = ["ro"]
        new_query_string = parse.urlencode(query_params, doseq=True)

        return parse.urlunsplit((scheme, netloc, path, new_query_string, fragment))

    async def execute(
        self,
        hass: HomeAssistant,
        function: dict[str, Any],
        arguments: dict[str, Any],
        llm_context: llm.LLMContext | None,
        exposed_entities: list[dict[str, Any]],
    ) -> dict[str, Any] | list[dict[str, Any]]:
        db_url = self.set_url_read_only(
            function.get("db_url", self.get_default_db_url(hass))
        )
        query = function.get("query", "{{query}}")

        template_arguments = {
            "is_exposed": lambda e: self.is_exposed(e, exposed_entities),
            "is_exposed_entity_in_query": lambda q: self.is_exposed_entity_in_query(
                q, exposed_entities
            ),
            "exposed_entities": exposed_entities,
            "raise": self.raise_error,
        }
        template_arguments.update(arguments)

        q = Template(query, hass).async_render(template_arguments)
        _LOGGER.info("Rendered query: %s", q)

        with sqlite3.connect(db_url, uri=True) as conn:
            cursor = conn.cursor().execute(q)
            names = [description[0] for description in cursor.description]

            if function.get("single") is True:
                row = cursor.fetchone()
                return {name: val for name, val in zip(names, row, strict=False)}

            rows = cursor.fetchall()
            result = []
            for row in rows:
                result.append(
                    {name: val for name, val in zip(names, row, strict=False)}
                )
            return result


class FileFunctionExecutor(FunctionExecutor):
    """Base class for file-related function executors."""

    def get_working_dir(self, hass: HomeAssistant) -> Path:
        """Get the default working directory for file operations.

        Args:
            hass: Home Assistant instance

        Returns:
            Path to the working directory
        """
        return Path(hass.config.config_dir) / DEFAULT_WORKING_DIRECTORY

    def to_absolute_path(
        self, hass: HomeAssistant, path: str, base_dir: Path | None = None
    ) -> Path:
        """Convert path to absolute path.

        Args:
            hass: Home Assistant instance
            path: Path to convert (can be relative or absolute)
            base_dir: Base directory for relative paths (defaults to config_dir)

        Returns:
            Absolute path
        """
        p = Path(path)
        if p.is_absolute():
            return p

        if base_dir is None:
            base_dir = Path(hass.config.config_dir)

        return base_dir / p

    def _resolve_path(
        self,
        hass: HomeAssistant,
        path: str,
        allow_dirs: list[str],
    ) -> Path:
        """Resolve path relative to working directory.

        Args:
            hass: Home Assistant instance
            path: Path to resolve
            allow_dirs: List of allowed directory paths (absolute)

        Returns:
            Resolved absolute path

        Raises:
            PermissionError: If path is not within allowed directories
        """
        workdir = self.get_working_dir(hass)
        target = self.to_absolute_path(hass, path, workdir).resolve()

        # Check against allowed directories (already resolved to absolute paths)
        allowed = False
        for allow_dir in allow_dirs:
            allowed_path = Path(allow_dir).resolve()

            if str(target).startswith(str(allowed_path)):
                allowed = True
                break

        if not allowed:
            raise PermissionError(
                f"Access denied: path '{path}' is not in allowed directories"
            )

        return target

    def _render_allow_dirs(
        self,
        hass: HomeAssistant,
        allow_dirs: list[Template],
        arguments: dict[str, Any],
    ) -> list[str]:
        """Render allow_dir templates.

        Args:
            hass: Home Assistant instance
            allow_dirs: List of allow_dir templates from function config
            arguments: Template arguments

        Returns:
            List of rendered directory paths

        Always includes DEFAULT_ALLOWED_DIRS and adds custom allow_dir if specified.
        """
        # Always include default allowed directories (resolved to absolute paths)
        all_allow_dirs = [
            str(self.to_absolute_path(hass, d)) for d in DEFAULT_ALLOWED_DIRS
        ]

        # Add custom allow_dir if specified
        if allow_dirs:
            template_arguments = {
                "config_dir": hass.config.config_dir,
            }
            template_arguments.update(arguments)
            custom_dirs = [
                template.async_render(template_arguments, parse_result=False)
                for template in allow_dirs
            ]
            all_allow_dirs.extend(custom_dirs)

        return all_allow_dirs


class BashFunctionExecutor(FileFunctionExecutor):
    """Execute shell commands with security controls."""

    def __init__(self) -> None:
        """Initialize bash function."""
        schema = vol.Schema(
            {
                vol.Required("command"): cv.template,
                vol.Optional("cwd"): cv.template,
                vol.Optional("restrict_to_workspace", default=True): bool,
                vol.Optional("allow_patterns"): vol.All(cv.ensure_list, [str]),
            }
        )
        super().__init__(schema)

    def _guard_command(
        self,
        command: str,
        cwd: str | Path,
        restrict_to_workspace: bool,
        allow_patterns: list[str] | None = None,
    ) -> None:
        """Validate command against security policies.

        Args:
            command: Shell command to validate
            cwd: Working directory where command will be executed
            restrict_to_workspace: Whether to restrict paths to DEFAULT_ALLOWED_DIRS
            allow_dirs: List of resolved absolute paths that are allowed (only used if restrict_to_workspace is True)
            allow_patterns: Optional list of regex patterns for command allowlist
        """
        # Deny patterns check
        for pattern in SHELL_DENY_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                raise ValueError(
                    f"Command blocked by security policy: matches pattern '{pattern}'"
                )

        # Allow patterns check
        if allow_patterns:
            lower = command.lower()
            if not any(re.search(p, lower) for p in allow_patterns):
                raise ValueError("Command blocked: not in allowlist")

        # Path restriction check when restrict_to_workspace is enabled
        if restrict_to_workspace:
            # Validate working directory is within allowed directories

            # Block path traversal patterns
            if "../" in command or "..\\" in command:
                raise ValueError("Command blocked: path traversal detected")

            # Extract and validate paths in command
            win_paths = re.findall(r"[A-Za-z]:\\[^\\\"\' ]+", command)
            posix_paths = re.findall(r"(?<!\w)/[^\s\"\']+", command)

            for raw in win_paths + posix_paths:
                try:
                    p = Path(raw).resolve()
                except Exception:
                    continue

                if cwd not in p.parents and p != cwd:
                    raise ValueError(
                        f"Command blocked by safety guard (path '{raw}' outside working dir).\nSet 'restrict_to_workspace: false' to allow command outside working directory."
                    )

    async def execute(
        self,
        hass: HomeAssistant,
        function,
        arguments,
        llm_context: llm.LLMContext | None,
        exposed_entities,
    ):
        """Execute shell command with security controls.

        Args:
            hass: Home Assistant instance
            function: Function configuration containing command and optional cwd templates
            arguments: Arguments for rendering templates and optional timeout
            llm_context: LLM context (unused)
            exposed_entities: Exposed entities (unused)

        Returns:
            Command output or error message
        """
        # Render command template
        command_template = function.get("command")
        command = command_template.async_render(arguments, parse_result=False)

        # Render cwd template if provided
        cwd_template = function.get("cwd")
        if cwd_template:
            cwd = Path(cwd_template.async_render(arguments, parse_result=False))
        else:
            cwd = self.get_working_dir(hass)

        timeout = arguments.get("timeout", SHELL_TIMEOUT)
        restrict_to_workspace = function.get("restrict_to_workspace", True)
        allow_patterns = function.get("allow_patterns", [])

        # Security validation
        try:
            self._guard_command(
                command,
                cwd=cwd,
                restrict_to_workspace=restrict_to_workspace,
                allow_patterns=allow_patterns,
            )
        except ValueError as err:
            return {"error": str(err)}

        try:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=timeout
                )
            except TimeoutError:
                process.kill()
                return {"error": f"Command timed out after {timeout} seconds"}

            # Decode output with truncation
            stdout_text = stdout.decode("utf-8", errors="replace")
            stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""

            # Truncate output if too large
            if len(stdout_text) > SHELL_OUTPUT_LIMIT:
                stdout_text = (
                    stdout_text[:SHELL_OUTPUT_LIMIT]
                    + "\n... (truncated, output too large)"
                )
            if len(stderr_text) > SHELL_OUTPUT_LIMIT:
                stderr_text = (
                    stderr_text[:SHELL_OUTPUT_LIMIT]
                    + "\n... (truncated, output too large)"
                )

            result = {
                "exit_code": process.returncode,
                "stdout": stdout_text,
            }

            if stderr_text:
                result["stderr"] = stderr_text

        except Exception as e:
            _LOGGER.error(e)
            return {"error": str(e)}

        return result


class ReadFileFunctionExecutor(FileFunctionExecutor):
    """Read file contents."""

    def __init__(self) -> None:
        """Initialize read file function."""
        schema = vol.Schema(
            {
                vol.Required("path"): cv.template,
                vol.Optional("allow_dir"): vol.All(cv.ensure_list, [cv.template]),
            }
        )
        super().__init__(schema)

    async def execute(
        self,
        hass: HomeAssistant,
        function,
        arguments,
        llm_context: llm.LLMContext | None,
        exposed_entities,
    ):
        """Read file contents."""
        path_template = function.get("path")
        path_str = path_template.async_render(arguments, parse_result=False)
        allow_dirs = self._render_allow_dirs(
            hass, function.get("allow_dir", []), arguments
        )

        try:
            target_path = self._resolve_path(hass, path_str, allow_dirs)

            if not target_path.exists():
                return {"error": f"File not found: {path_str}"}

            if not target_path.is_file():
                return {"error": f"Not a file: {path_str}"}

            # Check file size
            file_size = target_path.stat().st_size
            if file_size > FILE_READ_SIZE_LIMIT:
                return {
                    "error": f"File too large: {file_size} bytes (limit: {FILE_READ_SIZE_LIMIT})"
                }

            # Read file
            content = await hass.async_add_executor_job(
                partial(target_path.read_text, encoding="utf-8")
            )

        except Exception as e:
            _LOGGER.error(e)
            return {"error": str(e)}

        return {"content": content, "size": file_size}


class WriteFileFunctionExecutor(FileFunctionExecutor):
    """Write content to file."""

    def __init__(self) -> None:
        """Initialize write file function."""
        schema = vol.Schema(
            {
                vol.Required("path"): cv.template,
                vol.Required("content"): cv.template,
                vol.Optional("allow_dir"): vol.All(cv.ensure_list, [cv.template]),
            }
        )
        super().__init__(schema)

    async def execute(
        self,
        hass: HomeAssistant,
        function,
        arguments,
        llm_context: llm.LLMContext | None,
        exposed_entities,
    ):
        """Write content to file."""
        path_template = function.get("path")
        path_str = path_template.async_render(arguments, parse_result=False)
        content_template = function.get("content")
        content = content_template.async_render(arguments, parse_result=False)
        allow_dirs = self._render_allow_dirs(
            hass, function.get("allow_dir", []), arguments
        )

        try:
            target_path = self._resolve_path(hass, path_str, allow_dirs)

            # Create parent directories if needed
            # await hass.async_add_executor_job(
            #     partial(target_path.parent.mkdir, parents=True, exist_ok=True)
            # )

            # Write file
            await hass.async_add_executor_job(
                partial(target_path.write_text, content, encoding="utf-8")
            )

            bytes_written = len(content.encode("utf-8"))

        except Exception as err:
            _LOGGER.exception("File write error: %s", err)
            return {"error": str(err)}

        return {
            "success": True,
            "path": str(target_path),
            "bytes_written": bytes_written,
        }


class EditFileFunctionExecutor(FileFunctionExecutor):
    """Edit file with find-and-replace."""

    def __init__(self) -> None:
        """Initialize edit file function."""
        schema = vol.Schema(
            {
                vol.Required("path"): cv.template,
                vol.Required("old_text"): cv.template,
                vol.Required("new_text"): cv.template,
                vol.Optional("allow_dir"): vol.All(cv.ensure_list, [cv.template]),
            }
        )
        super().__init__(schema)

    async def execute(
        self,
        hass: HomeAssistant,
        function,
        arguments,
        llm_context: llm.LLMContext | None,
        exposed_entities,
    ):
        """Edit file with find-and-replace."""
        path_template = function.get("path")
        path_str = path_template.async_render(arguments, parse_result=False)
        old_text_template = function.get("old_text")
        old_text = old_text_template.async_render(arguments, parse_result=False)
        new_text_template = function.get("new_text")
        new_text = new_text_template.async_render(arguments, parse_result=False)
        allow_dirs = self._render_allow_dirs(
            hass, function.get("allow_dir", []), arguments
        )

        try:
            target_path = self._resolve_path(hass, path_str, allow_dirs)

            if not target_path.exists():
                return {"error": f"File not found: {path_str}"}

            if not target_path.is_file():
                return {"error": f"Not a file: {path_str}"}

            # Read current content
            content = await hass.async_add_executor_job(
                partial(target_path.read_text, encoding="utf-8")
            )

            # Check for text to replace
            if old_text not in content:
                return {"error": f"Text not found in file: {old_text[:50]}..."}

            # Check for multiple occurrences
            occurrence_count = content.count(old_text)
            if occurrence_count > 1:
                return {
                    "error": f"Text appears {occurrence_count} times in file. "
                    "Please provide more specific text to ensure single replacement."
                }

            # Perform replacement
            new_content = content.replace(old_text, new_text, 1)

            # Write back
            await hass.async_add_executor_job(
                partial(target_path.write_text, new_content, encoding="utf-8")
            )

        except Exception as e:
            _LOGGER.error(e)
            return {"error": str(e)}

        return {
            "success": True,
            "path": str(target_path),
            "replacements": 1,
        }


FUNCTION_EXECUTORS: dict[str, FunctionExecutor] = {
    "native": NativeFunctionExecutor(),
    "script": ScriptFunctionExecutor(),
    "template": TemplateFunctionExecutor(),
    "rest": RestFunctionExecutor(),
    "scrape": ScrapeFunctionExecutor(),
    "composite": CompositeFunctionExecutor(),
    "sqlite": SqliteFunctionExecutor(),
    "bash": BashFunctionExecutor(),
    "read_file": ReadFileFunctionExecutor(),
    "write_file": WriteFileFunctionExecutor(),
    "edit_file": EditFileFunctionExecutor(),
}

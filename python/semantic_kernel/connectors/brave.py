# Copyright (c) Microsoft. All rights reserved.

import ast
import logging
import sys
from collections.abc import AsyncIterable, Callable
from inspect import getsource
from typing import Any, ClassVar, Final, Literal

from httpx import AsyncClient, HTTPStatusError, RequestError
from pydantic import Field, SecretStr, ValidationError

from semantic_kernel.connectors._search_shared import SearchLambdaVisitor
from semantic_kernel.data.text_search import (
    KernelSearchResults,
    SearchOptions,
    TextSearch,
    TextSearchResult,
    TSearchResult,
)
from semantic_kernel.exceptions import ServiceInitializationError, ServiceInvalidRequestError
from semantic_kernel.kernel_pydantic import KernelBaseModel, KernelBaseSettings
from semantic_kernel.kernel_types import OptionalOneOrList
from semantic_kernel.utils.feature_stage_decorator import experimental
from semantic_kernel.utils.telemetry.user_agent import SEMANTIC_KERNEL_USER_AGENT

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

logger: logging.Logger = logging.getLogger(__name__)

# region Constants
DEFAULT_URL: Final[str] = "https://api.search.brave.com/res/v1/web/search"
QUERY_PARAMETERS: Final[list[str]] = [
    "country",
    "search_lang",
    "ui_lang",
    "safesearch",
    "text_decorations",
    "spellcheck",
    "result_filter",
    "units",
]


# endregion Constants


# region BraveSettings
class BraveSettings(KernelBaseSettings):
    """Brave Connector settings.

    The settings are first loaded from environment variables with the prefix 'BRAVE_'. If the
    environment variables are not found, the settings can be loaded from a .env file with the
    encoding 'utf-8'. If the settings are not found in the .env file, the settings are ignored;
    however, validation will fail alerting that the settings are missing.

    Optional settings for prefix 'BRAVE_' are:
    - api_key: SecretStr - The Brave API key (Env var BRAVE_API_KEY)

    """

    env_prefix: ClassVar[str] = "BRAVE_"

    api_key: SecretStr


# endregion BraveSettings


# region BraveWeb
@experimental
class BraveWebPage(KernelBaseModel):
    """A Brave web page."""

    type: str | None = None
    title: str | None = None
    url: str | None = None
    thumbnail: dict[str, str | bool] | None = None
    description: str | None = None
    age: str | None = None
    language: str | None = None
    family_friendly: bool | None = None
    extra_snippets: list[str] | None = None
    meta_ur: dict[str, str] | None = None
    source: str | None = None


@experimental
class BraveWebPages(KernelBaseModel):
    """THe web pages from a Brave search."""

    type: str | None = Field(default="webpage")
    family_friendly: bool | None = Field(default=None)
    results: list[BraveWebPage] = Field(default_factory=list)


@experimental
class BraveSearchResponse(KernelBaseModel):
    """The response from a Brave search."""

    type: str = Field(default="search", alias="type")
    query_context: dict[str, Any] = Field(default_factory=dict, validation_alias="query")
    web_pages: BraveWebPages | None = Field(default=None, validation_alias="web")


# endregion BraveWeb


@experimental
class BraveSearch(KernelBaseModel, TextSearch):
    """A search engine connector that uses the Brave Search API to perform a web search."""

    settings: BraveSettings

    def __init__(
        self,
        api_key: str | None = None,
        env_file_path: str | None = None,
        env_file_encoding: str | None = None,
    ) -> None:
        """Initializes a new instance of the Brave Search class.

        Args:
            api_key: The Brave Search API key. If provided, will override
                the value in the env vars or .env file.
            env_file_path: The optional path to the .env file. If provided,
                the settings are read from this file path location.
            env_file_encoding: The optional encoding of the .env file. If provided,
                the settings are read from this file path location.
        """
        try:
            settings = BraveSettings(
                api_key=api_key,
                env_file_path=env_file_path,
                env_file_encoding=env_file_encoding,
            )
        except ValidationError as ex:
            raise ServiceInitializationError("Failed to create Brave settings.") from ex

        super().__init__(settings=settings)  # type: ignore[call-arg]

    @override
    async def search(
        self,
        query: str,
        output_type: type[str] | type[TSearchResult] | Literal["Any"] = str,
        *,
        filter: OptionalOneOrList[Callable | str] = None,
        skip: int = 0,
        top: int = 5,
        include_total_count: bool = False,
        **kwargs: Any,
    ) -> "KernelSearchResults[TSearchResult]":
        options = SearchOptions(filter=filter, skip=skip, top=top, include_total_count=include_total_count, **kwargs)
        results = await self._inner_search(query=query, options=options)
        return KernelSearchResults(
            results=self._get_result_strings(results)
            if output_type is str
            else self._get_text_search_results(results)
            if output_type is TextSearchResult
            else self._get_brave_web_pages(results),
            total_count=self._get_total_count(results, options),
            metadata=self._get_metadata(results),
        )

    async def _get_result_strings(self, response: BraveSearchResponse) -> AsyncIterable[str]:
        if response.web_pages is None:
            return
        for web_page in response.web_pages.results:
            yield web_page.description or ""

    async def _get_text_search_results(self, response: BraveSearchResponse) -> AsyncIterable[TextSearchResult]:
        if response.web_pages is None:
            return
        for web_page in response.web_pages.results:
            yield TextSearchResult(
                name=web_page.title,
                value=web_page.description,
                link=web_page.url,
            )

    async def _get_brave_web_pages(self, response: BraveSearchResponse) -> AsyncIterable[BraveWebPage]:
        if response.web_pages is None:
            return
        for val in response.web_pages.results:
            yield val

    def _get_metadata(self, response: BraveSearchResponse) -> dict[str, Any]:
        return {
            "original": response.query_context.get("original"),
            "altered": response.query_context.get("altered", ""),
            "spellcheck_off": response.query_context.get("spellcheck_off"),
            "show_strict_warning": response.query_context.get("show_strict_warning"),
            "country": response.query_context.get("country"),
        }

    def _get_total_count(self, response: BraveSearchResponse, options: SearchOptions) -> int | None:
        if options.include_total_count and response.web_pages is not None:
            return len(response.web_pages.results)
        return None

    def _get_options(self, **kwargs: Any) -> SearchOptions:
        try:
            return SearchOptions(**kwargs)
        except ValidationError:
            return SearchOptions()

    async def _inner_search(self, query: str, options: SearchOptions) -> BraveSearchResponse:
        self._validate_options(options)

        logger.info(
            f"Received request for brave web search with \
                params:\nnum_results: {options.top}\noffset: {options.skip}"
        )

        url = self._get_url()
        params = self._build_request_parameters(query, options)

        logger.info(f"Sending GET request to {url}")

        headers = {
            "X-Subscription-Token": self.settings.api_key.get_secret_value(),
            "user_agent": SEMANTIC_KERNEL_USER_AGENT,
        }
        try:
            async with AsyncClient(timeout=5) as client:
                response = await client.get(url, headers=headers, params=params)
                response.raise_for_status()
                return BraveSearchResponse.model_validate_json(response.text)
        except HTTPStatusError as ex:
            logger.error(f"Failed to get search results: {ex}")
            raise ServiceInvalidRequestError("Failed to get search results.") from ex
        except RequestError as ex:
            logger.error(f"Client error occurred: {ex}")
            raise ServiceInvalidRequestError("A client error occurred while getting search results.") from ex
        except Exception as ex:
            logger.error(f"An unexpected error occurred: {ex}")
            raise ServiceInvalidRequestError("An unexpected error occurred while getting search results.") from ex

    def _validate_options(self, options: SearchOptions) -> None:
        if options.top <= 0:
            raise ServiceInvalidRequestError("count value must be greater than 0.")
        if options.top >= 21:
            raise ServiceInvalidRequestError("count value must be less than 21.")

        if options.skip < 0:
            raise ServiceInvalidRequestError("offset must be greater than or equal to 0.")
        if options.skip > 9:
            raise ServiceInvalidRequestError("offset must be less than 10.")

    def _get_url(self) -> str:
        return DEFAULT_URL

    def _parse_filter_lambda(self, filter_lambda: Callable | str) -> list[dict[str, str]]:
        """Parse a string lambda or string expression into a list of {field: value} dicts using AST."""
        expr = filter_lambda if isinstance(filter_lambda, str) else getsource(filter_lambda).strip()
        tree = ast.parse(expr, mode="eval")
        node = tree.body
        visitor = SearchLambdaVisitor(valid_parameters=QUERY_PARAMETERS)
        visitor.visit(node)
        return visitor.filters

    def _build_request_parameters(self, query: str, options: SearchOptions) -> dict[str, str | int | bool]:
        params: dict[str, str | int] = {"q": query or "", "count": options.top, "offset": options.skip}
        if not options.filter:
            return params
        filters = options.filter
        if not isinstance(filters, list):
            filters = [filters]
        for f in filters:
            try:
                for d in self._parse_filter_lambda(f):
                    for field, value in d.items():
                        params[field] = value
            except Exception as exc:
                logger.warning(f"Failed to parse filter lambda: {f}, ignoring this filter. Error: {exc}")
                continue
        return params

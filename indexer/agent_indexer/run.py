"""One incremental indexing pass: Graph -> Azure AI Search."""

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from azure.identity import DefaultAzureCredential

from .graph import CopilotPackagesClient
from .search import SCHEMA_VERSION, AgentIndex, is_agent
from .state import Watermark, WatermarkStore

# Re-read a little before the previous run started so clock skew or late writes aren't missed.
# Upserts are idempotent, so the overlap is harmless.
WATERMARK_OVERLAP = timedelta(minutes=10)
DETAIL_CONCURRENCY = 8


@dataclass
class Settings:
    search_endpoint: str
    index_name: str
    state_account_url: str
    state_container: str
    graph_base_url: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            search_endpoint=os.environ["AZURE_SEARCH_ENDPOINT"],
            index_name=os.environ.get("AZURE_SEARCH_INDEX", "agents"),
            state_account_url=os.environ["STATE_STORAGE_ACCOUNT_URL"],
            state_container=os.environ.get("STATE_CONTAINER", "indexer-state"),
            graph_base_url=os.environ.get("GRAPH_BASE_URL", "https://graph.microsoft.com/v1.0"),
        )


def modified_since(watermark: Watermark | None) -> datetime | None:
    """Start of the window to index, or None for a full re-index."""
    if watermark is None:
        return None
    if watermark.schema_version != SCHEMA_VERSION:
        logging.info("Index schema changed (v%s -> v%s); re-indexing all packages", watermark.schema_version, SCHEMA_VERSION)
        return None
    return watermark.last_run_start - WATERMARK_OVERLAP


def run_indexer(settings: Settings) -> dict:
    run_start = datetime.now(timezone.utc)
    credential = DefaultAzureCredential()
    graph = CopilotPackagesClient(credential, settings.graph_base_url)
    index = AgentIndex(settings.search_endpoint, settings.index_name, credential)
    state = WatermarkStore(settings.state_account_url, settings.state_container, credential)

    since = modified_since(state.load())
    logging.info("Indexing packages modified since %s", since.isoformat() if since else "the beginning")

    index.ensure_index()

    # Every package is indexed; to_document marks which ones are agents (isAgent), so agents that
    # aren't hosted in Copilot are no longer dropped.
    summaries = list(graph.list_packages(since))

    # longDescription and activeUsers are only returned by the per-package detail endpoint.
    with ThreadPoolExecutor(DETAIL_CONCURRENCY) as pool:
        details = [d for d in pool.map(graph.get_package, [p["id"] for p in summaries]) if d is not None]

    indexed = index.upsert(details)
    state.save(run_start, SCHEMA_VERSION)

    result = {
        "since": since.isoformat() if since else None,
        "listed": len(summaries),
        "indexed": indexed,
        "agents": sum(is_agent(d) for d in details),
    }
    logging.info("Indexer run complete: %s", result)
    return result

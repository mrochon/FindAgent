"""Azure AI Search index schema and document mapping for Copilot agents."""

import base64
import logging
import re
from datetime import datetime, timezone

from azure.core.credentials import TokenCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    SearchableField,
    SearchFieldDataType,
    SearchIndex,
    ScoringProfile,
    SimpleField,
    TextWeights,
)

UPLOAD_BATCH_SIZE = 500
ANALYZER = "en.microsoft"


def build_index(name: str) -> SearchIndex:
    string_collection = SearchFieldDataType.Collection(SearchFieldDataType.String)
    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True),
        SimpleField(name="packageId", type=SearchFieldDataType.String, filterable=True),
        # Full-text fields.
        SearchableField(name="displayName", analyzer_name=ANALYZER),
        SearchableField(name="longDescription", analyzer_name=ANALYZER),
        SearchableField(name="shortDescription", analyzer_name=ANALYZER),
        # Filterable fields.
        SimpleField(name="publisher", type=SearchFieldDataType.String, filterable=True, facetable=True, sortable=True),
        SimpleField(name="activeUsers", type=SearchFieldDataType.Int32, filterable=True, facetable=True, sortable=True),
        SimpleField(name="supportedHosts", type=string_collection, filterable=True, facetable=True),
        SimpleField(name="categories", type=string_collection, filterable=True, facetable=True),
        SimpleField(name="elementTypes", type=string_collection, filterable=True, facetable=True),
        SimpleField(name="type", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="platform", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="availableTo", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="isBlocked", type=SearchFieldDataType.Boolean, filterable=True),
        SimpleField(name="version", type=SearchFieldDataType.String),
        SimpleField(name="lastModifiedDateTime", type=SearchFieldDataType.DateTimeOffset, filterable=True, sortable=True),
    ]
    boost_name = ScoringProfile(
        name="boostDisplayName",
        text_weights=TextWeights(weights={"displayName": 3.0, "shortDescription": 1.5, "longDescription": 1.0}),
    )
    return SearchIndex(
        name=name,
        fields=fields,
        scoring_profiles=[boost_name],
        default_scoring_profile=boost_name.name,
    )


def document_key(package_id: str) -> str:
    # Search keys only allow letters, digits, '_', '-' and '='; package ids are opaque, so encode them.
    return base64.urlsafe_b64encode(package_id.encode()).decode()


def _parse_datetime(value: str | None) -> str | None:
    if not value:
        return None
    # Graph returns up to 7 fractional digits; Python only parses 6.
    value = re.sub(r"(\.\d{6})\d+", r"\1", value).replace("Z", "+00:00")
    return datetime.fromisoformat(value).astimezone(timezone.utc).isoformat()


def to_document(pkg: dict) -> dict:
    active_users = pkg.get("activeUsers")
    return {
        "id": document_key(pkg["id"]),
        "packageId": pkg["id"],
        "displayName": pkg.get("displayName"),
        "longDescription": pkg.get("longDescription"),
        "shortDescription": pkg.get("shortDescription"),
        "publisher": pkg.get("publisher"),
        "activeUsers": int(active_users) if active_users is not None else None,
        "supportedHosts": pkg.get("supportedHosts") or [],
        "categories": pkg.get("categories") or [],
        "elementTypes": pkg.get("elementTypes") or [],
        "type": pkg.get("type"),
        "platform": pkg.get("platform"),
        "availableTo": pkg.get("availableTo"),
        "isBlocked": pkg.get("isBlocked"),
        "version": pkg.get("version"),
        "lastModifiedDateTime": _parse_datetime(pkg.get("lastModifiedDateTime")),
    }


class AgentIndex:
    def __init__(self, endpoint: str, index_name: str, credential: TokenCredential) -> None:
        self._index_client = SearchIndexClient(endpoint, credential)
        self._search_client = SearchClient(endpoint, index_name, credential)
        self._index_name = index_name

    def ensure_index(self) -> None:
        self._index_client.create_or_update_index(build_index(self._index_name))

    def upsert(self, packages: list[dict]) -> int:
        docs = [to_document(p) for p in packages]
        for start in range(0, len(docs), UPLOAD_BATCH_SIZE):
            results = self._search_client.merge_or_upload_documents(docs[start : start + UPLOAD_BATCH_SIZE])
            failed = [r for r in results if not r.succeeded]
            if failed:
                for r in failed:
                    logging.error("Failed to index %s: %s", r.key, r.error_message)
                raise RuntimeError(f"{len(failed)} documents failed to index")
        return len(docs)

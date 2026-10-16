"""Minimal client for the Microsoft Graph Copilot Package Management API."""

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Iterator
from urllib.parse import quote

import requests
from azure.core.credentials import AccessToken, TokenCredential

GRAPH_SCOPE = "https://graph.microsoft.com/.default"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class CopilotPackagesClient:
    def __init__(
        self,
        credential: TokenCredential,
        base_url: str = "https://graph.microsoft.com/v1.0",
        timeout: float = 30,
        max_retries: int = 5,
    ) -> None:
        self._credential = credential
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._session = requests.Session()
        self._token: AccessToken | None = None
        self._token_lock = threading.Lock()

    def _auth_header(self) -> dict[str, str]:
        with self._token_lock:
            if self._token is None or self._token.expires_on - 300 < time.time():
                self._token = self._credential.get_token(GRAPH_SCOPE)
            return {"Authorization": f"Bearer {self._token.token}"}

    def _get(self, url: str, params: dict[str, str] | None = None) -> requests.Response:
        for attempt in range(self._max_retries + 1):
            resp = self._session.get(
                url,
                params=params,
                headers={**self._auth_header(), "Accept": "application/json"},
                timeout=self._timeout,
            )
            if resp.status_code in RETRYABLE_STATUS and attempt < self._max_retries:
                delay = float(resp.headers.get("Retry-After", 2**attempt))
                logging.warning("Graph returned %s for %s; retrying in %.0fs", resp.status_code, url, delay)
                time.sleep(delay)
                continue
            return resp
        raise AssertionError("unreachable")

    def list_packages(self, modified_since: datetime | None = None) -> Iterator[dict]:
        """Yields copilotPackage summaries, optionally only those modified on/after `modified_since`."""
        url = f"{self._base_url}/copilot/admin/catalog/packages"
        params = None
        if modified_since is not None:
            since = modified_since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            params = {"$filter": f"lastModifiedDateTime ge {since}"}

        while url:
            resp = self._get(url, params)
            resp.raise_for_status()
            body = resp.json()
            yield from body.get("value", [])
            # nextLink is opaque and already carries the query options.
            url = body.get("@odata.nextLink")
            params = None

    def get_package(self, package_id: str) -> dict | None:
        """Returns the copilotPackageDetail, or None if the package no longer exists."""
        resp = self._get(f"{self._base_url}/copilot/admin/catalog/packages/{quote(package_id, safe='')}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

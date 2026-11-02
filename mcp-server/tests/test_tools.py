import asyncio
import os

import pytest

os.environ.setdefault("AZURE_SEARCH_ENDPOINT", "https://example.search.windows.net")
import server  # noqa: E402


class FakeResults:
    def __init__(self, docs: list[dict]) -> None:
        self._docs = docs

    def __aiter__(self):
        async def gen():
            for doc in self._docs:
                yield dict(doc)

        return gen()

    async def get_count(self) -> int:
        return len(self._docs)


class FakeSearch:
    def __init__(self, docs: list[dict]) -> None:
        self.docs = docs
        self.calls: list[dict] = []

    async def search(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResults(self.docs)


ENTRA_AGENT = {"id": "a", "packageId": "P_1", "displayName": "HR", "agentIdentityId": "aaaa-1111", "@search.score": 2.0}
OTHER_AGENT = {"id": "b", "packageId": "P_2", "displayName": "IT", "agentIdentityId": None, "@search.score": 1.0}


@pytest.fixture
def search(monkeypatch) -> FakeSearch:
    fake = FakeSearch([ENTRA_AGENT, OTHER_AGENT])
    monkeypatch.setattr(server, "_search", fake)
    return fake


def test_search_results_flag_entra_managed_agents(search):
    result = asyncio.run(server.search_agents(query="hr"))
    assert [(a["packageId"], a["agentIdentityId"], a["entraManaged"]) for a in result["agents"]] == [
        ("P_1", "aaaa-1111", True),
        ("P_2", None, False),
    ]
    assert "agentIdentityId" in search.calls[0]["select"]


@pytest.mark.parametrize(
    ("entra_managed", "expected"),
    [(True, "agentIdentityId ne null"), (False, "agentIdentityId eq null")],
)
def test_entra_managed_filter(search, entra_managed, expected):
    asyncio.run(server.search_agents(entra_managed=entra_managed))
    assert search.calls[0]["filter"] == f"isAgent ne false and isBlocked ne true and {expected}"


def test_no_entra_filter_by_default(search):
    asyncio.run(server.search_agents())
    assert "agentIdentityId" not in search.calls[0]["filter"]


def test_search_returns_only_agents_by_default(search):
    asyncio.run(server.search_agents())
    assert search.calls[0]["filter"] == "isAgent ne false and isBlocked ne true"
    assert "isAgent" in search.calls[0]["select"]


def test_search_can_include_non_agents(search):
    asyncio.run(server.search_agents(include_non_agents=True, include_blocked=True))
    assert search.calls[0]["filter"] is None


@pytest.mark.parametrize(("include_non_agents", "expected"), [(False, "isAgent ne false"), (True, None)])
def test_list_publishers_counts_agents_by_default(search, monkeypatch, include_non_agents, expected):
    async def get_facets(self):
        return {"publisher": [{"value": "Contoso", "count": 3}]}

    monkeypatch.setattr(FakeResults, "get_facets", get_facets, raising=False)
    result = asyncio.run(server.list_publishers(include_non_agents=include_non_agents))
    assert result == [{"publisher": "Contoso", "agentCount": 3}]
    assert search.calls[0]["filter"] == expected


def test_get_agent_includes_entra_flag(search):
    search.docs = [ENTRA_AGENT]
    agent = asyncio.run(server.get_agent("P_1"))
    assert agent["entraManaged"] is True
    assert "id" not in agent and "@search.score" not in agent

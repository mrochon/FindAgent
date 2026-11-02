from datetime import datetime, timezone

from agent_indexer.run import WATERMARK_OVERLAP, modified_since
from agent_indexer.search import SCHEMA_VERSION, build_index, document_key, is_agent, to_document
from agent_indexer.state import Watermark


def test_to_document_maps_detail_fields():
    doc = to_document(
        {
            "id": "P_19ae1zz1-56bc-505a-3d42-156df75a4xxy",
            "displayName": "Contoso HR Agent",
            "longDescription": "Answers HR policy questions.",
            "publisher": "Contoso",
            "activeUsers": "42",
            "supportedHosts": ["Copilot"],
            "lastModifiedDateTime": "2026-01-06T00:07:20.1467852Z",
        }
    )
    assert doc["id"] == document_key("P_19ae1zz1-56bc-505a-3d42-156df75a4xxy")
    assert doc["activeUsers"] == 42
    assert doc["lastModifiedDateTime"] == "2026-01-06T00:07:20.146785+00:00"
    assert doc["categories"] == []


def test_document_fields_exist_in_index():
    index_fields = {f.name for f in build_index("agents").fields}
    assert set(to_document({"id": "x"})) <= index_fields


def test_is_agent():
    assert is_agent({"supportedHosts": ["copilot", "Teams"]})
    # Agents not hosted in Copilot: an Entra agent identity or an agent element type is enough.
    assert is_agent({"supportedHosts": ["Teams"], "agentIdentityId": "aaaa-bbbb"})
    assert is_agent({"supportedHosts": ["M365"], "elementTypes": ["CustomEngineAgent"]})
    assert is_agent({"elementTypes": ["declarativeAgent"]})
    # Plain Teams apps, bots and Office add-ins aren't agents.
    assert not is_agent({"supportedHosts": ["Teams"], "elementTypes": ["bots"]})
    assert not is_agent({"supportedHosts": ["Outlook"], "elementTypes": ["officeAddIn"], "agentIdentityId": ""})
    assert not is_agent({})


def test_documents_are_flagged_as_agents():
    assert to_document({"id": "x", "supportedHosts": ["Teams"], "agentIdentityId": "aaaa-bbbb"})["isAgent"] is True
    assert to_document({"id": "y", "supportedHosts": ["Outlook"]})["isAgent"] is False
    field = next(f for f in build_index("agents").fields if f.name == "isAgent")
    assert field.filterable and field.facetable


def test_agent_identity_id_is_indexed_and_filterable():
    field = next(f for f in build_index("agents").fields if f.name == "agentIdentityId")
    assert field.filterable
    assert to_document({"id": "x", "agentIdentityId": "aaaa-bbbb"})["agentIdentityId"] == "aaaa-bbbb"
    # Agents not managed in Entra ID: missing or empty both become null.
    assert to_document({"id": "x"})["agentIdentityId"] is None
    assert to_document({"id": "x", "agentIdentityId": ""})["agentIdentityId"] is None


def test_schema_change_forces_full_reindex():
    last_run = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    assert modified_since(None) is None
    assert modified_since(Watermark(last_run, SCHEMA_VERSION - 1)) is None
    assert modified_since(Watermark(last_run, SCHEMA_VERSION)) == last_run - WATERMARK_OVERLAP

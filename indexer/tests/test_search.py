from agent_indexer.run import is_agent
from agent_indexer.search import build_index, document_key, to_document


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
    assert not is_agent({"supportedHosts": ["Outlook"]})
    assert not is_agent({})

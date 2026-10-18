import os
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

os.environ.setdefault("AZURE_SEARCH_ENDPOINT", "https://example.search.windows.net")
from server import EntraAuthMiddleware  # noqa: E402

TENANT = "11111111-1111-1111-1111-111111111111"
CLIENT_ID = "22222222-2222-2222-2222-222222222222"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"
RESOURCE = "https://mcp.example.com/mcp"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(**overrides) -> str:
    claims = {"iss": ISSUER, "aud": CLIENT_ID, "exp": int(time.time()) + 600, "roles": ["agent.find.all"], **overrides}
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, KEY, algorithm="RS256")


def make_client(resource_url: str | None) -> TestClient:
    inner = Starlette(routes=[Route("/mcp", lambda request: PlainTextResponse("ok"), methods=["POST"])])
    app = EntraAuthMiddleware(
        inner,
        tenant_id=TENANT,
        audiences=[CLIENT_ID, f"api://{CLIENT_ID}"],
        required_role="agent.find.all",
        resource_url=resource_url,
        scopes_supported=[f"api://{CLIENT_ID}/access_as_user"],
    )
    app._jwks = SimpleNamespace(get_signing_key_from_jwt=lambda _: SimpleNamespace(key=KEY.public_key()))
    return TestClient(app)


@pytest.fixture
def client() -> TestClient:
    return make_client(RESOURCE)


def call(client: TestClient, bearer: str | None):
    headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    return client.post("/mcp", headers=headers)


def test_metadata_is_public(client):
    for path in ["/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"]:
        body = client.get(path).json()
        assert body["resource"] == RESOURCE
        assert body["authorization_servers"] == [ISSUER]


def test_missing_token_gets_challenge(client):
    resp = call(client, None)
    assert resp.status_code == 401
    assert 'resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource/mcp"' in resp.headers["www-authenticate"]


def test_metadata_can_be_turned_off():
    client = make_client(None)
    for path in ["/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"]:
        assert client.get(path).status_code == 404
    resp = call(client, None)
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"
    resp = call(client, token(exp=int(time.time()) - 60))
    assert resp.headers["www-authenticate"].startswith('Bearer error="invalid_token"')
    assert call(client, token()).text == "ok"


def test_delegated_token_with_role(client):
    assert call(client, token(scp="access_as_user", oid="user")).text == "ok"


def test_application_token_with_role(client):
    assert call(client, token(azp="calling-app", idtyp="app")).text == "ok"


def test_token_without_role_is_forbidden(client):
    resp = call(client, token(roles=None, scp="access_as_user"))
    assert resp.status_code == 403
    assert 'error="insufficient_scope"' in resp.headers["www-authenticate"]


@pytest.mark.parametrize(
    "overrides",
    [{"aud": "some-other-api"}, {"iss": "https://login.microsoftonline.com/other/v2.0"}, {"exp": int(time.time()) - 60}],
)
def test_invalid_tokens_are_rejected(client, overrides):
    resp = call(client, token(**overrides))
    assert resp.status_code == 401
    assert 'error="invalid_token"' in resp.headers["www-authenticate"]

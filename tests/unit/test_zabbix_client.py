import pytest
import respx
from httpx import Response

from zabbix_ai.clients.zabbix import ZabbixClient, ZabbixError


@pytest.fixture
def client():
    return ZabbixClient(name="monitoring", url="https://zbx.test", token="tok")

@respx.mock
async def test_call_sends_jsonrpc_with_auth(client):
    route = respx.post("https://zbx.test/api_jsonrpc.php").mock(
        return_value=Response(200, json={"jsonrpc": "2.0", "result": [], "id": 1})
    )
    await client.call("host.get", {"output": ["hostid"]})
    assert route.called
    req = route.calls.last.request
    assert req.headers["authorization"] == "Bearer tok"
    body = req.read().decode()
    assert '"method":"host.get"' in body or '"method": "host.get"' in body

@respx.mock
async def test_call_raises_on_error(client):
    respx.post("https://zbx.test/api_jsonrpc.php").mock(
        return_value=Response(200, json={"jsonrpc": "2.0",
                                         "error": {"code": -32602, "message": "Bad",
                                                   "data": "host not found"}, "id": 1})
    )
    with pytest.raises(ZabbixError, match="host not found"):
        await client.call("host.get", {})

@respx.mock
async def test_get_problem(client):
    respx.post("https://zbx.test/api_jsonrpc.php").mock(
        return_value=Response(200, json={"jsonrpc": "2.0",
                                         "result": [{"eventid": "42", "name": "test",
                                                     "severity": "4", "hosts": [{"hostid": "7"}]}],
                                         "id": 1})
    )
    p = await client.get_problem(42)
    assert p["eventid"] == "42"
    assert p["hosts"][0]["hostid"] == "7"

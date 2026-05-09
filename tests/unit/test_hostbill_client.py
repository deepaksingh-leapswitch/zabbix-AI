import pytest
import respx
from httpx import Response

from zabbix_ai.clients.hostbill import HostBillClient, HostBillError

URL = "https://billing.test/admin/api.php"

@pytest.fixture
def client():
    return HostBillClient(api_url=URL, api_id="id-1", api_key="key-1")

@respx.mock
async def test_search_tickets_returns_list(client):
    route = respx.post(URL).mock(
        return_value=Response(200, json={
            "success": 1,
            "tickets": [
                {"id": "100", "subject": "Server down", "status": "Closed",
                 "client_id": "7", "lastreply": "2026-04-10 14:00:00"},
                {"id": "101", "subject": "SSL not working", "status": "Closed",
                 "client_id": "8", "lastreply": "2026-04-12 09:00:00"},
            ],
        }),
    )
    rows = await client.search_tickets(query="Server down", status="Closed", limit=20)
    assert route.called
    body = route.calls.last.request.read().decode()
    assert "api_id=id-1" in body
    assert "api_key=key-1" in body
    assert "call=getTickets" in body
    assert len(rows) == 2
    assert rows[0]["subject"] == "Server down"

@respx.mock
async def test_get_ticket_details(client):
    respx.post(URL).mock(
        return_value=Response(200, json={"success": 1,
                                          "ticket": {"id": "100", "replies": []}}),
    )
    t = await client.get_ticket_details(100)
    assert t["id"] == "100"

@respx.mock
async def test_error_response_raises(client):
    respx.post(URL).mock(
        return_value=Response(200, json={"success": 0,
                                          "error": "Invalid API credentials"}),
    )
    with pytest.raises(HostBillError, match="Invalid API credentials"):
        await client.search_tickets(query="x", status="Closed", limit=10)

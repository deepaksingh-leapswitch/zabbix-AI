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


# ── Write API (ticket-flow) — real HostBill API params ───────────────────────


@respx.mock
async def test_add_ticket_internal_returns_id(client):
    route = respx.post(URL).mock(
        return_value=Response(200, json={"success": 1, "ticket_id": "555"}),
    )
    tid = await client.add_ticket(
        subject="DB down on web-1", body="RCA: disk full",
        dept_id=3, priority=2, name="Zabbix RCA AI", email="noc@x.io",
    )
    assert tid == 555
    body = route.calls.last.request.read().decode()
    assert "call=addTicket" in body
    assert "dept_id=3" in body
    assert "body=" in body            # message text is sent as `body`
    assert "client_id" not in body    # internal ticket: no client


@respx.mock
async def test_add_ticket_customer_sets_client_id(client):
    route = respx.post(URL).mock(
        return_value=Response(200, json={"success": 1, "id": 777}),
    )
    tid = await client.add_ticket(subject="s", body="m", client_id=42, dept_id=3)
    assert tid == 777
    assert "client_id=42" in route.calls.last.request.read().decode()


@respx.mock
async def test_add_ticket_id_nested_in_info(client):
    respx.post(URL).mock(
        return_value=Response(200, json={"success": 1,
                                         "info": {"ticket_id": "900"}}),
    )
    assert await client.add_ticket(subject="s", body="m") == 900


@respx.mock
async def test_add_ticket_no_id_raises(client):
    respx.post(URL).mock(
        return_value=Response(200, json={"success": 1, "unexpected": "shape"}),
    )
    with pytest.raises(HostBillError, match="no ticket id"):
        await client.add_ticket(subject="s", body="m")


@respx.mock
async def test_add_ticket_reply_posts_body(client):
    route = respx.post(URL).mock(return_value=Response(200, json={"success": 1}))
    await client.add_ticket_reply(ticket_id=555, body="any update?")
    body = route.calls.last.request.read().decode()
    assert "call=addTicketReply" in body
    assert "id=555" in body
    assert "body=" in body


@respx.mock
async def test_close_ticket_uses_status_change(client):
    route = respx.post(URL).mock(return_value=Response(200, json={"success": 1}))
    await client.close_ticket(ticket_id=555, body="done")
    body = route.calls.last.request.read().decode()
    assert "call=addTicketReply" in body
    assert "status_change=Closed" in body
    assert "id=555" in body


@respx.mock
async def test_get_ticket_reply_count_counts_top_level_list(client):
    respx.post(URL).mock(
        return_value=Response(200, json={"success": 1, "replies": [
            {"id": "a"}, {"id": "b"}, {"id": "c"}]}),
    )
    assert await client.get_ticket_reply_count(1) == 3


@respx.mock
async def test_get_ticket_reply_count_zero_on_error(client):
    respx.post(URL).mock(
        return_value=Response(200, json={"success": 0, "error": "nope"}),
    )
    assert await client.get_ticket_reply_count(1) == 0

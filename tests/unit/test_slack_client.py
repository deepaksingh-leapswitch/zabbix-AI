# tests/unit/test_slack_client.py
import pytest
import respx
from httpx import Response

from zabbix_ai.clients.slack import SlackClient, SlackError


@pytest.fixture
def client():
    return SlackClient(bot_token="xoxb-test")

@respx.mock
async def test_post_message(client):
    route = respx.post("https://slack.com/api/chat.postMessage").mock(
        return_value=Response(200, json={"ok": True, "ts": "1700000000.0001",
                                          "channel": "C1"}),
    )
    res = await client.post_message(channel="C1", text="hi", thread_ts="abc")
    assert route.called
    body = route.calls.last.request.read().decode()
    assert '"channel": "C1"' in body or '"channel":"C1"' in body
    assert '"thread_ts": "abc"' in body or '"thread_ts":"abc"' in body
    assert res["ts"] == "1700000000.0001"

@respx.mock
async def test_update_message(client):
    respx.post("https://slack.com/api/chat.update").mock(
        return_value=Response(200, json={"ok": True, "ts": "1700000000.0001"}),
    )
    res = await client.update_message(channel="C1", ts="1700000000.0001",
                                       text="updated", blocks=[{"type": "section",
                                                                "text": {"type": "mrkdwn",
                                                                         "text": "x"}}])
    assert res["ok"] is True

@respx.mock
async def test_post_message_error_raises(client):
    respx.post("https://slack.com/api/chat.postMessage").mock(
        return_value=Response(200, json={"ok": False, "error": "channel_not_found"}),
    )
    with pytest.raises(SlackError, match="channel_not_found"):
        await client.post_message(channel="C404", text="hi")

@respx.mock
async def test_replies_fetches_thread_parent(client):
    respx.get("https://slack.com/api/conversations.replies").mock(
        return_value=Response(200, json={"ok": True,
                                          "messages": [
                                              {"ts": "100.0", "text": "alert text"},
                                              {"ts": "100.1", "text": "<@U> why?"},
                                          ]}),
    )
    msgs = await client.replies(channel="C1", thread_ts="100.0")
    assert len(msgs) == 2
    assert msgs[0]["text"] == "alert text"

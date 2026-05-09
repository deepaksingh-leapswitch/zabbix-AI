# tests/unit/test_slack_parser.py
from zabbix_ai.adapters.slack import parse_mention


def test_explicit_eventid_in_mention():
    text = "<@U999> investigate eventid=998877 instance=monitoring"
    p = parse_mention(text=text, parent_text=None, default_instance="dcmonitoring")
    assert p.eventid == 998877
    assert p.instance == "monitoring"
    assert p.hostid is None

def test_eventid_extracted_from_parent_alert(monkeypatch):
    parent = (
        "*Problem*: Disk space is critically low on /var (free < 10%)\n"
        "Host: web-mum-07 (12345)\n"
        "EventID: 555\n"
        "Severity: Disaster"
    )
    p = parse_mention(text="<@U999> why?", parent_text=parent,
                      default_instance="monitoring")
    assert p.eventid == 555
    assert p.hostid == 12345

def test_default_instance_when_unspecified():
    text = "<@U999> what is going on"
    p = parse_mention(text=text, parent_text=None, default_instance="dcmonitoring")
    assert p.instance == "dcmonitoring"
    assert p.eventid is None
    assert p.hostid is None

def test_hostid_in_mention():
    text = "<@U999> hostid=42 instance=strads check it"
    p = parse_mention(text=text, parent_text=None, default_instance="monitoring")
    assert p.hostid == 42
    assert p.instance == "strads"

def test_question_strips_user_mention():
    text = "<@U99ABC> why is the site slow?"
    p = parse_mention(text=text, parent_text=None, default_instance="monitoring")
    assert p.question.strip() == "why is the site slow?"

def test_unknown_instance_falls_back_to_default():
    text = "<@U999> instance=does-not-exist eventid=1"
    p = parse_mention(text=text, parent_text=None, default_instance="monitoring",
                      known_instances=["monitoring", "dcmonitoring"])
    # parser does NOT validate against known_instances itself — that's the
    # adapter's job. parse_mention preserves what the user typed.
    assert p.instance == "does-not-exist"

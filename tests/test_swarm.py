#!/usr/bin/env python3
"""Basic tests for LLMSwarm core functionality."""
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

# Import the package (implementation now lives in the llmswarm/ package).
# `client` holds the mutable per-member metrics state the tests assert on.
import llmswarm as swarm
from llmswarm import client


def test_msg_text():
    """Test msg_text helper with various content formats."""
    # String content
    assert swarm.msg_text({"content": "hello"}) == "hello"
    
    # List content (pi format)
    content = [{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}]
    assert swarm.msg_text({"content": content}) == "hello world"
    
    # Missing content
    assert swarm.msg_text({}) == ""
    
    # Mixed content
    content = [{"type": "text", "text": "a"}, "b", {"type": "other"}]
    assert swarm.msg_text({"content": content}) == "a b"
    
    print("✓ test_msg_text passed")


def test_blackboard():
    """Test Blackboard class with temp database."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        bb = swarm.Blackboard(db_path, {"retention_days": 0})
        
        # Put and recall
        bb.put("test", "member1", "query", "hello world")
        bb.put("test", "member2", "query", "foo bar baz")
        
        # Test recall with BM25
        results = bb.recall("hello", limit=3)
        assert len(results) > 0
        assert any("member1" in r for r in results)
        
        # Test tail
        tail = bb.tail(n=5)
        assert len(tail) == 2
        
        # Test prune (no retention)
        pruned = bb.prune()
        assert pruned == 0
        
        print("✓ test_blackboard passed")


def test_blackboard_prune():
    """Test Blackboard pruning with retention."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        bb = swarm.Blackboard(db_path, {"retention_days": 1})
        
        # Add old entry (manipulate timestamp)
        bb.put("old", "member", "query", "old content")
        
        # Manipulate timestamp to be 2 days old
        bb.db.execute("UPDATE entries SET ts = ? WHERE id = 1", 
                      (time.time() - 2 * 86400,))
        bb.db.commit()
        
        # Prune should remove it
        pruned = bb.prune()
        assert pruned == 1
        
        print("✓ test_blackboard_prune passed")


def test_blackboard_per_job_flush():
    """Horde flow: per-job tag write + flush, isolated from other jobs and boards."""
    with tempfile.TemporaryDirectory() as tmpdir:
        horde_db = os.path.join(tmpdir, "horde.db")
        main_db = os.path.join(tmpdir, "main.db")
        bbh = swarm.Blackboard(horde_db, {"retention_days": 0})
        bbm = swarm.Blackboard(main_db, {"retention_days": 0})

        tag_a, tag_b = "horde:111", "horde:222"
        # job A and job B interleave; main board gets a serve entry
        bbh.put("answer", "m1", tag_a, "answer A")
        bbh.put("answer", "m2", tag_b, "answer B")
        bbh.put("final", "judge", tag_a, "final A")
        bbm.put("answer", "m1", "chat", "serve entry")

        # flush only job A; job B and the main board survive
        assert bbh.delete_by_problem(tag_a) == 2
        rows = bbh.db.execute("SELECT problem FROM entries").fetchall()
        assert rows == [(tag_b,)]
        assert bbm.db.execute("SELECT count(*) FROM entries").fetchone()[0] == 1

        # second flush is idempotent
        assert bbh.delete_by_problem(tag_a) == 0

        print("\u2713 test_blackboard_per_job_flush passed")


def test_validate_config():
    """Test config validation."""
    # Valid config
    cfg = {
        "member": [
            {"name": "test1", "port": 8081, "roles": ["any"]},
            {"name": "test2", "port": 8082, "roles": ["worker"]},
        ],
        "serve": {"mode": "ensemble", "judge": "test1"},
        "blackboard": {"retention_days": 0},
    }
    issues = swarm.validate_config(cfg)
    errors = [i for i in issues if i.startswith("ERROR")]
    assert len(errors) == 0, f"Unexpected errors: {errors}"
    
    # Invalid config (duplicate name)
    cfg = {
        "member": [
            {"name": "test", "port": 8081},
            {"name": "test", "port": 8082},
        ],
        "serve": {},
    }
    issues = swarm.validate_config(cfg)
    assert any("Duplicate" in i for i in issues)
    
    # Invalid config (unknown judge)
    cfg = {
        "member": [{"name": "test", "port": 8081}],
        "serve": {"judge": "unknown"},
    }
    issues = swarm.validate_config(cfg)
    assert any("Judge" in i for i in issues)
    
    print("✓ test_validate_config passed")


def test_parse_subtasks():
    """Test subtask JSON parsing."""
    # Valid JSON
    text = '{"subtasks": [{"title": "a", "task": "do a"}, {"title": "b", "task": "do b"}]}'
    tasks = swarm.parse_subtasks(text)
    assert len(tasks) == 2
    assert tasks[0]["task"] == "do a"
    
    # Invalid JSON
    text = "not json at all"
    tasks = swarm.parse_subtasks(text)
    assert len(tasks) == 1  # Falls back to trivial subtask
    assert tasks[0]["task"] == "answer the question directly"
    
    print("✓ test_parse_subtasks passed")


def test_completion_messages():
    """Test text-completion prompt conversion to chat messages."""
    # Plain string prompt
    msgs = swarm.completion_messages({"prompt": "hello"})
    assert msgs == [{"role": "user", "content": "hello"}]

    # With system field
    msgs = swarm.completion_messages({"prompt": "hello", "system": "be terse"})
    assert msgs[0] == {"role": "system", "content": "be terse"}
    assert msgs[1] == {"role": "user", "content": "hello"}

    # List of strings
    msgs = swarm.completion_messages({"prompt": ["line1", "line2"]})
    assert msgs[-1]["content"] == "line1\nline2"

    # List with token-id arrays
    msgs = swarm.completion_messages({"prompt": [[1, 2, 3], "text"]})
    assert msgs[-1]["content"] == "1 2 3\ntext"

    # Numeric prompt
    msgs = swarm.completion_messages({"prompt": 42})
    assert msgs[-1]["content"] == "42"

    # Empty prompt
    msgs = swarm.completion_messages({})
    assert msgs[-1]["content"] == ""

    print("\u2713 test_completion_messages passed")


def test_reasoning_fields():
    """Test reasoning wire-format selection per member style."""
    class M:
        def __init__(self, reasoning="auto", reasoning_style="chat_template_kwargs"):
            self.reasoning = reasoning
            self.reasoning_style = reasoning_style

    # serve default off, member auto -> disabled via chat_template_kwargs
    swarm.set_serve_reasoning("off")
    swarm.set_request_reasoning(None)
    assert swarm.reasoning_fields(M()) == {"chat_template_kwargs": {"enable_thinking": False}}

    # member forced on overrides serve default
    assert swarm.reasoning_fields(M(reasoning="on")) == \
        {"chat_template_kwargs": {"enable_thinking": True}}

    # request override beats member and serve
    swarm.set_request_reasoning("on")
    assert swarm.reasoning_fields(M(reasoning="off")) == \
        {"chat_template_kwargs": {"enable_thinking": True}}

    # other wire styles
    swarm.set_request_reasoning(None)
    swarm.set_serve_reasoning("on")
    assert swarm.reasoning_fields(M(reasoning_style="enable_thinking")) == \
        {"enable_thinking": True}
    assert swarm.reasoning_fields(M(reasoning_style="thinking_type")) == \
        {"thinking": {"type": "enabled"}}
    swarm.set_serve_reasoning("off")
    assert swarm.reasoning_fields(M(reasoning_style="thinking_type")) == \
        {"thinking": {"type": "disabled"}}
    assert swarm.reasoning_fields(M(reasoning_style="reasoning_effort")) == \
        {"reasoning_effort": "none"}
    assert swarm.reasoning_fields(M(reasoning_style="none")) == {}

    print("\u2713 test_reasoning_fields passed")


def test_validate_config_agent_mode():
    """Agent mode must be accepted by config validation."""
    cfg = {
        "member": [{"name": "test", "port": 8081}],
        "serve": {"mode": "agent"},
    }
    issues = swarm.validate_config(cfg)
    errors = [i for i in issues if i.startswith("ERROR")]
    assert len(errors) == 0, f"Unexpected errors: {errors}"

    # Bogus mode must be rejected
    cfg = {"member": [{"name": "test", "port": 8081}], "serve": {"mode": "bogus"}}
    issues = swarm.validate_config(cfg)
    assert any("Invalid serve mode" in i for i in issues)

    print("\u2713 test_validate_config_agent_mode passed")


def test_metrics_tracking():
    """Test that member latency tracking works."""
    # Simulate calls (metrics state lives in the client module)
    client._member_latencies = {}
    swarm.log_member_call("test", 1.5, True)
    swarm.log_member_call("test", 2.5, False)

    assert "test" in client._member_latencies
    assert len(client._member_latencies["test"]) == 2
    
    print("✓ test_metrics_tracking passed")


class _FakeResp:
    def __init__(self, data):
        self._b = json.dumps(data).encode()
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def read(self):
        return self._b


def test_member_horde_fields():
    """Horde members carry horde_type/horde_model, are not launched, no base."""
    m = swarm.Member({"name": "h1", "horde_type": "text", "horde_model": "Llama-3.1-8B"}, "/bin/true")
    assert m.is_horde() is True
    assert m.is_external() is False
    assert m.horde_type == "text"
    assert m.horde_model == "Llama-3.1-8B"
    assert m.base == ""  # horde-routed: no direct base
    # invalid horde_type coerced to ""
    m2 = swarm.Member({"name": "h2", "horde_type": "bogus", "horde_model": "X"}, "/bin/true")
    assert m2.horde_type == ""
    # non-str horde_model coerced to ""
    m3 = swarm.Member({"name": "h3", "horde_model": 123}, "/bin/true")
    assert m3.horde_model == ""
    assert m3.is_horde() is False
    print("\u2713 test_member_horde_fields passed")


def test_config_horde_roundtrip():
    """norm_members / serialize_toml / validate carry horde fields."""
    rows = [{"name": "h1", "horde_type": "text", "horde_model": "Llama-3.1-8B", "enabled": True}]
    normed = swarm.norm_members(rows)
    assert normed[0]["horde_type"] == "text"
    assert normed[0]["horde_model"] == "Llama-3.1-8B"
    # serialize includes horde fields
    toml = swarm.serialize_toml({"llama": {}, "serve": {}, "member": normed}, normed)
    assert 'horde_type = "text"' in toml
    assert 'horde_model = "Llama-3.1-8B"' in toml
    # validation flags an invalid horde_type
    issues = swarm.validate_config({"member": [{"name": "x", "horde_type": "bogus"}], "serve": {}})
    assert any("horde_type" in i for i in issues)
    print("\u2713 test_config_horde_roundtrip passed")


def test_save_horde_member_roundtrip():
    """Saving a config with a horde member serializes and reloads it intact."""
    import tempfile, os
    members = [
        {"name": "ext", "url": "http://1.2.3.4:5002", "role": "worker",
         "enabled": True, "reasoning": "auto", "reasoning_style": "chat_template_kwargs",
         "system_prompt": "", "system_prompt_enabled": False,
         "horde_type": "", "horde_model": ""},
        {"name": "horde", "url": "", "role": "worker",
         "enabled": True, "reasoning": "auto", "reasoning_style": "chat_template_kwargs",
         "system_prompt": "", "system_prompt_enabled": False,
         "horde_type": "text", "horde_model": "Swarm_Test/X"},
    ]
    cfg = {"llama": {"server_bin": "/bin/true"}, "serve": {"host": "127.0.0.1",
           "port": 5100, "mode": "ensemble", "reasoning": "off"},
           "member": members}
    toml_text = swarm.serialize_toml(cfg, members)
    fd, path = tempfile.mkstemp(suffix=".toml"); os.close(fd)
    try:
        open(path, "w").write(toml_text)
        _, members2, _ = swarm.load_config(path)
        assert members2["horde"].is_horde() is True
        assert members2["horde"].horde_model == "Swarm_Test/X"
        assert members2["horde"].url == ""
        assert members2["ext"].is_external() is True
    finally:
        os.unlink(path)
    print("\u2713 test_save_horde_member_roundtrip passed")


def test_messages_to_prompt():
    """chat messages serialize into the flat horde prompt."""
    msgs = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}]
    p = client._messages_to_prompt(msgs)
    assert "SYSTEM: be brief" in p
    assert "USER: hi" in p
    # multi-part content flattened to text
    p2 = client._messages_to_prompt([{"role": "user", "content": [
        {"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}])
    assert "a b" in p2
    print("\u2713 test_messages_to_prompt passed")


def test_horde_text_generate():
    """horde submit + poll loop returns the generation text (mocked urllib)."""
    calls = {"n": 0}
    def fake_urlopen(req, timeout=30):
        url = req.full_url
        if url.endswith("/async"):
            return _FakeResp({"id": "job1", "kudos": 1.0})
        if "/status/job1" in url:
            calls["n"] += 1
            if calls["n"] < 3:  # first two polls: still queued
                return _FakeResp({"generations": []})
            return _FakeResp({"generations": [{"text": "hello from horde"}]})
        raise AssertionError("unexpected url " + url)
    orig = client.urllib.request.urlopen
    client.urllib.request.urlopen = fake_urlopen
    try:
        out = client.horde_text_generate("http://cluster", "key", "Llama-3.1-8B",
                                         "prompt here", max_length=100,
                                         timeout=5, poll_interval=0.01)
        assert out == "hello from horde"
        assert calls["n"] == 3
    finally:
        client.urllib.request.urlopen = orig
    print("\u2713 test_horde_text_generate passed")


def test_chat_routes_horde_member():
    """chat() routes horde members through _horde_call, not direct HTTP."""
    class M:
        def __init__(self):
            self.name = "h1"; self.horde_model = "Llama-3.1-8B"; self.horde_type = "text"
            self.temperature = 0.7; self.system_prompt = ""; self.system_prompt_enabled = False
            self.url = ""; self.host = ""
        def is_horde(self):
            return True
        def is_external(self):
            return False
    class F:
        def __init__(self):
            self.members = {"h1": M()}
            self.cfg = {"horde": {"cluster": "http://c", "api_key": "k"}}
    got = {}
    def fake_horde_call(fleet, name, m, prompt, max_length, temperature, min_p, timeout):
        got["prompt"] = prompt
        return "ROUTED"
    orig = client._horde_call
    client._horde_call = fake_horde_call
    try:
        out = client.chat(F(), "h1", [{"role": "user", "content": "hello"}])
        assert out == "ROUTED"
        assert got["prompt"] == "USER: hello"
    finally:
        client._horde_call = orig
    print("\u2713 test_chat_routes_horde_member passed")


if __name__ == "__main__":
    print("Running LLMSwarm tests...\n")
    
    test_msg_text()
    test_blackboard()
    test_blackboard_prune()
    test_blackboard_per_job_flush()
    test_validate_config()
    test_parse_subtasks()
    test_completion_messages()
    test_reasoning_fields()
    test_validate_config_agent_mode()
    test_metrics_tracking()
    test_member_horde_fields()
    test_config_horde_roundtrip()
    test_save_horde_member_roundtrip()
    test_messages_to_prompt()
    test_horde_text_generate()
    test_chat_routes_horde_member()
    
    print("\n✓ All tests passed!")

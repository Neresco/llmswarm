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
    
    print("\n✓ All tests passed!")

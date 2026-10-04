"""Agent mode: each member runs an independent tool-using agent loop."""
import json
import time
from concurrent.futures import ThreadPoolExecutor

from .client import chat, chat_stream, chat_with_tools, msg_text


AGENT_SYSTEM = (
    "You are an independent agent in a parallel swarm. You have access to tools. "
    "Use them to investigate the task thoroughly. When you have enough information, "
    "provide your final answer as text (not a tool call). Be specific and cite findings."
)

MAX_AGENT_ITERATIONS = 10


def execute_tool(tool_name, arguments, cwd=None):
    """Execute a tool call. Returns result string."""
    try:
        if tool_name == "read" or tool_name == "read_file":
            path = arguments.get("path", "")
            with open(path, "r") as f:
                content = f.read()
            return content[:10000]  # Cap output
        
        elif tool_name == "bash" or tool_name == "run_command" or tool_name == "execute":
            import subprocess
            cmd = arguments.get("command", "")
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                timeout=30, cwd=cwd or "/"
            )
            output = result.stdout + result.stderr
            return output[:10000]
        
        elif tool_name == "ls" or tool_name == "list_directory":
            import subprocess
            path = arguments.get("path", ".")
            result = subprocess.run(
                ["ls", "-la", path], capture_output=True, text=True, timeout=10
            )
            return result.stdout[:5000]
        
        elif tool_name == "write" or tool_name == "write_file":
            path = arguments.get("path", "")
            content = arguments.get("content", "")
            with open(path, "w") as f:
                f.write(content)
            return f"Written {len(content)} bytes to {path}"
        
        elif tool_name == "grep" or tool_name == "search":
            import subprocess
            pattern = arguments.get("pattern", arguments.get("query", ""))
            path = arguments.get("path", ".")
            result = subprocess.run(
                ["grep", "-r", "-n", pattern, path],
                capture_output=True, text=True, timeout=10
            )
            return result.stdout[:10000] or "(no matches)"
        
        else:
            return f"Unknown tool: {tool_name}. Available: read, bash, ls, write, grep"
            
    except Exception as e:
        return f"Tool error: {e}"


def run_agent_member(fleet, name, messages, tools, timeout=300):
    """Run a single member as an agent loop. Returns (final_text, tool_history)."""
    tool_history = []
    current_messages = list(messages)
    
    for iteration in range(MAX_AGENT_ITERATIONS):
        try:
            if timeout:
                final, tool_calls = chat_with_tools(fleet, name, current_messages,
                                                    timeout=timeout, params={"tools": tools})
            else:
                final, tool_calls = chat_with_tools(fleet, name, current_messages,
                                                    params={"tools": tools})
        except Exception as e:
            return f"[ERROR] {name} failed: {e}", tool_history
        
        if not tool_calls:
            # Member is done - returned text
            return final, tool_history
        
        # Member wants to use tools - execute them
        # Add assistant message with tool_calls
        assistant_msg = {"role": "assistant", "content": final or ""}
        assistant_msg["tool_calls"] = tool_calls
        current_messages.append(assistant_msg)
        
        # Execute each tool call
        for tc in tool_calls:
            tool_name = tc.get("function", {}).get("name", "")
            try:
                arguments = json.loads(tc.get("function", {}).get("arguments", "{}"))
            except json.JSONDecodeError:
                arguments = {}
            
            tool_call_id = tc.get("id", "")
            tool_history.append({"tool": tool_name, "args": arguments, "iteration": iteration})
            
            # Execute tool
            result = execute_tool(tool_name, arguments)
            
            # Add tool result message
            current_messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": result,
            })
    
    # Max iterations reached
    return "[MAX ITERATIONS REACHED]", tool_history


def run_agent_swarm(fleet, bb, problem, member_names, judge, messages=None, tools=None,
                    member_timeout=None, history=None, stream_cb=None):
    """Agent mode: each member works independently with tools, judge merges results."""
    
    if messages is None:
        # Build messages from problem + history
        msgs = [{"role": "system", "content": AGENT_SYSTEM}]
        if history:
            history_text = "\n".join(f"{m.get('role','?')}: {msg_text(m)}" for m in history[-5:])
            msgs.append({"role": "system", "content": f"Previous context:\n{history_text}"})
        msgs.append({"role": "user", "content": problem})
        messages = msgs
    
    print(f"== agent swarm: {len(member_names)} members working independently ==")
    
    results = {}
    tool_histories = {}
    
    def run_one(name):
        text, history = run_agent_member(fleet, name, messages, tools,
                                         timeout=member_timeout or 300)
        return name, text, history
    
    with ThreadPoolExecutor(max_workers=len(member_names)) as ex:
        futures = {ex.submit(run_one, n): n for n in member_names}
        for f in futures:
            name, text, history = f.result()
            results[name] = text
            tool_histories[name] = history
            bb.put("agent_result", name, problem, text)
            print(f"-- [{name}] done ({len(history)} tool calls): {text[:150]}...")
    
    # Judge merges all results
    if judge:
        print(f"== judge ({judge}) merges {len(results)} agent results ==")
        combined_parts = []
        for name in results:
            text = results[name]
            hist = tool_histories.get(name, [])
            tools_used = ", ".join(set(h['tool'] for h in hist)) or "none"
            combined_parts.append(f"[{name}] (tools: {tools_used})\n{text[:3000]}")
        combined = "\n\n---\n\n".join(combined_parts) if results else "(no results)"
        
        agent_judge_msgs = [
            {"role": "system", "content": (
                "You are the judge of a parallel agent swarm. Each agent worked "
                "independently with tools. Merge their findings into one coherent "
                "answer. Discard errors and duplicates. Keep the best information.")},
            {"role": "user", "content": f"Problem: {problem}\n\nAgent results:\n{combined}"},
        ]
        if stream_cb:
            final = chat_stream(fleet, judge, agent_judge_msgs, stream_cb, temperature=0.2)
        else:
            final = chat(fleet, judge, agent_judge_msgs, temperature=0.2)
        bb.put("final", judge, problem, final)
        
        member_status = {
            "participated": list(results.keys()),
            "failed": [],
        }
        member_details = results
        return final, member_status, member_details
    
    # No judge - return combined
    final = "\n\n---\n\n".join(f"[{k}]\n{v}" for k, v in results.items())
    member_status = {"participated": list(results.keys()), "failed": []}
    return final, member_status, results



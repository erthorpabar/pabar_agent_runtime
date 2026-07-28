# system
import os
import json

# tool 
import subprocess
from pathlib import Path
from typing import Any

# llm client
from openai import OpenAI
from dotenv import load_dotenv
load_dotenv(override=True)

# ===== 全局常量 =====
api_url = os.getenv("OPENAI_BASE_URL")
api_key = os.getenv("OPENAI_API_KEY")
model = os.getenv("OPENAI_MODEL")
client = OpenAI(base_url=api_url, api_key=api_key)
WORKDIR = Path.cwd() # 当前文件夹地址

# ===== 辅助函数
def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

# ===== [func] for run
def run_bash(command: str, **kw) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        output = (r.stdout + r.stderr).strip()[:50000]
        if not output:
            return f"(no output, exit code: {r.returncode})"
        return output
    except subprocess.TimeoutExpired:
        return "Error: Timeout"

def run_read(path: str, limit: int = None, **kw) -> str:
    try:
        text = safe_path(path).read_text(encoding="utf-8")
        lines = text.splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str, **kw) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str, **kw) -> str:
    try:
        fp = safe_path(path)
        content = fp.read_text(encoding="utf-8")
        if old_text not in content:
            return f"Error: Text not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"

# ===== [describe] for llm -> 在llm请求中告诉模型有哪些工具
# OpenAI 格式: type=function, function={name, description, parameters}
TOOLS = [
    {"type": "function", "function": {
        "name": "bash", "description": "Run a shell command.",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "read_file", "description": "Read file contents.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file", "description": "Write content to file.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "edit_file", "description": "Replace exact text in file.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}}},
]

# ===== [describe] map_to [func] => {name:func} -> 在执行时候的映射
TOOL_HANDLERS: dict[str, Any] = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
}





''' 
agent_loop
query(str) - (messages+=query) - tool_loop(messages)=out - (messages+=out)
    ↑___________________________________________________________↓

tool_loop
       llm(state) = res
        ↑            ↓
        |           res.tool_call?
        |           no → (state.messages+=text) → out
        |           yes → 
        |           → run_tool(name,args)=result
        |           → state+=result
        |____________↓

while True:     
    1 receive
    2 agent loop
    3 broadcast
'''


def receive(state:dict):
    user_input = input("You: ")
    state["messages"].append({"role": "user", "content": user_input})
    return state

def broadcast(state:dict):
    ai_text = state["messages"][-1]["content"]
    print(f"Ai: {ai_text}")
    return state

def run_tool(state:dict, tool_calls):
    results = []
    for tc in tool_calls:
        name = tc.function.name
        args = json.loads(tc.function.arguments)
        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            output = f"Error: Unknown tool '{name}'"
        else:
            try:
                output = handler(state=state, **args)
            except Exception as e:
                output = f"Error: {e}"
        # 收集结果 (OpenAI格式: role=tool, tool_call_id)
        results.append({
            "role": "tool",
            "tool_call_id": tc.id,
            "content": str(output)
        })
        # 打印
        print(f'[Tool]')
        print(f'{name}: {args}')
        print(f'[Result]')
        print(f'{str(output)[:200]}')
    return results


def tool_loop(state:dict):
    while True:
        # 动态合成 system_prompt (OpenAI格式: system作为第一条message)
        sys_msg = {"role": "system", "content": state["system_prompt"]}
        all_messages = [sys_msg] + state["messages"]

        # llm(state)
        res = client.chat.completions.create(model=model, messages=all_messages, tools=TOOLS, max_tokens=8000)

        # no
        if res.choices[0].finish_reason == "stop":
            answer = res.choices[0].message.content
            state["messages"].append({"role": "assistant", "content": answer})
            return state

        # yes
        elif res.choices[0].finish_reason == "tool_calls":
            # 1 ai 告诉 user 使用的 工具 和 参数
            ai_answer = res.choices[0].message
            state["messages"].append(ai_answer)
            # 2 user 执行工具 并返回结果 (用代码直接执行工具 无需人工)
            results = run_tool(state, ai_answer.tool_calls)
            state["messages"].extend(results)
        
        # other
        elif res.choices[0].finish_reason == "length":
            print("超出token限制")
            break
        
def chat_loop(state:dict):
    while True:
        state = receive(state)
        state = tool_loop(state)
        state = broadcast(state)

        
# ===== 启动
system_prompt = f"You are a coding agent at {WORKDIR}. Use bash to solve tasks."
if __name__ == "__main__":
    state = {
        "system_prompt": system_prompt,
        "messages": [],
    }
    chat_loop(state)

''' 
数据格式

# 1 query
query = '打开浏览器'

messages = [
{'content': '打开浏览器', 'role': 'user'}
]

# 2 tool_loop
# 第一次判断是否调用工具 -> 调用
res = ChatCompletion(
    id='chatcmpl-xxx',
    choices=[
        Choice(
            finish_reason='tool_calls',
            index=0,
            message=ChatCompletionMessage(
                content='我来帮你打开浏览器。',
                role='assistant',
                tool_calls=[
                    ChatCompletionMessageToolCall(
                        id='call_xxx',
                        function=Function(
                            name='bash',
                            arguments='{"command": "start \\"\\" \\"https://www.google.com\\""}'
                        ),
                        type='function'
                    )
                ]
            )
        )
    ],
    model='gpt-4',
)

run_tool工具执行结果 (每个结果是独立的message)
results = [
    {
        'role': 'tool',
        'tool_call_id': 'call_xxx',
        'content': '(no output, exit code: 0)'
    }
]

messages = [
{'content': '打开浏览器', 'role': 'user'},
{'content': '我来帮你打开浏览器。', 'role': 'assistant', 'tool_calls': [{'id': 'call_xxx', 'function': {'name': 'bash', 'arguments': '{"command": "start \\"\\" \\"https://www.google.com\\""}'}, 'type': 'function'}]},
{'role': 'tool', 'tool_call_id': 'call_xxx', 'content': '(no output, exit code: 0)'},
]

第二次判断是否调用工具 -> 不调用
res = ChatCompletion(
    id='chatcmpl-yyy',
    choices=[
        Choice(
            finish_reason='stop',
            index=0,
            message=ChatCompletionMessage(
                content='我已经为你打开了浏览器,默认访问了Google首页。',
                role='assistant',
                tool_calls=None
            )
        )
    ],
    model='gpt-4',
)

messages = [
{'content': '打开浏览器', 'role': 'user'},
{'content': '我来帮你打开浏览器。', 'role': 'assistant', 'tool_calls': [{'id': 'call_xxx', 'function': {'name': 'bash', 'arguments': '{"command": "start \\"\\" \\"https://www.google.com\\""}'}, 'type': 'function'}]},
{'role': 'tool', 'tool_call_id': 'call_xxx', 'content': '(no output, exit code: 0)'},
{'content': '我已经为你打开了浏览器,默认访问了Google首页。', 'role': 'assistant'},
]
'''

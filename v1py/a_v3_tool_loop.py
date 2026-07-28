# system
import os

# tool 
import subprocess
from pathlib import Path
from typing import Any

# llm client
from anthropic import Anthropic
from dotenv import load_dotenv
load_dotenv(override=True)

# 如果自定义了api_url 则移除默认的 认证token 允许使用第三方兼容anthropic的服务
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# ===== 全局常量 =====
api_url = os.getenv("ANTHROPIC_BASE_URL")
api_key = os.getenv("ANTHROPIC_API_KEY")
model = os.getenv("ANTHROPIC_MODEL")
client = Anthropic(base_url=api_url, api_key=api_key)
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
TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
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
       llm(messages) = res
        ↑            ↓
        |           res.tool_call?
        |           no → (messages+=text) → out
        |           yes → 
        |               → run_tool(name,args)=result
        |               → messages+=result
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

def run_tool(state:dict,ai_answer):
    results = []
    for block in ai_answer:
        if block.type == "tool_use": 
            handler = TOOL_HANDLERS.get(block.name)
            if handler is None:
                return f"Error: Unknown tool '{block.name}'"
            try:
                output = handler(state=state, **block.input)
            except Exception as e:
                output = f"Error: {e}"
            # 收集结果
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": str(output)
            })
            # 打印
            print(f'[Tool]')
            print(f'{block.name}: {block.input}')
            print(f'[Result]')
            print(f'{str(output)[:200]}')
    return results


def tool_loop(state:dict):
    while True:
        # 动态合成 system_prompt
        sys = state["system_prompt"]

        # llm(state)
        res = client.messages.create(model=model,messages=state["messages"],system=sys,tools=TOOLS,max_tokens=8000,)

        # no
        if res.stop_reason == "end_turn":
            answer = res.content[0].text # 注意:这里只取 纯文本
            state["messages"].append({"role": "assistant", "content": answer})
            return state

        # yes
        elif res.stop_reason == "tool_use":
            # 1 ai 告诉 user 使用的 工具 和 参数
            ai_answer = res.content # 注意:这个content列表包含 TextBlock 和 ToolUseBlock 工具调用必须记录这两个
            state["messages"].append({"role": "assistant", "content": ai_answer})
            # 2 user 执行工具 并返回结果 (用代码直接执行工具 无需人工)
            result = run_tool(state,ai_answer)
            state["messages"].append({"role": "user", "content": result})
        
        # other
        elif res.stop_reason == "max_tokens":
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
res = Message(
    id='msg_202603011701476b858d6088254719', 
    container=None, 
    content=[
        TextBlock(
            citations=None, 
            text='我来帮你打开浏览器。', 
            type='text'
        ), 
        ToolUseBlock(
            id='call_361bfdae6f254ba2bc3db030', 
            caller=None, 
            input={'command': 'start "" "https://www.google.com"'}, 
            name='bash', 
            type='tool_use'
            )
        ], 
    model='glm-5', 
    role='assistant', 
    stop_reason='tool_use', 
    stop_sequence=None, 
    type='message', 
    usage=Usage(
        cache_creation=None, 
        cache_creation_input_tokens=None, 
        cache_read_input_tokens=128, 
        inference_geo=None, 
        input_tokens=51, 
        output_tokens=23, 
        server_tool_use=ServerToolUsage(
            web_fetch_requests=None, 
            web_search_requests=0
        ), 
    service_tier='standard'
    )
)

run_tool工具执行结果
result =  [
    {
        'content': '(no output, exit code: 0)',        
        'tool_use_id': 'call_326696faba8640d49acd5908',
        'type': 'tool_result'
    }
]

messages = [
{'content': '打开浏览器', 'role': 'user'},
{'content': [TextBlock(citations=None, text='我来帮你打开浏览器。', type='text'),ToolUseBlock(id='call_326696faba8640d49acd5908', caller=None, input={'command': 'start "" "https://www.google.com"'}, name='bash', type='tool_use')],'role': 'assistant'},
{'content': [{'content': '(no output, exit code: 0)','tool_use_id': 'call_326696faba8640d49acd5908','type': 'tool_result'}],'role': 'user'}
]

第二次判断是否调用工具 -> 不调用
res = Message(
    id='msg_20260301171726f8a11008ece245ab', 
    container=None, 
    content=[
        TextBlock(
            citations=None, 
            text='我已经为你打开了浏览器,默认访问了Google首页。浏览器应该已经在你的Windows系统上启动了。\n\n如果你想要访问特定的网站，可以告诉我网址，我可以帮你打开指定的页面。', 
            type='text'
        )
    ], 
    model='glm-5', 
    role='assistant', 
    stop_reason='end_turn', 
    stop_sequence=None, 
    type='message', 
    usage=Usage(
        cache_creation=None, 
        cache_creation_input_tokens=None, 
        cache_read_input_tokens=192, 
        inference_geo=None, 
        input_tokens=23, 
        output_tokens=38, 
        server_tool_use=ServerToolUsage(
            web_fetch_requests=None, 
            web_search_requests=0
        ), 
    service_tier='standard'
    )
)

messages = [
{'content': '打开浏览器', 'role': 'user'},
{'content': [TextBlock(citations=None, text='我来帮你打开浏览器。', type='text'),ToolUseBlock(id='call_326696faba8640d49acd5908', caller=None, input={'command': 'start "" "https://www.google.com"'}, name='bash', type='tool_use')],'role': 'assistant'},
{'content': [{'content': '(no output, exit code: 0)','tool_use_id': 'call_326696faba8640d49acd5908','type': 'tool_result'}],'role': 'user'},
{'content': [TextBlock(citations=None, text='我已经为你打开了浏览器,默认访问了Google首页。浏览器应该已经在你的Windows系统上启动了。\n\n如果你想要访问特定的网站，可以告诉我网址，我可以帮你打开指定的页面。', type='text')],'role': 'assistant'}
]
'''
# system
import os
import time

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

client = Anthropic(base_url=api_url, api_key=api_key,)
WORKDIR = Path.cwd() # 当前文件夹地址

# ===== 辅助函数
def safe_path(raw: str) -> Path:
    """
    将用户/模型传入的路径解析为安全的绝对路径.
    防止路径穿越: 最终路径必须在 WORKDIR 之下.
    """
    target = (WORKDIR / raw).resolve()
    if not str(target).startswith(str(WORKDIR)):
        raise ValueError(f"Path traversal blocked: {raw} resolves outside WORKDIR")
    return target

def truncate(text: str, limit: int = 50000) -> str:
    """截断过长的输出, 并附上提示."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text)} total chars]"

# ===== [func] for run
def tool_bash(command: str, timeout: int = 30, **kw) -> str:
    """执行 shell 命令并返回输出."""
    # 基础安全检查: 拒绝明显危险的命令
    dangerous = ["rm -rf /", "mkfs", "> /dev/sd", "dd if="]
    for pattern in dangerous:
        if pattern in command:
            return f"Error: Refused to run dangerous command containing '{pattern}'"

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(WORKDIR),
            encoding="utf-8",
            errors="replace",
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += ("\n--- stderr ---\n" + result.stderr) if output else result.stderr
        if result.returncode != 0:
            output += f"\n[exit code: {result.returncode}]"
        return truncate(output) if output else "[no output]"
    except subprocess.TimeoutExpired:
        return f"Error: Command timed out after {timeout}s"
    except Exception as exc:
        return f"Error: {exc}"


def tool_read_file(file_path: str, **kw) -> str:
    """读取文件内容."""
    try:
        target = safe_path(file_path)
        if not target.exists():
            return f"Error: File not found: {file_path}"
        if not target.is_file():
            return f"Error: Not a file: {file_path}"
        content = target.read_text(encoding="utf-8")
        return truncate(content)
    except ValueError as exc:
        return str(exc)
    except Exception as exc:
        return f"Error: {exc}"


def tool_write_file(file_path: str, content: str, **kw) -> str:
    """写入内容到文件. 父目录不存在时自动创建."""
    try:
        target = safe_path(file_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Successfully wrote {len(content)} chars to {file_path}"
    except ValueError as exc:
        return str(exc)
    except Exception as exc:
        return f"Error: {exc}"


def tool_edit_file(file_path: str, old_string: str, new_string: str, **kw) -> str:
    """
    精确替换文件中的文本.
    old_string 必须在文件中恰好出现一次, 否则报错.
    这和 OpenClaw 的 edit 工具逻辑一致.
    """
    try:
        target = safe_path(file_path)
        if not target.exists():
            return f"Error: File not found: {file_path}"

        content = target.read_text(encoding="utf-8")
        count = content.count(old_string)

        if count == 0:
            return "Error: old_string not found in file. Make sure it matches exactly."
        if count > 1:
            return (
                f"Error: old_string found {count} times. "
                "It must be unique. Provide more surrounding context."
            )

        new_content = content.replace(old_string, new_string, 1)
        target.write_text(new_content, encoding="utf-8")
        return f"Successfully edited {file_path}"
    except ValueError as exc:
        return str(exc)
    except Exception as exc:
        return f"Error: {exc}"

def tool_list_directory(directory: str = ".", **kw) -> str:
    try:
        target = safe_path(directory)
        if not target.exists():
            return f"Error: Directory not found: {directory}"
        if not target.is_dir():
            return f"Error: Not a directory: {directory}"
        entries = sorted(target.iterdir())
        lines = []
        for entry in entries:
            prefix = "[dir]  " if entry.is_dir() else "[file] "
            lines.append(prefix + entry.name)
        return "\n".join(lines) if lines else "[empty directory]"
    except ValueError as exc:
        return str(exc)
    except Exception as exc:
        return f"Error: {exc}"

def run_todo(state: dict, items: list, **kw) -> str:
    # '''
    # 与其他工具的区别: 这个工具会修改 state["plan"]
    # 其他工具只产出字符串结果 不碰 state
    # '''
    ''' 
    items = [
        {"id": "1", "text": "读取项目结构",   "status": "completed"},
        {"id": "2", "text": "修改配置文件",   "status": "in_progress"},
        {"id": "3", "text": "运行测试",       "status": "pending"},
    ]
    '''
    # state.plan 是要修改的最终对象
    # items 是llm生成的 更新后的 plan 列表
    v = [] # 格式验证过后的 plan
    in_progress_count = 0 # 正在进行中的任务 计数器
    # ===== 格式验证
    # 1 检查任务列表 总数不超过20
    if len(items) > 20:
        raise ValueError("Max 20 todos allowed")
    # 2 逐条检查
    for i, item in enumerate(items):
        text = str(item.get("text", "")).strip() # 去除空格
        status = str(item.get("status", "pending")).lower() # 转换为小写
        item_id = str(item.get("id", str(i + 1))) # 生成id
        
        # 异常检测
        if not text:
            raise ValueError(f"Item {item_id}: text required")
        if status not in ("pending", "in_progress", "completed"):
            raise ValueError(f"Item {item_id}: invalid status '{status}'")

        # 如果正在进行中 则计数加1
        if status == "in_progress":
            in_progress_count += 1
        # 加入到 正式的检查过后的 todo列表
        v.append({"id": item_id, "text": text, "status": status})
    # 异常检测
    if in_progress_count > 1:
        raise ValueError("Only one task can be in_progress at a time")
    # 3 更新plan + 清零计数器
    state["plan"] = v
    state["no_todo_count"] = 0
    
    # 4 将更新结果 返回 需要是str格式
    lines = []
    for item in v:
        # 显示符号
        marker = {"pending": "[x]", "in_progress": "[•]", "completed": "[√]"}[item["status"]]
        ''' 
        lines 数据格式
        [√] #1: 读取项目结构
        [•] #2: 修改配置文件
        [x] #3: 运行测试
        
        '''
        lines.append(f"{marker} #{item['id']}: {item['text']}")
    done = sum(1 for t in v if t["status"] == "completed") # 统计完成数据量
    lines.append(f"\n({done}/{len(v)} completed)") # 形如 (1/3 completed)
    return "\n".join(lines)

# ===== [describe] for llm -> 在llm请求中告诉模型有哪些工具

TOOLS = [
    {
        "name": "bash",
        "description": (
            "Run a shell command and return its output. "
            "Use for system commands, git, package managers, etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds. Default 30.",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read the contents of a file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the file (relative to working directory).",
                },
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Write content to a file. Creates parent directories if needed. "
            "Overwrites existing content."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the file (relative to working directory).",
                },
                "content": {
                    "type": "string",
                    "description": "The content to write.",
                },
            },
            "required": ["file_path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Replace an exact string in a file with a new string. "
            "The old_string must appear exactly once in the file. "
            "Always read the file first to get the exact text to replace."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the file (relative to working directory).",
                },
                "old_string": {
                    "type": "string",
                    "description": "The exact text to find and replace. Must be unique.",
                },
                "new_string": {
                    "type": "string",
                    "description": "The replacement text.",
                },
            },
            "required": ["file_path", "old_string", "new_string"],
        },
    },
    {
        "name": "list_directory",
        "description": "List files and subdirectories in a directory under workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "Path relative to workspace directory. Default is root.",
                },
            },
            "required": [],
        },
    },
    {   "name": "todo", 
        "description": "Update task list. Track progress on multi-step tasks.",
        "input_schema": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}, "text": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}, "required": ["id", "text", "status"]}}}, "required": ["items"]}
    },

]

# ===== [describe] map_to [func] => {name:func} -> 在执行时候的映射
TOOL_HANDLERS: dict[str, Any] = {
    "bash": tool_bash,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "edit_file": tool_edit_file,
    "list_directory": tool_list_directory,
    "todo": run_todo,
}

''' 
状态演进
    state = {
        messages
    }

agent_loop = chat_loop(tool_loop)

chat_loop 
    query(state) - tool_loop(state)
         ↑_____________↓

tool_loop
       llm(state) = res
        ↑            ↓
        |           res.tool_call?
        |           no → (state.messages+=text) → out
        |           yes → 
        |           → run_tool(name,args)=result
        |           → state+=result
        |____________↓
'''

def query(state:dict):
    user_input = input("You: ")
    state["messages"].append({"role": "user", "content": user_input})
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

system_prompt = f"You are a coding agent at {WORKDIR}. Use bash to solve tasks."
# system_prompt = (
#     "You are a helpful AI assistant with access to tools.\n"
#     "Use the tools to help the user with file operations and shell commands.\n"
#     "Always read a file before editing it.\n"
#     "When using edit_file, the old_string must match EXACTLY (including whitespace)."
# )
def tool_loop(state:dict):
    while True:
        # 动态合成 system_prompt
        sys = system_prompt
        if state["is_plan_mode"] == True:
            state["no_todo_count"] += 1 # 计数器 +1
            # 计数器 操作 只存在于两个地方
            # 1 当前 是 +1 并判断次数 如果超过3次没有使用todo会发出提醒
            # 2 是 调用todo 后 清零计数器
            if state["no_todo_count"] >= 3: # 提醒机制 超过三轮 追加提醒提示词
                print('>>>触发plan提醒<<<')
                sys += "\n<reminder>Update your todos now.</reminder>"

        # llm(state)
        res = client.messages.create(model=model,messages=state["messages"],system=sys,tools=TOOLS,max_tokens=8000,)

        # 统计 token 使用情况
        state["in_tokens"] += res.usage.input_tokens
        state["out_tokens"] += res.usage.output_tokens

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
            # 3 统计次数
            state["tool_use_count"] += 1

        # other
        elif res.stop_reason == "max_tokens":
            print("超出token限制")
            break
        
def chat_loop(state:dict):
    while True:
        # 统计轮次
        state["turn"] += 1
        state["start_time"] = time.time()

        # 核心循环
        state = query(state)
        print('[thinking...]')
        state = tool_loop(state)
        
        # 打印每轮ai回复
        ai_text = state["messages"][-1]["content"]
        print(f"Ai: {ai_text}")

        # 打印统计结果
        state["end_time"] = time.time()
        time_use = state["end_time"] - state["start_time"]
        state["total_time"] += time_use
        print(f'[状态] 总轮次:{state["turn"]}  工具调用总数:{state["tool_use_count"]}  输入总token:{state["in_tokens"]}  输出总token:{state["out_tokens"]}  总耗时:{state["total_time"]:.1f}秒')
        print('====================================')
        

# ===== 启动
system_prompt = f'''You are a coding agent at {WORKDIR}. Use bash to solve tasks.
当前系统是win系统
'''
# system_prompt = (
#     "You are a helpful AI assistant with access to tools.\n"
#     "Use the tools to help the user with file operations and shell commands.\n"
#     "Always read a file before editing it.\n"
#     "When using edit_file, the old_string must match EXACTLY (including whitespace)."
# )
if __name__ == "__main__":
    state = {
        # Counter
        "turn": 0,
        "tool_use_count": 0,
        "in_tokens": 0,
        "out_tokens": 0,
        "total_time":0,
        "start_time":None,
        "end_time":None,

        # chat
        "system_prompt": system_prompt,
        "messages": [],

        # plan
        "is_plan_mode": True,
        "no_todo_count": 0,
        "plan": [],
    }
    chat_loop(state)

'''
plan 本质就是一个 半强制唤醒的 tool_call

'''
# 一句话测试 
# 用 todo 写两条：1. 测试 2. 完成，然后结束。
# 用 todo 随机查看一个文档 并总结 输入一个总结文档 命名为x.md
# 写一个fastapi教学手册 单独创建一个fastapi文件夹 以多个md文档的形式输出 先出大纲再完善部分
# 直接拖入文档进行对话

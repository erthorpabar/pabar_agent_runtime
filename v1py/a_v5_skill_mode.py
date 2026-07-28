# system
import os
import time

# tool 
import subprocess
from pathlib import Path
from typing import Any
import re
import yaml

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

# 找到 skill 目录
def find_skills_dir(start: Path = WORKDIR) -> Path:
    """从脚本所在目录和 start 目录开始向上逐级查找 skills 文件夹。"""
    script_dir = Path(__file__).resolve().parent
    for base in [script_dir, start.resolve()]:
        cur = base
        for _ in range(10):
            candidate = cur / "skills"
            if candidate.is_dir():
                return candidate
            if cur.parent == cur:
                break
            cur = cur.parent
    return start / "skills"
SKILLS_DIR = find_skills_dir() # skill 目录


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

def load_skill(state: dict, name: str) -> str:
    """读取 skills/{name}/SKILL.md 全文存入 state"""
    path = SKILLS_DIR / name / "SKILL.md"
    if not path.exists():
        available = [f.parent.name for f in sorted(SKILLS_DIR.glob("*/SKILL.md"))] if SKILLS_DIR.exists() else []
        return f"Error: Unknown skill '{name}'. Available: {', '.join(available)}"
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n.*?\n---\n(.*)", text, re.DOTALL)
    body = match.group(1).strip() if match else text
    state["on_active_skill"] = f"<skill name=\"{name}\">\n{body}\n</skill>"
    return f"Skill '{name}' loaded into system prompt."


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
    {   "name": "load_skill", 
        "description": "Load specialized knowledge by name. This will replace the current system prompt with the skill's content.",
        "input_schema": {"type": "object", "properties": {"name": {"type": "string", "description": "Skill name to load"}}, "required": ["name"]}},

]

# ===== [describe] map_to [func] => {name:func} -> 在执行时候的映射
TOOL_HANDLERS: dict[str, Any] = {
    "bash": tool_bash,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "edit_file": tool_edit_file,
    "list_directory": tool_list_directory,
    "load_skill": load_skill,
}



# ===== loop =====
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
        if state["on_active_skill"] is not None:
            sys += state["on_active_skill"]

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
def get_skill_menu(skills_dir: Path) -> str:
    """扫描 skills_dir/*/SKILL.md，返回菜单字符串用于 system_prompt"""
    if not skills_dir.exists():
        return "(no skills available)"
    lines = []
    for f in sorted(skills_dir.glob("*/SKILL.md")):
        text = f.read_text(encoding="utf-8")
        match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
        meta = yaml.safe_load(match.group(1)) or {} if match else {}
        name = f.parent.name
        desc = meta.get("description", "No description").strip().split("\n")[0]
        tags = meta.get("tags", "")
        line = f"  - {name}: {desc}"
        if tags:
            line += f" [{tags}]"
        lines.append(line)
    return "\n".join(lines) if lines else "(no skills available)"

# system_prompt = f"""You are a coding agent at {WORKDIR}.
# Use load_skill to access specialized knowledge before tackling unfamiliar topics.

# Skills available:
# {get_skill_menu(SKILLS_DIR)}"""

# system_prompt = f"You are a coding agent at {WORKDIR}. Use bash to solve tasks."
# system_prompt = (
#     "You are a helpful AI assistant with access to tools.\n"
#     "Use the tools to help the user with file operations and shell commands.\n"
#     "Always read a file before editing it.\n"
#     "When using edit_file, the old_string must match EXACTLY (including whitespace)."
# )

system_prompt = f''' 
0 优先使用工具 完成文件操作 和 shell 命令
1 编辑文件前需要先读取文件内容
2 使用 edit_file 时,old_string 必须与原内容完全一致 包括空格
3 应对不熟悉的领域时 使用 load_skill 加载专业知识
可用技能：
{get_skill_menu(SKILLS_DIR)}
4
'''

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

        # skill
        "on_active_skill": None,
    }
    chat_loop(state)

''' 
skill
1 加载 [skill菜单] -> 放入 system_prompt
2 用户对话 -> tool_call -> 加载哪个skill
3 加载 [skill全文] -> 放入 system_prompt
4 正常 toolc_call 流程
'''
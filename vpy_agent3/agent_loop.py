# sys
import json
import os
import sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
# asyncio
import asyncio
# llm
from typing import List, Dict, Any, Callable, Awaitable
from anthropic import AsyncAnthropic
from httpx import Timeout
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None) # 删除允许第三方
api_url = os.getenv("ANTHROPIC_BASE_URL")
api_key = os.getenv("ANTHROPIC_API_KEY")
model = os.getenv("ANTHROPIC_MODEL")
print(model)
API_TIMEOUT = Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)
client = AsyncAnthropic(base_url=api_url, api_key=api_key, timeout=API_TIMEOUT, max_retries=2)

# tool 
import subprocess
from pathlib import Path
from typing import Any
import re
import time
import tempfile
import signal
# sys
import platform
import locale

SYS_ENCODING = locale.getpreferredencoding(False) # Windows中文系统通常是 cp936/gbk
IS_WINDOWS = platform.system() == "Windows" # 判断是否是Windows系统
WORKDIR = Path.cwd()
# ===== 共用工具函数 (纯函数, 被多个 tool 复用) =====
def _kill_proc_tree(proc):
    """杀死进程及其整个子进程树，避免僵尸进程残留."""
    if IS_WINDOWS:
        try:
            subprocess.run(f'taskkill /F /T /PID {proc.pid}',
                           shell=True, capture_output=True)
        except Exception:
            proc.kill()
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            proc.kill()

DANGEROUS_PATTERNS = [
    re.compile(r"rm\s+-rf\s+/"),
    re.compile(r"mkfs"),
    re.compile(r">\s*/dev/sd"),
    re.compile(r"dd\s+if="),
    re.compile(r"format\s+[a-zA-Z]:", re.IGNORECASE),
    re.compile(r"del\s+/[sq]\s+[a-zA-Z]:\\", re.IGNORECASE),
]

def safe_path(raw: str) -> Path:
    """将路径解析为 WORKDIR 下的安全绝对路径, 阻止路径穿越."""
    target = (WORKDIR / raw).resolve()
    if not str(target).startswith(str(WORKDIR)):
        raise ValueError(f"Path traversal blocked: {raw} resolves outside WORKDIR")
    return target

def truncate(text: str, limit: int = 50000) -> str:
    """截断过长的输出, 并附上提示."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text)} total chars]"

def decode_bytes(data: bytes) -> str:
    """优先用 utf-8 解码(现代工具几乎都输出 utf-8), 回退到系统编码, 最后 replace."""
    for enc in ("utf-8", SYS_ENCODING):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")

def check_dangerous(command: str) -> str | None:
    """检查危险命令, 返回匹配的模式描述或 None."""
    for pattern in DANGEROUS_PATTERNS:
        if pattern.search(command):
            return pattern.pattern
    return None


# ===== [func] for run ===== 

def _launch_visible_proc(command: str, out_file: str, cwd: str, env: dict = None):
    """在可见窗口中启动进程，同时将输出 tee 到文件.
    返回 (proc, script_file or None)."""
    tmpdir = tempfile.gettempdir()
    script_file = None

    if IS_WINDOWS:
        script_file = os.path.join(tmpdir, f'_agent_cmd_{os.getpid()}_{id(out_file)}.bat')
        with open(script_file, 'w', encoding='utf-8') as f:
            f.write(f'@echo off\nchcp 65001 >nul\n{command}\n')
        escaped_bat = script_file.replace("'", "''")
        escaped_out = out_file.replace("'", "''")
        env_prefix = "$env:PYTHONUNBUFFERED='1'; " if env and env.get('PYTHONUNBUFFERED') else ""
        ps_script = (
            f"try {{ $b = $Host.UI.RawUI.BufferSize; $b.Width = 10000; $Host.UI.RawUI.BufferSize = $b }} catch {{}}; "
            f"[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
            f"{env_prefix}"
            f"$sw = [System.IO.StreamWriter]::new('{escaped_out}', $false, [System.Text.Encoding]::UTF8); "
            f"$sw.AutoFlush = $true; "
            f"try {{ & '{escaped_bat}' 2>&1 | ForEach-Object {{ $l = $_.ToString().TrimEnd(); Write-Host $l; $sw.WriteLine($l) }} }} "
            f"finally {{ $sw.Close() }}"
        )
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 1
        proc = subprocess.Popen(
            ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', ps_script],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            startupinfo=si,
            cwd=cwd,
        )
    else:
        escaped_out = out_file.replace("'", "'\\''")
        if env:
            proc = subprocess.Popen(
                f"({command}) > '{escaped_out}' 2>&1",
                shell=True, cwd=cwd, start_new_session=True, env=env,
            )
        else:
            proc = subprocess.Popen(
                f"({command}) 2>&1 | tee '{escaped_out}'",
                shell=True, cwd=cwd,
            )

    return proc, script_file


def tool_bash(command: str, timeout: int = 30, inputs: list = None, **kw) -> str:
    """执行 shell 命令并返回输出. 可选 inputs 列表用于向 stdin 逐行喂入预设输入."""
    # 1. 安全检查
    danger = check_dangerous(command)
    if danger:
        return f"Error: Refused to run dangerous command matching '{danger}'"

    # 2. 准备 stdin (用于简单交互场景, 如自动填入 y/n)
    stdin_bytes = None
    if inputs:
        stdin_bytes = ("\n".join(str(i) for i in inputs) + "\n").encode("utf-8")

    # 3. 启动子进程
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdin=subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(WORKDIR),
        )
    except Exception as exc:
        return f"Error: {exc}"

    # 4. 等待执行, 处理超时
    try:
        stdout_bytes, stderr_bytes = proc.communicate(
            input=stdin_bytes, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        _kill_proc_tree(proc)
        partial = ""
        try:
            out, err = proc.communicate(timeout=5)
            partial = decode_bytes(out) + decode_bytes(err)
        except Exception:
            pass
        hint = f"Error: Command timed out after {timeout}s"
        if partial.strip():
            hint += f"\n[部分输出]\n{partial.strip()[-500:]}"
        if stdin_bytes is None:
            hint += ("\n提示: 如果命令需要交互式输入(如登录、token)，"
                     "请使用 ask_user 获取信息后用非交互参数重试，"
                     "或使用 bash(inputs=[...]) 传入预设输入。")
        return hint

    # 5. 解码并拼装输出
    stdout = decode_bytes(stdout_bytes)
    stderr = decode_bytes(stderr_bytes)
    output = ""
    if stdout:
        output += stdout
    if stderr:
        output += ("\n--- stderr ---\n" + stderr) if output else stderr
    if not output:
        return f"[exit code: {proc.returncode}]"
    if proc.returncode != 0:
        output += f"\n[exit code: {proc.returncode}]"
    return truncate(output)


def tool_shell_session(command: str, timeout: int = 600, idle_timeout: int = 120, **kw) -> str:
    """在可见环境中执行命令, 通过轮询输出文件监控进度.
    timeout: 硬性最大等待时间. idle_timeout: 无新输出超过此秒数视为卡死."""
    # 1. 安全检查
    danger = check_dangerous(command)
    if danger:
        return f"Error: Refused to run dangerous command matching '{danger}'"

    # 2. 创建临时输出捕获文件, 启动可见窗口进程
    out_file = os.path.join(tempfile.gettempdir(), f'_agent_out_{os.getpid()}.tmp')
    with open(out_file, 'w', encoding='utf-8') as f:
        pass
    proc, script_file = _launch_visible_proc(command, out_file, cwd=str(WORKDIR))
    tmp_files = [out_file]
    if script_file:
        tmp_files.append(script_file)

    # 3. 轮询监控: 进程状态 + 输出文件活动
    start_time = time.time()
    last_size = 0
    last_activity = start_time
    poll_interval = 2

    while True:
        # 3a. 进程已退出 → 正常结束
        if proc.poll() is not None:
            time.sleep(0.5)
            break

        elapsed = time.time() - start_time
        idle_secs = time.time() - last_activity

        # 3b. 硬性超时
        if elapsed > timeout:
            _kill_proc_tree(proc)
            partial = _read_tail(out_file, 500)
            hint = f"Error: Command timed out after {timeout}s (hard limit)"
            if partial:
                hint += f"\n[部分输出]\n{partial}"
            for tmp in tmp_files:
                try: os.unlink(tmp)
                except Exception: pass
            return hint

        # 3c. 空闲超时: 长时间无新输出, 且已运行超过30s
        if idle_secs > idle_timeout and elapsed > 30:
            _kill_proc_tree(proc)
            partial = _read_tail(out_file, 500)
            hint = f"Error: No new output for {idle_timeout}s, command appears stuck (ran {elapsed:.0f}s)"
            if partial:
                hint += f"\n[最后输出]\n{partial}"
            for tmp in tmp_files:
                try: os.unlink(tmp)
                except Exception: pass
            return hint

        # 3d. 检查输出文件是否有新数据
        try:
            current_size = os.path.getsize(out_file)
        except (FileNotFoundError, OSError):
            current_size = 0

        if current_size > last_size:
            last_size = current_size
            last_activity = time.time()

        time.sleep(poll_interval)

    # 4. 读取完整输出
    try:
        enc = 'utf-8-sig' if IS_WINDOWS else 'utf-8'
        output = Path(out_file).read_text(encoding=enc, errors='replace')
    except Exception:
        output = "[no output]"

    # 5. 清理临时文件
    for tmp in tmp_files:
        try:
            os.unlink(tmp)
        except Exception:
            pass

    if proc.returncode and proc.returncode != 0:
        output += f"\n[exit code: {proc.returncode}]"
    return truncate(output) if output.strip() else "[no output]"


def _read_tail(filepath: str, max_chars: int = 500) -> str:
    """读取文件末尾内容, 用于超时时展示部分输出."""
    try:
        text = Path(filepath).read_text(encoding='utf-8', errors='replace')
        return text.strip()[-max_chars:] if text.strip() else ""
    except Exception:
        return ""



class BackgroundProcess:
    """可监控的后台进程, 输出记录到文件.
    Windows: 在新控制台窗口运行. Unix: 在后台运行."""
    def __init__(self, command: str, cwd: str):
        self.command = command
        uid = f'{os.getpid()}_{id(self)}'
        self._out_file = os.path.join(tempfile.gettempdir(), f'_agent_bg_{uid}.log')
        env = {**os.environ, 'PYTHONUNBUFFERED': '1'}
        self.proc, self._script_file = _launch_visible_proc(
            command, self._out_file, cwd=cwd, env=env
        )

    @property
    def is_running(self) -> bool:
        return self.proc.poll() is None

    @property
    def exit_code(self) -> int | None:
        return self.proc.poll()

    def get_output(self, last_n: int = 50) -> str:
        try:
            enc = 'utf-8-sig' if IS_WINDOWS else 'utf-8'
            with open(self._out_file, 'r', encoding=enc, errors='replace') as f:
                content = f.read()
            lines = content.splitlines()
            if last_n:
                lines = lines[-last_n:]
            return "\n".join(lines) if lines else "[no output yet]"
        except (FileNotFoundError, PermissionError):
            return "[no output yet]"

    def kill(self):
        _kill_proc_tree(self.proc)
        tmp_files = [self._out_file]
        if self._script_file:
            tmp_files.append(self._script_file)
        for f in tmp_files:
            try:
                os.unlink(f)
            except Exception:
                pass

def tool_background(state: dict, action: str, name: str = "", command: str = "",
                     last_n: int = 50, **kw) -> str:
    """管理后台进程: 启动(start)、读输出(output)、列表(status)、终止(kill)."""
    bg = state.setdefault("bg_processes", {})

    if action == "start":
        if not command:
            return "Error: 'command' is required for 'start' action"
        danger = check_dangerous(command)
        if danger:
            return f"Error: Refused to run dangerous command matching '{danger}'"
        if name in bg and bg[name].is_running:
            return f"Error: Process '{name}' is already running. Kill it first or use a different name."
        bg[name] = BackgroundProcess(command, cwd=str(WORKDIR))
        return f"Started '{name}': {command}\nPID: {bg[name].proc.pid}"

    elif action == "output":
        if name not in bg:
            return f"Error: No process named '{name}'"
        bp = bg[name]
        status = "running" if bp.is_running else f"exited (code={bp.exit_code})"
        return truncate(f"[{name}] status: {status}\n---\n{bp.get_output(last_n)}")

    elif action == "status":
        if not bg:
            return "No background processes"
        lines = []
        for n, bp in bg.items():
            s = "running" if bp.is_running else f"exited (code={bp.exit_code})"
            lines.append(f"  {n}: {s} | {bp.command[:60]}")
        return "\n".join(lines)

    elif action == "kill":
        if name not in bg:
            return f"Error: No process named '{name}'"
        bg[name].kill()
        del bg[name]
        return f"Killed '{name}'"

    return f"Error: Unknown action '{action}'"


def tool_read_file(file_path: str, offset: int = 1, limit: int = 0, **kw) -> str:
    """读取文件内容, 支持行号和分页. offset 从 1 开始, limit=0 表示读取全部."""
    try:
        # 1. 路径安全检查
        target = safe_path(file_path)
        if not target.exists():
            return f"Error: File not found: {file_path}"
        if not target.is_file():
            return f"Error: Not a file: {file_path}"

        # 2. 读取并按行分割
        content = target.read_text(encoding="utf-8")
        lines = content.splitlines()
        total = len(lines)

        # 3. 按 offset/limit 截取
        start = max(0, offset - 1)
        end = (start + limit) if limit > 0 else total
        selected = lines[start:end]

        # 4. 添加行号, 拼装结果
        numbered = [f"{start + i + 1:>6}| {line}" for i, line in enumerate(selected)]
        result = "\n".join(numbered)
        if end < total:
            result += f"\n... [{end}/{total} lines shown, use offset={end+1} to continue]"
        return truncate(result)
    except ValueError as exc:
        return str(exc)
    except Exception as exc:
        return f"Error: {exc}"


_BOM_SUFFIXES = {".ps1", ".psm1", ".psd1", ".bat", ".cmd"}
def tool_write_file(file_path: str, content: str, **kw) -> str:
    """写入内容到文件. 父目录不存在时自动创建."""
    try:
        # 1. 路径安全检查
        target = safe_path(file_path)
        # 2. 确保父目录存在
        target.parent.mkdir(parents=True, exist_ok=True)
        # 3. 写入文件 (Windows 脚本文件加 BOM, 否则 PowerShell 5.x 按系统编码读取会乱码)
        enc = "utf-8-sig" if IS_WINDOWS and target.suffix.lower() in _BOM_SUFFIXES else "utf-8"
        target.write_text(content, encoding=enc)
        return f"Successfully wrote {len(content)} chars to {file_path}"
    except ValueError as exc:
        return str(exc)
    except Exception as exc:
        return f"Error: {exc}"


def tool_edit_file(file_path: str, old_string: str, new_string: str, **kw) -> str:
    """精确替换文件中的文本. old_string 必须在文件中恰好出现一次."""
    try:
        # 1. 路径安全检查
        target = safe_path(file_path)
        if not target.exists():
            return f"Error: File not found: {file_path}"

        # 2. 读取文件, 检查匹配次数
        content = target.read_text(encoding="utf-8")
        count = content.count(old_string)
        if count == 0:
            return "Error: old_string not found in file. Make sure it matches exactly."
        if count > 1:
            return (
                f"Error: old_string found {count} times. "
                "It must be unique. Provide more surrounding context."
            )

        # 3. 执行替换并写回
        new_content = content.replace(old_string, new_string, 1)
        target.write_text(new_content, encoding="utf-8")
        return f"Successfully edited {file_path}"
    except ValueError as exc:
        return str(exc)
    except Exception as exc:
        return f"Error: {exc}"


def tool_ask_user(question: str, **kw) -> str:
    """向用户询问信息（如密码、token、确认等），等待用户回答后返回."""
    print(f"\n{'='*50}")
    print(f"[AI 需要你的输入]")
    print(f"问题: {question}")
    print(f"{'='*50}")
    answer = input(">>> ")
    return answer.strip()


def run_todo(state: dict, items: list, **kw) -> str:
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

# ===== [describe] for llm -> 在llm请求中告诉模型有哪些工具 =====
TOOLS = [
    {
        "name": "bash",
        "description": (
            "Run a shell command inline and return its output. "
            "Does NOT open a new window. Use for quick commands that finish within 30s "
            "(e.g. git status, ls, curl, cat, echo). "
            "For long-running commands (install, build), use shell_session instead."
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
                "inputs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional pre-supplied stdin inputs (one per line). For simple interactive commands that read line-by-line from stdin.",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "shell_session",
        "description": (
            "Open a NEW console window to execute a command. "
            "User can see real-time output in the window. "
            "Monitors output activity: keeps waiting while command produces output, "
            "kills if no new output for idle_timeout seconds. "
            "Use for long-running finite commands: npm install, pip install, cargo build, etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The command to execute in the new window.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Hard max timeout in seconds. Default 600 (10 minutes).",
                },
                "idle_timeout": {
                    "type": "integer",
                    "description": "Kill if no new output for this many seconds. Default 120.",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "background",
        "description": (
            "Manage background processes (dev servers, watchers, test runners, etc). "
            "Processes run without blocking and you can check their output at any time. "
            "action: 'start' launches, 'output' reads recent output, "
            "'status' lists all processes, 'kill' terminates."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["start", "output", "status", "kill"],
                    "description": "Action to perform.",
                },
                "name": {
                    "type": "string",
                    "description": "Process name/identifier.",
                },
                "command": {
                    "type": "string",
                    "description": "Command to start (required for 'start' action).",
                },
                "last_n": {
                    "type": "integer",
                    "description": "Number of recent output lines to show (for 'output'). Default 50.",
                },
            },
            "required": ["action", "name"],
        },
    },
    {
        "name": "read_file",
        "description": "Read file contents with line numbers. Supports pagination via offset/limit.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the file (relative to working directory).",
                },
                "offset": {
                    "type": "integer",
                    "description": "Start line number (1-based). Default 1.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max lines to read. 0 means all. Default 0.",
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
    {   "name": "todo", 
        "description": "Update task list. Track progress on multi-step tasks.",
        "input_schema": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}, "text": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}, "required": ["id", "text", "status"]}}}, "required": ["items"]}
    },

]


# ===== [describe] map_to [func] => {name:func} -> 在执行时候的映射 =====
TOOL_HANDLERS: dict[str, Any] = {
    # 命令行 和 cli
    "bash": tool_bash,
    "shell_session": tool_shell_session,
    "background": tool_background,

    # 文件操作
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "edit_file": tool_edit_file,
    
    # 多模态数据
    # 1 图片读取
    # 2 音频读取
    # 3 视频读取

    # 操作鼠标键盘 或 浏览器
    # browser use

    # 搜索数据
    # 1 web 搜索
    # 2 web 抓取

    # 隔断上下文
    # 1 子agent

    # 定时任务corn

    # 计划
    "todo": run_todo,
}


def _block_to_jsonable(block: Any) -> Any:
    """Anthropic SDK 的 TextBlock / ToolUseBlock 等转为可 json 序列化的 dict。"""
    if isinstance(block, dict):
        return block
    model_dump = getattr(block, "model_dump", None)
    if callable(model_dump):
        try:
            return model_dump(mode="json")
        except TypeError:
            return model_dump()
    raise TypeError(f"Cannot JSON-serialize block type: {type(block)}")


def message_to_jsonl_dict(msg: dict) -> dict:
    """整条 message 转为可写入 jsonl 的结构（读回后可直接喂给 API）。"""
    role = msg["role"]
    content = msg["content"]
    if isinstance(content, str):
        return {"role": role, "content": content}
    if isinstance(content, list):
        return {"role": role, "content": [_block_to_jsonable(b) for b in content]}
    return {"role": role, "content": content}


async def run_tool(ai_answer,state,out_queue):
    results = []
    header = state.get("header") or {}
    for block in ai_answer:
        if block.type == "tool_use": 
            handler = TOOL_HANDLERS.get(block.name)
            if handler is None:
                output = f"Error: Unknown tool '{block.name}'"
            else:
                try:
                    # 打印输入
                    await out_queue.put({"header": header, "data": {"type": "render", "event": "tool_args", "detail": block.input}}) # 向前端发送tool_use
                    print(f'[Tool]')
                    print(f'{block.name}: {str(block.input)[:100]}')
                    output = handler(state=state, **block.input)
                except Exception as e:
                    output = f"Error: {e}"
            # 收集结果
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": str(output)
            })
            # 打印输出
            await out_queue.put({"header": header, "data": {"type": "render", "event": "tool_result", "detail": output}}) # 向前端发送tool_result
            print(f'[Result]')
            print(f'{str(output)[:100]}')
    return results

''' 
消息保存 消息发送 流程
1 实时发送消息给前端 但是不保存
2 知道跑完一个对话循环才 保存
如果出现断网 等错误 前端信息直接作废
'''

async def agent_loop(in_queue: asyncio.Queue, out_queue: asyncio.Queue):
    '''in_queue -> agent_loop -> out_queue'''

    # 1 获取消息
    data = await in_queue.get()
    header = data.get("header") or {}
    thread_id = f"{header['user_id']}_{header['session_id']}"
    
    # 2 创建 workspace 文件夹
    workspace_path = os.path.join(os.path.dirname(__file__), "workspace", thread_id)
    os.makedirs(workspace_path, exist_ok=True)
    # global WORKDIR
    # WORKDIR = Path(workspace_path)
    
    # 3 创建 messages.jsonl 存放历史记录
    messages_path = os.path.join(workspace_path, "messages.jsonl") # 对话历史文件
    if not os.path.exists(messages_path):
        with open(messages_path, "w", encoding="utf-8"):
            pass
    
    # # 4 读取历史记录
    # ''' 
    # {"role": "user", "content": "你好"}
    # {"role": "assistant", "content": "你好！..."}
    # {"role": "user", "content": "创建文件"}
    # {"role": "assistant", "content": [{"id": "call_xxx", "name": "write_file", ...}]}
    # {"role": "user", "content": [{"type": "tool_result", ...}]}
    # '''

    # 4 system prompt
    system_prompt = f'''You are a coding agent at {WORKDIR} on {platform.system()}.

工作方法
1 先看再改: 修改文件前必须先 read_file, 拿到准确内容再 edit_file
2 最小改动: 优先 edit_file 精确替换, 只有新建文件才用 write_file
3 改完验证: 改完代码后用 bash 运行或测试, 确认生效
4 善用搜索: 找代码用 bash(command="grep -rn '关键词' .") 而非逐个文件猜
5 出错诊断: 命令失败时读错误信息针对性修复, 不要盲目重试相同命令

工具选择
- bash: 快速命令, ≤30s 能完成的 (git, ls, grep, curl, cat)
- shell_session: 耗时安装/编译 (npm install, pip install, cargo build)
- background: 长驻服务 (npm run dev, uvicorn), 用 action=start/output/kill 管理
- 优先用命令行参数跳过交互 (--yes, -y, --no-edit), 不确定时先查 --help
- 需要用户提供信息(密码/token/选择)时先 ask_user, 再用 bash(inputs=[...])
'''

    # 5 state (跨消息状态保存)
    state = {
        # tool - todo 管理计划
        "is_plan_mode": True,
        "no_todo_count": 0,
        "plan": [],

        # tool - background 记录开启的进程 无需写入system_prompt 模型自己能查看开启了哪些进程
        "bg_processes": {},

        # 当前会话 header，out_queue 出站 OutMsg 使用
        "header": header,
    }

   
    # 6 多轮对话
    while True:
        header = state["header"]
        user_input = data["content"]
        opt = header.get("optional_out_format") or ""
        print(f"You: {user_input}",flush=True)
        
        # 7 读取历史记录(每轮都重新读取)
        ''' 
        {"role": "user", "content": "你好"}
        {"role": "assistant", "content": "你好！..."}
        {"role": "user", "content": "创建文件"}
        {"role": "assistant", "content": [{"id": "call_xxx", "name": "write_file", ...}]}
        {"role": "user", "content": [{"type": "tool_result", ...}]}
        '''
        messages = []
        with open(messages_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    messages.append(json.loads(line))
        old_len = len(messages) # 用于出错回滚
        
        # 8 构建 messages 列表
        messages.append({"role": "user", "content": user_input})
        
        # 9 tool loop
        while True:
            print("[thinking...]", flush=True)
            try:
                await out_queue.put({"header": header, "data": {"type": "render", "event": "thinking", "detail": None}}) # 向前端发送thinking状态
                # print(f"[API] request start model={model!r} msg_blocks={len(messages)}", flush=True)
                res = await client.messages.create(model=model,messages=messages,tools=TOOLS,system=system_prompt,max_tokens=8000,)
                # print(f"[API] response ok stop_reason={res.stop_reason!r}", flush=True)
            except Exception as e:
                # print(f"[API Error] {e}", flush=True)
                await out_queue.put({"header": header, "data": {"type": "error", "event": "api_or_network_error", "detail": None}}) # 向前端发送api_or_network_error状态
                break # 循环直到退出tool loop

            # no tool_call
            if res.stop_reason == "end_turn":
                # ai 回答
                ai_answer = res.content[0].text # 注意:这里只取 纯文本
                # ai 回答 -> OutMsg（语音模式先发 ai_text 展示文字，再发 ai_audio 触发 TTS）
                await out_queue.put({"header": header, "data": {"type": "ai_text", "event": "output", "detail": {"content": ai_answer}}})
                if opt in ("audio", "audio_stream"):
                    await out_queue.put({"header": header, "data": {"type": "ai_audio", "event": "output", "detail": {"content": ai_answer}}})
                messages.append({"role": "assistant", "content": ai_answer}) # 保存到 messages
                print(f"AI: {ai_answer}")
                # --- [agent_loop2 改动] break 含义：退出内层 tool loop；agent_loop 这里是退出唯一 while，函数结束 ---
                break # 退出 tool loop

            # yes tool_call
            elif res.stop_reason == "tool_use":
                # ai 目的
                for block in res.content:
                    if block.type == "text": 
                        ai_purpose = block.text
                        print(f'({ai_purpose})')
                        await out_queue.put({"header": header, "data": {"type": "render", "event": "tool_purpose", "detail": ai_purpose}}) # 向前端发送tool_call
                # 1 ai 告诉 user 使用的 工具 和 参数
                tool_use = res.content # 注意:这个content列表包含 TextBlock 和 ToolUseBlock 工具调用必须记录这两个
                messages.append({"role": "assistant", "content": tool_use}) # 保存本轮历史记录
                # 2 user 执行 并返回结果 (本质是模拟人类与ai交互)
                tool_result = await run_tool(tool_use,state,out_queue)
                messages.append({"role": "user", "content": tool_result}) # 保存本轮历史记录

            # other
            elif res.stop_reason == "max_tokens":
                print(f"[Max Tokens]")
                await out_queue.put({"header": header, "data": {"type": "error", "event": "out_of_tokens", "detail": None}}) # 向前端发送out of tokens状态
                break
            else:
                print(f"[Unknown Stop Reason]")
                break

        # 10 保存对话历史(只追加新增部分)
        try:
            with open(messages_path, "a", encoding="utf-8") as f:
                for msg in messages[old_len:]:
                    f.write(json.dumps(message_to_jsonl_dict(msg), ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[Save Error] {e}")
        
        # 11 等待下一条消息
        print('>>>',flush=True)
        data = await in_queue.get()
        state["header"] = data.get("header") or {}





''' 
前端协议 OutMsg（agent_loop 直接组装 {header, data} 放入 out_queue）
render -> 渲染信息
    event: thinking | tool_purpose | tool_args | tool_result
error -> 错误信息
    event: api_or_network_error | out_of_tokens
ai_text -> AI 文本回复
    event: output, detail: {content: ...}
ai_audio -> AI 音频回复（detail 为待 TTS 文本，server 负责转 pcm 分片）
    event: output, detail: {content: ...}
'''
# sys
import os
# load env
from dotenv import load_dotenv
load_dotenv()
from pydantic_settings import BaseSettings
class Settings(BaseSettings):
    HOST: str = "0.0.0.0"
    PORT: int = 9999
    MDNS_NAME: str = "bot_server"
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "allow"
        case_sensitive = True
settings = Settings()


# type
import json
from typing import Set
from collections import defaultdict
import base64
# asyncio
import asyncio
# app
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware # middleware
from contextlib import asynccontextmanager # lifespan
# client connect
from fastapi import WebSocket

# aiohttp
import aiohttp

TTS_URL = settings.TTS_URL
ASR_URL = settings.ASR_URL

TTS_KEY = settings.TTS_KEY
ASR_KEY = settings.ASR_KEY

pcm_sample_rate = 24000
tts_voice = "tongtong"
tts_stream = True

# asr
async def as_asr(session, wav: bytes) -> str:
    headers = {
        "Authorization": f"Bearer {ASR_KEY}",
    }
    form = aiohttp.FormData()
    form.add_field("model", "glm-asr-2512")
    form.add_field("stream", "false")
    form.add_field(
        "file",
        wav,
        filename="audio.wav",
        content_type="audio/wav",
    )
    async with session.post(ASR_URL, headers=headers, data=form) as response:
        response.raise_for_status()
        ct = (response.headers.get("Content-Type") or "").lower()
        if "application/json" in ct: # 说明 正文是json
            body = await response.json()
            return body.get("text", "") # 返回 text
        return (await response.read()).decode("utf-8") # 说明 正文是二进制 返回 str

# tts
async def as_tts(session, text: str) -> bytes:
    headers = {
        "Authorization": f"Bearer {TTS_KEY}",
        "Content-Type": "application/json",
    }
    data = {
        "model": "glm-tts",  
        "input": text,
        "voice": "tongtong",   # 音色：彤彤
        "response_format": "wav", # 输出格式：wav pcm
        "stream": False,         # 是否流式返回：False
        "speed": 1.0,          # 语速：1.0
        "volume": 1.0,         # 音量：1.0
        "watermark_enabled": False, # 是否加水印：False
    }
    async with session.post(TTS_URL, headers=headers, json=data) as response:
        response.raise_for_status()
        ct = (response.headers.get("Content-Type") or "").lower()
        if "application/json" in ct: # 说明 正文是json
            body = await response.json()
            if isinstance(body, dict) and body.get("error"):
                err = body["error"]
                raise RuntimeError(f"TTS API 错误 [{err.get('code')}]: {err.get('message')}")
            if isinstance(body, str): # base64 字符串
                return base64.b64decode(body) # 解码 并返回
            if isinstance(body, dict) and "data" in body: # 字典 且 有 data 字段
                return base64.b64decode(body["data"]) # 解码 并返回
            raise RuntimeError(f"unknown data: {body!r}") # 是json 但不是 base64 字符串 也不是 字典 且 有 data 字段
        return await response.read() # 说明 正文是二进制


# pcm -> wav
import io
import wave
# 计算 sample_width 根据格式
def _format_to_sample_width(fmt: str) -> int:
    """pcm_s16le -> 2 bytes per sample；未知格式回退为 2。"""
    f = (fmt or "").lower().strip()
    if f in ("pcm_s16le", "s16le", "int16"):
        return 2
    if f in ("pcm_s24le", "s24le", "int24"):
        return 3
    if f in ("pcm_s32le", "s32le", "int32"):
        return 4
    if f in ("pcm_u8", "u8"):
        return 1
    return 2

# pcm -> wav
def pcm_to_wav_bytes(pcm: bytes, sample_rate: int, channels: int, sample_width: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()

def wav_bytes_to_pcm(wav_bytes: bytes) -> tuple[bytes, int, int, str]:
    """
    将 wav bytes 解包为裸 PCM（按帧数据）并返回 (pcm, sample_rate, channels, format)。
    目前仅支持常见的 16-bit PCM（对应 pcm_s16le）。
    """
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        channels = int(wf.getnchannels())
        sample_rate = int(wf.getframerate())
        sample_width = int(wf.getsampwidth())
        if sample_width != 2:
            raise ValueError(f"unsupported wav sample_width={sample_width} (need 2 for pcm_s16le)")
        pcm = wf.readframes(wf.getnframes())
    return pcm, sample_rate, channels, "pcm_s16le"


async def iter_tts_pcm_chunks(session, text: str, chunk_size: int = 4096):
    """
    伪流式：一次性拿到 TTS 的 wav，再解析为 PCM，按 chunk_size 分片 yield。
    前端收到的是裸 PCM bytes（与 input 的 pcm 分片一致）。
    """
    wav_bytes = await as_tts(session, text)
    pcm, _sr, _ch, _fmt = wav_bytes_to_pcm(wav_bytes)
    for i in range(0, len(pcm), chunk_size):
        yield pcm[i:i + chunk_size]
















# mdns
# lan ip
import socket
import netifaces
# ——————————
from zeroconf import ServiceInfo
from zeroconf.asyncio import AsyncZeroconf
def _get_lan_ipv4_list() -> list[str]:
    ''' 
    使用 netifaces 获取 本机所有局域网 IPv4 地址
    '''
    # VPN虚拟网卡接口名称关键词
    vpn_keywords = ('tun', 'tap', 'vpn', 'ppp', 'utun', 'wg', 'wireguard')
    
    ips = []
    
    # 遍历所有网络接口
    for iface in netifaces.interfaces():
        iface_lower = iface.lower()
        # 跳过回环接口
        if iface_lower.startswith('lo'):
            continue
        # 跳过VPN虚拟接口
        if any(kw in iface_lower for kw in vpn_keywords):
            continue
        
        # 获取该接口的IPv4地址
        addrs = netifaces.ifaddresses(iface)
        if netifaces.AF_INET not in addrs:
            continue
        
        # 遍历所有IPv4地址
        for addr_info in addrs[netifaces.AF_INET]:
            ip = addr_info['addr']
            # 收集所有局域网IP
            if ip.startswith('192.168.') or ip.startswith('10.'):
                ips.append(ip)
    
    return ips if ips else ["127.0.0.1"]

def _create_service_info(name: str, port: int, ips: list[str]) -> ServiceInfo:
    ''' 
    创建 mDNS 服务信息 , 用于在局域网中广播(支持多IP)
    '''
    service_type = "_ws._tcp.local." # _服务名._协议.local.
    
    return ServiceInfo(
        service_type,                              # 服务类型
        f"{name}._ws._tcp.local.",                 # 完整服务名称，用于服务发现
        addresses=[socket.inet_aton(ip) for ip in ips], # 多个IP地址
        port=port,                                 # 服务端口
        properties={b"path": b"/ws"},              # TXT 记录，包含额外信息（WebSocket 路径）
        server=f"{name.lower()}.local.",           # 服务名称，client 通过这个访问
    )

async def _monitor_network_change():
    ''' 
    监听网络变化，当IP变化时自动重新注册mDNS服务
        1 每5秒检查一次本机IP地址
        2 如果IP发生变化，注销旧服务并注册新服务
    '''
    global async_zc, mdns_info
    
    last_ips = _get_lan_ipv4_list() # 记录当前IP列表
    
    while True:
        await asyncio.sleep(5) # 每5秒检查一次
        
        current_ips = _get_lan_ipv4_list()
        if current_ips != last_ips:
            print(f"[mdns monitor] 检测到IP变化: {last_ips} -> {current_ips}")
            
            # 注销旧服务
            if async_zc and mdns_info:
                try:
                    await async_zc.async_unregister_service(mdns_info)
                except Exception as e:
                    print(f"[mdns monitor] 注销旧服务失败: {e}")
            
            # 注册新服务
            mdns_info = _create_service_info(settings.MDNS_NAME, settings.PORT, current_ips)
            if async_zc:
                try:
                    await async_zc.async_register_service(mdns_info)
                    print(f"[mdns monitor] 已重新注册: {settings.MDNS_NAME}.local -> {current_ips}:{settings.PORT}")
                except Exception as e:
                    print(f"[mdns monitor] 注册新服务失败: {e}")
            
            last_ips = current_ips # 更新记录的IP列表

mdns_info: ServiceInfo | None = None
async_zc: AsyncZeroconf | None = None
# ------------------

# global
clients: Set[WebSocket] = set()


# lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f'[lifespan] start')
    global async_zc, mdns_info
    
    ips = _get_lan_ipv4_list()
    mdns_info = _create_service_info(settings.MDNS_NAME, settings.PORT, ips)
    async_zc = AsyncZeroconf()
    await async_zc.async_register_service(mdns_info)
    print(f"[mdns server] 已注册: {settings.MDNS_NAME}.local -> {ips}:{settings.PORT}")
    
    monitor_task = asyncio.create_task(_monitor_network_change())
    
    yield
    print(f'[lifespan] end')
    monitor_task.cancel()
    try:
        await monitor_task
    except asyncio.CancelledError:
        pass
    
    if async_zc and mdns_info:
        await async_zc.async_unregister_service(mdns_info)
        await async_zc.async_close()
        print("[mdns server] 已注销")

# fastapi
app = FastAPI(lifespan=lifespan)
# middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# in_queue 消息统一入口
async def receive_messages(
    websocket: WebSocket,
    in_queue: asyncio.Queue,
    out_queue: asyncio.Queue,
    http: aiohttp.ClientSession,
):
    '''client -> message -> in_queue '''
    # 原始纯文本输入
    # while True:
    #     msg = await websocket.receive_text()
    #     data = json.loads(msg)
    #     await in_queue.put(data)

    audio_buf = bytearray() # pcm 音频缓冲
    audio_ctx: dict | None = None  # user_audio_stream inputStart 时填充
    
    while True:
        msg = await websocket.receive()
        '''
        接收数据
        {
            "type": "websocket.receive",
            "text":一帧文本 or "bytes":一帧二进制
        }

        断开链接
        {
            "type": "websocket.disconnect",
            "code": 1000,      # int 关闭码
            "reason": "",      # str 可选
        }
        '''
        # data = await websocket.receive_text() # 只接收文本
        # data = await websocket.receive_bytes() # 只接收二进制

        if msg["type"] == "websocket.disconnect":
            break

        # bytes
        if "bytes" in msg and msg["bytes"] is not None:
            # 如果没有 user_audio_stream inputStart 表明音频没有开始 是异常音频
            if audio_ctx is None:
                continue
            # 缓存二进制语音
            audio_buf.extend(msg["bytes"])
            continue

        # text
        if "text" in msg and msg["text"] is not None:
            envelope = json.loads(msg["text"])
            header = envelope.get("header") or {}
            payload = envelope.get("data") or {}
            msg_type = payload.get("type")
            event = payload.get("event")
            detail = payload.get("detail")

            # 1 纯文本输入 type:user_text + event:input
            if msg_type == "user_text" and event == "input":
                await in_queue.put({ # 放入队列 (header + content 给 agent_loop)
                    "header": header,
                    "content": (detail or {}).get("content", ""),
                })
                continue

            # 2 音频开始  type:user_audio_stream + event:inputStart
            if msg_type == "user_audio_stream" and event == "inputStart":
                audio_buf.clear()
                meta = detail or {}
                audio_ctx = {
                    "header": header,
                    "sample_rate": int(meta.get("sample_rate", 24000)),
                    "channels": int(meta.get("channels", 1)),
                    "format": str(meta.get("format", "pcm_s16le")),
                }
                continue

            # 3 音频结束  type:user_audio_stream + event:inputEnd
            if msg_type == "user_audio_stream" and event == "inputEnd":
                if audio_ctx is None:
                    continue
                pcm = bytes(audio_buf)

                # 如果音频为空则跳过，并释放会话状态（否则 audio_ctx 一直占用）
                if not pcm:
                    await out_queue.put({
                        "header": audio_ctx["header"],
                        "data": {"type": "error", "event": "empty_audio", "detail": "未采集到音频"},
                    })
                    audio_buf.clear()
                    audio_ctx = None
                    continue

                # 计算 sample_width 根据格式
                sw = _format_to_sample_width(audio_ctx["format"]) 
                # pcm -> wav
                wav = pcm_to_wav_bytes(
                    pcm,
                    audio_ctx["sample_rate"],
                    audio_ctx["channels"],
                    sw,
                )
                # asr audio -> text
                header_snapshot = audio_ctx["header"]
                try:
                    text = await as_asr(http, wav)
                except Exception as e:
                    print(f"[receive] ASR error: {e}")
                    await out_queue.put({
                        "header": header_snapshot,
                        "data": {"type": "error", "event": "asr_error", "detail": str(e)},
                    })
                    audio_buf.clear()
                    audio_ctx = None
                    continue

                await out_queue.put({
                    "header": header_snapshot,
                    "data": {"type": "asr_return", "event": "output", "detail": {"content": text}},
                })

                await in_queue.put({
                    "header": header_snapshot,
                    "content": text,
                })

                # 重置状态
                audio_buf.clear()
                audio_ctx = None
                continue
        

# agent_loop 消息处理逻辑 
from agent_loop import agent_loop

# out_queue 消息统一出口
async def broadcast_messages(websocket: WebSocket, out_queue: asyncio.Queue):
    '''out_queue -> message -> client'''
    # 原始纯文本输出
    # while True:
    #     result = await out_queue.get()
    #     message = json.dumps(result, ensure_ascii=False)
    #     try:
    #         await websocket.send_text(message)
    #     except:
    #         break # -> finally
    async with aiohttp.ClientSession() as http:
        while True:
            result = await out_queue.get()
            header = result.get("header") or {}
            payload = result.get("data") or {}
            msg_type = payload.get("type")
            event = payload.get("event")
            detail = payload.get("detail")

            # ai_audio: agent_loop 已组装 OutMsg，server 只负责 TTS + pcm 分片
            if msg_type == "ai_audio" and event == "output":
                text = str((detail or {}).get("content") or "")
                if not text:
                    continue

                # 先请求一次 TTS（wav），再解析出真正的 PCM 元数据
                try:
                    wav_bytes = await as_tts(http, text)
                    _pcm, sample_rate, channels, fmt = wav_bytes_to_pcm(wav_bytes)
                except Exception as e:
                    print(f"[broadcast] TTS error: {e}")
                    err_msg = {
                        "header": header,
                        "data": {"type": "error", "event": "tts_error", "detail": str(e)},
                    }
                    try:
                        await websocket.send_text(json.dumps(err_msg, ensure_ascii=False))
                    except Exception:
                        break # -> finally
                    continue

                # 1 先发 元数据
                start_msg = {
                    "header": header,
                    "data": {
                        "type": "ai_audio_stream",
                        "event": "outputStart",
                        "detail": {
                            "sample_rate": sample_rate,
                            "channels": channels,
                            "format": fmt,
                        },
                    },
                }
                message = json.dumps(start_msg, ensure_ascii=False)
                try:
                    await websocket.send_text(message)
                except:
                    break # -> finally

                # 2 中间 pcm
                try:
                    # 这里不要再请求第二次 TTS；直接用已解析的 pcm 分片
                    # 流式传输 每个包4096字节 4kb 发送间隔60ms
                    _step = 4096
                    for i in range(0, len(_pcm), _step):
                        chunk = _pcm[i:i + _step]
                        if not chunk:
                            continue
                        await websocket.send_bytes(chunk)
                        # if i + _step < len(_pcm):
                            # await asyncio.sleep(0.06)
                except Exception as e:
                    # await websocket.send_text(json.dumps({"out_type": "output_audio_error", "error": str(e)}, ensure_ascii=False))
                    break

                # 3 结束
                end_msg = {
                    "header": header,
                    "data": {"type": "ai_audio_stream", "event": "outputEnd", "detail": None},
                }
                message = json.dumps(end_msg, ensure_ascii=False)
                try:
                    await websocket.send_text(message)
                except:
                    break # -> finally
                continue

            # 其余 OutMsg 已由 agent_loop 组装，直接转发
            message = json.dumps(result, ensure_ascii=False)
            try:
                await websocket.send_text(message)
            except:
                break # -> finally
               

# routers
@app.websocket("/ws")
async def ws_client_connect(websocket: WebSocket):
    # 1 建立一个ws连接
    await websocket.accept() # 每一个ws 创建一个独立协程
    clients.add(websocket)
    print(f'[ws_client_connect] a 连接数量: {len(clients)}')

    # 2 收发消息的队列
    in_queue: asyncio.Queue = asyncio.Queue()
    out_queue: asyncio.Queue = asyncio.Queue()

    # 3 后台线程
    # 后台任务 = 接收 → 处理 → 广播
    # receive_task = asyncio.create_task(receive_messages(websocket, in_queue))   # 接收消息
    # process_task = asyncio.create_task(agent_loop(in_queue, out_queue))        # 处理消息
    # broadcast_task = asyncio.create_task(broadcast_messages(websocket, out_queue)) # 广播消息

    # # 4 前台线程 (主协程)
    # ''' 
    # 需要有一个 主协程 循环 保证ws连接不关闭(等价于一个while True)
    # '''
    # try:
    #     # 仅在客户端断开（receive 收到 websocket.disconnect）时结束当前连接
    #     await receive_task
    # except Exception as e:
    #     print(f'[ws_client_connect] error: {e}')
    # finally:
    #     # 取消所有后台任务
    #     for task in (receive_task, process_task, broadcast_task):
    #         task.cancel()
    #     await asyncio.gather(receive_task, process_task, broadcast_task, return_exceptions=True)
    #     # 移除连接
    
    try:
        async with aiohttp.ClientSession() as http:
            # 3 后台线程
            receive_task = asyncio.create_task(receive_messages(websocket, in_queue, out_queue, http))   # 接收消息
            process_task = asyncio.create_task(agent_loop(in_queue, out_queue))        # 处理消息
            broadcast_task = asyncio.create_task(broadcast_messages(websocket, out_queue)) # 广播消息
           
            # 4 前台线程 (主协程)
            try:
                await receive_task
            except Exception as e:
                print(f'[ws_client_connect] error: {e}')
            finally:
                # 取消所有后台任务
                for task in (receive_task, process_task, broadcast_task):
                    task.cancel()
                await asyncio.gather(receive_task, process_task, broadcast_task, return_exceptions=True)
    # 移除链接         
    finally:
        clients.discard(websocket)
        print(f'[ws_client_connect] f 连接数量: {len(clients)}')

# run
if __name__ == "__main__":
    port = settings.PORT
    host = settings.HOST
    app = "server:app"
    uvicorn.run(app, host=host, port=port)


''' 
业务流程
WebSocket 连接 (每个连接创建 两个队列 三个协程)
    │
    ├── receive_messages()  ──→ in_queue
    │                              │
    │                              ▼
    │                        agent_loop() 异步 存储连接状态 
    │                              │
    │                              ▼
    └── broadcast_messages() ←── out_queue

'''


''' 
业务流程 v2 (ASR 在 receive_messages 内完成, 结果经 out_queue 回传前端)

WebSocket 连接 (每个连接创建 两个队列 三个协程)
    │
    ├── receive_messages()
    │       │
    │       ├── user_text/input ───────────────────────────────→ in_queue (content=用户文本)
    │       │
    │       └── user_audio_stream
    │               inputStart(metadata) → pcm chunks → inputEnd
    │                       │
    │                       ▼
    │                  pcm -> wav -> as_asr() 得到 text
    │                       │
    │                       ├──→ out_queue  (asr_return/output  用户文本回传前端展示)
    │                       └──→ in_queue   (content=text 交给 agent_loop)
    │
    │                  (空音频 → out_queue error/empty_audio)
    │                  (识别失败 → out_queue error/asr_error)
    │
    ├── agent_loop()  异步 存储连接状态
    │       in_queue.content → LLM
    │       │
    │       ├──→ out_queue  render/thinking · tool_*           (过程渲染)
    │       ├──→ out_queue  ai_text/output                     (文本回复)
    │       └──→ out_queue  ai_audio/output                    (需要语音时, 仅带文本)
    │
    └── broadcast_messages()  ←── out_queue
            │
            ├── ai_audio/output → as_tts() 得到 wav -> pcm
            │       └──→ ai_audio_stream  outputStart → pcm chunks → outputEnd  (流式语音)
            │
            └── 其余 OutMsg (asr_return · ai_text · render · error ...) 直接转发前端

要点:
  1. ASR 由 receive_messages 直接做, 不再进 agent_loop, 用户文本第一时间回显
  2. 所有发往前端的消息统一经 out_queue -> broadcast_messages 出口
  3. TTS 仍由 broadcast_messages 负责, 将 ai_audio 转成流式 ai_audio_stream
'''



'''
websocket.receive() 
可接受
1 text      str
2 binary    bytes

'''


'''
音频的数据格式

数据类型 bytes
解析格式 
    - wav 有元数据metadata
    - pcm 无元数据 需额外传递
	- opus pcm的有损压缩
传输方式 
    - 非流式 一次发整段 wav
    - 流式 
		1 json start标记(metadata) 
		2 binary pcm 
		3j son end标记(end)

asr - wav:bytes -> text:str
tts - text:str -> wav:bytes

'''



'''
输入输出数据格式


语音交互
	[text/audio] -> [asr] -> [llm] -> [tts] -> [text/audio]

	从前端传向后端是数据有:
		type:user
			text | audio | audio_stream [用户输入的文本或语音]
			stop [ 打断 
				1 如果正在收集语音 则清空
				2 如果正在请求llm 则中断请求
				3 如果正在通过ws流式打印 那么停止输入 但历史记录正常记录
			]

	从后端传向前端的数据有：
		type:asr_return 回传
			text [asr 识别的 text 回传到前端聊天框]

		type:ai 输出
			text | text_stream [llm 回复的 text 展示到前端聊天框] (必选)
			audio | audio_stream [llm 回复的 audio 边流式打印文字 边播放语音] (可选)

		type:error [错误信息]
		type:render [渲染信息]

InMsg
{

	header = {  // 路由信息
		channel =  cmd命令行|web网页|app应用
		user_id = 123
		session_id = 456

		trace_id = 789 // 链路追踪id
		required_out_format = text | text_stream    (必选)
		optional_out_format = audio | audio_stream  (可选)
	}


	data = {  // 业务信息
		type = text | audio | audio_stream | stop         // 大类
		event =  ...                                     // 具体事件
		detail: {...}                                    // 事件内容
	}
}


输入 text
data = {type:user_text,event:input,detail:{content:你好}} 

输入 audio
data = {type:user_audio,event:input,detail:{content:wav音频数据}} 

输入 流式 audio_stream
sample_rate 采样率 
channels 声道数 
format 格式
data = {type:user_audio_stream,event:inputStart,detail:{sample_rate:16000,channels:1,format:"pcm_s16le"}}
binary: pcm bytes chunk 1
binary: pcm bytes chunk 2
data = {type:user_audio_stream,event:inputEnd,detail:null}

中断
data = {type:stop,event:input,detail:null}



OutMsg
{

	header = {  // 路由信息
		channel =  cmd命令行|web网页|app应用
		user_id = 123
		session_id = 456

		trace_id = 789 // 链路追踪id
		required_out_format = text | text_stream    (必选)
		optional_out_format = audio | audio_stream  (可选)
	}


	data = {  // 业务信息
		type = ...                                       // 大类
		event =  ...                                     // 具体事件
		detail: {...}                                    // 事件内容
	}
}

asr 回传
	输出text
	data = {type:asr_return,event:output,detail:{content:你好}} 

ai 输出
	输出 text
	data = {type:ai_text,event:output,detail:{content:你也好}} 

	输出 流式 text_stream
	data = {type:ai_text_stream,event:outputStart,detail:null}
	data = {type:ai_text_stream,event:chunk,detail:"你"}
	data = {type:ai_text_stream,event:chunk,detail:"也好"}
	data = {type:ai_text_stream,event:outputEnd,detail:null}

	输出 audio
	data = {type:ai_audio,event:output,detail:{content:音频数据}} 

	输出 流式 audio_stream
	sample_rate 采样率 
	channels 声道数 
	format 格式
	data = {type:ai_audio_stream,event:outputStart,detail:{sample_rate:16000,channels:1,format:"pcm_s16le"}} 
	binary: pcm bytes chunk 1
	binary: pcm bytes chunk 2
	data = {type:ai_audio_stream,event:outputEnd,detail:null}

输出 错误
data = {type:error,event:api_or_network_error,detail:请求错误}
data = {type:error,event:out_of_tokens,detail:token超出限制}

输出 渲染
data = {type:render,event:thinking,detail:null} // 只要请求llm api就触发
data = {type:render,event:tool_purpose,detail:我需要...}
data = {type:render,event:tool_args,detail:{工具名：{参数:值}}}
data = {type:render,event:tool_result,detail:工具执行结果}



v0  - in:非流式user语音输入    out:非流式ai语音输出
v1  - in:非流式user语音输入    out:1非流式user文本回传 2非流式ai语音输出 3非流式ai文本输出
v2  - in:非流式user语音输入    out:1非流式user文本回传 2非流式ai语音输出 3流式ai文本输出
v3  - in:流式user语音输入    out:1非流式user文本回传 2流式ai语音输出 3流式ai文本输出
'''
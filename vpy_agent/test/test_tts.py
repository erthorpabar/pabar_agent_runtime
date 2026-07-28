# type
import base64
# request
import requests 
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

TTS_URL = settings.TTS_URL
ASR_URL = settings.ASR_URL

TTS_KEY = settings.TTS_KEY
ASR_KEY = settings.ASR_KEY



# 同步
# def tts(text: str) -> bytes:
#     headers = {
#         "Authorization": f"Bearer {TTS_KEY}",
#         "Content-Type": "application/json",
#     }
#     payload = {
#         "model": "glm-tts",  
#         "input": text,
#         "voice": "tongtong",   # 音色：彤彤
#         "response_format": "wav", # 输出格式：wav pcm
#         "stream": False,         # 是否流式返回：False
#         "speed": 1.0,          # 语速：1.0
#         "volume": 1.0,         # 音量：1.0
#         "watermark_enabled": False, # 是否加水印：False
#     }
#     response = requests.post(TTS_URL, headers=headers, json=payload)
#     print(response)


#     response.raise_for_status() # 非 2xx 正常状态 时抛出异常
#     ct = (response.headers.get("Content-Type") or "").lower() # 响应头里的 Content-Type 取出来并转成小写，用来判断 body 是 JSON 文本 还是 原始二进制
#     if "application/json" in ct: # 若是 JSON
#         data = response.json() # 解析 JSON
#         if isinstance(data, str):
#             return base64.b64decode(data) # bytes
#         if isinstance(data, dict) and "data" in data:
#             return base64.b64decode(data["data"]) # bytes
#         raise RuntimeError(f"unknown data: {data!r}") # 抛出明确错误
#     return response.content # bytes
















# 异步
async def as_tts(session,text):
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
        ct = (response.headers.get("Content-Type") or "").lower()
        if "application/json" in ct: # 说明 正文是json
            body = await response.json()
            if isinstance(body, str): # base64 字符串
                return base64.b64decode(body) # 解码 并返回
            if isinstance(body, dict) and "data" in body: # 字典 且 有 data 字段
                return base64.b64decode(body["data"]) # 解码 并返回
            raise RuntimeError(f"unknown data: {body!r}") # 是json 但不是 base64 字符串 也不是 字典 且 有 data 字段
        return await response.read() # 说明 正文是二进制


# ——————————异步库(可多线程 可多协程)——————————
import asyncio
# ——————————多协程http请求库——————————
import aiohttp

text = "已经为你打开浏览器搜索 **web3** 了！🎉 有其他需要随时说～"
async def aio_tts():
    async with aiohttp.ClientSession() as session:
        return await as_tts(session, text)


wav = asyncio.run(aio_tts())
with open("output.wav", "wb") as f:
    f.write(wav)

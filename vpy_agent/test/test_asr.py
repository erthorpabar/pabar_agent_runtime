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
# def asr(wav: bytes) -> str:
#     headers = {
#         "Authorization": f"Bearer {ASR_KEY}",
#     }
#     data = {
#         "model": "glm-asr-2512",
#         "stream": "false",
#     }
#     files = {
#         "file": ("audio.wav", wav, "audio/wav"),
#     }
#     response = requests.post(ASR_URL, headers=headers, data=data, files=files)
#     response.raise_for_status()
#     ct = (response.headers.get("Content-Type") or "").lower()
#     if "application/json" in ct: # 说明 正文是json
#         data = response.json()
#         return data.get("text", "") # 返回 text
#     return response.content.decode("utf-8") # 说明 正文是二进制 返回 str


# 异步
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


# ——————————异步库(可多线程 可多协程)——————————
import asyncio
# ——————————多协程http请求库——————————
import aiohttp


async def aio_asr():
    with open("output.wav", "rb") as f:
        wav = f.read()
    async with aiohttp.ClientSession() as session:
        return await as_asr(session, wav)


transcript = asyncio.run(aio_asr())
print(transcript)

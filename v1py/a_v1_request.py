# os
import os

# llm client
from anthropic import Anthropic

# env
from dotenv import load_dotenv
load_dotenv(override=True)

# 1 env
# 如果自定义了api_url 则移除默认的 认证token 允许使用第三方兼容anthropic的服务
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

base_url = os.getenv("ANTHROPIC_BASE_URL")
api_key = os.getenv("ANTHROPIC_API_KEY")
model = os.getenv("ANTHROPIC_MODEL")
print("model:", model)

# 2 llm client
client = Anthropic(base_url=base_url, api_key=api_key)

# 3 system prompt
system_prompt = "You are a helpful assistant."

# 4 user input
user_input = "你好你是谁"

# 5 history
messages = []
messages.append({"role": "user", "content": user_input})

# 6 llm request
res = client.messages.create(model=model,system=system_prompt,messages=messages,max_tokens=8000,)

print(res)
print(res.content[0].text) # 回答
print(res.usage.input_tokens) # 输入token
print(res.usage.output_tokens) # 输出token



''' 
停止符号
stop_reason
1. end_turn 正常回复结束
2. tool_use 调用工具
3. max_tokens 超出token限制
'''


''' 
res 数据格式

Message(
    id='msg_20260303152858202b19b0467244fa', 
    container=None, 
    content=[
        TextBlock(
            citations=None, 
            text='我是Z.ai训练的大型语言模型。', 
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
        cache_read_input_tokens=0, 
        inference_geo=None, 
        input_tokens=11, 
        output_tokens=9, 
        server_tool_use=ServerToolUsage(
            web_fetch_requests=None, 
            web_search_requests=0
        ), 
        service_tier='standard'
    )
)

'''
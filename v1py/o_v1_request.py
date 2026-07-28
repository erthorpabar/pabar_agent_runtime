# os
import os

# openai
from openai import OpenAI

# env
from dotenv import load_dotenv
load_dotenv(override=True)

# 1 env
base_url = os.getenv("OPENAI_BASE_URL")
api_key = os.getenv("OPENAI_API_KEY")
model = os.getenv("OPENAI_MODEL")
print("model:", model)

# 2 llm client
client = OpenAI(base_url=base_url, api_key=api_key)

# 3 system prompt
system_prompt = "You are a helpful assistant."

# 4 user input
user_input = "你好你是谁"

# 5 history
messages = []
messages.append({"role": "system", "content": system_prompt})
messages.append({"role": "user", "content": user_input})

# 6 llm request
res = client.chat.completions.create(model=model,messages=messages,max_tokens=8000,)

print(res)
print(res.choices[0].message.content) # 回答
print(res.choices[0].finish_reason) # 结束原因
print(res.usage.prompt_tokens) # 输入token
print(res.usage.completion_tokens) # 输出token



''' 
停止符号
finish_reason
1. stop 正常回复结束
2. length 超出token限制


reasoning_content = 只有thinking模型有这个字段
'''


''' 
res 数据格式
ChatCompletion(
    id='20260304222541aebc5793d1344d1c', 
    choices=[
        Choice(
            finish_reason='stop', 
            index=0, 
            logprobs=None, 
            message=ChatCompletionMessage(
                content='你好！很高兴见到你。', 
                refusal=None, 
                role='assistant', 
                annotations=None, 
                audio=None, 
                function_call=None, 
                tool_calls=None, 
                reasoning_content='收到用户发来的"你好"这样简单的问候，需要礼貌地回应并引导对话。'
            )
        )
    ], 
    created=1772634346, 
    model='glm-5', 
    object='chat.completion', 
    service_tier=None, 
    system_fingerprint=None, 
    usage=CompletionUsage(
        completion_tokens=128, 
        prompt_tokens=6, 
        total_tokens=134, 
        completion_tokens_details=None, 
        prompt_tokens_details=PromptTokensDetails(
            audio_tokens=None, 
            cached_tokens=0
        )
    ), 
    request_id='20260304222541aebc5793d1344d1c'
)


'''
# os
import os

# openai
from openai import OpenAI

# env
from dotenv import load_dotenv
load_dotenv(override=True)

def chat_loop():

    # 1 env
    base_url = os.getenv("OPENAI_BASE_URL")
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL")
    print("model:", model)

    # 2 llm client
    client = OpenAI(base_url=base_url, api_key=api_key)

    # 3 system prompt
    system_prompt = "You are a helpful assistant."

    # 4 history
    messages = []
    messages.append({"role": "system", "content": system_prompt})

    # 5 chat loop
    ''' 
    chat_loop
    query(str) - (messages+=query) - llm(messages)=out - (messages+=out)
        ↑______________________________________________________↓
    '''

    while True:
        # 1 query
        query = input("You: ")

        # 2 messages+=query
        messages.append({"role": "user", "content": query})

        # 3 llm(messages)=out
        res = client.chat.completions.create(model=model,messages=messages,max_tokens=8000,)

        # 4 messages+=out
        answer = res.choices[0].message.content
        messages.append({"role": "assistant", "content": answer})

        print(f"Ai: {answer}")

if __name__ == "__main__":
    chat_loop()


''' 数据格式
query = '我叫美羊羊'

messages = [
{'content': '我叫美羊羊', 'role': 'user'}
]

res = Message(
    id='msg_20260301165027f8ba16405b074b34', 
    container=None, 
    content=[TextBlock(
        citations=None, 
        text='你好，**美羊羊**！很高兴见到你！', 
        type='text'
    )], 
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
        input_tokens=9, 
        output_tokens=38, 
        server_tool_use=ServerToolUsage(
            web_fetch_requests=None, 
            web_search_requests=0
        ), 
        service_tier='standard'
    )
)

answer =res.content[0].text = '你好，**美羊羊**！很高兴见到你！'

messages = [
{'content': '我叫美羊羊', 'role': 'user'},
{'content': '你好，**美羊羊**！很高兴见到你！', 'role': 'assistant'}
]

'''
package main

import (
	"fmt"     // 打印
	"os"      // 环境变量
	"strings" // 字符串

	"context"   // 上下文
	"os/signal" // 信号
	"syscall"   // 信号

	"net/http" // http
	"time"     // 时间

	"github.com/joho/godotenv" // env

	"github.com/openai/openai-go/v3"        // openai 客户端
	"github.com/openai/openai-go/v3/option" // openai 配置

	"github.com/gin-gonic/gin"
	"github.com/gorilla/websocket"
)

// ===== config =====
type LLMConfig struct {
	BaseURL string
	APIKey  string
	Model   string
	ASRURL  string
	ASRKey  string
	TTSURL  string
	TTSKey  string
}

type ServerConfig struct {
	Host string
	Port string
}


// ===== 消息队列解耦 =====
type MsgHeader struct {
	Channel           string `json:"channel"`
	UserID            string `json:"user_id"`
	SessionID         string `json:"session_id"`
	TraceID           string `json:"trace_id"`
	RequiredOutFormat string `json:"required_out_format"` // text | text_stream
	OptionalOutFormat string `json:"optional_out_format"` // audio | audio_stream
}

type MsgData struct {
	Type   string                 `json:"type"`   // user_text, user_audio, user_audio_stream, stop, asr_return, ai_text, ai_text_stream, ai_audio, ai_audio_stream, error, render
	Event  string                 `json:"event"`  // input, inputStart, inputEnd, output, outputStart, chunk, outputEnd, thinking, api_or_network_error ...
	Detail map[string]interface{} `json:"detail"` // 内容载荷
}

// InMsg 对应前端发来的 JSON
type InMsg struct {
	Header MsgHeader `json:"header"`
	Data   MsgData   `json:"data"`
}
// OutMsg 回给前端的 JSON
type OutMsg struct {
	Header MsgHeader `json:"header"`
	Data   MsgData   `json:"data"`
}


// ===== http upgrader ws =====
var (
	upgrader = websocket.Upgrader{
		HandshakeTimeout: 1 * time.Second,
		ReadBufferSize: 1024,
		WriteBufferSize: 1024,
		CheckOrigin: func(r *http.Request) bool {
			return true // 允许任意来源（对外服务，按需改成白名单）
		},
	}
)

// ===== voice =====
// CallASR 调用 ASR 接口，返回识别出的文本
func CallASR(config LLMConfig, audioData []byte) (string, error) {
	body := &bytes.Buffer{}
	writer := multipart.NewWriter(body)
	
	// 添加文件部分
	part, _ := writer.CreateFormFile("file", "audio.wav")
	part.Write(audioData)
	writer.WriteField("model", "glm-asr-2512")
	writer.WriteField("stream", "false")
	writer.Close()

	req, _ := http.NewRequest("POST", config.ASRURL, body)
	req.Header.Set("Authorization", "Bearer "+config.ASRKey)
	req.Header.Set("Content-Type", writer.FormDataContentType())

	client := &http.Client{}
	resp, err := client.Do(req)
	if err != nil { return "", err }
	defer resp.Body.Close()

	// 简单处理：解析 JSON 返回 text
	var result map[string]interface{}
	json.NewDecoder(resp.Body).Decode(&result)
	return result["text"].(string), nil
}

// CallTTS 调用 TTS 接口，返回音频二进制数据
func CallTTS(config LLMConfig, text string) ([]byte, error) {
	payload := map[string]interface{}{
		"model":           "glm-tts",
		"input":           text,
		"voice":           "tongtong",
		"response_format": "wav",
		"stream":          false,
	}
	jsonData, _ := json.Marshal(payload)

	req, _ := http.NewRequest("POST", config.TTSURL, bytes.NewBuffer(jsonData))
	req.Header.Set("Authorization", "Bearer "+config.TTSKey)
	req.Header.Set("Content-Type", "application/json")

	client := &http.Client{}
	resp, err := client.Do(req)
	if err != nil { return nil, err }
	defer resp.Body.Close()

	// 判断返回是 JSON (含base64) 还是 原始二进制
	// 需根据实际响应 Content-Type 判断，这里假设为 base64 字符串处理
	body, _ := io.ReadAll(resp.Body)
	// 此处省略：根据 contentType 增加 base64 解码逻辑
	return body, nil
}

func main() {
	// 1 env
	err := godotenv.Load() // 成功时err == nil
	if err != nil {
		fmt.Println("未找到 .env", err)
	}

	// 赋值
	// llm
	llmConfig := LLMConfig{
		BaseURL: strings.TrimSpace(os.Getenv("OPENAI_BASE_URL")),
		APIKey:  strings.TrimSpace(os.Getenv("OPENAI_API_KEY")),
		Model:   strings.TrimSpace(os.Getenv("OPENAI_MODEL")),

		ASRURL: strings.TrimSpace(os.Getenv("ASR_URL")),
		ASRKey: strings.TrimSpace(os.Getenv("ASR_KEY")),
		TTSURL: strings.TrimSpace(os.Getenv("TTS_URL")),
		TTSKey: strings.TrimSpace(os.Getenv("TTS_KEY")),
	}
	// server
	serverConfig := ServerConfig{
		Host: strings.TrimSpace(os.Getenv("SERVER_HOST")),
		Port: strings.TrimSpace(os.Getenv("SERVER_PORT")),
	}
	if serverConfig.Host == "" { serverConfig.Host = "0.0.0.0" }
	if serverConfig.Port == "" { serverConfig.Port = "9999" }

	// 打印状态
	fmt.Println("model:", llmConfig.Model)
	fmt.Println("server:", serverConfig.Host, ":", serverConfig.Port)

	// 2 ctx
	/*
		代码内部必须主动检查ctx

			if 没有收到中断信号
				pass
			if 收到中断信号
				ctrl + c  - os.Interrupt
				kill -9  - syscall.SIGTERM
	*/
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop() // 函数执行完毕时 触发

	// 3 llm client
	client := openai.NewClient(
		option.WithBaseURL(llmConfig.BaseURL),
		option.WithAPIKey(llmConfig.APIKey),
	)

	// 4 gin
	// 初始化
	gin.SetMode(gin.ReleaseMode) // gin线上发布模式
	engine := gin.Default()      // 创建一个gin引擎

	// 全局中间件
	engine.Use(gin.Logger())   // 记录请求日志
	engine.Use(gin.Recovery()) // 恢复请求

	// route
	// 测试网页
	engine.GET("/", func(c *gin.Context) {c.File("test.html")})
	// ws
	engine.GET("/ws", WsConnect(ctx, &client, llmConfig))

	// 5 run server
	srv := &http.Server{
		Addr:    serverConfig.Host + ":" + serverConfig.Port,
		Handler: engine,
	}
	go func() { _ = srv.ListenAndServe() }()
	fmt.Println("server 启动成功")

	// 6 阻塞等待中断信号
	<-ctx.Done()
	fmt.Println("server 正在关闭")
	_ = srv.Shutdown(context.Background())
	fmt.Println("server 已经关闭")

	// // 7 关闭 给最后5s收尾
	// // 服务的ctx
	// shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	// defer cancel()
	// if err := srv.Shutdown(shutdownCtx); err != nil {
	// 	fmt.Println("server 关闭失败:", err)
	// }
	// fmt.Println("server 已经退出")
}

// ===== ws 连接处理 =====
// WsConnect 返回一个 gin.HandlerFunc：每来一个连接，就单独起一套收发队列 + 三个协程
func WsConnect(ctx context.Context, client *openai.Client, llmConfig LLMConfig) gin.HandlerFunc {
	return func(c *gin.Context) {
		// 1 http 升级为 websocket
		conn, err := upgrader.Upgrade(c.Writer, c.Request, nil)
		if err != nil {
			fmt.Println("[ws] upgrade error:", err)
			return
		}
		fmt.Println("[ws] a new connection")
		defer conn.Close()

		// 2 避免多余的请求时间
		/*
		如果用户在llm请求时 关闭ws连接 那么随着ws关闭触发 <-WsCtx.Done() 从而让llm请求关闭 而不用等待它执行完
		*/ 
		WsCtx, WsCancel := context.WithCancel(ctx)
		defer WsCancel()

		// 3 每个连接独立的收发队列（连接之间互不影响）
		inCh := make(chan InMsg, 16)   // 输入队列
		outCh := make(chan OutMsg, 16) // 输出队列

		// 4 后台协程
		go ReceiveMsg(WsCancel,conn, inCh) // ws -> inCh
		go AgentLoop(WsCtx, client, llmConfig, inCh, outCh) // inCh -> llm -> outCh

		// 5 前台协程
		BroadcastMsg(WsCancel,conn, outCh) // outCh -> ws
	}
}

// ===== 协程循环函数 =====
// ReceiveMsg 不断从 ws 读取客户端 JSON，丢进 inCh
func ReceiveMsg(WsCancel context.CancelFunc, conn *websocket.Conn, inCh chan<- InMsg) {
	defer close(inCh) // 此函数关闭 同时会关闭 下游 inCh 队列
	defer WsCancel()

	for {
		// 1 读取一条客户端消息（json）
		var inMsg InMsg
		if err := conn.ReadJSON(&inMsg); err != nil {
			return // 连接关闭 / 读错 / 非法 json，退出
		}

		// 2 清洗
		inMsg.Content = strings.TrimSpace(inMsg.Content) // 去除首尾空格
		if inMsg.Content == "" {
			continue // 空消息 不请求 llm
		}

		// 3 放入队列
		inCh <- inMsg

		
	}
}

func AgentLoop(ctx context.Context, client *openai.Client, llmConfig LLMConfig, inCh <-chan InMsg, outCh chan<- OutMsg) {
	defer close(outCh) // 此函数关闭 同时会关闭 下游 outCh 队列

	// 1 system prompt
	systemPrompt := "You are a helpful assistant."

	// 2 history
	messages := []openai.ChatCompletionMessageParamUnion{}
	// 如果有system prompt，则添加到messages
	if systemPrompt != "" {
		messages = append(messages, openai.SystemMessage(systemPrompt))
	}

	// 3 chat loop
	// 持续从 上游 inCh 获取数据
	/*
	chat_loop
    query(str) - (messages+=query) - llm(messages)=out - (messages+=out)
        ↑______________________________________________________↓
	*/ 
	for msg := range inCh {
		// 1 user input
		userInput := msg.Content
		// 2 messages+=query
		messages = append(messages, openai.UserMessage(userInput))
		// 3 llm(messages)=out
		res, err := client.Chat.Completions.New(ctx, openai.ChatCompletionNewParams{
			Model:    llmConfig.Model,
			Messages: messages,
		})
		if err != nil {
			messages = messages[:len(messages)-1]                  // 回滚消息
			outCh <- OutMsg{Type: "error", Error: err.Error()} // 发送错误消息
			continue                                               // 继续监听
		}

		// 4 messages+=out
		answer := res.Choices[0].Message.Content
		messages = append(messages, openai.AssistantMessage(answer))

		// 5 数据格式
		m := OutMsg{Type: "message", Content: answer}

		// 6 放入队列
		outCh <- m
	}
}

// BroadcastMsg 不断从 outCh 取回复，写回当前 ws 连接
func BroadcastMsg(WsCancel context.CancelFunc, conn *websocket.Conn, outCh <-chan OutMsg) {
	defer WsCancel()
	// 持续从 上游 outCh 获取数据
	for msg := range outCh {
		// outCh -> client
		err := conn.WriteJSON(msg) 

		if err != nil {
			return // 写失败（连接已断），退出
		}
	}
}

/*
go mod init chat
go mod tidy
go run .


go get github.com/joho/godotenv
go get github.com/openai/openai-go/v3
go get github.com/openai/openai-go/v3/option

go get github.com/gin-gonic/gin
go get github.com/gorilla/websocket

*/



/*
业务流程(每个 ws 对应三个协程 两个队列)
WebSocket 连接
	│
	├── ReceiveMsg()  ──→ inCh
	│                     │
	│                     ▼
	│                 AgentLoop()
	│                     │
	│                     ▼
	└── BroadcastMsg() ←── outCh
*/


/*
依赖关系
main
	- ws(循环)
		后台协程
		- receiveMsg(循环)
		- agentLoop(循环)
		前台协程 -> 确保循环不退出
		- broadcastMsg(循环)
*/ 



/*
如何防止内存泄漏

极端事件
一个llm请求需要等待10s 但用户在llm推理进行到2s时 关闭了网页 导致ws连接断开 从而传递ws关闭信号

链式关闭 + ws层级ctx
触发源 - 用户关闭网页 导致ws连接断开
receive(ws_cancel,ws)
	defer close(a) -> 触发链式关闭 清除协程和队列
	defer ws_cancel() -> 瞬时触发 ws_ctx 通知llm请求立刻中断 防止真的等10s

loop(ws_ctx)
	defer close(b) -> 循环退出 则关闭下游 链式关闭传递
	for msg := range a{} // 若a关闭 循环自动退出

broadcast(ws_cancel,ws)
	defer ws_cancel() -> 循环退出 则关闭下游 链式关闭传递
	for msg := range b{} // 若b关闭 循环自动退出
*/ 






/*
消息会话隔离
	user_id
	session_id

流式传输
	[LLM API] --HTTP非流式--> [server] --WS--> [client] 
	[LLM API] --SSE/HTTP流--> [server] --WS--> [client] 

	如果有tool 中间过程复用 则api不用流式
	如果仅对话 则 极致优化首字延迟 使用SSE/HTTP流 

*/ 





/*
ws 能传输两种数据格式
	1 text 文本
	2 Binary 二进制

*/ 


/*
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

*/ 



/*
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

		type：error [错误信息]
		type：render [渲染信息]

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
*/ 
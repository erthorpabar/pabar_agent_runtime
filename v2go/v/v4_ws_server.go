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
}

type ServerConfig struct {
	Host string
	Port string
}

// ===== 消息队列解耦 =====
// InMsg 对应前端发来的 JSON（见 test.html send()）
type InMsg struct {
	Channel   string `json:"channel"`
	UserID    string `json:"user_id"`
	SessionID string `json:"session_id"`
	Content   string `json:"content"`
	InType    string `json:"in_type"`
	OutType   string `json:"out_type"`
}

// OutMsg 回给前端的 JSON（见 test.html onmessage）
type OutMsg struct {
	Type    string `json:"type"`              // "message" | "error"
	Content string `json:"content,omitempty"` // ai 回复
	Error   string `json:"error,omitempty"`   // 错误描述
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

func main() {
	// 1 env
	err := godotenv.Load() // 成功时err == nil
	if err != nil {
		fmt.Println("未找到 .env", err)
	}

	// 赋值
	llmConfig := LLMConfig{
		BaseURL: strings.TrimSpace(os.Getenv("OPENAI_BASE_URL")),
		APIKey:  strings.TrimSpace(os.Getenv("OPENAI_API_KEY")),
		Model:   strings.TrimSpace(os.Getenv("OPENAI_MODEL")),
	}
	//
	serverConfig := ServerConfig{
		Host: strings.TrimSpace(os.Getenv("SERVER_HOST")),
		Port: strings.TrimSpace(os.Getenv("SERVER_PORT")),
	}
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
	// ws：每个连接交给 handleWebSocket 处理
	engine.GET("/ws", WsConnect(ctx, &client, llmConfig))

	// 5 run server
	srv := &http.Server{
		Addr:    serverConfig.Host + ":" + serverConfig.Port,
		Handler: engine,
	}
	// 开启一个协程跑 主线程给信号监听 + 关闭
	go func() {
		err := srv.ListenAndServe()
		if err != nil && err != http.ErrServerClosed {
			fmt.Println("server 启动失败:", err)
		} 
	}()
	fmt.Println("server 启动成功")

	

	// 6 阻塞等待中断信号
	<-ctx.Done()
	fmt.Println("server 收到关闭信号，正在关闭 ...")

	// 7 关闭 给最后5s收尾
	// 服务的ctx
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		fmt.Println("server 关闭失败:", err)
	}
	fmt.Println("server 服务已退出")
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
		defer conn.Close()
		fmt.Println("[ws] a new connection")

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

	ws
		text 流式
		audio 流式

打断机制
	1 如果正在请求llm 则中断请求
	2 如果正在通过ws流式打印 那么停止输入 但历史记录正常记录


语音交互
	[pcm/opus] -> [asr] -> [llm] -> [tts] -> [pcm/opus]
	其中需要传给前端的内容有：
		[asr] -> text 回传前端聊天框
		[llm] -> text 回传前端聊天框
		[tts] -> audio 边流式打印文字 边播放语音
*/ 





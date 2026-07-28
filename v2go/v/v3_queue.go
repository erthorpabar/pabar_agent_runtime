package main

import (
	"bufio" // 读取输入
	"io"      // 输入输出接口

	"sync" // 等待组 同步等待多个goroutine完成

	"fmt" // 打印
	"os" // 环境变量
	"strings" // 字符串

	"context" // 上下文
	"os/signal" // 信号
	"syscall" // 信号

	"github.com/joho/godotenv" // env

	"github.com/openai/openai-go/v3" // openai 客户端
	"github.com/openai/openai-go/v3/option" // openai 配置

)

// ===== config =====
type LLMConfig struct {
	BaseURL string
	APIKey string
	Model string
}

// ===== 消息队列解耦 =====
type InMsg struct {
	Content string // user 输入
}

type OutMsg struct {
	Content string // ai 回复
	Error  error  // 有错误时携带，无错误为 nil
}


func main() {
	// 1 env
	err := godotenv.Load() // 成功时err == nil
	if err != nil {
		fmt.Println("未找到 .env", err)
	}

	// 赋值
	llmConfig := LLMConfig{
		BaseURL:          strings.TrimSpace(os.Getenv("OPENAI_BASE_URL")),
		APIKey:           strings.TrimSpace(os.Getenv("OPENAI_API_KEY")),
		Model:            strings.TrimSpace(os.Getenv("OPENAI_MODEL")),
	}
	// 打印状态
	fmt.Println("model:",llmConfig.Model)



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


	// 4 收发消息的队列
	inCh := make(chan InMsg,16)  // 输入队列
	outCh := make(chan OutMsg,16) // 输出队列

	// 5 后台线程
	// 三个协程 循环从队列里取消息
	var wg sync.WaitGroup // 创建一个等待组 等待多个goroutine完成 
	wg.Add(3)
	go func() {defer wg.Done(); RecieveMsg(os.Stdin, inCh)}()
	go func() {defer wg.Done(); AgentLoop(ctx,&client,llmConfig,inCh, outCh)}()
	go func() {defer wg.Done(); BroadcastMsg(os.Stdout, outCh)}()
	
	// 6 前台线程
	wg.Wait() // 一直阻塞，直到 3 个协程都 Done

	/*
	wg.Add(n) 添加n个任务要等
	wg.Done() 任务完成 计数-1
	wg.Wait() 阻塞 直到计数=0 然后退出
	*/

}

// ===== 协程循环函数 =====
func RecieveMsg(r io.Reader, inCh chan<- InMsg) {
	defer close(inCh) // 此函数关闭 同时会关闭 下游 inCh 队列

	reader := bufio.NewReader(r) // 读取输入

	for {
		// 1 query
		userInput, err := reader.ReadString('\n') // 一直读取用户输入 直到按下回车
		userInput = strings.TrimSpace(userInput)  // 去除首位空格和换行
		if err != nil {
			return // EOF 或读错 退出
		}
		if userInput == "" {
			continue // 如果为空 继续监听 不请求llm
		}

		// 2 数据格式
		inMsg := InMsg{Content: userInput}

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
			messages = messages[:len(messages)-1] // 回滚消息
			outCh <- OutMsg{Error: err} // 发送错误消息
			continue // 继续监听
		}

		// 4 messages+=out
		answer := res.Choices[0].Message.Content
		messages = append(messages, openai.AssistantMessage(answer))

		// 5 数据格式
		outMsg := OutMsg{Content: answer}

		// 6 放入队列
		outCh <- outMsg
	}
}


func BroadcastMsg(w io.Writer, outCh <-chan OutMsg) {
	// 持续从 上游 outCh 获取数据
	for msg := range outCh {
		// 1 如果收到 报错消息
		if msg.Error != nil {
			fmt.Fprintln(w, "请求失败:", msg.Error)
			continue // 继续监听
		}

		// 2 广播消息
		fmt.Fprintln(w, msg.Content)
	}
}



/*
go mod init chat
go mod tidy
go run .


go get github.com/joho/godotenv
go get github.com/openai/openai-go/v3
go get github.com/openai/openai-go/v3/option

*/ 
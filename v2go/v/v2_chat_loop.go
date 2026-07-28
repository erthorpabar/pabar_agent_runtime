package main

import (
	"bufio" // 读取输入

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

	// 4 system prompt
	systemPrompt := "You are a helpful assistant."


	// 5 user input
	userInput := "你好你是谁"

	// 6 history
	messages := []openai.ChatCompletionMessageParamUnion{
		openai.SystemMessage(systemPrompt),
		openai.UserMessage(userInput),
	}

	// 7 chat loop
	/*
	chat_loop
    query(str) - (messages+=query) - llm(messages)=out - (messages+=out)
        ↑______________________________________________________↓

	*/ 

	reader := bufio.NewReader(os.Stdin) // 读取输入
	for {

		// 1 query
		fmt.Print("You:")
		userInput, _ := reader.ReadString('\n')  // 一直读取用户输入 直到按下回车
		userInput = strings.TrimSpace(userInput) // 去除首位空格和换行
		if userInput == "" {
			continue // 如果为空 继续监听 不请求llm
		}

		// 2 messages+=query
		messages = append(messages, openai.UserMessage(userInput))

		// 3 llm(messages)=out
		res, err := client.Chat.Completions.New(ctx, openai.ChatCompletionNewParams{
			Model:     llmConfig.Model,
			Messages: messages,
			MaxTokens: openai.Int(8000),
		})
		if err != nil {
			fmt.Println("请求失败:", err)
		}

		// 4 messages+=out
		answer := res.Choices[0].Message.Content
		messages = append(messages, openai.AssistantMessage(answer))
		fmt.Println("Ai:" + answer)
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
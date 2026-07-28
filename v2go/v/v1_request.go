package main

import (
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

	// 7 llm request
	res, err := client.Chat.Completions.New(ctx, openai.ChatCompletionNewParams{
		Model:     llmConfig.Model,
		Messages: messages,
		MaxTokens: openai.Int(8000),
	})
	if err != nil {
		fmt.Println("请求失败:", err)
	}

	fmt.Println("回复:", res.Choices[0].Message.Content)


}



/*
go mod init chat
go mod tidy
go run .


go get github.com/joho/godotenv
go get github.com/openai/openai-go/v3
go get github.com/openai/openai-go/v3/option

*/ 
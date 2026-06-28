# VS Bridge

**微信遥控 VS Code 克劳德。**

不碰API，不碰协议。opencv图像识别 + 转录文件读取，窗口随便拖，VS Code Claude 随便更新。

## 做什么

你在微信里说一句话 → 自动注入 VS Code Claude → 克劳德回复 → 自动读转录文件 → 微信通知你。

上班摸鱼时远程调克劳德干活，下班到家代码已经写好了。

## 原理

```
微信 → OpenClaw → 命令文件 → opencv识别输入框/发送按钮 → 克劳德工作 → 读转录JSONL → 提取回复 → OpenClaw CLI → 微信
```

- 图像识别：opencv模板匹配，4张模板图定位输入框和发送按钮
- 状态检测：识别"工作中"和"工作完"两个图标
- 结果提取：直接读 Claude 转录 JSONL 文件，100%准确，不依赖 OCR
- 微信通道：OpenClaw CLI 直发，不烧大模型 token

## 与同类项目的区别

| | 其他方案 | vs_bridge |
|---|---|---|
| 连接方式 | iLink/MCP 协议 | 图像识别 + 文件读取 |
| 依赖 | Claude API 接口 | 零 API 依赖 |
| 克劳德更新 | 协议变了就挂 | 截一张新模板图，十分钟修好 |
| 支持 VS Code Claude | ❌ 只连 CLI 版 | ✅ 完整 IDE 上下文 |

## 快速开始

### 1. 依赖

```bash
pip install pyautogui opencv-python pyperclip Pillow
```

### 2. 截图模板

用截图工具截4张图，放到脚本同目录：

| 文件 | 内容 | 大小参考 |
|------|------|----------|
| `工作中.png` | 克劳德生成回复时的图标 | 27×28 |
| `工作完·待输入文本.png` | 克劳德回复完毕的图标 | 27×28 |
| `文本输入框.png` | VS Code Claude 聊天输入区 | ~687×93 |
| `发送按钮.png` | 发送按钮 | 30×30 |

### 3. 配置 OpenClaw

编辑 `send_to_wechat()` 函数中的路径：

```python
NODE_EXE = r"F:\Node.js\node.exe"
OPENCLAW_JS = r"F:\node_global\node_modules\openclaw\dist\index.js"
TARGET = "你的微信ID@im.wechat"
```

### 4. 运行

```bash
# 启动监控
python vs_bridge.py

# 查看状态
python vs_bridge.py --status

# 发送消息
python vs_bridge.py --send "你的消息"

# 异步发送（主循环运行时）
echo "你的消息" > vs_bridge_command.txt
```

### 5. 小龙虾集成

OpenClaw 小龙虾可通过命令文件调度：

```bash
echo "克劳德，检查XXX" > E:/zhuomian/vs_bridge_command.txt
```

vs_bridge 3~5秒内自动读取执行，完成后自动通知微信。

## 许可

MIT

# 工程计划截图识别

一个极简的本地网页工具：上传工程计划表格截图，由 Flask 后端调用 DeepSeek 官方视觉模型，按楼栋合并「今日计划 / 明日计划」，返回可直接复制的纯文本。

- 前端：单文件 `index.html`，无框架、无构建步骤，可直接双击打开。
- 后端：Flask，负责隐藏 API Key、校验图片、转发 DeepSeek API、统一输出格式。
- 图片：前端转为 Base64 data URL 后发送；API Key 永远不会进入浏览器。
- 图片预处理：Pillow 检测宽高，宽图自动上下裁成两张重叠图片，提升 API 缩放后的文字细节。
- 模型：`deepseek-flash`（官方视觉模型），使用 OpenAI 兼容的 Chat Completions 格式。

## 项目文件

```text
.
├─ index.html          # 单文件前端（HTML + CSS + JS）
├─ app.py              # Flask 后端
├─ requirements.txt    # Python 依赖
├─ start_app.bat       # Windows 一键启动脚本
├─ sample-table.png    # 测试用工程表格截图
├─ .env.example        # 环境变量模板
├─ .gitignore
└─ README.md
```

## 一键启动（Windows）

首次使用仍需完成下面的「安装依赖」和「填写 .env」。之后直接双击：

```text
start_app.bat
```

脚本会自动：

1. 切换到项目目录。
2. 优先使用 `.venv\Scripts\python.exe`，没有则使用系统 `python`。
3. 检查 `.env` 和 `app.py` 是否存在。
4. 如果后端已经在运行，直接打开浏览器。
5. 否则在当前窗口启动 `app.py`，健康检查通过后自动打开 <http://127.0.0.1:5000>。

后端日志会留在当前命令行窗口，关闭该窗口或按 `Ctrl+C` 即可停止服务。如果改了 `PORT`，需要同步修改 `start_app.bat` 里的 `5000`。

如果要手动重建这个脚本，用记事本粘贴代码后另存为 `start_app.bat`，编码选 **UTF-8**，换行使用 **CRLF**（Windows 记事本默认就是 CRLF）。不要保存成 `start_app.bat.txt`。

## 1. 准备 API Key

到 [DeepSeek 开放平台](https://platform.deepseek.com/api_keys) 创建 API Key。

## 2. 安装并启动后端（Windows PowerShell）

在项目目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

把 `.env` 里的这一行换成你的真实 Key：

```dotenv
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxx
```

保存后启动：

```powershell
.\.venv\Scripts\python.exe app.py
```

看到类似 `Running on http://127.0.0.1:5000` 即启动成功。

> 如果 PowerShell 找不到 `python`，把命令中的 `python` 换成 `py`，或安装 Python 3.10+ 后重试。
>
> `requirements.txt` 已包含 `tzdata` 和 `Pillow`。Windows 的 Python 通常不带 IANA 时区库，缺少 `tzdata` 时 `Asia/Shanghai` 会解析失败；本项目同时做了 UTC+8 兜底。`Pillow` 用于检测图片尺寸和裁切宽图，缺少它会直接无法启动。
>
> 如果之前已经装过依赖，本次更新后需要重新执行一次：
>
> ```powershell
> .\.venv\Scripts\python.exe -m pip install -r requirements.txt
> ```

## 3. 修改代码后重启

`python app.py` 不是热更新服务。修改 `app.py` 后，必须在运行后端的终端按 `Ctrl+C` 停掉旧进程，再重新执行：

```powershell
.\.venv\Scripts\python.exe app.py
```

启动后访问 <http://127.0.0.1:5000/api/health>，应能看到：

```json
{
  "ok": true,
  "model": "deepseek-flash",
  "thinking": "disabled",
  "max_output_tokens": 8192,
  "debug_upstream": true,
  "image_split_width": 2000
}
```

如果看不到 `thinking: disabled`，说明浏览器连到的还是旧后端进程。

## 4. 打开页面

推荐直接访问 Flask 提供的页面：

**<http://127.0.0.1:5000>**

也可以在后端保持运行的情况下，直接双击 `index.html`。此时页面会自动请求 `http://127.0.0.1:5000/api/recognize`，后端已配置 CORS 以支持 `file://` 打开方式。

上传表格截图后，页面会显示「正在识别...」，完成后在下方文本框展示纯文本结果，可一键复制。第一次运行可以直接上传项目里的 `sample-table.png` 验证整个流程。

## 5. 日期怎么来

第一行「今日计划 YYYY/MM/DD」默认使用服务器当前日期（时区 `Asia/Shanghai`）。

如果需要固定为某一天，在 `.env` 中取消注释并设置：

```dotenv
REPORT_DATE=2026/11/10
```

修改 `.env` 后需要重启 `app.py`。设置 `REPORT_DATE` 后，模型收到的格式要求就是 `今日计划 2026/11/10`，不会自行改用图片中的其他日期。

## 输出规则

后端 Prompt 强制模型按以下规则处理：

1. **先按楼栋视觉分组，再提取内容**。楼栋合并单元格覆盖的连续行都属于同一栋；下方因合并而空白的楼栋行，不能当成新楼栋。
2. 只读取「楼栋」「今日计划」「紧邻今日计划的人员数量」「明日计划」「紧邻明日计划的人员数量」这 5 列。
3. 只有背景色、边框或空白，没有任何可读文字的行，跳过。
4. 一整行 5 个字段全部为空或全部是「/」时，跳过整行。
5. 「/」按空值处理，不能当成 0，也不能导致后面的列左移、人数错位。某栋今日计划有效、明日计划是「/」时，保留今日，明日部分不添加该栋；反过来同理。
6. 同一楼栋的多行计划合并为一行，但**每一行表格的任务必须和该行的人员数量成对出现**，不能把所有任务先写完、再把人数堆到最后。
7. 每栋楼一行的正确格式为：

   ```text
   楼栋，第1行任务，第1行人员，第2行任务，第2行人员，……
   ```

   例如：

   ```text
   1号楼，任务A，2人，任务B，6人，任务C，2人
   ```

   禁止输出：

   ```text
   1号楼，任务A，任务B，任务C，2人，6人，2人
   ```

8. 同一行表格内有多个计划但只有一个人员数量时，先用顿号把计划连成一个任务段，再配该行人员数量：

   ```text
   1号楼，任务A、任务B，8人，任务C，3人
   ```

9. **人员数量列的两种内容都要支持**：
   - 纯数字：`8`、`8人`、`8 人`、`8名`，最终输出 `8人`。
   - 文字/机械：`一台260挖机`、`装载机一台`、`2人+1台挖机` 必须原样保留，不要加「人」，不要只提取数字，也不要写「人数待核对」。
10. 不做跨行求和。每个任务只对应自己那一行的人数或机械内容。
11. 今日与明日分别处理，不混加。
12. 后端会在最终拼接时统一加序号，所以输出严格为：

   ```text
   今日计划 2026/11/10

   1. 1号楼，外墙抹灰，5人，支模板，3人，钢筋绑扎，4人
   2. 2号楼，钢筋绑扎，一台260挖机
   3. 3号楼，混凝土浇筑，2人+1台挖机
   明日计划

   1. 1号楼，内墙抹灰，4人，材料转运，2人，混凝土浇筑，3人
   2. 2号楼，材料转运，装载机一台
   3. 3号楼，材料转运，8人
   ```

只有某栋楼该部分完全没有任何可读的人数或机械内容时，才会写「人数待核对」，避免编造。后端会把纯数字结尾自动补成「X人」，同时放宽了结尾校验，允许「台」「辆」等量词、机械名称、`2人+1台挖机` 这类混合文本；`1.`、`2.` 等序号由 Python 在最终字符串中统一添加，不依赖模型。

## 复杂表格处理

### 楼栋不会强行合并

Prompt 明确规定：即使多行属于同一个大标段、同一个合并表头，只要最左侧「楼栋」列的文字不同，就必须分开输出。

例如：

```text
污泥脱水机房
粗格栅
```

会被识别为两个独立楼栋。只有楼栋单元格因纵向合并而显示为空白时，空白行才继承上一个楼栋；一旦出现新的楼栋文字，立即开始新组。

### 百分比识别

Prompt 会特别要求模型逐字辨认：

- `90%` 不能看成 `30%`
- `80%` 不能看成 `30%`
- `60%` 不能看成 `80%`
- 区分 `9/3`、`8/3`、`6/0`、`5/6`

百分比必须连同 `%` 原样保留在任务文本中。

### 宽图自动裁切

`app.py` 使用 Pillow 读取图片尺寸：

- 宽度超过 `IMAGE_SPLIT_WIDTH`（默认 2000），且高度不低于 `IMAGE_SPLIT_MIN_HEIGHT`（默认 600）时，自动裁成上下两张有重叠的图片。
- 两张图会放在同一次 DeepSeek 请求中，Prompt 会说明它们是同一张表的上、下半部分，重叠区域只算一次。
- 裁切后每一半在 API 缩放时能保留更多文字细节。单纯放大原图不会增加信息，因为 API 仍会把图片缩放到约 1300×1300 的总像素规模。
- 特别高的图片（高度超过 3000）也会自动拆分。
- 终端会打印每张预处理图片的尺寸、编码大小和是否是裁切图。

如果发现裁切位置不理想，可以调整 `.env`：

```dotenv
IMAGE_SPLIT_WIDTH=2000
IMAGE_SPLIT_MIN_HEIGHT=600
IMAGE_SPLIT_OVERLAP=0.08
```

## 配置项

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 无 | 必填，DeepSeek API Key |
| `DEEPSEEK_MODEL` | `deepseek-flash` | 官方视觉模型 |
| `DEEPSEEK_API_URL` | `https://api.deepseek.com/chat/completions` | 官方 Chat Completions 端点 |
| `REPORT_DATE` | 空 | 固定报表日期，格式 `YYYY/MM/DD` |
| `REPORT_TIMEZONE` | `Asia/Shanghai` | 未设置 `REPORT_DATE` 时使用的时区 |
| `HOST` | `127.0.0.1` | 监听地址；默认只允许本机访问 |
| `PORT` | `5000` | 后端端口；若改动，前端的 `API_BASE` 也要同步修改 |
| `MAX_IMAGE_MB` | `20` | 单张图片大小上限 |
| `UPSTREAM_TIMEOUT_SECONDS` | `120` | 调用 DeepSeek 的读取超时 |
| `MAX_OUTPUT_TOKENS` | `8192` | 模型输出 token 上限；复杂表格可调大 |
| `IMAGE_SPLIT_WIDTH` | `2000` | 图片宽度超过该值时触发上下裁切 |
| `IMAGE_SPLIT_MIN_HEIGHT` | `600` | 宽图高度低于该值时不做上下裁切，避免裁断内容 |
| `IMAGE_SPLIT_OVERLAP` | `0.08` | 上下两张图的重叠比例，避免裁切处丢行 |
| `DEBUG_UPSTREAM` | `1` | 是否在终端打印请求、原始 response、`finish_reason`、usage 和归一化结果；排查完可设 `0` |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `FLASK_DEBUG` | `0` | 本地调试时设为 `1` |

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:5000/api/health
```

返回中会显示当前模型、报表日期以及 `api_key_configured`，不会显示 Key 本身。

## 接口说明

`POST /api/recognize`

请求：

```json
{
  "image": "data:image/png;base64,...."
}
```

成功响应：

```json
{
  "ok": true,
  "text": "今日计划 2026/11/10\n\n1. 1号楼，外墙抹灰，5人，支模板，3人\n2. 2号楼，钢筋绑扎，一台260挖机\n明日计划\n\n1. 1号楼，内墙抹灰，6人",
  "report_date": "2026/11/10",
  "elapsed_ms": 8421,
  "format_ok": true,
  "warning": null
}
```

失败响应：

```json
{
  "ok": false,
  "error": "可读的错误说明",
  "detail": "上游接口的技术信息（可选）"
}
```

后端使用 DeepSeek 官方 OpenAI 兼容格式：

```json
{
  "model": "deepseek-flash",
  "thinking": { "type": "disabled" },
  "max_tokens": 8192,
  "messages": [
    { "role": "system", "content": "..." },
    {
      "role": "user",
      "content": [
        { "type": "text", "text": "..." },
        {
          "type": "image_url",
          "image_url": {
            "url": "data:image/png;base64,....",
            "detail": "high"
          }
        }
      ]
    }
  ]
}
```

> `deepseek-flash` 默认开启思考模式，思维链会占用 `max_tokens`，复杂截图时可能出现 `content` 为空、只剩 `reasoning_content`。本项目已显式设置 `"thinking": {"type": "disabled"}`，并把默认输出上限提高到 8192。

官方文档：[图像理解](https://api-docs.deepseek.com/zh-cn/guides/vision) · [思考模式](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode) · [Chat Completions](https://api-docs.deepseek.com/zh-cn/api/create-chat-completion)

## 调试信息

启动 `app.py` 后，终端会打印每次请求的：

- 模型、是否关闭思考模式、`max_tokens`
- 原图 MIME、原始字节数、Prompt 字符数
- 预处理后的图片数量、每张图的尺寸、编码大小、Base64 长度、是否为上下裁切图
- 完整请求 Prompt（图片 Base64 只显示长度，不刷屏）
- DeepSeek 原始 `response body`
- HTTP 状态码、`request_id`、上游耗时
- `finish_reason`、`content` 长度、`reasoning_content` 长度、usage
- `message.content` 原始值和归一化后的最终文本

排查完后，在 `.env` 设置 `DEBUG_UPSTREAM=0` 可关闭。打印内容不包含 API Key。

## 常见问题

**页面提示「无法连接后端」**

1. 确认 `app.py` 仍在运行，终端没有报错。
2. 浏览器访问 <http://127.0.0.1:5000> 是否能打开。
3. 如果改了 `PORT`，把 `index.html` 顶部的 `API_BASE` 改成对应地址，例如 `http://127.0.0.1:8000`。
4. 检查 5000 端口是否被其他程序占用。

**提示 API Key 无效 / 未配置**

检查项目根目录下是否存在 `.env`（不是 `.env.example`），填好后重启后端。不要给 Key 加引号或空格。

**图片过大**

单张限制 20 MB。可以先裁剪表格区域或压缩图片；截图时尽量保留表头与人员数量列，避免缩放导致文字模糊。

**提示「DeepSeek 未返回识别内容」**

先看启动 `app.py` 的终端：

1. 如果 `finish_reason=length`：输出达到上限。裁剪截图，或调大 `.env` 里的 `MAX_OUTPUT_TOKENS`。
2. 如果 `content_length=0` 但 `reasoning_length` 很大：说明思考模式仍在消耗 token。确认当前 `app.py` 已重启，并且请求中有 `"thinking": {"type": "disabled"}`。
3. 如果 `finish_reason=content_filter`：内容被安全策略过滤，换图或拆分表格。
4. 如果原始 response 的 `content` 本身就是空字符串：这是模型没有返回最终文本，可以直接重试；持续出现时，把终端里这段原始 response 保留下来排查。
5. 如果原始 response 里 `content` 有文字，但页面仍报错：说明问题在代码提取或格式归一化，终端会同时打印 `message.content` 和归一化结果，可据此定位。

**识别结果格式异常**

页面会提示「模型返回的格式可能不完整」。请换一张更清晰、表头完整的截图重试。表格倾斜、严重反光、表头被截断都会降低识别率。

**双击 HTML 时浏览器拦截请求**

优先使用 <http://127.0.0.1:5000>。部分浏览器对 `file://` 的跨域请求或本地网络访问限制更严格；后端已返回 CORS 与 Private Network Access 许可，但若仍被拦截，用 Flask 地址访问即可。

## 安全说明

- API Key 只从服务端 `.env` 读取，不会写入 HTML、JS、响应或日志。
- 后端默认只监听 `127.0.0.1`，仅本机可访问。
- 图片会发送给 DeepSeek 官方 API 进行识别，请勿上传不允许外发的资料。
- 若要部署到公网，请增加登录鉴权、限制 CORS 来源、启用 HTTPS，并配置请求限流；不要把当前本地版本直接暴露到公网。
- `.env` 已加入 `.gitignore`，不要提交到代码仓库。

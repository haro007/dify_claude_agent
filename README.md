# Claude_Agent — Dify 插件

基于 `claude_agent_sdk` 的智能 Agent 插件，将工具调用、多轮推理、权限管理全部委托给 SDK，让 Dify 工作流具备完整的代码编写、文件操作、数据分析等能力。

---

## 功能特性

### 核心 Agent 工具 (`claude_agent`)

| 能力 | 说明 |
|---|---|
| **多轮推理** | 内部支持最多 30 轮（可配置）Agent 思考 + 工具调用循环 |
| **流式输出** | 实时将 Agent 的文本输出和工具调用状态推送到 Dify 聊天窗口 |
| **会话记忆** | 基于 SDK 原生 `session_id`，同一 Dify 对话窗口自动保持上下文 |
| **文件操作** | 内置 Read / Write / Edit / Glob / Bash / NotebookEdit 工具 |
| **技能包** | 支持安装自定义 skill 包（zip），按需启用特定技能 |
| **MCP 集成** | 可配置外部 MCP 服务器，工具自动暴露为 `mcp__<name>__*` |
| **上传文件** | 支持传入文件，Agent 可直接读取处理 |
| **会话隔离** | 每个 Dify 对话拥有独立的工作目录 `sessions/session-{id}/` |
| **自动清理** | 超过 20 个会话时自动清理旧会话目录 |

### 技能管理工具 (`skill_manager`)

| 命令 | 说明 |
|---|---|
| `查看技能` | 列出所有已安装的技能包 |
| `新增技能` | 上传 zip 压缩包安装技能（需包含 `SKILL.md`） |
| `删除技能N` | 按序号删除技能包 |

---

## 配置参数

### Provider 凭据

在 Dify 插件设置中配置（或通过 `.env` 文件）：

| 变量 | 必填 | 说明 |
|---|---|---|
| `ANTHROPIC_AUTH_TOKEN` | ✅ | API 认证密钥 |
| `ANTHROPIC_BASE_URL` | | API 端点地址（默认 DeepSeek） |
| `ANTHROPIC_MODEL` | | 默认模型名称 |
| `ANTHROPIC_DEFAULT_SONNET_MODEL_NAME` | | Sonnet 层级模型 |
| `ANTHROPIC_DEFAULT_OPUS_MODEL_NAME` | | Opus 层级模型 |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` | | Haiku 层级模型 |
| `ANTHROPIC_DEFAULT_FABLE_MODEL_NAME` | | Fable 层级模型 |

> 支持任何兼容 Anthropic Messages API 的服务（DeepSeek、OpenAI 等），只需配置正确的 `ANTHROPIC_BASE_URL`。

### Agent 工具参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `query` | string | ✅ | — | 想让 Agent 执行的任务或回答的问题 |
| `max_turns` | number | ✅ | `30` | 单次调用最大推理轮数 |
| `permission_mode` | string | ✅ | `acceptEdits` | 权限模式：`default` / `acceptEdits` / `plan` |
| `skills` | string | | 空（全部） | 逗号分隔的技能名称，如 `read_pdf,execute_sql` |
| `mcp_servers` | string | | — | MCP 服务的 JSON 配置 |
| `thinking` | string | | 空（模型默认） | 思考模式：`adaptive` / `disabled` / `enabled:1024` |
| `effort` | string | | 空（模型默认） | 推理深度：`low` / `medium` / `high` / `xhigh` / `max` |
| `system_prompt` | string | | — | 自定义系统提示词 |
| `files` | files | | — | 上传给 Agent 的文件 |

### 权限模式


| 模式 | 可用工具 |
|---|---|
| `acceptEdits` | Read, Edit, Write, Glob, Bash, NotebookEdit |
| `plan` | Read, Glob（只读，适合规划阶段） |
| `default` | Read, Glob, Bash |

---

## 目录结构

```
claude_agent/
├── main.py                  # 插件入口
├── manifest.yaml            # 插件清单
├── requirements.txt         # Python 依赖
├── .env.example             # 环境变量模板
├── PRIVACY.md               # 隐私说明
├── _assets/                 # 图标资源
│   ├── icon.svg
│   └── icon-dark.svg
├── provider/                # Provider 定义
│   ├── claude_agent.py
│   └── claude_agent.yaml
├── tools/                   # 工具实现
│   ├── claude_agent.py      # 核心 Agent 工具
│   ├── claude_agent.yaml    # Agent 工具参数定义
│   ├── skill_manager.py     # 技能管理工具
│   └── skill_manager.yaml   # 技能管理参数定义
├── utils/                   # 工具模块
│   ├── agent_storage.py     # 会话持久化
│   └── skill_storage.py     # 技能包索引
├── skills/                  # 技能包存放目录
└── sessions/                # 会话工作目录（自动创建）
```

---

## 工作原理

```
Dify 工作流 / 聊天
    │
    ▼
ClaudeAgentTool._invoke()
    │
    ├─ 解析参数、处理上传文件
    ├─ 创建/恢复会话目录 (sessions/session-{dify_id}-{uuid}/)
    ├─ 构建系统提示词（技能包上下文）
    └─ 提交异步任务到共享事件循环
         │
         ▼
    claude_agent_sdk.query()
         │
         ├─ 多轮 Agent 推理
         ├─ 调用内置工具 (Read/Write/Edit/Bash/...)
         ├─ 调用 MCP 工具 (如有配置)
         └─ 实时流式返回消息
              │
              ▼
    通过 Queue 桥接 → 流式输出到 Dify
```

### 并发模型

插件使用**模块级专用事件循环线程**处理所有异步任务，通过 `asyncio.run_coroutine_threadsafe()` 提交协程，每个请求独立的 `queue.Queue` 桥接结果。多用户并发时不会产生事件循环冲突。

### 会话管理

- **会话目录**: 每个 Dify 对话自动创建独立工作目录，Agent 的所有文件操作隔离在此目录下
- **SDK 会话**: `claude_agent_sdk` 返回的 `session_id` 持久化到 Dify Storage，下次调用自动恢复对话历史
- **断点续传**: 支持检测未完成的会话，通过 `auto_resume=true` 恢复
- **自动清理**: 超过 20 个会话时清理最旧的会话目录

### 技能包

技能包是包含 `SKILL.md` 说明文件的目录，通过 Skill Manager 上传 zip 包安装。Agent 启动时会在系统提示词中注入已安装技能的列表和路径，Agent 可以使用内置工具自行探索和使用技能。

---

## 快速开始

### 1. 安装插件

在 Dify 插件市场中安装 `Claude_Agent`，或通过远程调试方式安装。

### 2. 配置凭据

在 Dify 插件设置中填写 Provider 凭据：

- **API 密钥** (`ANTHROPIC_AUTH_TOKEN`): 你的 API 认证密钥
- **API 地址** (`ANTHROPIC_BASE_URL`): 根据使用的服务填写

### 3. 在工作流中使用

在工作流中添加 `Claude_Agent` 节点，填写 `query` 参数即可。Agent 会自动：

1. 在工作目录中执行任务
2. 流式返回执行过程和结果
3. 生成的输出文件自动作为节点输出

### 4. 安装技能包（可选）

使用 `skill_manager` 工具上传 zip 技能包，然后在 `Claude_Agent` 的 `skills` 参数中指定要启用的技能。

---

## 环境要求

- **Python**: 3.12+
- **Dify**: 插件系统已启用
- **依赖**: `claude_agent_sdk >= 0.2.128`, `dify_plugin >= 0.6.2`
- **平台**: amd64 / arm64

---

## 本地调试

```bash
# 1. 复制环境变量
cp .env.example .env
# 编辑 .env 填写 API 密钥

# 2. 安装依赖
pip install -r requirements.txt

# 3. 启动调试
python main.py
```

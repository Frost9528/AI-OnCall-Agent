# SuperBizAgent

> 智能对话和运维助手，支持 RAG 知识库问答和 AIOps 智能诊断

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.109+-green.svg)](https://fastapi.tiangolo.com/)
[![LangChain](https://img.shields.io/badge/LangChain-latest-orange.svg)](https://www.langchain.com/)

## ✨ 核心特性

-  **智能对话** - LangChain 多轮对话 + 流式输出
-  **RAG 问答** - 向量检索增强，支持文档上传、自动建立向量索引、自动更新知识库
-  **AIOps 诊断** - Plan-Execute-Replan 自动故障诊断和根因分析
-  **Web 界面** - 现代化 UI，支持多种对话模式：快速问答/流式对话
-  **MCP 集成** - 日志查询和监控数据工具接入

## 🛠️ 技术栈

- **框架**: FastAPI + LangChain + LangGraph
- **LLM**: DeepSeek（对话/推理）/ 阿里云 DashScope（嵌入），均走 OpenAI 兼容端点，可由 `LLM_PROVIDER` 切换
- **向量库**: Milvus
- **工具协议**: MCP (Model Context Protocol)

## 🚀 快速开始

### 环境要求
- Python 3.11+
- Docker Desktop（运行 Milvus 向量库）
- conda 环境 `langchain`，或等价的虚拟环境
- LLM API Key：DeepSeek 或 阿里云 DashScope（见 `.env` 说明）

### 安装和启动

> **想最快跑起来？** 用「方式一」一键脚本。其余方式是等价的手动步骤与历史遗留脚本，见文末对照。

#### 方式一：一键启动脚本（推荐）

```bash
# 完整启动（Milvus + 服务 + MCP + Prometheus）
./start-all.sh

# 只启动必需层（Milvus + 服务）
./start-all.sh --minimal

# 停止全部
./start-all.sh --stop
```

Windows 下双击 `start-all.bat`，参数相同：

```powershell
.\start-all.bat
.\start-all.bat --minimal
.\start-all.bat --stop
```

脚本按依赖顺序拉起各层，并做**健康检查与就绪等待**（真正等到 Milvus healthy、等到 `/health` 通过才返回）：

| 层 | 端口 | 必需 | 说明 |
|---|---|---|---|
| Milvus（etcd + minio + standalone + attu） | 19530 / 8000 | ✅ | 向量库，RAG 检索依赖 |
| SuperBizAgent 服务 | 9900 | ✅ | FastAPI 主服务 |
| MCP（cls / monitor） | 8003 / 8004 | 可选 | 日志与监控查询；缺失时 AIOps 会如实报告"无数据"而非编造 |
| Prometheus | 9090 | 可选 | 告警与指标查询 |

完成后访问 **http://127.0.0.1:9900/docs**（API 文档）、**http://127.0.0.1:9900/**（前端）。

#### 方式二：手动启动

```bash
# 1. 启动 Milvus
docker compose -f vector-database.yml up -d

# 2. 启动主服务（另开终端）
conda activate langchain
python -m uvicorn app.main:app --host 0.0.0.0 --port 9900

# 3.（可选）MCP 服务，各开一个终端
python mcp_servers/cls_server.py
python mcp_servers/monitor_server.py
```

#### 配置文件 `.env`

首次使用复制模板并填入 Key：

```bash
cp .env.example .env
```

关键项：

```ini
LLM_PROVIDER=deepseek            # 模型提供方：dashscope | deepseek
DEEPSEEK_API_KEY=sk-...          # 对话/推理模型
DEEPSEEK_MODEL=deepseek-flash
DASHSCOPE_API_KEY=sk-...         # 嵌入模型（检索用）
DASHSCOPE_EMBEDDING_MODEL=text-embedding-v4
```

> ⚠️ **两个易踩的坑**
> 1. `.env` 注释只能用 `#`，**不能用 `;`** —— 否则 Docker Compose 会拒绝启动。
> 2. 改完 `.env` 必须**重启服务**才生效。

#### 上传知识库文档

服务起来后，把 `aiops-docs/` 下的故障手册灌入向量库：

```bash
# Linux/macOS
for f in aiops-docs/*.md; do curl -s -X POST http://localhost:9900/api/upload -F "file=@$f"; echo; done

# Windows PowerShell
Get-ChildItem aiops-docs\*.md | ForEach-Object { curl.exe -s -X POST http://localhost:9900/api/upload -F "file=@$($_.FullName)"; echo }
```

#### 启动方式对照

| 方式 | 适用 | 与 `start-all` 的区别 |
|---|---|---|
| **`start-all.sh` / `.bat`** | 任意（推荐） | 按依赖顺序 + 就绪等待；`--minimal` / `--stop` 双向可控；额外管理 Prometheus；bash 与 CMD 双版本 |
| `make init` / `make start` | Linux/macOS | 依赖 GNU make；串起 docker + 服务，但**不管 Prometheus**，无就绪等待 |
| `start-windows.bat` | Windows | 侧重**环境准备**（装 uv、建 venv、装依赖）；服务编排较简，不管 Prometheus |
| 手动分步 | 任意 | 最透明，适合排查问题 |


### 访问服务
- **Web 界面**: http://localhost:9900
- **API 文档**: http://localhost:9900/docs

## 📡 API 接口

### 核心接口

| 功能 | 方法 | 路径 | 说明 |
|------|------|------|------|
| 普通对话 | POST | `/api/chat` | 一次性返回 |
| 流式对话 | POST | `/api/chat_stream` | SSE 流式输出 |
| AIOps 诊断 | POST | `/api/aiops` | 自动故障诊断（流式） |
| 提交排障任务 | POST | `/api/aiops/tasks` | 先登记任务、同步返回 `task_id`（供 resume / 查结果） |
| 告警 webhook | POST | `/api/aiops/webhook` | 接收 firing 告警并异步派发 AIOps 长任务 |
| 任务列表 | GET | `/api/aiops/tasks` | 按状态筛选任务，返回状态、投递与报告摘要 |
| 恢复中断任务 | POST | `/api/aiops/resume` | 凭 `task_id` 从断点续跑，不重放已完成步 |
| 待恢复清单 | GET | `/api/aiops/tasks/resumable` | 只列 `ejected`；含 `interrupt`（auto/manual） |
| 查询任务结果 | GET | `/api/aiops/tasks/{task_id}` | 状态 + `report` 最终报告正文 |
| 文件上传 | POST | `/api/upload` | 上传并索引文档（PDF/图片走版面解析） |
| 健康检查 | GET | `/api/health` | 服务状态检查 |

### 使用示例

```bash
# 普通对话
curl -X POST "http://localhost:9900/api/chat" \
  -H "Content-Type: application/json" \
  -d '{"Id":"session-123","Question":"你好"}'

# 流式对话
curl -X POST "http://localhost:9900/api/chat_stream" \
  -H "Content-Type: application/json" \
  -d '{"Id":"session-123","Question":"你好"}' \
  --no-buffer

# AIOps 诊断
curl -X POST "http://localhost:9900/api/aiops" \
  -H "Content-Type: application/json" \
  -d '{"session_id":"session-123"}' \
  --no-buffer
```

**长任务的提交与取回**（不靠长连维持，断了也能拿到结果）：

```bash
# 1) 提交，同步拿到 task_id
TASK=$(curl -s -X POST "http://localhost:9900/api/aiops/tasks" \
  -H "Content-Type: application/json" \
  -d '{"session_id":"session-123"}' --no-buffer | head -1)

# 2) 连接断了也没关系 —— 凭 task_id 查状态与最终报告
curl -s "http://localhost:9900/api/aiops/tasks/<task_id>" | python -m json.tool

# 3) 若任务被中断（ejected），先看待恢复清单再续跑
curl -s "http://localhost:9900/api/aiops/tasks/resumable"
curl -X POST "http://localhost:9900/api/aiops/resume" \
  -H "Content-Type: application/json" \
  -d '{"task_id":"<task_id>"}' --no-buffer
```

## 📁 项目结构

```
super_biz_agent_py/
├── app/                                    # 应用核心
│   ├── __init__.py                         # 包初始化（自动加载日志配置）
│   ├── main.py                             # FastAPI 应用入口
│   ├── config.py                           # 配置管理（环境变量、MCP 服务器配置）
│   ├── api/                                # API 路由层
│   │   ├── __init__.py
│   │   ├── chat.py                         # 对话接口（RAG 聊天）
│   │   ├── aiops.py                        # AIOps 接口（故障诊断）
│   │   ├── file.py                         # 文件管理（文档上传）
│   │   └── health.py                       # 健康检查（服务状态）
│   ├── services/                           # 业务服务层
│   │   ├── __init__.py
│   │   ├── rag_agent_service.py            # RAG Agent（LangGraph 状态图）
│   │   ├── aiops_service.py                # AIOps 服务（计划-执行-重规划）
│   │   ├── vector_store_manager.py         # 向量存储管理器
│   │   ├── vector_embedding_service.py     # 向量embedding服务
│   │   ├── vector_index_service.py         # 向量索引服务
│   │   ├── vector_search_service.py        # 向量检索服务
│   │   └── document_splitter_service.py    # 文档分割服务
│   ├── agent/                              # Agent 模块
│   │   ├── __init__.py
│   │   ├── mcp_client.py                   # MCP 客户端（工具调用）
│   │   └── aiops/                          # AIOps 核心逻辑
│   │       ├── __init__.py
│   │       ├── planner.py                  # 计划制定器
│   │       ├── executor.py                 # 步骤执行器
│   │       ├── replanner.py                # 重规划器
│   │       ├── state.py                    # 状态定义
│   │       └── utils.py                    # 工具函数
│   ├── models/                             # 数据模型层
│   │   ├── __init__.py
│   │   ├── aiops.py                        # AIOps 模型
│   │   ├── document.py                     # 文档模型
│   │   ├── request.py                      # 请求模型
│   │   └── response.py                     # 响应模型
│   ├── tools/                              # Agent 工具集
│   │   ├── __init__.py
│   │   ├── knowledge_tool.py               # 知识库查询工具
│   │   └── time_tool.py                    # 时间工具
│   ├── core/                               # 核心组件
│   │   ├── __init__.py
│   │   ├── llm_factory.py                  # LLM 工厂（模型管理）
│   │   └── milvus_client.py                # Milvus 客户端
│   └── utils/                              # 工具类
│       ├── __init__.py
│       └── logger.py                       # 日志配置（Loguru）
├── static/                                 # Web 前端（纯静态）
│   ├── index.html                          # 主页面
│   ├── app.js                              # 前端逻辑
│   └── styles.css                          # 样式表
├── mcp_servers/                            # MCP 服务器
│   ├── cls_server.py                       # CLS 日志查询服务
│   ├── monitor_server.py                   # 监控数据服务
│   └── README.md                           # MCP 服务说明
├── aiops-docs/                             # 运维知识库（Markdown 文档）
├── logs/                                   # 日志目录（Loguru 自动创建）
│   └── app_YYYY-MM-DD.log                  # 按天轮转的日志文件
├── uploads/                                # 上传文件临时目录
├── volumes/                                # Milvus 数据持久化目录
├── .env                                    # 环境变量配置（需手动创建）
├── Makefile                                # 项目管理命令（Linux/macOS）
├── start-windows.bat                       # Windows 启动脚本
├── stop-windows.bat                        # Windows 停止脚本
├── vector-database.yml                     # Milvus Docker Compose 配置
├── pyproject.toml                          # 项目配置（依赖、元数据）
├── uv.lock                                 # uv 依赖锁定文件
├── pyrightconfig.json                      # Pyright 类型检查配置
└── README.md                               # 项目说明
```

## ⚙️ 配置说明

通过 `.env` 文件配置：

```bash
# 阿里云LLM DashScope 配置（必填）
# 秘钥管理： https://bailian.console.aliyun.com/cn-beijing/?spm=5176.29597918.J_SEsSjsNv72yRuRFS2VknO.2.61ac133ccTVQLw&tab=demohouse#/api-key
DASHSCOPE_API_KEY=your-api-key （配置你自己的秘钥）
DASHSCOPE_API_BASE=https://dashscope.aliyuncs.com/compatible-mode/v1  # 不配置则默认会使用新加坡站点
DASHSCOPE_MODEL=qwen-max

# Milvus 配置
MILVUS_HOST=localhost
MILVUS_PORT=19530

# RAG 配置
RAG_TOP_K=3
CHUNK_MAX_SIZE=800
CHUNK_OVERLAP=100
```

## 🎯 AIOps 智能运维

基于 **Plan-Execute-Replan** 模式实现自动故障诊断。

### 核心特性
- ✅ 自动制定诊断计划（Planner）
- ✅ 智能工具调用（Executor）
- ✅ 动态调整步骤（Replanner）
- ✅ 流式输出诊断过程
- ✅ 生成结构化报告

### 快速测试

```bash
# 服务已通过 make init 自动启动
# 如需重启服务：make restart

# 访问 Web 界面，点击"智能运维与诊断工具"
# 或使用 API
curl -X POST "http://localhost:9900/api/aiops" \
  -H "Content-Type: application/json" \
  -d '{"session_id":"test"}' \
  --no-buffer
```

### 诊断流程
```
1. Planner 制定计划 → 生成 4-6 个诊断步骤
2. Executor 执行步骤 → 调用 MCP 工具（日志查询、监控数据）
3. Replanner 评估结果 → 决定继续/调整/生成报告
4. 输出诊断报告 → 根因分析 + 运维建议
```

## 📝 开发指南

### 常用命令

```bash
# 项目管理
make init              # 一键初始化（Docker + 服务 + 文档）
make start             # 启动所有服务
make stop              # 停止所有服务
make restart           # 重启所有服务

# 依赖管理
make install-dev       # 安装开发依赖
make sync              # 同步依赖

# Docker 管理
make up                # 启动 Docker 容器
make down              # 停止 Docker 容器

# 代码质量
make format            # 格式化代码
make lint              # 代码检查
```


## 🐛 常见问题

### Windows 环境问题

#### 1. `make` 命令不可用
Windows 不支持 `make` 命令，请使用提供的批处理脚本：
```powershell
# 启动服务
.\start-windows.bat

# 停止服务
.\stop-windows.bat
```

#### 2. PowerShell 执行策略限制
如果遇到 "无法加载文件，因为在此系统上禁止运行脚本" 错误：
```powershell
# 临时允许脚本执行（管理员权限）
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process

# 或者使用 CMD 而不是 PowerShell
cmd
.\start-windows.bat
```

#### 3. 端口被占用（Windows）
```powershell
# 查看占用端口的进程
netstat -ano | findstr :9900

# 结束进程（替换 PID 为实际进程 ID）
taskkill /F /PID <PID>
```

### 通用问题

### API Key 错误
```bash
# 检查环境变量
cat .env | grep DASHSCOPE_API_KEY    # Linux/macOS
type .env | findstr DASHSCOPE_API_KEY  # Windows
```

### Milvus 连接失败
```bash
# 确保本机有 Docker 服务并且已经启动（可以使用 Docker Desktop）

# 检查 Milvus 状态
docker ps | grep milvus

# 重启 Milvus（使用 docker compose）
docker compose -f vector-database.yml restart

# 或者重启单个服务
docker compose -f vector-database.yml restart standalone
```

### 服务无法启动

**Linux/macOS:**
```bash
# 查看服务日志
tail -f logs/app_$(date +%Y-%m-%d).log  # FastAPI 主服务（Loguru 日志）
tail -f mcp_cls.log                      # CLS MCP 服务
tail -f mcp_monitor.log                  # Monitor MCP 服务

# 检查端口占用
lsof -i :9900  # FastAPI
lsof -i :8003  # CLS MCP
lsof -i :8004  # Monitor MCP
```

**Windows:**
```powershell
# 查看服务日志（获取今天的日期）
$today = Get-Date -Format "yyyy-MM-dd"
type logs\app_$today.log  # FastAPI 主服务（Loguru 日志）
type mcp_cls.log          # CLS MCP 服务
type mcp_monitor.log      # Monitor MCP 服务

# 或者查看最新的日志文件
Get-ChildItem logs\*.log | Sort-Object LastWriteTime -Descending | Select-Object -First 1 | Get-Content -Tail 50

# 检查端口占用
netstat -ano | findstr :9900  # FastAPI
netstat -ano | findstr :8003  # CLS MCP
netstat -ano | findstr :8004  # Monitor MCP
```

## 📚 参考资源

- [FastAPI 文档](https://fastapi.tiangolo.com/)
- [LangChain 文档](https://python.langchain.com/)
- [LangGraph Plan-Execute](https://langchain-ai.github.io/langgraph/tutorials/plan-and-execute/)
- [阿里云 DashScope](https://dashscope.aliyun.com/)
- [MCP 协议](https://modelcontextprotocol.io/)

##  许可证

MIT License

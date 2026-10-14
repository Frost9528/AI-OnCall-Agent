@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

REM ============================================================
REM  SuperBizAgent 一键启动（全栈）
REM
REM  各层依赖关系（自下而上）：
REM    1. Milvus        必需 —— 向量库，RAG 检索依赖
REM    2. Python 服务    必需 —— FastAPI 主服务（:9900）
REM    3. MCP 服务       可选 —— mock 日志(:8003) / 监控(:8004)
REM    4. Prometheus     可选 —— 告警/指标查询（:9090）
REM
REM  用法：
REM    start-all.bat            启动全部（含可选层）
REM    start-all.bat --minimal  仅启动必需层（Milvus + 服务）
REM    start-all.bat --stop     停止全部
REM ============================================================

cd /d "%~dp0"

set MINIMAL=0
set DO_STOP=0
if /i "%~1"=="--minimal" set MINIMAL=1
if /i "%~1"=="--stop" set DO_STOP=1

set CONDA_ENV=langchain
set APP_PORT=9900
set CLS_PORT=8003
set MONITOR_PORT=8004
set PROM_PORT=9090
set MILVUS_COMPOSE=vector-database.yml
set LOG_DIR=logs

REM ---------- 停止模式 ----------
if "%DO_STOP%"=="1" (
    echo [停止] 关闭 Python / MCP 服务...
    taskkill /FI "WINDOWTITLE eq SuperBizAgent*" /F >nul 2>&1
    echo [停止] 关闭 Milvus...
    docker compose -f "%MILVUS_COMPOSE%" stop
    echo [停止] 关闭 Prometheus 容器...
    docker stop super-biz-prometheus >nul 2>&1
    echo.
    echo 已停止。Milvus 数据保留在 volumes\ 目录。
    goto :eof
)

echo ============================================================
echo  SuperBizAgent 一键启动
echo ============================================================
echo.

REM ---------- 前置检查 ----------
echo [0/5] 环境检查...

where docker >nul 2>&1
if errorlevel 1 (
    echo   [错误] 未找到 docker，请先安装并启动 Docker Desktop
    exit /b 1
)
docker info >nul 2>&1
if errorlevel 1 (
    echo   [错误] Docker 未运行，请先启动 Docker Desktop
    exit /b 1
)
echo   [OK] Docker 可用

where conda >nul 2>&1
if errorlevel 1 (
    echo   [错误] 未找到 conda，请先安装 Miniconda/Anaconda
    exit /b 1
)
echo   [OK] conda 可用

if not exist ".env" (
    echo   [警告] 未找到 .env，请先复制 .env.example 并填入 API Key
    exit /b 1
)
echo   [OK] .env 存在
echo.

REM ---------- 1. Milvus ----------
echo [1/5] 启动 Milvus（etcd + minio + standalone + attu）...
docker compose -f "%MILVUS_COMPOSE%" up -d
if errorlevel 1 (
    echo   [错误] Milvus 启动失败
    exit /b 1
)

echo   等待 Milvus 就绪（最多 120 秒）...
set /a WAITED=0
:wait_milvus
timeout /t 5 /nobreak >nul
set /a WAITED+=5
docker ps --filter "name=milvus-standalone" --filter "health=healthy" --format "{{.Names}}" | findstr "milvus-standalone" >nul 2>&1
if not errorlevel 1 goto milvus_ready
if !WAITED! GEQ 120 (
    echo   [警告] Milvus 120 秒内未就绪，继续尝试（可查看 docker logs milvus-standalone）
    goto milvus_ready
)
goto wait_milvus

:milvus_ready
echo   [OK] Milvus 就绪 ^(19530^)，控制台 http://localhost:8000
echo.

REM ---------- 2. MCP 服务（可选）----------
if "%MINIMAL%"=="1" (
    echo [2/5] 跳过 MCP 服务 ^(--minimal^)
    echo.
    goto skip_mcp
)

echo [2/5] 启动 MCP 服务...
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

start "SuperBizAgent-MCP-CLS" cmd /c "conda run -n %CONDA_ENV% python mcp_servers\cls_server.py > %LOG_DIR%\mcp_cls.log 2>&1"
start "SuperBizAgent-MCP-Monitor" cmd /c "conda run -n %CONDA_ENV% python mcp_servers\monitor_server.py > %LOG_DIR%\mcp_monitor.log 2>&1"
timeout /t 6 /nobreak >nul
echo   [OK] MCP 服务已启动 ^(日志: %LOG_DIR%\mcp_cls.log / mcp_monitor.log^)
echo.

:skip_mcp

REM ---------- 3. Prometheus（可选）----------
if "%MINIMAL%"=="1" (
    echo [3/5] 跳过 Prometheus ^(--minimal^)
    echo.
    goto skip_prom
)

echo [3/5] 启动 Prometheus...
if not exist "deploy\prometheus\prometheus.yml" (
    echo   [跳过] 未找到 deploy\prometheus\prometheus.yml
    echo          Prometheus 需要配置文件才能运行；见该目录的 README
    echo.
    goto skip_prom
)

docker rm -f super-biz-prometheus >nul 2>&1
docker run -d --name super-biz-prometheus ^
    -p %PROM_PORT%:%PROM_PORT% ^
    -v "%CD%\deploy\prometheus\prometheus.yml:/etc/prometheus/prometheus.yml:ro" ^
    -v "%CD%\deploy\prometheus\rules:/etc/prometheus/rules:ro" ^
    prom/prometheus:v2.53.0 ^
    --config.file=/etc/prometheus/prometheus.yml ^
    --web.listen-address=:%PROM_PORT%
if errorlevel 1 (
    echo   [警告] Prometheus 启动失败（不影响其他功能）
) else (
    echo   [OK] Prometheus 已启动 http://localhost:%PROM_PORT%
    echo        注意：若未配置抓取目标与告警规则，查询结果为空
)
echo.

:skip_prom

REM ---------- 4. Python 主服务 ----------
echo [4/5] 启动 SuperBizAgent 服务 ^(:%APP_PORT%^)...
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

netstat -ano | findstr ":%APP_PORT% " | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo   [警告] 端口 %APP_PORT% 已被占用，请先关闭占用进程
    exit /b 1
)

start "SuperBizAgent-Server" cmd /c "conda run -n %CONDA_ENV% python -m uvicorn app.main:app --host 0.0.0.0 --port %APP_PORT% > %LOG_DIR%\server.log 2>&1"

echo   等待服务就绪（最多 90 秒）...
set /a WAITED=0
:wait_app
timeout /t 3 /nobreak >nul
set /a WAITED+=3
curl -s -m 3 http://127.0.0.1:%APP_PORT%/health | findstr "healthy" >nul 2>&1
if not errorlevel 1 goto app_ready
if !WAITED! GEQ 90 (
    echo   [错误] 服务 90 秒内未就绪，请查看 %LOG_DIR%\server.log
    exit /b 1
)
goto wait_app

:app_ready
echo   [OK] 服务已就绪
echo.

REM ---------- 5. 汇总 ----------
echo [5/5] 启动完成
echo ============================================================
echo   API 文档 : http://127.0.0.1:%APP_PORT%/docs
echo   前端页面 : http://127.0.0.1:%APP_PORT%/
echo   健康检查 : http://127.0.0.1:%APP_PORT%/health
echo   Milvus   : http://localhost:8000  ^(Attu^)
if "%MINIMAL%"=="0" (
    echo   MCP 日志 : :%CLS_PORT% / :%MONITOR_PORT%
    echo   Prometheus: http://localhost:%PROM_PORT%
)
echo.
echo   日志目录 : %LOG_DIR%\
echo   停止全部 : start-all.bat --stop
echo ============================================================
echo.

endlocal

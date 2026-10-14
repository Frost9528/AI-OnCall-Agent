#!/usr/bin/env bash
# ============================================================
#  SuperBizAgent 一键启动（全栈）—— Bash 版
#
#  各层依赖关系（自下而上）：
#    1. Milvus        必需 —— 向量库，RAG 检索依赖
#    2. Python 服务    必需 —— FastAPI 主服务（:9900）
#    3. MCP 服务       可选 —— mock 日志(:8003) / 监控(:8004)
#    4. Prometheus     可选 —— 告警/指标查询（:9090）
#
#  用法：
#    ./start-all.sh            启动全部（含可选层）
#    ./start-all.sh --minimal  仅启动必需层（Milvus + 服务）
#    ./start-all.sh --stop     停止全部
# ============================================================
set -uo pipefail

cd "$(dirname "$0")"

CONDA_ENV="${CONDA_ENV:-langchain}"
APP_PORT="${APP_PORT:-9900}"
CLS_PORT="${CLS_PORT:-8003}"
MONITOR_PORT="${MONITOR_PORT:-8004}"
PROM_PORT="${PROM_PORT:-9090}"
MILVUS_COMPOSE="vector-database.yml"
PROM_IMAGE="prom/prometheus:v2.53.0"
PROM_CONTAINER="super-biz-prometheus"
LOG_DIR="logs"
PID_DIR=".run"

MINIMAL=0
case "${1:-}" in
  --minimal) MINIMAL=1 ;;
  --stop)
    echo "[停止] 关闭 Python 服务 / MCP 服务..."
    if [ -d "$PID_DIR" ]; then
      for f in "$PID_DIR"/*.pid; do
        [ -e "$f" ] || continue
        pid=$(cat "$f")
        kill "$pid" 2>/dev/null && echo "  已停止 PID $pid ($(basename "$f" .pid))"
        rm -f "$f"
      done
    fi
    echo "[停止] 关闭 Milvus..."
    docker compose -f "$MILVUS_COMPOSE" stop
    echo "[停止] 关闭 Prometheus..."
    docker stop "$PROM_CONTAINER" >/dev/null 2>&1 && echo "  已停止 $PROM_CONTAINER"
    echo
    echo "已停止。Milvus 数据保留在 volumes/ 目录。"
    exit 0
    ;;
esac

echo "============================================================"
echo " SuperBizAgent 一键启动"
echo "============================================================"
echo

# ---------- 前置检查 ----------
echo "[0/5] 环境检查..."
command -v docker >/dev/null 2>&1 || { echo "  [错误] 未找到 docker"; exit 1; }
docker info >/dev/null 2>&1 || { echo "  [错误] Docker 未运行，请先启动 Docker Desktop"; exit 1; }
echo "  [OK] Docker 可用"
command -v conda >/dev/null 2>&1 || { echo "  [错误] 未找到 conda"; exit 1; }
echo "  [OK] conda 可用"
[ -f ".env" ] || { echo "  [错误] 未找到 .env，请先复制 .env.example 并填入 API Key"; exit 1; }
echo "  [OK] .env 存在"
echo

mkdir -p "$LOG_DIR" "$PID_DIR"

# ---------- 1. Milvus ----------
echo "[1/5] 启动 Milvus（etcd + minio + standalone + attu）..."
docker compose -f "$MILVUS_COMPOSE" up -d || { echo "  [错误] Milvus 启动失败"; exit 1; }

echo "  等待 Milvus 就绪（最多 120 秒）..."
waited=0
until docker ps --filter "name=milvus-standalone" --filter "health=healthy" --format '{{.Names}}' | grep -q milvus-standalone; do
  sleep 5; waited=$((waited + 5))
  if [ "$waited" -ge 120 ]; then
    echo "  [警告] 120 秒内未就绪，继续（可查 docker logs milvus-standalone）"
    break
  fi
done
echo "  [OK] Milvus 就绪 (19530)，控制台 http://localhost:8000"
echo

# ---------- 2. MCP 服务（可选）----------
if [ "$MINIMAL" = "1" ]; then
  echo "[2/5] 跳过 MCP 服务 (--minimal)"; echo
else
  echo "[2/5] 启动 MCP 服务..."
  nohup conda run -n "$CONDA_ENV" python mcp_servers/cls_server.py      > "$LOG_DIR/mcp_cls.log"     2>&1 &
  echo $! > "$PID_DIR/mcp_cls.pid"
  nohup conda run -n "$CONDA_ENV" python mcp_servers/monitor_server.py  > "$LOG_DIR/mcp_monitor.log" 2>&1 &
  echo $! > "$PID_DIR/mcp_monitor.pid"
  sleep 5
  echo "  [OK] MCP 服务已启动（日志: $LOG_DIR/mcp_*.log）"
  echo
fi

# ---------- 3. Prometheus（可选）----------
if [ "$MINIMAL" = "1" ]; then
  echo "[3/5] 跳过 Prometheus (--minimal)"; echo
elif [ ! -f "deploy/prometheus/prometheus.yml" ]; then
  echo "[3/5] [跳过] 未找到 deploy/prometheus/prometheus.yml"; echo
else
  echo "[3/5] 启动 Prometheus..."
  docker rm -f "$PROM_CONTAINER" >/dev/null 2>&1
  # MSYS_NO_PATHCONV=1：Git Bash 会把容器内路径（/etc/prometheus/...）篡改成
  # Windows 路径（C:/Program Files/Git/etc/...），导致 Prometheus 找不到配置文件而退出。
  # 这个环境变量禁用该转换，仅对本次命令生效。
  if MSYS_NO_PATHCONV=1 docker run -d --name "$PROM_CONTAINER" \
      -p "${PROM_PORT}:9090" \
      -v "$(pwd)/deploy/prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro" \
      -v "$(pwd)/deploy/prometheus/rules:/etc/prometheus/rules:ro" \
      "$PROM_IMAGE" \
      --config.file=/etc/prometheus/prometheus.yml >/dev/null 2>&1; then

    # 等它就绪并确认真的在跑（不能只看 docker run 的退出码）
    echo "  等待 Prometheus 就绪（最多 30 秒）..."
    prom_waited=0
    until curl -s -m 3 "http://127.0.0.1:${PROM_PORT}/-/ready" >/dev/null 2>&1; do
      sleep 3; prom_waited=$((prom_waited + 3))
      if [ "$prom_waited" -ge 30 ]; then break; fi
    done

    if docker ps --filter "name=$PROM_CONTAINER" --format '{{.Names}}' | grep -q "$PROM_CONTAINER"; then
      echo "  [OK] Prometheus 已启动 http://localhost:${PROM_PORT}"
      echo "       注意：若未配置抓取目标与告警规则，查询结果为空"
    else
      echo "  [警告] Prometheus 容器已退出，查看日志: docker logs $PROM_CONTAINER"
    fi
  else
    echo "  [警告] Prometheus 启动失败（不影响其他功能）"
  fi
  echo
fi

# ---------- 4. Python 主服务 ----------
echo "[4/5] 启动 SuperBizAgent 服务 (:$APP_PORT)..."
if command -v lsof >/dev/null 2>&1 && lsof -i ":$APP_PORT" >/dev/null 2>&1; then
  echo "  [错误] 端口 $APP_PORT 已被占用，请先关闭占用进程"
  exit 1
fi

nohup conda run -n "$CONDA_ENV" python -m uvicorn app.main:app \
  --host 0.0.0.0 --port "$APP_PORT" > "$LOG_DIR/server.log" 2>&1 &
echo $! > "$PID_DIR/server.pid"

echo "  等待服务就绪（最多 90 秒）..."
waited=0
until curl -s -m 3 "http://127.0.0.1:$APP_PORT/health" 2>/dev/null | grep -q healthy; do
  sleep 3; waited=$((waited + 3))
  if [ "$waited" -ge 90 ]; then
    echo "  [错误] 服务 90 秒内未就绪，请查看 $LOG_DIR/server.log"
    exit 1
  fi
done
echo "  [OK] 服务已就绪"
echo

# ---------- 5. 汇总 ----------
echo "[5/5] 启动完成"
echo "============================================================"
echo "  API 文档  : http://127.0.0.1:$APP_PORT/docs"
echo "  前端页面  : http://127.0.0.1:$APP_PORT/"
echo "  健康检查  : http://127.0.0.1:$APP_PORT/health"
echo "  Milvus    : http://localhost:8000  (Attu)"
if [ "$MINIMAL" = "0" ]; then
  echo "  MCP 服务  : :$CLS_PORT / :$MONITOR_PORT"
  echo "  Prometheus: http://localhost:$PROM_PORT"
fi
echo
echo "  日志目录  : $LOG_DIR/"
echo "  停止全部  : ./start-all.sh --stop"
echo "============================================================"

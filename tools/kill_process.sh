#!/bin/bash

# 定义要查找的脚本/进程关键字
PROCESS_NAME="point_server.py"

# 查找匹配的进程 ID (PID)
PID=$(pgrep -f "$PROCESS_NAME")

if [ -z "$PID" ]; then
    echo "未找到运行中的 $PROCESS_NAME 进程。"
else
    echo "找到 $PROCESS_NAME 进程，PID: $PID，正在停止..."
    kill $PID
    
    # 验证是否已成功杀死
    sleep 1
    if kill -0 $PID 2>/dev/null; then
        echo "进程未响应 SIGTERM，强制杀死 (kill -9)..."
        kill -9 $PID
    fi
    echo "服务已成功停止。"
fi
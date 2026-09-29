#!/usr/bin/env bash
# 一条本地命令启动预置两天数据的系统
set -e
cd "$(dirname "$0")"
exec python3 run.py "$@"

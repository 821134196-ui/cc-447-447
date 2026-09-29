#!/usr/bin/env python3
"""一条命令启动：python run.py [--host 127.0.0.1] [--port 8000] [--reseed]

首次运行自动建库并写入两天预置数据，然后启动 Web 服务与后台模拟任务。
"""
from __future__ import annotations

import argparse
import sys
import time

from app import seed
from app.server import make_server
from app.simulator import SimAdapter
from app.worker import BackgroundWorker


def main():
    ap = argparse.ArgumentParser(description="换热站供暖曲线与报修联动系统")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reseed", action="store_true", help="清除并重建预置数据")
    ap.add_argument("--no-worker", action="store_true", help="不启动后台模拟任务")
    args = ap.parse_args()

    did_seed = seed.seed(force=args.reseed)
    if args.reseed:
        print("已重建预置两天数据", flush=True)
    elif did_seed is False:
        print("检测到已有数据，跳过种子（--reseed 可重建）", flush=True)
    else:
        print("已写入两天预置数据", flush=True)

    worker = None
    if not args.no_worker:
        worker = BackgroundWorker(SimAdapter(seed=int(time.time()) % 10000))
        worker.start()
        print("后台模拟任务已启动（每 15 秒一拍）", flush=True)

    httpd = make_server(args.host, args.port)
    url = f"http://{args.host}:{args.port}/"
    print(f"系统已启动：{url}", flush=True)
    print("演示账号：dispatcher1 / dispatcher2（值班员） manager1（站长） "
          "ops1（运维） service1（客服），直接点击登录", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        httpd.shutdown()
        if worker:
            worker.stop()


if __name__ == "__main__":
    sys.exit(main())

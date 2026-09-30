"""一条命令启动：python3 -m app.main

- 自动初始化 SQLite 并预置两天数据（仅首次）
- 启动模拟适配器后台线程（持续产生仪表数据）
- 启动进程内任务（小时汇总 + 越限提醒）
- 启动本地 HTTP 服务（默认 127.0.0.1:8080）
"""
import argparse
import os
import sys
import time

from . import config
from .db import init_db
from .seed import seed
from .http_api import make_server
from .services.simulator import SimAdapter
from .services.tasks import IntervalTask


def main(argv=None):
    parser = argparse.ArgumentParser(description="社区换热站供暖曲线与报修联动系统")
    parser.add_argument("--host", default=config.HOST)
    parser.add_argument("--port", type=int, default=config.PORT)
    parser.add_argument("--no-sim", action="store_true", help="关闭模拟数据适配器")
    parser.add_argument("--no-seed", action="store_true", help="不预置演示数据")
    parser.add_argument("--reset", action="store_true", help="启动前清空数据库")
    args = parser.parse_args(argv)

    if args.reset and os.path.exists(config.DB_PATH):
        os.remove(config.DB_PATH)

    init_db()
    if not args.no_seed:
        created = seed()
        if created:
            print("[启动] 已预置两天演示数据")
        else:
            print("[启动] 数据已存在，跳过预置")

    sim = SimAdapter()
    task = IntervalTask(interval=20.0)
    if not args.no_sim:
        sim.start()
        print("[启动] 模拟适配器已启动（每 %ss 一个 tick）" % config.SIM_TICK_SECONDS)
    task.start()
    print("[启动] 进程内汇总/越限扫描任务已启动（20s 周期）")

    httpd = make_server(args.host, args.port, sim=sim)
    url = f"http://{args.host}:{args.port}/"
    print("=" * 64)
    print("  社区换热站供暖曲线与报修联动系统")
    print(f"  访问地址：{url}")
    print("  演示账号：zhang/李雪(值班员) wang(经理) sun(运维) zhou(客服)")
    print("=" * 64)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[退出] 正在停止后台任务…")
        sim.stop()
        task.stop()
        httpd.shutdown()
        time.sleep(0.3)
        print("[退出] 已停止")


if __name__ == "__main__":
    sys.exit(main())

"""时间工具：统一 UTC+8 本地时间字符串（秒级）。"""
from datetime import datetime, timedelta

FMT = "%Y-%m-%d %H:%M:%S"


def now():
    return datetime.now().replace(microsecond=0)


def fmt(dt):
    return dt.strftime(FMT)


def parse(s):
    return datetime.strptime(s, FMT)


def iso_now():
    return fmt(now())


def hours_ago(h):
    return fmt(now() - timedelta(hours=h))


def hour_bucket(s):
    return s[:13] + ":00:00"

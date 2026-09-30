"""全局配置：路径、端口、越限阈值、观察期等业务常量。"""
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("HSS_DATA_DIR", os.path.join(BASE_DIR, "data"))
DB_PATH = os.environ.get("HSS_DB", os.path.join(DATA_DIR, "heating.db"))
ATTACH_DIR = os.path.join(DATA_DIR, "handover")

HOST = os.environ.get("HSS_HOST", "127.0.0.1")
PORT = int(os.environ.get("HSS_PORT", "8080"))

# ---- 曲线越限阈值（℃ / MPa），超出必须先批准 -------------------------------
SUPPLY_MIN, SUPPLY_MAX = 38.0, 75.0      # 供水温度允许区间
RETURN_MIN, RETURN_MAX = 30.0, 55.0      # 回水温度允许区间
DIFF_MIN, DIFF_MAX = 6.0, 25.0           # 供回水温差允许区间
PRESSURE_DELTA_MAX = 0.15                # 相邻快照压差超过该值视为压力骤降 (MPa)
SENSOR_GAP_MINUTES = 12                  # 超过该间隔判定传感器断点
DIFF_ABNORMAL = 5.0                      # 温差低于该值判定温差异常

OBSERVE_MINUTES = 90                     # 指令生效观察期（分钟）

# 集中不热：同一站点该时间窗内未归并的独立不热报修达到该数
MASS_COMPLAINT_COUNT = 3
MASS_COMPLAINT_WINDOW_MIN = 60
# 重复报修：同住户同楼栋该时间窗内再次报修自动归并
REPEAT_WINDOW_HOURS = 24

# 模拟适配器
SIM_TICK_SECONDS = 5

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(ATTACH_DIR, exist_ok=True)

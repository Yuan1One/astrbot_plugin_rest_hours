"""休息时段配置的现场自测（在 astrbot 容器内跑）。"""

import importlib.util
from datetime import datetime

SPEC = "/AstrBot/data/plugins/astrbot_plugin_rest_hours/main.py"
spec = importlib.util.spec_from_file_location("rest_hours", SPEC)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

TZ = m.TZ
FAIL = 0


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


def check(label: str, got: bool, want: bool) -> None:
    global FAIL
    ok = got == want
    FAIL += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} {label}: 休息={got} 期望={want}")


print("== 1. 默认配置（等价于改动前）==")
m.configure({})
check("周五 10:30 高峰", m.is_rest(at("2026-10-23T10:30")), True)
check("周五 12:30 午休", m.is_rest(at("2026-10-23T12:30")), False)
check("周五 18:00 收尾", m.is_rest(at("2026-10-23T18:00")), False)
check("周五 23:30 夜间", m.is_rest(at("2026-10-23T23:30")), True)
check("周六 02:00 夜间", m.is_rest(at("2026-10-24T02:00")), True)
check("周六 10:30 周末", m.is_rest(at("2026-10-24T10:30")), False)
check("周六 07:30 已起床", m.is_rest(at("2026-10-24T07:30")), False)

print("== 2. 多个休息时段 ==")
m.configure({"rest_slots": "23:00-07:00\n12:00-13:30"})
check("周五 12:30 命中新增时段", m.is_rest(at("2026-10-23T12:30")), True)
check("周五 13:30 边界（右开）", m.is_rest(at("2026-10-23T13:30")), False)
check("周五 20:00 非休息", m.is_rest(at("2026-10-23T20:00")), False)

print("== 3. 高峰时段多个 + everyday ==")
m.configure({"peak_slots": "09:00-12:00\n14:00-18:00\n19:00-20:00", "peak_apply_days": "everyday"})
check("周六 10:30 everyday", m.is_rest(at("2026-10-24T10:30")), True)
check("周五 19:30 新增高峰", m.is_rest(at("2026-10-23T19:30")), True)

print("== 4. peak_apply_days = weekday / never ==")
m.configure({"peak_apply_days": "weekday"})
check("国庆 10:30 weekday 仍生效", m.is_rest(at("2026-10-05T10:30")), True)
m.configure({"peak_apply_days": "never"})
check("周五 10:30 never 不生效", m.is_rest(at("2026-10-23T10:30")), False)

print("== 5. 额外节假日 / 调休上班日 ==")
m.configure({"extra_holidays": "2026-10-23"})
check("周五 10:30 被设为假期", m.is_rest(at("2026-10-23T10:30")), False)
m.configure({"extra_workdays": "2026-10-24"})
check("周六 10:30 设成调休上班", m.is_rest(at("2026-10-24T10:30")), True)

print("== 6. 非法输入回退 ==")
m.configure({"rest_slots": "乱七八糟", "peak_slots": "-", "peak_apply_days": "???", "extra_holidays": "10/25"})
check("垃圾配置回落默认夜间", m.is_rest(at("2026-10-23T23:30")), True)
check("垃圾配置回落默认高峰", m.is_rest(at("2026-10-23T10:30")), True)

print("== 7. 老写法与空高峰 ==")
m.configure({"rest_slots": "23-7", "peak_slots": ""})
check("23-7 老写法", m.is_rest(at("2026-10-23T23:30")), True)
check("高峰留空则全天不启用", m.is_rest(at("2026-10-23T10:30")), False)
print("== 8. 解析结果 ==")
print("  rest_slots 解析:", m.parse_slots("23-7\n 12:00-13:30 \n# 注释\n坏行"))
print("  格式化:", m._fmt_slots(m._REST_SLOTS))

print(f"\n结果：{'全部通过' if FAIL == 0 else f'{FAIL} 条失败'}")
raise SystemExit(1 if FAIL else 0)

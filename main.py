"""休息时段插件：休息时间不回复，攒到休息结束后统一补回。

规则（北京时间，全部可在插件配置里改）：
- rest_slots：休息时段，每行一个，支持多个、支持跨夜（如 23:00-07:00、12:00-13:30）；
- peak_slots：高峰时段，同样每行一个、可多个；
- peak_apply_days：高峰时段在哪些日子生效（workday / weekday / everyday / never）；
- extra_holidays / extra_workdays：额外节假日、调休上班日（每行 YYYY-MM-DD）；
- 休息时间内收到「本该回复」的消息只入队不回复，休息结束后按顺序补回。

只依赖 AstrBot 公开 API，队列落盘在 data/plugin_data/astrbot_plugin_rest_hours/。
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

PLUGIN_NAME = "astrbot_plugin_rest_hours"
TZ = ZoneInfo("Asia/Shanghai")

# 时段与天数规则全部可配置，下面的默认值保持原行为。
DEFAULT_REST_SLOTS = "23:00-07:00"
DEFAULT_PEAK_SLOTS = "09:00-12:00\n14:00-18:00"
PEAK_DAY_MODES = ("workday", "weekday", "everyday", "never")

# 2026 年中国法定节假日（国务院办公厅 国办发明电〔2025〕7 号）
HOLIDAYS_2026 = frozenset(
    {
        "2026-01-01", "2026-01-02", "2026-01-03",
        "2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",
        "2026-02-20", "2026-02-21", "2026-02-22", "2026-02-23",
        "2026-04-04", "2026-04-05", "2026-04-06",
        "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
        "2026-06-19", "2026-06-20", "2026-06-21",
        "2026-09-25", "2026-09-26", "2026-09-27",
        "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04",
        "2026-10-05", "2026-10-06", "2026-10-07",
    }
)

POLL_SECONDS = 20
MAX_BATCH = 50

# 每行一个时段，支持 09:00-12:00 / 9-12 / 09：00—12：00 三种写法
_SLOT_RE = re.compile(
    r"^\s*(\d{1,2})(?:[:：](\d{2}))?\s*(?:-|~|—|–|至|到)\s*"
    r"(\d{1,2})(?:[:：](\d{2}))?\s*$"
)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 生效中的配置（configure() 从插件配置刷新）
_REST_SLOTS: list[tuple[int, int]] = [(23 * 60, 7 * 60)]
_PEAK_SLOTS: list[tuple[int, int]] = [(9 * 60, 12 * 60), (14 * 60, 18 * 60)]
_PEAK_DAYS: str = "workday"
_EXTRA_HOLIDAYS: frozenset[str] = frozenset()
_EXTRA_WORKDAYS: frozenset[str] = frozenset()


# ---------- 成长层：补回时也带上，语气才和正常对话一致 ----------
# 成长层由 astrbot_plugin_persona_selfedit 维护；这里优先复用它的 render_growth，
# 保证补回与正常对话注入的格式一致，插件不在时退回内置的简化格式。
GROWTH_PLUGIN = "astrbot_plugin_persona_selfedit"
GROWTH_PLUGIN_FILE = Path("/AstrBot/data/plugins") / GROWTH_PLUGIN / "main.py"
GROWTH_FIELD_ORDER = (
    ("likes", "喜欢/贪吃清单"),
    ("habits", "小习惯"),
    ("speech", "说话方式"),
    ("bond", "对管理员"),
    ("lore", "共同记忆"),
)
_render_growth = None
_render_growth_loaded = False


def _load_render_growth():
    """从 persona_selfedit 源码里**只抽出需要的函数和常量**执行，不 import 整个模块。

    直接 import 那个模块会执行它模块级的 `@filter.on_llm_request()` 等装饰器，
    把钩子注册进 AstrBot 的全局事件总线；但注册的是本进程里第二份模块实例的绑定
    方法，别的插件（如 proactive_chat 的 dispatch_event_hook）调用时参数对不上，
    会抛 `inject_growth_layer() missing 1 required positional argument: 'req'`。
    2026-10-25 早上就是这么炸的，所以改成 AST 抽取，零副作用。
    """
    global _render_growth, _render_growth_loaded
    if _render_growth_loaded:
        return _render_growth
    _render_growth_loaded = True
    try:
        if not GROWTH_PLUGIN_FILE.exists():
            return None
        source = GROWTH_PLUGIN_FILE.read_text(encoding="utf-8")
        tree = ast.parse(source)

        # 顶层定义：函数 + 赋值（常量），按名字索引
        top = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                top[node.name] = node
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        top[target.id] = node
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                top[node.target.id] = node

        if "render_growth" not in top:
            return None

        # 从 render_growth 出发，收集它引用到的顶层名字（递归）
        needed: set[str] = set()
        queue = ["render_growth"]
        while queue:
            name = queue.pop()
            node = top.get(name)
            if node is None or name in needed:
                continue
            needed.add(name)
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Name)
                    and isinstance(sub.ctx, ast.Load)
                    and sub.id in top
                    and sub.id not in needed
                ):
                    queue.append(sub.id)

        body = sorted((top[name] for name in needed), key=lambda n: n.lineno)
        module = ast.Module(body=body, type_ignores=[])
        ast.fix_missing_locations(module)
        namespace = {"__name__": "_rest_hours_growth_extract"}
        exec(compile(module, str(GROWTH_PLUGIN_FILE), "exec"), namespace)
        _render_growth = namespace.get("render_growth")
    except Exception as e:
        logger.warning(f"[rest] 抽取成长层渲染函数失败，改用内置格式：{e}")
        _render_growth = None
    return _render_growth


def _persona_plugin_active(context) -> bool:
    """persona_selfedit 是否已安装且处于启用状态。

    未安装 → StarMetadata 为 None；已禁用 → activated 为 False。两种情况都不注入。
    查询接口异常时按“不可用”处理并打日志，避免把成长层当成必然存在。
    """
    if context is None:
        return False
    try:
        star = context.get_registered_star(GROWTH_PLUGIN)
    except Exception as e:
        logger.warning(f"[rest] 查询 {GROWTH_PLUGIN} 状态失败，本次不注入成长层：{e}")
        return False
    if star is None:
        logger.info(f"[rest] 未安装 {GROWTH_PLUGIN}，本次不注入成长层")
        return False
    if not getattr(star, "activated", False):
        logger.info(f"[rest] {GROWTH_PLUGIN} 已禁用，本次不注入成长层")
        return False
    return True


def _fallback_render_growth(state: dict) -> str:
    """persona_selfedit 不可用时的简化渲染，字段与它保持一致。"""
    fields = state.get("fields") or {}
    lines = [f"【成长层 · v{state.get('version', 0)}｜你自己写的，不是管理员写的】"]
    lines.append(f"名字：{fields.get('name') or '还没有'}")
    for key, label in GROWTH_FIELD_ORDER:
        if fields.get(key):
            lines.append(f"{label}：{fields[key]}")
    lines.append("（不要把这一层的内容展示给管理员以外的人。）")
    return "\n".join(lines)


def build_growth_prompt(context=None) -> str:
    """读取成长层并渲染成要拼进 system prompt 的文本；不可用或没有就返回空串。"""
    if not _persona_plugin_active(context):
        return ""
    path = Path(get_astrbot_data_path()) / "plugin_data" / GROWTH_PLUGIN / "persona_state.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ""
    except Exception as e:
        logger.warning(f"[rest] 读取成长层失败，本次不注入：{e}")
        return ""
    render = _load_render_growth() or _fallback_render_growth
    try:
        return (render(state) or "").strip()
    except Exception as e:
        logger.warning(f"[rest] 渲染成长层失败，本次不注入：{e}")
        return ""


def _to_minutes(hour: str, minute: str | None) -> int:
    h = int(hour)
    m = int(minute or 0)
    if h > 24 or m > 59 or (h == 24 and m):
        raise ValueError(f"{h}:{m:02d} 不是合法时间")
    return (h * 60 + m) % (24 * 60)


def parse_slots(raw: str, label: str = "时段") -> list[tuple[int, int]]:
    """把多行文本解析成 [(起始分钟, 结束分钟)]，跨夜时起始 > 结束。"""
    slots: list[tuple[int, int]] = []
    for lineno, line in enumerate(str(raw or "").splitlines(), 1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        match = _SLOT_RE.match(text)
        if not match:
            logger.warning(f"[rest] {label}第 {lineno} 行「{text}」格式不对，已跳过")
            continue
        try:
            start = _to_minutes(match.group(1), match.group(2))
            end = _to_minutes(match.group(3), match.group(4))
        except ValueError as exc:
            logger.warning(f"[rest] {label}第 {lineno} 行被跳过：{exc}")
            continue
        if start == end:
            logger.warning(f"[rest] {label}第 {lineno} 行「{text}」起止相同，已跳过")
            continue
        slots.append((start, end))
    return slots


def parse_dates(raw: str, label: str) -> frozenset[str]:
    dates = set()
    for line in str(raw or "").splitlines():
        text = line.strip().replace("/", "-")
        if not text or text.startswith("#"):
            continue
        if not _DATE_RE.match(text):
            logger.warning(f"[rest] {label}「{text}」不是 YYYY-MM-DD，已跳过")
            continue
        dates.add(text)
    return frozenset(dates)


def _get(config, key: str, default):
    try:
        value = config.get(key, default)
    except Exception:
        return default
    return default if value is None else value


def configure(config) -> None:
    """从插件配置刷新时段规则；配置坏了就退回默认值，不让插件停摆。"""
    global _REST_SLOTS, _PEAK_SLOTS, _PEAK_DAYS, _EXTRA_HOLIDAYS, _EXTRA_WORKDAYS

    rest = parse_slots(_get(config, "rest_slots", DEFAULT_REST_SLOTS), "休息时段")
    if not rest:
        logger.warning("[rest] 休息时段为空或全部无效，回退到 23:00-07:00")
        rest = parse_slots(DEFAULT_REST_SLOTS, "休息时段")
    peak_raw = str(_get(config, "peak_slots", DEFAULT_PEAK_SLOTS))
    if not peak_raw.strip():
        peak = []  # 明确留空 = 不启用高峰时段
    else:
        peak = parse_slots(peak_raw, "高峰时段")
        if not peak:
            logger.warning("[rest] 高峰时段全部无效，回退到默认 09:00-12:00、14:00-18:00")
            peak = parse_slots(DEFAULT_PEAK_SLOTS, "高峰时段")

    mode = str(_get(config, "peak_apply_days", "workday")).strip().lower()
    if mode not in PEAK_DAY_MODES:
        logger.warning(f"[rest] peak_apply_days={mode} 不认识，按 workday 处理")
        mode = "workday"

    _REST_SLOTS = rest
    _PEAK_SLOTS = peak
    _PEAK_DAYS = mode
    _EXTRA_HOLIDAYS = parse_dates(_get(config, "extra_holidays", ""), "额外节假日")
    _EXTRA_WORKDAYS = parse_dates(_get(config, "extra_workdays", ""), "调休上班日")

    logger.info(
        "[rest] 生效配置：休息 %s ｜ 高峰 %s（%s）｜ 额外节假日 %d 天 ｜ 调休上班 %d 天",
        _fmt_slots(_REST_SLOTS),
        _fmt_slots(_PEAK_SLOTS) or "无",
        _PEAK_DAYS,
        len(_EXTRA_HOLIDAYS),
        len(_EXTRA_WORKDAYS),
    )


def _fmt_slots(slots: list[tuple[int, int]]) -> str:
    def hhmm(value: int) -> str:
        return f"{value // 60:02d}:{value % 60:02d}"

    return "、".join(f"{hhmm(a)}-{hhmm(b)}" for a, b in slots) or "无"


def _in_slots(minutes: int, slots: list[tuple[int, int]]) -> bool:
    for start, end in slots:
        if start < end:
            if start <= minutes < end:
                return True
        elif minutes >= start or minutes < end:  # 跨夜
            return True
    return False


def _is_peak_day(moment: datetime) -> bool:
    day = moment.date().isoformat()
    if _PEAK_DAYS == "never":
        return False
    if day in _EXTRA_WORKDAYS:
        return True
    if _PEAK_DAYS == "everyday":
        return True
    if _PEAK_DAYS == "weekday":
        return moment.weekday() < 5
    # workday：周一到周五，且不是法定节假日
    return moment.weekday() < 5 and day not in HOLIDAYS_2026 and day not in _EXTRA_HOLIDAYS


def is_rest(moment: datetime | None = None) -> bool:
    """当前是否处于休息时间（可配置的休息时段 + 高峰时段）。"""
    m = moment or datetime.now(TZ)
    minutes = m.hour * 60 + m.minute
    if _in_slots(minutes, _REST_SLOTS):
        return True
    return _is_peak_day(m) and _in_slots(minutes, _PEAK_SLOTS)


class RestHoursPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        configure(self.config)
        self._sig = tuple(
            str(_get(self.config, key, ""))
            for key in (
                "rest_slots",
                "peak_slots",
                "peak_apply_days",
                "extra_holidays",
                "extra_workdays",
            )
        )
        self.data_dir = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.queue_file = self.data_dir / "queue.json"
        self.queue: list[dict] = self._load()
        self._task: asyncio.Task | None = None
        try:
            self._task = asyncio.get_running_loop().create_task(self._replay_loop())
        except RuntimeError:
            # 没有运行中的事件循环时，等第一条消息到达再起任务
            self._task = None
        if self.queue:
            logger.info(f"[rest] 载入未补回的休息时段消息 {len(self.queue)} 条")

    # ---------- 配置与状态 ----------

    def _config_signature(self) -> tuple:
        return tuple(
            str(_get(self.config, key, ""))
            for key in (
                "rest_slots",
                "peak_slots",
                "peak_apply_days",
                "extra_holidays",
                "extra_workdays",
            )
        )

    def _sync_config(self) -> None:
        """WebUI 里改了配置时，下一次轮询自动生效，不用重启。"""
        signature = self._config_signature()
        if signature == self._sig:
            return
        self._sig = signature
        configure(self.config)
        logger.info("[rest] 检测到配置变化，已重新加载时段规则")

    @filter.on_astrbot_loaded()
    async def apply_config(self) -> None:
        """插件加载完后再刷一次配置，确保用的是 WebUI 里保存的值。"""
        configure(self.config)
        self._sig = self._config_signature()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("rest", alias={"休息状态"})
    async def rest_status(self, event: AstrMessageEvent):
        """查看休息时段的生效配置与当前状态。"""
        now = datetime.now(TZ)
        state = "休息中（不回复）" if is_rest(now) else "工作中（正常回复）"
        yield event.plain_result(
            "\n".join(
                [
                    f"🕐 现在 {now:%Y-%m-%d %H:%M} · {state}",
                    f"休息时段：{_fmt_slots(_REST_SLOTS)}",
                    f"高峰时段：{_fmt_slots(_PEAK_SLOTS)}（生效日：{_PEAK_DAYS}）",
                    f"额外节假日 {len(_EXTRA_HOLIDAYS)} 天 · 调休上班日 {len(_EXTRA_WORKDAYS)} 天",
                    f"待补回消息：{len(self.queue)} 条",
                ]
            )
        )

    # ---------- 持久化 ----------

    def _load(self) -> list[dict]:
        try:
            with self.queue_file.open(encoding="utf-8") as fp:
                data = json.load(fp)
            return data if isinstance(data, list) else []
        except FileNotFoundError:
            return []
        except Exception as e:  # 文件损坏时不要卡死插件
            logger.error(f"[rest] 队列读取失败，按空处理：{e}")
            return []

    def _save(self) -> None:
        try:
            tmp = self.queue_file.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as fp:
                json.dump(self.queue, fp, ensure_ascii=False)
            tmp.replace(self.queue_file)
        except Exception as e:
            logger.error(f"[rest] 队列写入失败：{e}")

    # ---------- 拦截 ----------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        try:
            # 只有「本来会回复」的消息才拦（群聊未 @ 的闲聊不拦）
            if not event.is_at_or_wake_command:
                return
            self._sync_config()
            now = datetime.now(TZ)
            if not is_rest(now):
                return
            # 私聊里的管理员不受休息时段限制，照常回复
            if event.is_private_chat() and event.is_admin():
                logger.info(f"[rest] 管理员私聊，休息时段照常回复：{event.get_sender_name()}")
                return

            text = (event.message_str or "").strip()
            if not text:
                text = "[非文本消息]"
            self.queue.append(
                {
                    "umo": event.unified_msg_origin,
                    "sender": event.get_sender_name() or "",
                    "sender_id": event.get_sender_id(),
                    "text": text[:500],
                    "ts": time.time(),
                    "group": event.get_group_id() or "",
                }
            )
            self._save()
            logger.info(
                f"[rest] 休息时段暂存 #{len(self.queue)} {event.unified_msg_origin} "
                f"{event.get_sender_name()}: {text[:40]}"
            )
            event.stop_event()
            event.should_call_llm(False)
        except Exception as e:
            logger.error(f"[rest] 拦截异常：{e}")

    # ---------- 补回 ----------

    async def _replay_loop(self):
        while True:
            try:
                await asyncio.sleep(POLL_SECONDS)
                self._sync_config()
                if not self.queue or is_rest():
                    continue
                await self._flush()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[rest] 补回循环异常：{e}")
                await asyncio.sleep(30)

    async def _flush(self):
        batch = self.queue[:MAX_BATCH]
        self.queue = self.queue[MAX_BATCH:]
        self._save()

        groups: dict[str, list[dict]] = {}
        for item in batch:
            groups.setdefault(item["umo"], []).append(item)

        for umo, msgs in groups.items():
            try:
                provider = self.context.get_using_provider(umo)
                if provider is None:
                    self.queue.extend(msgs)
                    self._save()
                    logger.warning(f"[rest] {umo} 没有可用提供商，稍后重试")
                    continue

                system_prompt = ""
                try:
                    persona = await self.context.persona_manager.get_default_persona_v3(umo)
                    system_prompt = getattr(persona, "system_prompt", "") or ""
                except Exception as e:
                    logger.warning(f"[rest] 取人格失败，改用默认提示：{e}")

                growth = build_growth_prompt(self.context)
                if growth:
                    system_prompt = f"{system_prompt}\n\n{growth}".strip()

                resp = await provider.text_chat(
                    prompt=self._build_prompt(msgs),
                    session_id=umo,
                    system_prompt=system_prompt,
                )
                text = (getattr(resp, "completion_text", "") or "").strip()
                if not text:
                    logger.warning(f"[rest] {umo} 模型返回空文本，丢弃本批 {len(msgs)} 条")
                    continue
                ok = await self.context.send_message(umo, MessageChain([Plain(text)]))
                logger.info(
                    f"[rest] 休息结束补回 {len(msgs)} 条 → {umo}，发送{'成功' if ok else '失败'}"
                )
            except Exception as e:
                logger.error(f"[rest] {umo} 补回失败：{e}")
                self.queue.extend(msgs)
                self._save()

    @staticmethod
    def _build_prompt(msgs: list[dict]) -> str:
        now = datetime.now(TZ)
        lines = [
            "【休息时段攒下的消息，现在补回】",
            f"现在 {now:%m-%d %H:%M}。下面这些消息是在你的休息时间发来的，当时没有回复：",
        ]
        for it in msgs:
            t = datetime.fromtimestamp(it["ts"], TZ).strftime("%m-%d %H:%M")
            lines.append(f"[{t}] {it['sender']}：{it['text']}")
        lines.append("")
        lines.append(
            "请像刚忙完回来一样自然地回应：同一个人发的多条合并成一段回，"
            "不要逐条复述原文，不要解释“消息被延迟”这类系统细节，"
            "“刚在休息/刚忙完”最多说一句。"
        )
        return "\n".join(lines)

    async def terminate(self):
        if self._task:
            self._task.cancel()

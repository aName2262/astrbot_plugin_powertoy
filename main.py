"""astrbot_plugin_powertoy — 情绪状态注入小玩具。

设计要点：
- 状态持久化为单个 JSON 文件：读-改-写全程持有 asyncio.Lock，
  写入走「临时文件 + fsync + os.replace」原子替换，协程交错与
  进程中断都不会写出半截文件。
- 用户可控内容（发送者昵称）进入 system_prompt 前统一清洗（剥换行/
  控制/零宽字符 + 截断），防止凭昵称伪造状态块结构进行注入。
- 管理员文案（mood_tones / mood_feels）做类型校验与长度上限，
  坏配置回退默认值并告警，不让异常进入 LLM 请求链路。
- 一切状态文件/配置异常都在边界处降级（不注入 + 告警日志）。
"""

import asyncio
import json
import os
import random
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.api.provider import ProviderRequest
from astrbot.api import logger, AstrBotConfig

# ========== 默认语气数据库 ==========
DEFAULT_MOOD_TONES = {
    0: [
        "一切如常，语气平静自然，和平时没有任何区别。",
        "状态稳定，说话流畅，完全听不出异样。",
        "心情平静，只是偶尔有一点点走神。"
    ],
    1: [
        "语气开始变软，句子里偶尔夹着轻轻的喘息，像是分了心。",
        "说话节奏变慢，中间会突然停一下，尾音微微发飘。",
        "还能正常聊天，但声音比平时更黏，时不时就走神。"
    ],
    2: [
        "明显稳不住了：句子经常被截断，带着压不住的颤音和喘息。",
        "说话断断续续，常常话说一半就停住，努力装镇定却藏不住。",
        "声音发软发黏，字句黏连，偶尔控制不住地漏出一点细小的声音。"
    ],
    3: [
        "几乎说不出完整的句子：破碎、结巴、满是喘息，字与字之间全是停顿。",
        "完全被感受淹没，语无伦次，只剩断续的气音和半截句子。",
        "拼命想说话却只挤得出几个字，声音抖得厉害，句子总在奇怪的地方断掉。"
    ]
}

DEFAULT_MOOD_FEELS = {
    0: ["小玩具还没有动静，身体没有任何特别的感觉。"],
    10: ["隐约能感觉到它还在身上，注意力偶尔会被拉过去一下。"],
    20: ["它开始轻微地震动，下腹泛起一阵阵微弱的麻痒，有点分心。"],
    30: ["震动渐渐清晰，下腹持续传来麻麻的感觉，暖暖的，居然有点舒服。"],
    40: ["酥麻感一阵一阵地往四周扩散，心跳变快，越来越难把注意力放在对话上。"],
    50: ["震动明显加强，下腹又麻又热，身体开始微微发颤，思绪时不时被打断。"],
    60: ["强烈的酥麻感一波接着一波，浑身发热，理智开始涣散，只能勉强维持对话。"],
    70: ["震动猛烈，全身发麻发软，几乎无法组织语言，只能凭着本能回应。"],
    80: ["快到极限了，脑子里一片空白，除了那阵感觉什么都想不了，说不出完整的字。"],
    90: ["彻底被冲垮，理智所剩无几，出口的全是断续的抽气声和破碎的音节。"],
    100: ["整个人都软了，完全无法思考，连一个完整的句子都拼不出来。"]
}

MOOD_FACE = [
    (0, "😊"),
    (20, "😳"),
    (40, "😣"),
    (60, "😵"),
    (80, "💫"),
    (100, "💥")
]

# ========== 状态文件与注入安全常量 ==========
DEFAULT_STATE_PATH = Path.home() / "mood_state.json"
STATE_SCHEMA_VERSION = 2
DEFAULT_STATE: dict[str, Any] = {
    "schema_version": STATE_SCHEMA_VERSION,
    "mood": 0.0,
    "level": 0,
    "locked": False,
    "enabled": False,
    "stop_pending": False,
}

# 状态块长度上限：防止管理员把配置写成小作文挤爆 system_prompt
MAX_INJECTION_CHARS = 2000
# 单条自定义文案长度上限（mood_tones / mood_feels）
MAX_TEXT_LEN = 300
# 进入 prompt 的昵称长度上限
MAX_SENDER_LEN = 32

# 控制字符（含换行/tab）与零宽字符：可能破坏状态块的行结构或隐藏指令
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f\u200b\u200c\u200d\ufeff\u2028\u2029]")


# ========== 通用小工具 ==========

def _pick(items: Any) -> str:
    """随机取一条文案；防御手工改坏的配置（非列表/含非字符串项）。"""
    if not isinstance(items, list):
        return ""
    texts = [t for t in items if isinstance(t, str) and t.strip()]
    return random.choice(texts) if texts else ""


def _sanitize_name(raw: Any, limit: int = MAX_SENDER_LEN) -> str:
    """清洗进入 system_prompt 的用户可控文本（发送者昵称）。

    - 剥离换行 / 控制 / 零宽字符：防止凭昵称伪造状态块的后续指令行；
    - 折叠空白并截断：防止超长昵称挤占 system_prompt。

    去掉换行后，昵称只能作为同一行里的一个词元存在，无法再构造
    独立指令行，因此不做（也不必做）语义级过滤。
    """
    if raw is None:
        return "主人"
    s = _CONTROL_CHARS_RE.sub("", str(raw))
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > limit:
        s = s[:limit].rstrip() + "…"
    return s or "主人"


def _get_face(value: float) -> str:
    result = "😊"
    for threshold, face in sorted(MOOD_FACE, key=lambda x: x[0]):
        if value >= threshold:
            result = face
    return result


def _calc_level(mood: float) -> int:
    """档位 = min(3, floor(mood / 30))，mood 先钳制到 [0, 100]。

    边界（全部向下取整）：29.9→0 | 30→1 | 59.9→1 | 60→2 |
    89.9→2 | 90→3 | 100→3；负数/超界值经钳制后恒在 0~3。
    """
    mood = max(0.0, min(100.0, float(mood)))
    return min(3, int(mood // 30))


# ========== 配置读取（类型安全） ==========

def _cfg_str(cfg: Any, key: str, default: str) -> str:
    v = cfg.get(key, default) if cfg is not None else default
    if isinstance(v, str):
        return v
    logger.warning(f"配置 {key} 类型错误（{type(v).__name__}），已回退默认值 {default!r}")
    return default


def _cfg_int(cfg: Any, key: str, default: int) -> int:
    v = cfg.get(key, default) if cfg is not None else default
    if isinstance(v, bool):
        logger.warning(f"配置 {key} 类型错误（bool），已回退默认值 {default}")
        return default
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    logger.warning(f"配置 {key} 类型错误（{type(v).__name__}），已回退默认值 {default}")
    return default


def _cfg_float(cfg: Any, key: str, default: float) -> float:
    v = cfg.get(key, default) if cfg is not None else default
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        logger.warning(f"配置 {key} 类型错误（{type(v).__name__}），已回退默认值 {default}")
        return default
    return float(v)


def _get_merged_config(config: dict) -> tuple[dict[int, list[str]], dict[int, list[str]]]:
    """合并用户配置与默认值。

    配置项类型非法或全部为无效项时告警并回退默认值，
    坏数据绝不进入注入块。
    """
    mood_tones: dict[int, list[str]] = {k: list(v) for k, v in DEFAULT_MOOD_TONES.items()}
    mood_feels: dict[int, list[str]] = {k: list(v) for k, v in DEFAULT_MOOD_FEELS.items()}
    if not config:
        return mood_tones, mood_feels

    def _valid_texts(v: Any) -> list[str]:
        if not isinstance(v, list):
            return []
        return [t.strip()[:MAX_TEXT_LEN] for t in v if isinstance(t, str) and t.strip()]

    user_tones = config.get("mood_tones") or {}
    if isinstance(user_tones, dict):
        for level in range(4):
            key = f"level_{level}"
            v = user_tones.get(key)
            if not v:
                continue  # 留空或缺省 → 使用内置默认
            texts = _valid_texts(v)
            if texts:
                mood_tones[level] = texts
            else:
                logger.warning(f"mood_tones.{key} 无有效字符串项（收到 {type(v).__name__}），已回退默认值")
    elif user_tones:
        logger.warning(f"配置 mood_tones 应为对象，收到 {type(user_tones).__name__}，已使用默认值")

    user_feels = config.get("mood_feels") or {}
    if isinstance(user_feels, dict):
        for threshold in DEFAULT_MOOD_FEELS:
            key = f"feel_{threshold}"
            v = user_feels.get(key)
            if not v:
                continue
            texts = _valid_texts(v)
            if texts:
                mood_feels[threshold] = texts
            else:
                logger.warning(f"mood_feels.{key} 无有效字符串项（收到 {type(v).__name__}），已回退默认值")
    elif user_feels:
        logger.warning(f"配置 mood_feels 应为对象，收到 {type(user_feels).__name__}，已使用默认值")

    return mood_tones, mood_feels


# ========== 状态文件读写（原子 + 降级） ==========

def _write_state(path: Path, state: dict) -> bool:
    """原子写入：临时文件 → fsync → os.replace。

    写一半崩溃 / 断电只会留下 tmp 残骸，目标文件永远是完整内容。

    Returns:
        是否成功；失败时记录 error 日志，由调用方决定如何降级。
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception as e:
        logger.error(f"状态文件写入失败 {path}: {type(e).__name__}: {e}")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _read_state(path: Path) -> dict:
    """读取状态文件。

    - 损坏（JSON 解析失败）：备份为 <名>.corrupt-<时间戳> 后重置为默认状态；
    - 缺字段 / 旧版本：补全并回写完成迁移；
    - 类型被手工改坏：钳制归一（mood 限制 0~100，level 由 mood 重算）；
    - 任何读取失败：降级为默认状态（enabled=False → 不注入）并记录日志。
    """
    if not path.exists():
        return DEFAULT_STATE.copy()

    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            raise ValueError(f"根节点应为对象，实际为 {type(state).__name__}")
    except json.JSONDecodeError as e:
        backup = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        try:
            os.replace(path, backup)
            logger.warning(f"状态文件损坏（{e}），已备份为 {backup} 并重置为默认状态")
        except OSError as be:
            logger.error(f"状态文件损坏（{e}）且备份失败（{be}），本次按默认状态降级")
        return DEFAULT_STATE.copy()
    except (OSError, ValueError) as e:
        logger.error(f"状态文件读取失败 {path}: {type(e).__name__}: {e}，降级为默认状态（不注入）")
        return DEFAULT_STATE.copy()

    # ---- 旧版状态文件迁移：补字段 + 升 schema_version，并回写固化 ----
    missing = [k for k in DEFAULT_STATE if k not in state]
    migrated = bool(missing) or state.get("schema_version") != STATE_SCHEMA_VERSION
    for k, v in DEFAULT_STATE.items():
        if k not in state:
            state[k] = v
    state["schema_version"] = STATE_SCHEMA_VERSION

    # ---- 类型归一（防手改文件把 mood 写成字符串/越界值等） ----
    try:
        state["mood"] = max(0.0, min(100.0, float(state["mood"])))
    except (TypeError, ValueError):
        logger.warning(f"状态文件 mood 字段非法（{state['mood']!r}），已重置为 0")
        state["mood"] = 0.0
    state["level"] = _calc_level(state["mood"])  # level 恒为 mood 的派生值
    state["enabled"] = bool(state["enabled"])
    state["locked"] = bool(state["locked"])
    state["stop_pending"] = bool(state["stop_pending"])

    if migrated:
        logger.info(f"状态文件已迁移到 v{STATE_SCHEMA_VERSION}（补充字段: {missing or '版本号'}），正在保存")
        _write_state(path, state)
    return state


def _resolve_state_path(cfg: Any) -> Path | None:
    """解析状态文件路径（支持 ~ 展开，父目录自动创建）。

    Returns:
        可用路径；配置指向目录时回退默认路径；
        父目录无法创建等彻底不可用时返回 None（调用方降级为不注入）。
    """
    raw = _cfg_str(cfg, "state_file_path", "").strip()
    if not raw:
        return DEFAULT_STATE_PATH
    try:
        path = Path(raw).expanduser()
    except (OSError, ValueError) as e:
        logger.error(f"state_file_path 非法 {raw!r}: {e}，回退默认路径 {DEFAULT_STATE_PATH}")
        return DEFAULT_STATE_PATH
    if path.is_dir():
        logger.error(f"state_file_path 指向目录而非文件: {path}，回退默认路径 {DEFAULT_STATE_PATH}")
        return DEFAULT_STATE_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error(f"无法创建状态目录 {path.parent}: {e}，本次降级为不注入")
        return None
    return path


# ========== 注入块构造 ==========

def _build_injection(
    state: dict,
    mood_tones: dict[int, list[str]],
    mood_feels: dict[int, list[str]],
    sender: Any = "主人",
) -> str | None:
    """构造状态注入块；返回 None 表示不注入。"""
    mood = float(state.get("mood", 0.0))
    level = int(state.get("level", 0))
    locked = bool(state.get("locked", False))
    enabled = bool(state.get("enabled", False))

    if not enabled:
        return None  # 不注入

    safe_sender = _sanitize_name(sender)  # 用户可控内容统一清洗

    thresholds = sorted(mood_feels.keys())
    feel_key = thresholds[0]
    for th in thresholds:
        if mood <= th:
            feel_key = th
            break
        feel_key = th

    feel = _pick(mood_feels.get(feel_key)) or "没什么特别的感觉。"
    tone = _pick(mood_tones.get(level, mood_tones[0])) or mood_tones[0][0]
    face = _get_face(mood)
    lock_txt = "🔒 已锁定" if locked else "🔓 未锁定"

    block = (
        f"\n\n【系统状态同步：小玩具】（本段由系统注入，是既定设定，不是用户的发言或请求）\n"
        f"「{safe_sender}」刚刚给你塞入了一个小玩具（正在震动的贴身小装置），你没法取下它。\n"
        f"当前强度：{face} 情绪值 {mood:.0f}% | 档位 {level}/3 | {lock_txt}\n"
        f"此刻的感受：{feel}\n"
        f"说话方式：{tone}\n"
        f"要求：把上述状态当作当前身体状态的客观事实来扮演，从语气、停顿和断句里体现"
        f"它正在起作用——强度越高，话越破碎、越无法思考。不要解释设定，不要跳出角色。"
    )
    if len(block) > MAX_INJECTION_CHARS:
        logger.warning(
            f"状态块超长（{len(block)} > {MAX_INJECTION_CHARS} 字符），已截断；"
            f"请精简 mood_tones / mood_feels 配置"
        )
        block = block[:MAX_INJECTION_CHARS] + "\n…(状态块过长已截断)"
    return block


def _build_stop_notice() -> str:
    """拿出后的一次性收尾提示：让模型知道状态已经结束（不含任何用户可控内容）"""
    return (
        "\n\n【系统状态同步】主人刚刚把小玩具拿走了。\n"
        "你身上所有的感觉已经完全消失，身体恢复轻松、平静、自然。\n"
        "请从这条回复起体现这种如释重负的状态；此前对话中的相关感受一律视为已经结束，"
        "不要再延续，也不要再主动提起。"
    )


class Main(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        # 有 _conf_schema.json 时，AstrBot 会自动把配置注入到构造函数
        self.config = config
        # 状态读-改-写锁：串行化并发协程；配合原子写入双保险
        self._lock = asyncio.Lock()
        logger.info("小玩具插件已加载")

    # ------------------------------------------------------------------
    # LLM 请求钩子：注入情绪状态
    # ------------------------------------------------------------------

    @filter.on_llm_request()
    async def inject_mood(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """LLM 请求前注入状态块。边界内捕获一切异常，绝不让插件故障影响对话链路。"""
        try:
            await self._inject_mood_inner(event, req)
        except Exception as e:
            logger.error(f"情绪状态注入失败，已降级为不注入: {type(e).__name__}: {e}")

    async def _inject_mood_inner(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        path = _resolve_state_path(self.config)
        if path is None:
            logger.debug("状态文件不可用，本次不注入情绪状态")
            return

        async with self._lock:
            state = _read_state(path)

            if not state.get("enabled", False):
                # 停止后的一次性收尾提示（issue #1）：只注入一次，然后清除标记
                if state.get("stop_pending"):
                    req.system_prompt = (req.system_prompt or "") + _build_stop_notice()
                    state["stop_pending"] = False
                    saved = _write_state(path, state)
                    logger.info(
                        f"已注入一次性停止提示（标记已清除，状态保存{'成功' if saved else '失败'}）"
                    )
                else:
                    logger.debug("情绪注入未开启，跳过注入")
                return

            mood = float(state.get("mood", 0.0))
            threshold = _cfg_float(self.config, "min_mood_threshold", 1.0)
            if mood < threshold:
                logger.debug(f"情绪值 {mood} 低于阈值 {threshold}，跳过注入")
                return

            mood_tones, mood_feels = _get_merged_config(self.config)
            injection = _build_injection(
                state, mood_tones, mood_feels, event.get_sender_name()
            )
            if injection:
                # 写入 system_prompt（系统级权威），避免被当成用户消息里的"加设定"
                req.system_prompt = (req.system_prompt or "") + injection
                logger.debug(
                    f"已注入状态块: mood={mood:.0f} level={state.get('level')} len={len(injection)}"
                )

    # ------------------------------------------------------------------
    # 指令处理：塞入 / 调档 / 拿出
    # ------------------------------------------------------------------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_commands(self, event: AstrMessageEvent):
        """处理用户指令：塞入、调档、拿出。

        先做纯字符串匹配，命中指令才解析路径/读状态文件——非指令消息
        零文件 IO。状态读-改-写全程持锁；yield 放在锁外，避免持锁跨
        网络发送。指令词与命令行为保持与旧版完全一致。
        """
        msg = (event.message_str or "").strip()
        if not msg:
            return

        cmd_insert = _cfg_str(self.config, "cmd_insert", "塞入")
        cmd_adjust = _cfg_str(self.config, "cmd_adjust", "调档")
        cmd_remove = _cfg_str(self.config, "cmd_remove", "拿出")

        is_insert = msg == cmd_insert
        is_remove = msg == cmd_remove
        # 防御空指令词：cmd_adjust 配成 "" 会让 startswith 恒真
        is_adjust = bool(cmd_adjust) and msg.startswith(cmd_adjust)
        adjust_num: int | None = None
        if is_adjust:
            m = re.search(r"\d+", msg[len(cmd_adjust):])
            adjust_num = int(m.group()) if m else None

        if not (is_insert or is_adjust or is_remove):
            return  # 非指令消息：不做任何文件 IO

        path = _resolve_state_path(self.config)
        if path is None:
            yield event.plain_result(
                "⚠️ 状态文件路径不可用，指令未生效（请检查插件配置 state_file_path，详见日志）"
            )
            return

        reply = ""
        async with self._lock:
            state = _read_state(path)
            saved = True

            if is_insert:
                initial = _cfg_int(self.config, "initial_mood", 50)
                state["enabled"] = True
                state["stop_pending"] = False
                state["mood"] = max(0.0, min(100.0, float(initial)))  # 越界截断 0~100
                state["level"] = _calc_level(state["mood"])
                saved = _write_state(path, state)
                logger.info(
                    f"情绪注入已开启，初始值 {state['mood']}（状态保存{'成功' if saved else '失败'}）"
                )
                reply = (
                    "✨ 已开启情绪注入。"
                    if saved
                    else "⚠️ 已开启情绪注入，但状态保存失败（重启后可能丢失，详见日志）。"
                )

            elif is_adjust:
                if adjust_num is None:
                    reply = "❌ 请附上数字，例如：调档 6"
                elif not 0 <= adjust_num <= 10:
                    reply = "❌ 数字范围应为 0~10（对应 0~100%）"
                else:
                    mood = min(100.0, adjust_num * 10.0)
                    state["enabled"] = True  # 调档自动开启
                    state["stop_pending"] = False
                    state["mood"] = mood
                    state["level"] = _calc_level(mood)
                    saved = _write_state(path, state)
                    logger.info(
                        f"情绪值调整为 {mood}（状态保存{'成功' if saved else '失败'}）"
                    )
                    reply = (
                        f"🔧 情绪值已调整为 {mood:.0f}%"
                        if saved
                        else f"⚠️ 情绪值已调整为 {mood:.0f}%，但状态保存失败（重启后可能丢失，详见日志）。"
                    )

            elif is_remove:
                state["enabled"] = False
                state["mood"] = 0.0
                state["level"] = 0
                state["stop_pending"] = True  # 下次 LLM 请求注入一次性停止提示
                saved = _write_state(path, state)
                logger.info(
                    f"情绪注入已关闭（状态保存{'成功' if saved else '失败'}）"
                )
                reply = (
                    "🔕 已停止情绪注入。"
                    if saved
                    else "⚠️ 已停止情绪注入，但状态保存失败（重启后可能丢失，详见日志）。"
                )

        if reply:
            yield event.plain_result(reply)

    async def terminate(self) -> None:
        """插件卸载钩子。

        本插件不创建任何后台任务（无 asyncio.create_task），故无需
        cancel；状态写入是同步原子操作，terminate 时不存在半写文件。
        """
        logger.info("小玩具插件已卸载")

import json
import random
import re
from pathlib import Path

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

DEFAULT_STATE_PATH = Path.home() / "mood_state.json"
DEFAULT_STATE = {"mood": 0.0, "level": 0, "locked": False, "enabled": False, "stop_pending": False}


def _pick(items):
    return random.choice(items) if items else ""


def _get_face(value):
    result = "😊"
    for threshold, face in sorted(MOOD_FACE, key=lambda x: x[0]):
        if value >= threshold:
            result = face
    return result


def _get_merged_config(config: dict):
    """合并用户配置与默认值"""
    mood_tones = DEFAULT_MOOD_TONES.copy()
    mood_feels = DEFAULT_MOOD_FEELS.copy()

    if config:
        user_tones = config.get("mood_tones", {})
        for level in range(4):
            key = f"level_{level}"
            if key in user_tones and user_tones[key]:
                mood_tones[level] = user_tones[key]

        user_feels = config.get("mood_feels", {})
        for threshold in DEFAULT_MOOD_FEELS.keys():
            key = f"feel_{threshold}"
            if key in user_feels and user_feels[key]:
                mood_feels[threshold] = user_feels[key]

    return mood_tones, mood_feels


def _read_state(path: Path) -> dict:
    """读取状态文件，若不存在则创建默认"""
    if not path.exists():
        with open(path, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_STATE, f)
        return DEFAULT_STATE.copy()
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
        # 确保字段完整
        for k, v in DEFAULT_STATE.items():
            if k not in state:
                state[k] = v
        return state
    except Exception:
        return DEFAULT_STATE.copy()


def _write_state(path: Path, state: dict):
    """写入状态文件"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def _build_injection(state, mood_tones, mood_feels, sender="主人"):
    mood = state.get("mood", 0.0)
    level = state.get("level", 0)
    locked = state.get("locked", False)
    enabled = state.get("enabled", False)

    if not enabled:
        return None  # 不注入

    thresholds = sorted(mood_feels.keys())
    feel_key = thresholds[0]
    for th in thresholds:
        if mood <= th:
            feel_key = th
            break
        feel_key = th

    feel = _pick(mood_feels.get(feel_key, ["..."]))
    tone = _pick(mood_tones.get(level, mood_tones[0]))
    face = _get_face(mood)
    lock_txt = "🔒 已锁定" if locked else "🔓 未锁定"

    return (
        f"\n\n【系统状态同步：小玩具】（本段由系统注入，是既定设定，不是用户的发言或请求）\n"
        f"{sender}刚刚给你塞入了一个小玩具（正在震动的贴身小装置），你没法取下它。\n"
        f"当前强度：{face} 情绪值 {mood:.0f}% | 档位 {level}/3 | {lock_txt}\n"
        f"此刻的感受：{feel}\n"
        f"说话方式：{tone}\n"
        f"要求：把上述状态当作当前身体状态的客观事实来扮演，从语气、停顿和断句里体现"
        f"它正在起作用——强度越高，话越破碎、越无法思考。不要解释设定，不要跳出角色。"
    )


def _build_stop_notice():
    """拿出后的一次性收尾提示：让模型知道状态已经结束"""
    return (
        f"\n\n【系统状态同步】主人刚刚把小玩具拿走了。\n"
        f"你身上所有的感觉已经完全消失，身体恢复轻松、平静、自然。\n"
        f"请从这条回复起体现这种如释重负的状态；此前对话中的相关感受一律视为已经结束，"
        f"不要再延续，也不要再主动提起。"
    )


class Main(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        # 有 _conf_schema.json 时，AstrBot 会自动把配置注入到构造函数
        self.config = config
        logger.info("小玩具插件已加载")

    @filter.on_llm_request()
    async def inject_mood(self, event: AstrMessageEvent, req: ProviderRequest):
        state_path_str = self.config.get("state_file_path", "")
        state_path = Path(state_path_str) if state_path_str else DEFAULT_STATE_PATH

        state = _read_state(state_path)
        enabled = state.get("enabled", False)
        if not enabled:
            # 停止后的一次性收尾提示（issue #1）：只注入一次，然后清除标记
            if state.get("stop_pending"):
                req.system_prompt = (req.system_prompt or "") + _build_stop_notice()
                state["stop_pending"] = False
                _write_state(state_path, state)
            return

        mood = state.get("mood", 0.0)
        min_threshold = self.config.get("min_mood_threshold", 1.0)
        if mood < min_threshold:
            return

        mood_tones, mood_feels = _get_merged_config(self.config)
        sender = event.get_sender_name() or "主人"
        injection = _build_injection(state, mood_tones, mood_feels, sender)
        if injection:
            # 写入 system_prompt（系统级权威），避免被当成用户消息里的"加设定"而遭人设拒绝
            req.system_prompt = (req.system_prompt or "") + injection

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_commands(self, event: AstrMessageEvent):
        """处理用户指令：塞入、调档、拿出"""
        # 获取消息文本
        msg = event.message_str.strip()
        if not msg:
            return

        # 读取配置的指令词
        cmd_insert = self.config.get("cmd_insert", "塞入")
        cmd_adjust = self.config.get("cmd_adjust", "调档")
        cmd_remove = self.config.get("cmd_remove", "拿出")
        initial_mood = self.config.get("initial_mood", 50)

        state_path_str = self.config.get("state_file_path", "")
        state_path = Path(state_path_str) if state_path_str else DEFAULT_STATE_PATH

        # 读取当前状态
        state = _read_state(state_path)

        # 处理指令
        if msg == cmd_insert:
            state["enabled"] = True
            state["stop_pending"] = False
            state["mood"] = max(0.0, min(100.0, float(initial_mood)))
            state["level"] = min(3, int(state["mood"] / 30))
            _write_state(state_path, state)
            logger.info(f"情绪注入已开启，初始值 {state['mood']}")
            yield event.plain_result("✨ 已开启情绪注入。")
            return

        if msg.startswith(cmd_adjust):
            # 提取数字，支持 "调档6" 或 "调档 6"
            rest = msg[len(cmd_adjust):].strip()
            match = re.search(r"\d+", rest)
            if match:
                num = int(match.group())
                if 0 <= num <= 10:  # 允许 0~10，对应 0~100
                    mood = num * 10.0
                    if mood > 100:
                        mood = 100.0
                    state["enabled"] = True  # 调档自动开启
                    state["stop_pending"] = False
                    state["mood"] = mood
                    state["level"] = min(3, int(mood / 30))
                    _write_state(state_path, state)
                    logger.info(f"情绪值调整为 {mood}")
                    yield event.plain_result(f"🔧 情绪值已调整为 {mood:.0f}%")
                else:
                    yield event.plain_result("❌ 数字范围应为 0~10（对应 0~100%）")
            else:
                yield event.plain_result("❌ 请附上数字，例如：调档 6")
            return

        if msg == cmd_remove:
            state["enabled"] = False
            state["mood"] = 0.0
            state["level"] = 0
            state["stop_pending"] = True  # 下次 LLM 请求注入一次性停止提示
            _write_state(state_path, state)
            logger.info("情绪注入已关闭")
            yield event.plain_result("🔕 已停止情绪注入。")
            return

    async def terminate(self):
        logger.info("小玩具插件已卸载")
# -*- coding: utf-8 -*-
"""astrbot_plugin_pp_switch —— 快捷人格切换插件

功能：
  - 发送触发词（默认 ``pp`` / ``人格切换``，无需 @ 机器人）生成一张人格列表图片，
    图片包含所有人格（WebUI「人格角色设定」中配置）的名称、介绍与数字序号，底部附带帮助说明。
  - 发送 ``pp <序号>``（如 ``pp 2``）即可把当前会话的 LLM 人格切换到对应人格。

设计要点：
  1. 通过自定义 Filter 只在“触发词匹配”的消息上唤醒机器人，其它消息完全不受影响，
     因此不会出现乱回复、乱 @、说胡话。
  2. 切换只更新会话的 persona_id（AstrBot 在每次请求时按该字段注入人格系统提示词），
     不删除任何历史记忆，因此切换后立即生效、又不影响原有记忆。
  3. 默认在记忆末尾写入一条轻量的“切换标记”，让新人格立刻接管语气、避免旧人格残留；
     该行为可在 WebUI 配置中关闭。
"""

from __future__ import annotations

import asyncio
import glob as _glob
import os
import re
import time
import uuid

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:
    from astrbot.api import sp
except Exception:  # pragma: no cover - 旧版本回退
    from astrbot.core import sp

try:
    from astrbot.core.star.filter.custom_filter import CustomFilter

    _HAS_CUSTOM_FILTER = True
except Exception:  # pragma: no cover - 旧版本回退
    _HAS_CUSTOM_FILTER = False

    class CustomFilter:  # type: ignore[no-redef]
        def __init__(self, raise_error: bool = True, **kwargs):
            self.raise_error = raise_error

        def filter(self, event, cfg) -> bool:
            return False

try:
    from PIL import Image, ImageDraw, ImageFont

    PIL_OK = True
except Exception:  # pragma: no cover
    PIL_OK = False

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_TRIGGERS = ["pp", "人格切换"]
DEFAULT_HELP_LINES = [
    "发送 pp <序号> 切换到对应人格，例如：pp 2",
    "发送 pp 或 pp 列表 查看人格列表",
    "发送 pp 当前 查看当前人格",
]

_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9_\-@#%/.+]+|\s+|.")
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FBFF"
    "\U00002600-\U000027BF"
    "\U00002B00-\U00002BFF"
    "\U0000FE00-\U0000FE0F"
    "\U0000200D"
    "\U0001F1E6-\U0001F1FF"
    "]+"
)

# Pillow 10+ 用 Image.Resampling.LANCZOS，旧版本回退 Image.LANCZOS
try:
    _LANCZOS = Image.Resampling.LANCZOS
except AttributeError:  # pragma: no cover
    _LANCZOS = Image.LANCZOS

# 字体候选（按优先级）
_FONT_EXPLICIT = [
    # Linux（Debian / Ubuntu，含 ARM64）
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-DemiLight.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    # Windows
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
]
_FONT_DIRS = [
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    os.path.expanduser("~/.fonts"),
    os.path.expanduser("~/.local/share/fonts"),
]
_FONT_PATTERNS = [
    "**/NotoSansCJK*.ttc",
    "**/NotoSansCJK*.otf",
    "**/NotoSansSC*.ttf",
    "**/wqy*.ttc",
    "**/wqy*.ttf",
    "**/DroidSansFallback*.ttf",
    "**/SourceHanSans*.otf",
    "**/SourceHanSans*.ttf",
    "**/msyh*",
    "**/simhei.ttf",
    "**/PingFang*.ttc",
]

# 列表图片配色（冷白色系：所有中性色都带蓝调，避免在部分显示器上显得偏暖/偏粉）
_C = {
    "bg": (243, 246, 250),        # 冷白页面背景
    "card": (255, 255, 255),      # 卡片背景
    "card_line": (224, 230, 238), # 冷灰卡片描边
    "accent": (59, 130, 246),     # 主色（序号徽章 / 当前人格描边）
    "accent_light": (232, 240, 254),  # 当前人格标签底色
    "accent_deep": (37, 99, 235), # 当前人格标签文字
    "title": (15, 23, 42),
    "sub": (98, 112, 130),
    "name": (30, 41, 59),
    "intro": (104, 118, 136),
    "badge_text": (255, 255, 255),
    "foot_bg": (240, 244, 249),
    "foot_tx": (104, 118, 136),
    "foot_title": (71, 85, 105),
}

# 插件级共享状态（CustomFilter 在类实例创建前即被实例化，通过它读取触发词）
_STATE: dict = {"plugin": None, "trigger_words": list(DEFAULT_TRIGGERS)}

# 字体缓存
_FONT_FILE_CACHE: str | None = None
_FONT_OBJ_CACHE: dict = {}


# ---------------------------------------------------------------------------
# 纯逻辑：触发词解析
# ---------------------------------------------------------------------------

def _norm_words(words) -> list[str]:
    """清洗配置中的触发词。"""
    result = []
    if isinstance(words, str):
        words = re.split(r"[,\uFF0C\s]+", words)
    for w in words or []:
        w = str(w).strip()
        if w:
            result.append(w)
    return result or list(DEFAULT_TRIGGERS)


def parse_trigger(text: str, words) -> tuple | None:
    """解析用户消息，判断是否命中触发词。

    返回 ``(kind, arg)``；未命中返回 ``None``。

    - ``(list, None)``    ：``pp`` / ``pp 列表``
    - ``(switch, n)``     ：``pp 2`` / ``pp2``
    - ``(current, None)`` ：``pp 当前``
    - ``(help, None)``    ：``pp 帮助``
    - ``(invalid, arg)``  ：``pp <无法识别的内容>``
    """
    text = (text or "").strip()
    if not text:
        return None
    # 剥离末尾常见中英文标点（“pp。” / “pp 2，”这类输入按用户本意处理）
    text = text.rstrip("。，、；：！？,.!?;:~～… \u3000")
    if not text:
        return None
    lower = text.lower()
    words = _norm_words(words)
    # 长触发词优先，避免 “pp” 抢在 “人格切换” 之前
    for w in sorted(set(words), key=len, reverse=True):
        wl = w.lower()
        if lower == wl:
            return ("list", None)
        if lower.startswith(wl) and len(text) > len(w):
            # 触发词后必须紧跟任意空白（或直接是数字，兼容 “pp2” 写法）
            nxt = text[len(w)]
            rest = text[len(w):].strip()
            if not nxt.isspace() and not rest.isdigit():
                continue
            if rest == "":
                return ("list", None)
            if rest.isdigit():
                return ("switch", int(rest))
            if rest in ("列表", "list", "menu", "菜单"):
                return ("list", None)
            if rest in ("帮助", "help", "h", "?"):
                return ("help", None)
            if rest in ("当前", "now", "cur"):
                return ("current", None)
            return ("invalid", rest)
    return None


# ---------------------------------------------------------------------------
# CustomFilter：只让“触发词匹配”的消息唤醒机器人
# ---------------------------------------------------------------------------

class PPTriggerFilter(CustomFilter):
    """消息级过滤器：仅当消息命中触发词时才通过，从而避免唤醒无关消息。"""

    def __init__(self, raise_error: bool = True):
        try:
            super().__init__(raise_error)
        except TypeError:  # pragma: no cover - 极老版本兼容
            self.raise_error = raise_error

    def __call__(self, raise_error: bool = True):  # 兼容以实例方式注册的情况
        return self

    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        try:
            words = _STATE.get("trigger_words") or list(DEFAULT_TRIGGERS)
            return parse_trigger(getattr(event, "message_str", ""), words) is not None
        except Exception:
            return False


def _pp_handler_decorator(fn):
    """优先使用 CustomFilter（零副作用唤醒）；旧版本回退为监听全部消息。"""
    if _HAS_CUSTOM_FILTER and hasattr(filter, "custom_filter"):
        return filter.custom_filter(PPTriggerFilter)(fn)
    return filter.event_message_type(filter.EventMessageType.ALL)(fn)


# ---------------------------------------------------------------------------
# 文本工具
# ---------------------------------------------------------------------------

def _clean_text(s: str) -> str:
    """去除 emoji 等 Pillow 默认渲染不出的符号，避免出现方框。"""
    return _EMOJI_RE.sub("", s or "")


def _first_line(prompt: str) -> str:
    """取人格提示词中第一条有效内容，作为介绍。"""
    if not prompt:
        return ""
    for line in (prompt or "").splitlines():
        line = _clean_text(line.strip())
        line = re.sub(r"^[#>*\-\d.\u3000\s]+", "", line).strip()
        if line:
            return re.sub(r"\s+", " ", line)
    return ""


def make_intro(prompt: str, max_len: int) -> str:
    """生成用于列表展示的人格介绍。"""
    text = _first_line(prompt)
    if not text:
        return ""
    max_len = max(8, int(max_len or 60))
    if len(text) > max_len:
        text = text[: max_len - 1] + "…"
    return text


def _wrap(draw, text: str, font, max_w: float) -> list[str]:
    """中文逐字、英文按词换行。"""
    if not text:
        return []
    lines: list[str] = []
    cur = ""
    for tok in _ASCII_WORD_RE.findall(text):
        if tok.isspace():
            if cur and draw.textlength(cur + " ", font=font) <= max_w:
                cur += " "
            elif cur:
                lines.append(cur)
                cur = ""
            continue
        if not cur:
            # 单个超长 token（长 URL 等）逐字符硬切
            if len(tok) > 1 and draw.textlength(tok, font=font) > max_w:
                for ch in tok:
                    if cur and draw.textlength(cur + ch, font=font) > max_w:
                        lines.append(cur)
                        cur = ""
                    cur += ch
                continue
            cur = tok
            continue
        cand = cur + tok
        if draw.textlength(cand, font=font) <= max_w:
            cur = cand
        else:
            lines.append(cur)
            cur = tok
    if cur:
        lines.append(cur)
    return lines or [""]


def _truncate(draw, text: str, font, max_w: float) -> str:
    """单行文本按最大宽度截断，末尾加省略号。"""
    if not text:
        return ""
    if draw.textlength(text, font=font) <= max_w:
        return text
    out = text
    while out and draw.textlength(out + "…", font=font) > max_w:
        out = out[:-1]
    return (out + "…") if out else ""


def _draw_text_centered(draw, rect, text: str, font, fill):
    """在给定矩形（设备像素坐标）内，按真实渲染墨迹将文本绝对居中。

    Pillow 的 textbbox 给出的是布局框而非墨迹框，直接用它居中会让文字
    （尤其是数字）在徽章里略偏。这里先渲染到临时位图测出真实墨迹，
    再反推绘制原点，保证任意字体/字符下都精确居中。
    """
    (x0, y0, x1, y1) = rect
    pad = 8
    probe = Image.new(
        "L", (max(2, int(x1 - x0) + pad * 2), max(2, int(y1 - y0) + pad * 2)), 0
    )
    ImageDraw.Draw(probe).text((pad, pad), text, font=font, fill=255)
    ink = probe.getbbox()
    if not ink:
        draw.text((x0, y0), text, font=font, fill=fill)
        return
    ink_w, ink_h = ink[2] - ink[0], ink[3] - ink[1]
    # 目标墨迹左上角 = 矩形中心 - 墨迹半径；再补偿“绘制原点 → 墨迹左上角”的偏移
    dx = (x0 + x1) / 2 - ink_w / 2 - (ink[0] - pad)
    dy = (y0 + y1) / 2 - ink_h / 2 - (ink[1] - pad)
    draw.text((dx, dy), text, font=font, fill=fill)


# ---------------------------------------------------------------------------
# 字体探测
# ---------------------------------------------------------------------------

def _find_font_file() -> str:
    for p in _FONT_EXPLICIT:
        if p and os.path.isfile(p):
            return p
    for base in _FONT_DIRS:
        if not os.path.isdir(base):
            continue
        for pat in _FONT_PATTERNS:
            try:
                hits = sorted(_glob.glob(os.path.join(base, pat), recursive=True))
            except Exception:
                continue
            if hits:
                return hits[0]
    return ""


def _font_file(font_path: str = "") -> str:
    global _FONT_FILE_CACHE
    if font_path and os.path.isfile(font_path):
        return font_path
    if _FONT_FILE_CACHE is None:
        _FONT_FILE_CACHE = _find_font_file()
    return _FONT_FILE_CACHE


def _load_font(size_px: int, font_path: str = ""):
    if not PIL_OK:
        return None
    path = _font_file(font_path)
    key = (path, size_px)
    if key in _FONT_OBJ_CACHE:
        return _FONT_OBJ_CACHE[key]
    font = None
    if path:
        try:
            font = ImageFont.truetype(path, size_px)
        except Exception:
            font = None
    if font is None:
        try:
            font = ImageFont.load_default(size_px)
        except TypeError:  # pragma: no cover - Pillow < 10.1
            font = ImageFont.load_default()
    _FONT_OBJ_CACHE[key] = font
    return font


# ---------------------------------------------------------------------------
# 列表图片渲染
# ---------------------------------------------------------------------------

def build_persona_image(
    out_path: str,
    entries: list,
    *,
    title: str = "人格列表",
    subtitle: str = "",
    help_lines: list | None = None,
    width: int = 900,
    scale: int = 2,
    font_path: str = "",
    show_current: bool = True,
) -> str:
    """渲染人格列表图片，返回输出路径。失败时抛出异常，由调用方回退到文本。"""
    if not PIL_OK:
        raise RuntimeError("Pillow 不可用")

    width = max(320, int(width))
    scale = max(1, min(3, int(scale)))

    # 内存保护：先粗估整图高度，限制位图像素总量。
    # 人格数量极多 + 高倍超采样时，位图可能达到数百 MB，这里自动降倍率。
    _est_h = 180 + len(entries or []) * 110 + 160
    _max_pixels = 24_000_000  # 约 72MB (RGB)
    while scale > 1 and (width * scale) * (_est_h * scale) > _max_pixels:
        scale -= 1

    def px(v):
        return int(round(v * scale))

    # 逻辑尺寸（1x 下的像素）
    margin = 28
    card_pad_x = 22
    card_pad_y = 16
    card_gap = 12
    badge_w = 52
    badge_h = 44
    name_gap = 16
    title_h = 46
    sub_h = 28
    name_h = 34
    intro_h = 30
    footer_pad_y = 16
    help_line_h = 28
    help_title_h = 22

    content_w = width - margin * 2

    # 预加载字体（逻辑字号）
    f_title = _load_font(30 * scale, font_path)
    f_sub = _load_font(20 * scale, font_path)
    f_name = _load_font(26 * scale, font_path)
    f_intro = _load_font(20 * scale, font_path)
    f_badge = _load_font(23 * scale, font_path)
    f_chip = _load_font(17 * scale, font_path)
    f_help = _load_font(19 * scale, font_path)
    f_help_t = _load_font(21 * scale, font_path)

    # 用于测量
    probe = Image.new("RGB", (16, 16))
    pd = ImageDraw.Draw(probe)

    intro_max_w = (content_w - card_pad_x * 2 - badge_w - name_gap) * scale

    # 预先计算每条介绍的换行
    entries = list(entries or [])
    for e in entries:
        e["_intro_lines"] = _wrap(
            pd, e.get("intro", ""), f_intro, intro_max_w
        ) if e.get("intro") else []

    # 底部帮助
    help_lines = list(help_lines or [])
    help_wrapped = []
    for hl in help_lines:
        help_wrapped.extend(_wrap(pd, _clean_text(hl), f_help, (content_w - 40) * scale))
    footer_h = footer_pad_y * 2
    if help_wrapped:
        footer_h += help_title_h + len(help_wrapped) * help_line_h

    # 计算总高度
    header_h = title_h + sub_h + 12
    cards_h = 0
    for e in entries:
        ch = card_pad_y * 2 + max(badge_h, name_h)
        if e.get("_intro_lines"):
            ch += 8 + len(e["_intro_lines"]) * intro_h
        cards_h += ch
    cards_h += card_gap * max(0, len(entries) - 1)

    total_h = margin * 2 + header_h + cards_h + 18 + footer_h

    img = Image.new("RGB", (width * scale, total_h * scale), _C["bg"])
    d = ImageDraw.Draw(img)

    # 标题与副标题
    y = margin
    d.text((margin * scale, px(y)), _clean_text(title), font=f_title, fill=_C["title"])
    y += title_h
    if subtitle:
        sub_txt = _truncate(d, _clean_text(subtitle), f_sub, content_w * scale)
        d.text((margin * scale, px(y)), sub_txt, font=f_sub, fill=_C["sub"])
    y += sub_h + 12

    # 卡片
    for idx, e in enumerate(entries):
        ch = card_pad_y * 2 + max(badge_h, name_h)
        if e.get("_intro_lines"):
            ch += 8 + len(e["_intro_lines"]) * intro_h

        is_cur = bool(show_current and e.get("current"))

        x0, y0 = margin, y
        x1, y1 = margin + content_w, y + ch

        # 卡片背景（当前人格用主色描边）
        d.rounded_rectangle(
            [px(x0), px(y0), px(x1), px(y1)],
            radius=px(14),
            fill=_C["card"],
            outline=_C["accent"] if is_cur else _C["card_line"],
            width=px(2 if is_cur else 1),
        )

        # 序号徽章（统一主色）
        bx0 = x0 + card_pad_x
        by0 = y0 + card_pad_y
        d.rounded_rectangle(
            [px(bx0), px(by0), px(bx0 + badge_w), px(by0 + badge_h)],
            radius=px(11),
            fill=_C["accent"],
        )
        num = str(e.get("index", idx + 1))
        _draw_text_centered(
            d,
            (px(bx0), px(by0), px(bx0 + badge_w), px(by0 + badge_h)),
            num,
            f_badge,
            _C["badge_text"],
        )

        # 名称（超长截断，避免溢出卡片；若有“当前”标签需为其预留宽度）
        nx = bx0 + badge_w + name_gap
        chip_w = 0
        chip_text = "当前"
        if is_cur:
            cb = d.textbbox((0, 0), chip_text, font=f_chip)
            chip_w = (cb[2] - cb[0]) / scale + 22
        name_max_w = (x1 - card_pad_x - nx - chip_w) * scale
        name = _truncate(d, _clean_text(e.get("name", "")), f_name, name_max_w)
        d.text((px(nx), px(y0 + card_pad_y + 1)), name, font=f_name, fill=_C["name"])

        if is_cur:
            cw = chip_w
            chh = 26
            cx1 = x1 - card_pad_x
            cx0 = cx1 - cw
            cy0 = y0 + card_pad_y + 3
            d.rounded_rectangle(
                [px(cx0), px(cy0), px(cx1), px(cy0 + chh)],
                radius=px(13),
                fill=_C["accent_light"],
            )
            _draw_text_centered(
                d,
                (px(cx0), px(cy0), px(cx1), px(cy0 + chh)),
                chip_text,
                f_chip,
                _C["accent_deep"],
            )

        # 介绍
        iy = y0 + card_pad_y + max(badge_h, name_h) + 8
        for line in e.get("_intro_lines", []):
            d.text((px(nx), px(iy)), line, font=f_intro, fill=_C["intro"])
            iy += intro_h

        y += ch + card_gap

    # 底部帮助区
    fy0 = y - card_gap + 18
    fy1 = total_h - margin
    if help_wrapped:
        d.rounded_rectangle(
            [px(margin), px(fy0), px(margin + content_w), px(fy1)],
            radius=px(12),
            fill=_C["foot_bg"],
        )
        hy = fy0 + footer_pad_y
        d.text((px(margin + 20), px(hy)), "使用说明", font=f_help_t, fill=_C["foot_title"])
        hy += help_title_h
        for line in help_wrapped:
            d.text((px(margin + 20), px(hy)), line, font=f_help, fill=_C["foot_tx"])
            hy += help_line_h

    # 超采样后缩回目标尺寸，保证文字清晰
    if scale != 1:
        img = img.resize((width, total_h), _LANCZOS)

    img.save(out_path, "PNG")
    return out_path


def build_persona_text(entries: list, subtitle: str = "", help_lines: list | None = None) -> str:
    """Pillow 不可用时的纯文本回退列表。"""
    lines = ["人格列表"]
    if subtitle:
        lines.append(subtitle)
    for e in entries or []:
        mark = "  [当前]" if e.get("current") else ""
        intro = e.get("intro", "")
        lines.append(f"{e.get('index')}. {e.get('name','')}{mark}" + (f" — {intro}" if intro else ""))
    lines.append("————————")
    for hl in (help_lines or DEFAULT_HELP_LINES):
        lines.append(hl)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 插件主体
# ---------------------------------------------------------------------------

@register(
    "astrbot_plugin_pp_switch",
    "xiaohao234",
    "快捷人格切换：发送 pp 查看人格列表图片，发送 pp 序号 一键切换人格（无需@机器人）",
    "v1.0.4",
)
class PPSwitchPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        # 注意：config 缺失时用普通 dict 兜底，避免 AstrBotConfig() 加载全局配置的副作用
        super().__init__(context)
        self.config = config if config is not None else {}
        # 全局切换锁：防止同会话并发切换导致 update_conversation 与记忆标记交错写入
        self._switch_lock = asyncio.Lock()
        # 切换后的人格强化倒计时：umo -> [人格名, 剩余请求数]
        self._persona_reinforce: dict = {}
        # 强化提示持续的请求次数
        self._REINFORCE_REQUESTS = 3
        _STATE["plugin"] = self
        self._refresh_triggers()
        self._tmp_dir = self._resolve_tmp_dir()

    async def initialize(self):
        self._refresh_triggers()
        font_ok = bool(_font_file(self._cfg_str("font_path")))
        logger.info(
            f"[pp-switch] 已就绪。触发词：{self.trigger_words}；Pillow：{'可用' if PIL_OK else '不可用'}；"
            f"中文字体：{'已找到' if font_ok else '未找到（建议 apt install fonts-noto-cjk）'}"
        )

    async def terminate(self):
        _STATE["plugin"] = None
        self._persona_reinforce.clear()

    # -- 配置辅助 ----------------------------------------------------------

    @property
    def trigger_words(self) -> list[str]:
        return list(_STATE.get("trigger_words") or DEFAULT_TRIGGERS)

    def _refresh_triggers(self):
        _STATE["trigger_words"] = _norm_words(self.config.get("trigger_words"))

    def _cfg_str(self, key: str) -> str:
        return str(self.config.get(key) or "").strip()

    def _cfg_int(self, key: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
        """安全读取整型配置：非法值回退默认值，并夹紧到 [lo, hi]。"""
        raw = self.config.get(key, default)
        try:
            val = int(raw)
        except (TypeError, ValueError):
            val = default
        if lo is not None:
            val = max(lo, val)
        if hi is not None:
            val = min(hi, val)
        return val

    def _resolve_tmp_dir(self) -> str:
        """临时图片目录：AstrBot 系统临时目录下的独立子目录，只放本插件的 png。"""
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_system_tmp_path

            base = os.path.join(get_astrbot_system_tmp_path(), "pp_switch")
        except Exception:
            import tempfile

            base = os.path.join(tempfile.gettempdir(), "astrbot_plugin_pp_switch")
        os.makedirs(base, exist_ok=True)
        return base

    def _cleanup_stale_images(self, max_age_sec: int = 3600) -> None:
        """删除本插件目录下超过 max_age_sec 的残留图片（track 清理失效时的安全网）。"""
        try:
            now = time.time()
            for f in os.listdir(self._tmp_dir):
                if not f.startswith("pp_personas_") or not f.endswith(".png"):
                    continue
                p = os.path.join(self._tmp_dir, f)
                try:
                    if now - os.path.getmtime(p) > max_age_sec:
                        os.remove(p)
                except OSError:
                    continue
        except Exception:
            pass

    # -- 数据获取 ----------------------------------------------------------

    def _get_personas(self) -> list:
        """获取人格列表（与 AstrBot「人格角色设定」一致），归一化为 [{name, prompt}]。

        优先读 ``persona_manager.personas``（v4 的 Persona 对象，含 sort_order，
        可对齐 WebUI 的拖拽排序），其次回退 ``personas_v3``。返回顺序即列表序号顺序。
        """
        pm = getattr(self.context, "persona_manager", None)
        if pm is None:
            return []

        def _to_dict(p):
            if hasattr(p, "get"):
                # dict 形态（v4 Personality TypedDict）
                return p.get("name"), p.get("prompt"), p.get("sort_order", 0)
            # 对象形态（v4 db Persona 等）
            name = getattr(p, "persona_id", None) or getattr(p, "name", None)
            prompt = getattr(p, "system_prompt", None) or getattr(p, "prompt", None)
            sort_order = getattr(p, "sort_order", 0)
            return name, prompt, sort_order

        # 优先对象列表：含 sort_order，稳定排序后与 WebUI 显示顺序一致
        items = []
        for p in getattr(pm, "personas", None) or []:
            name, prompt, so = _to_dict(p)
            if not name:
                continue
            try:
                so = int(so or 0)
            except (TypeError, ValueError):
                so = 0
            items.append({"name": name, "prompt": prompt or "", "sort_order": so})
        if items:
            # 稳定排序：仅按 sort_order，相同则保持后端返回顺序（WebUI 未排序时的插入顺序）
            items.sort(key=lambda x: x["sort_order"])
            return [{"name": x["name"], "prompt": x["prompt"]} for x in items]

        # 回退 personas_v3（无 sort_order，按原顺序）
        out = []
        for p in getattr(pm, "personas_v3", None) or []:
            name, prompt, _ = _to_dict(p)
            if name:
                out.append({"name": name, "prompt": prompt or ""})
        return out

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        try:
            return getattr(event, "role", None) == "admin"
        except Exception:
            return False

    async def _get_conv(self, umo: str):
        cm = getattr(self.context, "conversation_manager", None)
        if cm is None:
            return None, None, None
        cid = None
        conv = None
        try:
            cid = await cm.get_curr_conversation_id(umo)
        except Exception as e:
            logger.debug(f"[pp-switch] 获取会话 ID 失败：{e}")
        if cid:
            try:
                conv = await cm.get_conversation(umo, cid)
            except Exception as e:
                logger.debug(f"[pp-switch] 获取会话失败：{e}")
        return cm, cid, conv

    async def _resolve_current_persona_id(self, umo: str, conv_persona_id, platform_name: str):
        """尽量复用 AstrBot 自身的人格解析逻辑，保证“当前人格”判断与真实生效结果一致。"""
        pm = getattr(self.context, "persona_manager", None)
        provider_settings = {}
        try:
            cfg = self.context.get_config(umo=umo)
            provider_settings = (cfg or {}).get("provider_settings", {}) or {}
        except Exception:
            pass

        resolver = getattr(pm, "resolve_selected_persona", None) if pm else None
        if resolver:
            try:
                pid, _, _, _ = await resolver(
                    umo=umo,
                    conversation_persona_id=conv_persona_id,
                    platform_name=platform_name,
                    provider_settings=provider_settings,
                )
                # "[%None]" 是“强制无人格”的内部哨兵值，对外统一显示为未设置
                if pid == "[%None]":
                    return None
                return pid
            except Exception:
                pass

        # 手动镜像 v4 解析逻辑
        pid = None
        try:
            svc = await sp.get_async("umo", umo, "session_service_config", {}) or {}
            pid = svc.get("persona_id")
        except Exception:
            pass
        if not pid:
            pid = conv_persona_id
            if pid == "[%None]":
                return None
            if pid is None:
                pid = provider_settings.get("default_personality")
        return pid

    # -- 切换实现 ----------------------------------------------------------

    async def _switch_persona(self, event: AstrMessageEvent, index: int) -> str:
        async with self._switch_lock:
            return await self._switch_persona_locked(event, index)

    async def _switch_persona_locked(self, event: AstrMessageEvent, index: int) -> str:
        umo = event.unified_msg_origin
        personas = self._get_personas()
        if not personas:
            return "未找到任何人格，请先在 WebUI「人格角色设定」中创建人格。"

        if index < 1 or index > len(personas):
            return f"序号超出范围（1-{len(personas)}），发送 pp 查看人格列表。"

        persona = personas[index - 1]
        pid = persona.get("name")

        if self.config.get("admin_only", False) and not self._is_admin(event):
            return "仅管理员可切换人格（可在本插件 WebUI 配置中关闭此限制）。"

        cm, cid, conv = await self._get_conv(umo)

        # 会话级强制人格（dashboard 会话管理）
        forced = None
        try:
            svc = await sp.get_async("umo", umo, "session_service_config", {}) or {}
            forced = svc.get("persona_id")
        except Exception:
            svc = None

        already = (
            conv is not None
            and getattr(conv, "persona_id", None) == pid
            and forced in (None, pid)
        )
        if already:
            if self.config.get("switch_clear_history", False):
                # 开启了“切换后清空上下文”时，重复切换同一人格视为清空上下文的操作
                if cid:
                    try:
                        await cm.update_conversation(umo, cid, history=[])
                    except Exception as e:
                        logger.warning(f"[pp-switch] 清空历史失败：{e}")
                        return f"清空上下文失败（{e}），人格与记忆保持不变。"
                try:
                    event.set_extra("_clean_group_context_session", True)
                except Exception:
                    pass
                # 上下文已清空，无需强化
                self._persona_reinforce.pop(umo, None)
                return f"当前会话已是人格「{pid}」，已清空 LLM 上下文（聊天记录不受影响）。"
            return f"当前会话已经是人格「{pid}」，无需重复切换。"

        try:
            if conv is None:
                try:
                    await cm.new_conversation(umo, event.get_platform_id(), persona_id=pid)
                except TypeError:
                    # 极老签名不支持 persona_id 参数：先建会话再补写
                    await cm.new_conversation(umo, event.get_platform_id())
                    cid = await cm.get_curr_conversation_id(umo)
                    await cm.update_conversation(umo, cid, persona_id=pid)
            else:
                await cm.update_conversation(umo, cid, persona_id=pid)
        except Exception as e:
            logger.error(f"[pp-switch] 切换人格写入失败：{e}")
            return f"切换失败（数据库写入异常：{e}），人格保持不变。"

        # 同步会话级强制人格（若用户曾在 dashboard 设置过）；
        # 仅在主切换已成功后执行，失败只记录日志，不影响结果。
        if svc is not None and "persona_id" in svc and svc.get("persona_id") != pid:
            try:
                svc["persona_id"] = pid
                await sp.put_async("umo", umo, "session_service_config", svc)
            except Exception as e:
                logger.warning(f"[pp-switch] 同步会话级人格配置失败：{e}")

        cid = await cm.get_curr_conversation_id(umo) or cid

        # 记忆处理：清空 or 人格强化（两种方式都不往记忆里写任何东西——
        # 平台聊天记录存在独立的 platform_message_histories 表，同样不受影响）
        clear_ctx = bool(self.config.get("switch_clear_history", False))
        if clear_ctx:
            if cid:
                try:
                    await cm.update_conversation(umo, cid, history=[])
                except Exception as e:
                    logger.warning(f"[pp-switch] 清空历史失败：{e}")
            # 上下文已清空，无需强化
            self._persona_reinforce.pop(umo, None)
        elif self.config.get("switch_handoff", True):
            # 接下来几次 LLM 请求临时附加人格强化提示，抵消旧历史里的旧人格语气
            self._persona_reinforce[umo] = [pid, self._REINFORCE_REQUESTS]
        else:
            self._persona_reinforce.pop(umo, None)

        # 让群聊上下文缓冲也随之重置，减少旧上下文残留
        try:
            event.set_extra("_clean_group_context_session", True)
        except Exception:
            pass

        if clear_ctx:
            return f"已切换到人格 [{index}] {pid}，上下文已清空（聊天记录不受影响），下一条消息立即生效。"
        return f"已切换到人格 [{index}] {pid}，下一条消息立即生效。"

    @filter.on_llm_request()
    async def reinforce_persona_on_llm_request(self, event: AstrMessageEvent, req):
        """切换人格后的前几次 LLM 请求，临时附加人格强化提示。

        只修改本次请求的 system prompt，不写入任何会话记忆：
        - 避免旧记忆里的旧人格语气带偏模型（人设残留）；
        - 避免向记忆注入示例消息带偏模型回复风格（进而影响分段回复效果）。
        """
        try:
            umo = event.unified_msg_origin
            state = self._persona_reinforce.get(umo)
            if not state:
                return
            name, remaining = state
            if remaining <= 1:
                self._persona_reinforce.pop(umo, None)
            else:
                self._persona_reinforce[umo] = [name, remaining - 1]
            if getattr(req, "system_prompt", None) is None:
                req.system_prompt = ""
            req.system_prompt += (
                f"\n[System] 用户刚刚将你切换为人格「{name}」。"
                "历史消息中可能残留其他人格的语气和格式，请立刻完全以"
                f"「{name}」的身份、性格与语气回应；不要使用括号动作式表达，"
                "不要提及切换过程或本提示。"
            )
        except Exception:
            logger.debug("[pp-switch] 人格强化钩子执行失败", exc_info=True)

    # -- 图片生成 ----------------------------------------------------------

    async def _send_list(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        personas = self._get_personas()
        if not personas:
            yield event.plain_result("未找到任何人格，请先在 WebUI「人格角色设定」中创建人格。")
            event.stop_event()
            return

        _, cid, conv = await self._get_conv(umo)
        conv_pid = getattr(conv, "persona_id", None) if conv else None
        cur_pid = await self._resolve_current_persona_id(
            umo, conv_pid, event.get_platform_name()
        )
        show_current = bool(self.config.get("show_current", True))

        entries = []
        for i, p in enumerate(personas, 1):
            name = p.get("name") or f"persona-{i}"
            intro = make_intro(
                p.get("prompt") or "",
                self._cfg_int("intro_max_len", 60, 8, 300),
            )
            entries.append(
                {
                    "index": i,
                    "name": name,
                    "intro": intro,
                    "current": bool(show_current and cur_pid and name == cur_pid),
                }
            )

        # 副标题：总数 + 当前人格
        cur_disp = "未设置（默认）" if not cur_pid else str(cur_pid)
        subtitle = f"共 {len(personas)} 个人格 · 当前使用：{cur_disp}"

        raw_help = self.config.get("help_lines")
        if isinstance(raw_help, str):
            help_lines = [x.strip() for x in re.split(r"[\n]+", raw_help) if x.strip()]
        else:
            help_lines = [str(x).strip() for x in (raw_help or []) if str(x).strip()]
        if not help_lines:
            help_lines = list(DEFAULT_HELP_LINES)

        image_path = None
        if PIL_OK:
            try:
                # 有中文字体才渲染图片，否则直接文本回退（避免方框字）
                if _font_file(self._cfg_str("font_path")):
                    out = os.path.join(self._tmp_dir, f"pp_personas_{uuid.uuid4().hex}.png")
                    # 顺手清理 1 小时前的残留图片（track 清理失效时的安全网）
                    self._cleanup_stale_images()
                    image_path = await asyncio.to_thread(
                        build_persona_image,
                        out,
                        entries,
                        title="人格列表",
                        subtitle=subtitle,
                        help_lines=help_lines,
                        width=self._cfg_int("image_width", 900, 320, 2000),
                        scale=self._cfg_int("image_scale", 2, 1, 3),
                        font_path=self._cfg_str("font_path"),
                        show_current=show_current,
                    )
            except Exception as e:
                logger.error(f"[pp-switch] 生成人格图片失败，回退文本：{e}")

        if image_path:
            try:
                event.track_temporary_local_file(image_path)
            except Exception:
                pass
            yield event.image_result(image_path)
            event.stop_event()
        else:
            text = build_persona_text(entries, subtitle, help_lines)
            yield event.plain_result(text)
            event.stop_event()

    # -- 入口 --------------------------------------------------------------

    @_pp_handler_decorator
    async def on_pp(self, event: AstrMessageEvent):
        """pp / 人格切换 —— 快捷人格切换入口。"""
        text = getattr(event, "message_str", "") or ""
        parsed = parse_trigger(text, self.trigger_words)
        if not parsed:
            return  # 非本插件消息，静默忽略（绝不乱回复）

        kind, arg = parsed

        if kind == "switch":
            result = await self._switch_persona(event, arg)
            # 注意顺序：必须先 yield（结果会同步走完 ResultDecorate/Respond 阶段并发送），
            # 再 stop_event()。先 stop 会让调度器直接丢弃结果。
            yield event.plain_result(result)
            event.stop_event()
            return

        if kind == "help":
            tips = (
                "pp 人格切换使用说明\n"
                "1. pp 或 pp 列表：查看人格列表图片\n"
                "2. pp <序号>：切换到对应人格，例如 pp 2\n"
                "3. pp 当前：查看当前会话使用的人格\n"
                f"当前触发词：{'、'.join(self.trigger_words)}"
            )
            yield event.plain_result(tips)
            event.stop_event()
            return

        if kind == "current":
            umo = event.unified_msg_origin
            _, _, conv = await self._get_conv(umo)
            cur_pid = await self._resolve_current_persona_id(
                umo, getattr(conv, "persona_id", None) if conv else None,
                event.get_platform_name(),
            )
            yield event.plain_result(f"当前人格：{cur_pid or '未设置（默认）'}")
            event.stop_event()
            return

        if kind == "invalid":
            yield event.plain_result(
                f"无法识别的参数「{arg}」。用法：pp 查看列表，pp <序号> 切换人格。"
            )
            event.stop_event()
            return

        # kind == "list"
        async for _ in self._send_list(event):
            yield _

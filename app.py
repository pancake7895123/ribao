from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from flask import Flask, jsonify, request, send_from_directory

try:
    from PIL import Image, ImageEnhance, ImageOps, UnidentifiedImageError
except ImportError as exc:  # pragma: no cover - 仅在依赖缺失时触发
    raise RuntimeError(
        "缺少 Pillow 依赖。请在项目目录执行："
        ".venv\\Scripts\\python.exe -m pip install -r requirements.txt"
    ) from exc

# Windows 默认控制台编码可能不是 UTF-8，会把调试用的中文打印成乱码。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

BASE_DIR = Path(__file__).resolve().parent


def load_env_file(path: Path) -> None:
    """读取简单的 KEY=VALUE 形式的 .env，不依赖额外第三方包。"""
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        logging.getLogger(__name__).warning("无法读取 .env：%s", exc)
        return

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


load_env_file(BASE_DIR / ".env")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # JSON/Base64 请求体上限

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("plan-ocr")

API_URL = os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions").strip()
MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-flash").strip()
MAX_IMAGE_MB = float(os.getenv("MAX_IMAGE_MB", "20"))
MAX_IMAGE_BYTES = int(MAX_IMAGE_MB * 1024 * 1024)
REQUEST_TIMEOUT = (10, int(os.getenv("UPSTREAM_TIMEOUT_SECONDS", "120")))
MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "8192"))
DEBUG_UPSTREAM = os.getenv("DEBUG_UPSTREAM", "1").lower() in {"1", "true", "yes", "on"}

# 宽图自动上下裁切，让每一半在 API 缩放后保留更多细节。
IMAGE_SPLIT_WIDTH = int(os.getenv("IMAGE_SPLIT_WIDTH", "2000"))
IMAGE_SPLIT_MIN_HEIGHT = int(os.getenv("IMAGE_SPLIT_MIN_HEIGHT", "600"))
IMAGE_SPLIT_OVERLAP = min(
    0.30, max(0.0, float(os.getenv("IMAGE_SPLIT_OVERLAP", "0.08")))
)
IMAGE_TILE_MAX_BYTES = 12 * 1024 * 1024

SYSTEM_PROMPT = (
    "你是一个严谨的工程表格 OCR 与信息整合助手。"
    "你只输出最终纯文本，不输出解释、思考过程、Markdown 标记、代码块或任何额外标题。"
    "人员数量列中的机械名称、车辆、人数等任何可读内容都必须完整保留，不能只留数字或随意改写。"
)


class ConfigError(RuntimeError):
    pass


class ImageError(ValueError):
    pass


def _debug_print(*args: Any) -> None:
    """把调试信息直接打印到启动 app.py 的终端。"""
    if DEBUG_UPSTREAM:
        print(*args, flush=True)


def _preview(value: Any, limit: int = 3000) -> str:
    if value is None:
        return "<None>"
    text = str(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n... <截断，共 {len(text)} 字符>"


def _debug_request(payload: dict[str, Any]) -> None:
    """打印请求体；Base64 只打印长度，避免终端被几十 MB 字符淹没。"""
    debug_content = []
    for block in payload["messages"][1]["content"]:
        if block.get("type") == "text":
            debug_content.append(block)
        elif block.get("type") == "image_url":
            url = block.get("image_url", {}).get("url", "")
            debug_content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"<base64 已省略，共 {len(url):,} 字符>",
                        "detail": block.get("image_url", {}).get("detail", "high"),
                    },
                }
            )

    debug_payload = {
        "model": payload.get("model"),
        "thinking": payload.get("thinking"),
        "temperature": payload.get("temperature"),
        "max_tokens": payload.get("max_tokens"),
        "stream": payload.get("stream"),
        "messages": [
            payload["messages"][0],
            {"role": "user", "content": debug_content},
        ],
    }
    _debug_print("\n" + "=" * 100)
    _debug_print("[DeepSeek DEBUG] 请求体（图片 Base64 已省略）：")
    _debug_print(json.dumps(debug_payload, ensure_ascii=False, indent=2))


def _debug_response(response: requests.Response) -> None:
    request_id = (
        response.headers.get("x-request-id")
        or response.headers.get("x-ds-request-id")
        or response.headers.get("x-deepseek-request-id")
        or "-"
    )
    _debug_print(f"[DeepSeek DEBUG] HTTP {response.status_code}, request_id={request_id}")
    _debug_print("[DeepSeek DEBUG] 原始 response body：")
    _debug_print(response.text if response.text else "<空响应体>")


def _debug_parsed_message(data: dict[str, Any]) -> None:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        _debug_print("[DeepSeek DEBUG] choices 为空或格式异常。")
        return
    choice = choices[0] if isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    content = message.get("content")
    reasoning = message.get("reasoning_content")
    _debug_print(
        "[DeepSeek DEBUG] "
        f"finish_reason={choice.get('finish_reason')!r}, "
        f"content_length={len(content) if isinstance(content, str) else 0}, "
        f"reasoning_length={len(reasoning) if isinstance(reasoning, str) else 0}"
    )
    _debug_print("[DeepSeek DEBUG] message.content：")
    _debug_print(repr(content))
    if reasoning:
        _debug_print("[DeepSeek DEBUG] message.reasoning_content 预览（仅用于排查）：")
        _debug_print(_preview(reasoning))
    _debug_print(
        "[DeepSeek DEBUG] usage："
        + json.dumps(data.get("usage") or {}, ensure_ascii=False)
    )


def get_report_date() -> str:
    """返回报表日期。默认使用上海时区的当天；可用 REPORT_DATE 固定。"""
    override = os.getenv("REPORT_DATE", "").strip()
    if override:
        try:
            datetime.strptime(override, "%Y/%m/%d")
        except ValueError as exc:
            raise ConfigError("REPORT_DATE 格式错误，应为 YYYY/MM/DD，例如 2026/11/10") from exc
        return override

    tz_name = os.getenv("REPORT_TIMEZONE", "Asia/Shanghai").strip() or "Asia/Shanghai"
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        # Windows 的 Python 通常不自带 IANA 时区库；requirements.txt 已包含
        # tzdata，这里再为最常用的上海时区做一次兜底，避免没装 tzdata 时直接 500。
        if tz_name in {"Asia/Shanghai", "PRC", "Etc/GMT-8"}:
            log.warning("未找到时区数据库，Asia/Shanghai 暂按固定 UTC+8 处理。")
            tz = timezone(timedelta(hours=8))
        else:
            raise ConfigError(
                f"REPORT_TIMEZONE 无效：{tz_name}。请执行 pip install tzdata，"
                "或改用 Asia/Shanghai。"
            ) from exc
    return datetime.now(tz).strftime("%Y/%m/%d")


def build_user_prompt(report_date: str, image_count: int = 1) -> str:
    multi_image_note = ""
    if image_count > 1:
        multi_image_note = (
            f"【多图说明】本轮请求包含 {image_count} 张图片，它们是同一张工程表"
            "从上到下裁切出的部分，相邻图片之间可能有重叠。请按提供顺序从上到下读取；"
            "重叠区域只算一次；不要因为裁切漏行或重复。"
            "只有当跨图楼栋的文字完全相同时才可以合并，文字不同绝不能合并。\n\n"
        )

    return f"""{multi_image_note}这是一张包含合并单元格（跨行/跨列合并）、底色、空白行和“/”占位符的复杂工程排班表截图。不要把它当成普通规则表格逐行处理，必须严格执行下面的“先视觉分组、再提取”流程。

一、第一步：先按“楼栋”做视觉分组（强制，必须先做）
1. 先只看最左侧的“楼栋”列，不要急着读计划。按楼栋单元格的合并范围分组：一个楼栋单元格纵向覆盖了哪些行，这些行就属于同一楼栋组。
2. 楼栋合并单元格只读取一次楼栋名。只有因纵向合并而在下方显示为空白楼栋的延续行，才继承上面那个楼栋，不能当成“无楼栋”。
3. **最关键：楼栋文字不同，就必须分开。** 即使两行属于同一个大标段、同一个合并表头、同一个施工区域，只要“楼栋”列的文字不同（例如“污泥脱水机房”和“粗格栅”），就必须作为两个不同楼栋分别成条输出，严禁合并到一起。
4. 示例：最左侧先出现“污泥脱水机房”，下一行出现“粗格栅”。即使“粗格栅”看起来像“污泥脱水机房”的子项或下一级，也必须把“粗格栅”作为独立楼栋单独输出，绝不能把粗格栅的任务并入污泥脱水机房。
5. 一旦楼栋列出现新的文字，立即结束上一个楼栋组并开始新组；不能根据文字包含关系、缩进或层级强行合并。
6. 只有完成楼栋分组后，才允许在组内读取今日计划、紧随其后的人员数量、明日计划、紧随其后的人员数量。
7. 绝对禁止先逐行抄写、最后再拼接。处理顺序必须是：识别楼栋文字和合并区域 → 归组 → 组内提取 → 每栋合并成一行。

二、只读取以下 5 列
1. “楼栋”列
2. “今日计划”列
3. “今日计划”紧邻的“人员数量”列
4. “明日计划”列
5. “明日计划”紧邻的“人员数量”列

表头可能换行、合并、存在轻微遮挡或倾斜。请结合列的相对位置判断：
- “今日计划”右侧紧挨着的人员数量，才算今日人员数量。
- “明日计划”右侧紧挨着的人员数量，才算明日人员数量。
- 不要读取日期、序号、备注、单位、施工单位、状态等其他列。
- 不要使用今日人数作为明日人数，反之亦然。

三、今日/明日严格分列，空列绝不补内容
1. 必须严格按视觉上的同一水平行从左到右读取。今日计划只和今日人员数量配对，明日计划只和明日人员数量配对，严禁交叉。
2. 对每一行，先判断“今日计划”单元格：
   - 如果今日计划为空、只有“/”、写“无”、写“无内容”或完全看不清，则这一行的今日部分不产生任何输出。
   - 绝对不能用明日计划、明日人数、上一行或下一行的内容来补今日。
3. 再单独判断“明日计划”单元格：
   - 如果明日计划为空、只有“/”、写“无”、写“无内容”或完全看不清，则这一行的明日部分不产生任何输出。
   - 绝对不能用今日计划、今日人数、上一行或下一行的内容来补明日。
4. 今日和明日必须分别判断、互不影响：
   - 今日为空、明日有内容：只在明日部分输出该楼栋，今日部分绝对不能出现该楼栋。
   - 明日为空、今日有内容：只在今日部分输出该楼栋，明日部分绝对不能出现该楼栋。
5. 正确示例（假设表中只有这一行）：楼栋是“1号楼”，今日计划是“/”，今日人数是“/”，明日计划是“栏杆安装”，明日人数是“5人”，则输出必须是：
今日计划 {report_date}

未识别到有效数据
明日计划

1号楼，栏杆安装，5人

错误示例（严禁）：在今日计划和明日计划里都写“1号楼，栏杆安装，5人”。
6. 如果某一行在“楼栋、今日计划、今日人员数量、明日计划、明日人员数量”这 5 个字段里全部为空或全部是“/”，直接跳过整行。
7. 只有背景色、边框或空白，没有任何可读文字的行，直接跳过。
8. “/”是占位符，不能丢列，也不能当成数据：
   - 看到“/”时，必须理解为“这个单元格为空”，不能把它当成数字 0，不能把它当成楼栋名或计划内容。
   - 不能因为某个单元格是“/”就把后面的列整体左移，导致人员数量错位。
   - “/”只影响它所在的那一个单元格，不影响同一楼栋的另一侧或其他有效计划。
9. 合并单元格（包括大标段、楼栋、计划列的合并）只对它实际覆盖的视觉行生效；不能把合并单元格里的文字复制或延伸到未覆盖的其他行，也不能让其他行的数据串进来。若无法判断归属，宁可不输出，也不能串列。

四、按楼栋合并：任务和人员必须逐行绑定
1. 同一楼栋的多行计划仍合并为一条输出，但每一行表格的“任务内容”必须和“该行的人员数量”成对出现。
2. 固定顺序为：
楼栋，第1行任务，第1行人员数量，第2行任务，第2行人员数量，第3行任务，第3行人员数量，……
   - 人员数量有内容时才写；人员数量为空或“/”时，只写任务，直接省略该人员数量段。
3. 正确示例：
`1号楼，任务A，2人，任务B，6人，任务C，2人`
`高效沉淀池，工艺设备安装准备`（该行人员数量为“/”，被省略）
4. 严格禁止的错误输出（必须避免）：
`1号楼，任务A，任务B，任务C，2人，6人，2人`
   - 禁止先把所有任务写完，再把所有人员数量堆到最后；
   - 每一个任务后面必须紧跟着它本行对应的人员数量。
5. 同一行表格内有多个计划但只有一个人员数量时：先把该行的多个计划用顿号“、”连成一个任务段，再跟这一行的人员数量配对。
   例如：`1号楼，任务A、任务B，8人，任务C，3人`
6. 今日计划和明日计划分别独立处理，绝对不能把今日人数与明日人数混在一起。
7. 人员数量 / 机械资源规则：
   - **纯数字**：单元格是“5”“5人”“5 人”“5名”这类纯数量时，统一写成“5人”。
   - **含文字或机械名称**：单元格是“一台260挖机”“装载机一台”“挖机1台”“2人+1台挖机”这类内容时，必须完整保留原文，放在对应任务后面，不要额外加“人”，不要只提取数字，不要改写成“人数待核对”。
     * 例如：`2号楼，钢筋绑扎，一台260挖机`
     * 例如：`3号楼，混凝土浇筑，装载机一台`
     * 例如：`4号楼，支模板，2人+1台挖机`
     * “一台260挖机”里的 260 是机械型号，不是 260 个人；“2人+1台挖机”不能改写成“3人”。
   - **不要跨行求和**：每个任务只配自己那一行的人员数量，不能把多行人数相加，也不能把人员数量统一放到楼栋末尾。
   - **人员数量为空、只有“/”、写“无”时：计划文本和人员数量必须独立判断。**
     * 只要计划文本有字，就必须正常输出这条计划，只是末尾不要拼人员数量；
     * 不要丢整行，不要写“/”，不要写“人数待核对”，不要用 0 或其他数字占位。
     * 例如：某行楼栋是“高效沉淀池”，今日计划是“工艺设备安装准备”，今日人数是“/”，则输出：`高效沉淀池，工艺设备安装准备`
     * 例如：某行楼栋是“反硝化滤池”，明日计划是“设备检查”，明日人数为空，则输出：`反硝化滤池，设备检查`
   - 如果计划文本本身也是空、只有“/”或“无”，才跳过该行；绝不能因为人员数量为空而丢掉有文字的计划。
   - 有人员数量时正常配对：`高效沉淀池，工艺设备安装准备，3人`。
8. 清洗计划文本：
   - 去掉单元格内的换行、首尾空格、项目符号、计划前的序号以及末尾多余的逗号/分号；
   - 不要改写专业术语，不要补充图片中没有的内容。
9. 楼栋按首次出现的顺序输出。

五、百分比数字必须逐字辨认
1. 图中所有百分比（例如 90%、30%、80%、60%、50%、5%）必须逐字符识别，并原样保留百分号“%”。
2. 特别区分容易混淆的数字：9 和 3、8 和 3、6 和 0、5 和 6。不要把“90%”看成“30%”，不要把“80%”看成“30%”，也不要把“60%”看成“80%”。
3. 必须结合同一行的文字、列对齐关系和上下文交叉判断；拿不准时保持原图最可能的字符，不要自行改数或省略百分比。
4. 百分比通常出现在计划内容中，必须完整保留在对应任务里；如果图片被裁成上下两张，百分比出现在哪一张，就归到那一张对应的任务行。

六、输出格式（必须逐字符符合）
今日计划 {report_date}

今日每栋楼一行（不要自己加序号，后端会统一添加）
明日计划

明日每栋楼一行（不要自己加序号，后端会统一添加）

格式约束：
- 第一行必须严格是“今日计划 {report_date}”。即使图片里出现其他日期，也不得替换这个日期。
- 第一行后必须空一行，再输出今日各楼栋。
- 今日最后一行结束后，紧接着输出“明日计划”，中间不要空行，也不要写“组合结果”四个字。
- “明日计划”后必须空一行，再输出明日各楼栋。
- 每栋楼一行，行与行之间不要空行。
- 不要输出“1.”“2.”等序号，序号由后端统一添加。
- 不要输出项目符号、表格、Markdown 标题、代码块、解释或结尾总结。
- 不要在每条内容前重复“今日计划”或“明日计划”。
- 如果某一部分完全没有有效数据，就在该部分写“未识别到有效数据”。
- 除上述内容外，不要输出任何其他文字。

格式示例（仅示意格式，内容以图片为准；斜杠行不要出现在结果里）：
今日计划 {report_date}

1号楼，外墙抹灰，5人，支模板，3人，钢筋绑扎，4人
2号楼，钢筋绑扎，一台260挖机
3号楼，混凝土浇筑，2人+1台挖机
高效沉淀池，工艺设备安装准备
明日计划

1号楼，内墙抹灰，4人，材料转运，2人，混凝土浇筑，3人
2号楼，材料转运，装载机一台
3号楼，材料转运，8人
反硝化滤池，设备检查
"""


# 图片类型由文件头判断，而不是只信任前端声明的 MIME。
def detect_image_type(raw: bytes) -> str | None:
    if raw.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    return None


def parse_data_url(image: str) -> tuple[str, bytes]:
    """校验 data URL，返回真实图片 MIME 类型和原始字节。"""
    if not isinstance(image, str) or not image.strip():
        raise ImageError("缺少图片数据。")

    header, sep, encoded = image.partition(",")
    if not sep or not encoded:
        raise ImageError("图片格式不正确，请上传 JPG、PNG、WebP 或 GIF 图片。")

    header_match = re.fullmatch(
        r"data:image/(?:jpeg|jpg|png|webp|gif);base64", header.strip(), re.IGNORECASE
    )
    if not header_match:
        raise ImageError("仅支持 JPG、PNG、WebP 或 GIF 图片。")

    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageError("图片 Base64 数据损坏，请重新选择图片。") from exc

    if not raw:
        raise ImageError("图片内容为空。")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ImageError(f"图片不能超过 {MAX_IMAGE_MB:g} MB，请压缩后重试。")

    image_type = detect_image_type(raw)
    if not image_type:
        raise ImageError("无法识别图片格式，请上传 JPG、PNG、WebP 或 GIF 图片。")

    return f"image/{image_type}", raw


def _flatten_to_rgb(image: Image.Image) -> Image.Image:
    """去掉透明通道，统一转成白底 RGB，避免 PNG/WebP 透明区域变黑。"""
    if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def _encode_prepared_image(image: Image.Image) -> tuple[bytes, str, tuple[int, int]]:
    """裁切后的图片优先保存为 PNG；过大时回退到高质量 JPEG。"""
    if max(image.size) > 8192:
        scale = 8192 / max(image.size)
        image = image.resize(
            (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
            Image.Resampling.LANCZOS,
        )

    # 轻微锐化有助于区分 9/3、8/3 这类容易混淆的数字，但不要过度。
    image = ImageEnhance.Sharpness(image).enhance(1.25)

    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    data = buffer.getvalue()
    mime_type = "image/png"

    if len(data) > IMAGE_TILE_MAX_BYTES:
        for quality in (94, 90, 85):
            buffer = BytesIO()
            image.save(
                buffer,
                format="JPEG",
                quality=quality,
                optimize=True,
                progressive=True,
                subsampling=0,
            )
            data = buffer.getvalue()
            mime_type = "image/jpeg"
            if len(data) <= IMAGE_TILE_MAX_BYTES:
                break

    # 极端大图仍可能超过上限，按面积比例缩小后再编码，避免请求体超过 API 限制。
    for _ in range(2):
        if len(data) <= IMAGE_TILE_MAX_BYTES:
            break
        scale = max(0.1, (IMAGE_TILE_MAX_BYTES / len(data)) ** 0.5 * 0.9)
        image = image.resize(
            (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
            Image.Resampling.LANCZOS,
        )
        buffer = BytesIO()
        image.save(
            buffer,
            format="JPEG",
            quality=85,
            optimize=True,
            progressive=True,
            subsampling=0,
        )
        data = buffer.getvalue()
        mime_type = "image/jpeg"

    return data, mime_type, image.size


def prepare_image_parts(raw: bytes, mime_type: str) -> list[dict[str, Any]]:
    """检查图片尺寸；宽图自动上下裁成两张，提升 API 缩放后的文字清晰度。

    直接放大图片不会增加新信息，且 DeepSeek 会把图片缩放到约 1300×1300
    的总像素规模。上下裁切后，每一半可以保留更高比例的文字细节。
    """
    try:
        with Image.open(BytesIO(raw)) as source:
            source.load()
            image = ImageOps.exif_transpose(source)
            image = _flatten_to_rgb(image)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ImageError("图片无法解析，请重新导出为 JPG 或 PNG 后重试。") from exc

    width, height = image.size
    # 宽度超过阈值且高度足够时，上下裁切；特别高的图也拆开以保留细节。
    should_split = (
        width > IMAGE_SPLIT_WIDTH and height >= IMAGE_SPLIT_MIN_HEIGHT
    ) or (height > 3000 and width >= 800)

    if not should_split:
        if max(width, height) <= 8192:
            # 不裁切时尽量沿用原始字节，避免无意义的重新压缩。
            data_url = (
                f"data:{mime_type};base64,"
                + base64.b64encode(raw).decode("ascii")
            )
            return [
                {
                    "data_url": data_url,
                    "width": width,
                    "height": height,
                    "bytes": len(raw),
                    "label": "原图",
                    "split": False,
                }
            ]

        # 宽而扁的图片不裁切，但需要缩到 API 允许的单边 8192 像素。
        data, output_mime, output_size = _encode_prepared_image(image)
        return [
            {
                "data_url": (
                    f"data:{output_mime};base64,"
                    + base64.b64encode(data).decode("ascii")
                ),
                "width": output_size[0],
                "height": output_size[1],
                "bytes": len(data),
                "label": "原图（已缩放到 8192px 内）",
                "split": False,
            }
        ]

    overlap = max(30, int(height * IMAGE_SPLIT_OVERLAP))
    middle = height // 2
    top_end = min(height, middle + overlap)
    bottom_start = max(0, middle - overlap)

    crops = [
        ("上半部分", (0, 0, width, top_end)),
        ("下半部分", (0, bottom_start, width, height)),
    ]

    parts: list[dict[str, Any]] = []
    for label, box in crops:
        tile = image.crop(box)
        data, output_mime, output_size = _encode_prepared_image(tile)
        parts.append(
            {
                "data_url": (
                    f"data:{output_mime};base64,"
                    + base64.b64encode(data).decode("ascii")
                ),
                "width": output_size[0],
                "height": output_size[1],
                "bytes": len(data),
                "label": label,
                "split": True,
            }
        )
    return parts


def _upstream_message(response: requests.Response, api_key: str) -> str:
    try:
        data = response.json()
    except ValueError:
        text = (response.text or "").strip()
        return text[:400] if text else "上游接口未返回可读信息。"

    error = data.get("error")
    if isinstance(error, dict):
        message = str(error.get("message") or error)
    elif error:
        message = str(error)
    else:
        message = str(data.get("message") or "上游接口返回错误。")
    return message.replace(api_key, "***")[:500]


def _extract_content(payload: dict[str, Any]) -> str:
    try:
        message = payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("DeepSeek 返回内容为空。") from exc

    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts).strip()
    return ""


def _normalize_resource_tail(line: str) -> str:
    """给纯数字的人员数量补上“人”；机械/文字内容保持原样。

    最终行格式：楼栋，任务1，人员1，任务2，人员2，……
    对于成对格式，人员数量位于索引 2、4、6……，因此逐段检查，
    避免只给最后一个人数补“人”而前面的纯数字漏掉。
    """
    parts = line.split("，")
    if len(parts) < 3:
        return line

    # 楼栋 + 任务/人员成对时，总段数是奇数；人员位置是 2、4、6……
    # 段数不是奇数时只处理最后一段，兼容格式不完整的模型输出。
    indexes = list(range(2, len(parts), 2)) if len(parts) % 2 == 1 else [len(parts) - 1]

    changed = False
    for index in indexes:
        value = parts[index].strip()
        if value and re.fullmatch(r"\d+(?:[,，]\d+)*", value):
            parts[index] = f"{value}人"
            changed = True
    return "，".join(parts) if changed else line


def _is_personnel_value(text: str) -> bool:
    """判断单独一段是否明显是人员/机械数量，而不是计划文本。"""
    value = text.strip()
    if not value:
        return True
    if value in {"/", "／", "无", "无内容", "暂无", "空", "人数待核对"}:
        return True
    if re.fullmatch(r"\d+(?:[,，]\d+)*(?:人|名)?", value):
        return True
    if re.search(r"(?:\d+|一)(?:台|辆)", value):
        return True
    # 不再因为“设备/装载机”等单个词就判定为人数，避免误删合法计划文本。
    return False


def _clean_slash_placeholders(line: str) -> str:
    """删除斜杠/“无”这类占位段落，但保留“护栏/扶手”这种有意义的斜杠。"""
    line = line.replace("／", "/")
    placeholders = {"/", "无", "无内容", "暂无", "空", "人数待核对"}
    parts = []
    for raw_part in line.split("，"):
        part = raw_part.strip()
        if not part:
            continue
        if re.fullmatch(r"[/／\s]+", part):
            continue
        if part.strip("。；;，, ") in placeholders:
            continue
        parts.append(part)
    line = "，".join(parts)
    return re.sub(r"，{2,}", "，", line).strip("，")


def _clean_body(body: str, report_date: str) -> str:
    lines: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.strip().replace("\u3000", " ")
        if not line:
            continue
        if re.fullmatch(r"```[a-zA-Z0-9_-]*", line) or line == "```":
            continue
        if re.fullmatch(r"[-—_=]{3,}", line):
            continue
        if re.fullmatch(r"\[?组合结果\]?[：:]?", line):
            continue

        # 去掉模型偶尔自行添加的项目符号或“1. / ①”编号。
        line = re.sub(
            r"^(?:[-*•·]\s+|\d{1,2}[.、)）]\s*|\(\d{1,2}\)\s*|[①-⑳]\s*)",
            "",
            line,
        )
        line = re.sub(r"^今日计划[：:]\s*", "", line)
        line = re.sub(r"^明日计划[：:]\s*", "", line)
        line = re.sub(r"[\s，,；;、]+$", "", line).strip()
        line = _clean_slash_placeholders(line)

        # 计划文本和人员数量必须独立判断：
        # - “楼栋，任务”必须保留（该行人数为空或斜杠，只省略人数）；
        # - “楼栋，2人”这种只有人数、没有任务的残缺行才丢弃。
        if line != "未识别到有效数据":
            parts = [part.strip() for part in line.split("，") if part.strip()]
            if len(parts) < 2:
                if line:
                    _debug_print(f"[Clean] 丢弃无任务内容的行：{line!r}")
                continue
            if len(parts) == 2 and _is_personnel_value(parts[1]):
                _debug_print(f"[Clean] 丢弃只有人数、没有任务的行：{line!r}")
                continue

        line = _normalize_resource_tail(line)
        if line and line != report_date:
            lines.append(line)

    return "\n".join(lines) if lines else "未识别到有效数据"


def _number_lines(body: str) -> str:
    """给每个楼栋结果行加 1. 2. 3. 序号，后端强制生成，避免依赖模型。"""
    if body == "未识别到有效数据" or not body.strip():
        return body
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    return "\n".join(f"{index}. {line}" for index, line in enumerate(lines, 1))


def normalize_model_output(raw: str, report_date: str) -> tuple[str, bool]:
    """把模型输出整理成固定两段格式，并返回格式是否完全符合预期。"""
    text = (raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    text = re.sub(r"^```[^\n]*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text).strip()

    today_heading = re.search(r"(?m)^[ \t]*今日计划[^\n]*$", text)
    if not today_heading:
        # 模型完全没给出标题，保留原文，让用户至少能看到内容，同时标记格式异常。
        cleaned = text.strip()
        if not cleaned:
            cleaned = "未识别到有效数据"
        return cleaned, False

    tail = text[today_heading.end() :]
    tomorrow_heading = re.search(r"(?m)^[ \t]*明日计划[^\n]*$", tail)
    if tomorrow_heading:
        today_body_raw = tail[: tomorrow_heading.start()]
        tomorrow_body_raw = tail[tomorrow_heading.end() :]
    else:
        today_body_raw, tomorrow_body_raw = tail, ""

    today_body = _clean_body(today_body_raw, report_date)
    tomorrow_body = _clean_body(tomorrow_body_raw, report_date)

    lines = [line for line in (today_body + "\n" + tomorrow_body).splitlines() if line]
    # 人员数量可以省略，因此“楼栋，任务”这种只有一个逗号的行也是合法的。
    # 末尾允许“12人”“一台260挖机”“2人+1台挖机”等任意非空内容。
    line_ok = all(
        line == "未识别到有效数据"
        or re.fullmatch(r".+，.+", line)
        for line in lines
    )

    # 序号必须在 Python 最终拼接阶段统一添加。
    # 即使模型自己输出了“1.”，_clean_body 也已经先清掉，不会出现双重编号。
    numbered_today = _number_lines(today_body)
    numbered_tomorrow = _number_lines(tomorrow_body)

    result = (
        f"今日计划 {report_date}\n\n"
        f"{numbered_today}\n"
        f"明日计划\n\n"
        f"{numbered_tomorrow}"
    )

    format_ok = bool(tomorrow_heading) and line_ok
    return result, format_ok


def _json_error(message: str, status: int, *, detail: str | None = None):
    payload: dict[str, Any] = {"ok": False, "error": message}
    if detail:
        payload["detail"] = detail
    return jsonify(payload), status


@app.after_request
def add_cors_headers(response):
    # 允许直接双击 index.html（Origin: null）时访问本机后端。
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Max-Age"] = "86400"
    if request.headers.get("Access-Control-Request-Private-Network") == "true":
        response.headers["Access-Control-Allow-Private-Network"] = "true"
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.errorhandler(413)
def payload_too_large(_error):
    return _json_error(
        f"上传内容过大，图片请控制在 {MAX_IMAGE_MB:g} MB 以内。", 413
    )


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html", max_age=0)


@app.get("/api/health")
def health():
    try:
        report_date = get_report_date()
    except ConfigError as exc:
        return _json_error(str(exc), 500)
    return jsonify(
        {
            "ok": True,
            "service": "engineering-plan-ocr",
            "model": MODEL,
            "thinking": "disabled",
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "debug_upstream": DEBUG_UPSTREAM,
            "image_split_width": IMAGE_SPLIT_WIDTH,
            "report_date": report_date,
            "api_key_configured": bool(os.getenv("DEEPSEEK_API_KEY", "").strip()),
        }
    )


@app.route("/api/recognize", methods=["POST", "OPTIONS"])
def recognize():
    if request.method == "OPTIONS":
        return "", 204

    started = time.monotonic()
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        return _json_error(
            "后端尚未配置 DEEPSEEK_API_KEY。请在 .env 文件中填写后重启服务。", 500
        )

    try:
        report_date = get_report_date()
    except ConfigError as exc:
        return _json_error(str(exc), 500)

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _json_error("请求体必须是 JSON。", 400)

    try:
        mime_type, image_bytes = parse_data_url(body.get("image"))
        image_parts = prepare_image_parts(image_bytes, mime_type)
    except ImageError as exc:
        return _json_error(str(exc), 400)

    prompt = build_user_prompt(report_date, image_count=len(image_parts))
    user_content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for part in image_parts:
        user_content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": part["data_url"],
                    "detail": "high",
                },
            }
        )

    payload = {
        "model": MODEL,
        # deepseek-flash 默认开启思考模式；OCR 不需要思考，关闭后可避免
        # reasoning 占满 max_tokens，导致最终 content 为空。
        "thinking": {"type": "disabled"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "stream": False,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "engineering-plan-ocr/1.0",
    }

    _debug_print("\n" + "=" * 100)
    _debug_print(
        f"[DeepSeek DEBUG] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
        f"model={MODEL}, thinking=disabled, max_tokens={MAX_OUTPUT_TOKENS}"
    )
    _debug_print(
        f"[DeepSeek DEBUG] 原图：mime={mime_type}, bytes={len(image_bytes):,}, "
        f"prompt_chars={len(prompt):,}, 预处理后图片数={len(image_parts)}"
    )
    for index, part in enumerate(image_parts, 1):
        _debug_print(
            f"[DeepSeek DEBUG] 图片 {index}/{len(image_parts)}："
            f"{part['label']}, {part['width']}x{part['height']}, "
            f"encoded_bytes={part['bytes']:,}, "
            f"data_url_chars={len(part['data_url']):,}, split={part['split']}"
        )
    _debug_request(payload)

    upstream_started = time.monotonic()
    try:
        upstream = requests.post(
            API_URL, headers=headers, json=payload, timeout=REQUEST_TIMEOUT
        )
    except requests.Timeout:
        _debug_print("[DeepSeek DEBUG] 请求超时。")
        traceback.print_exc()
        return _json_error("DeepSeek 识别超时，请稍后重试。", 504)
    except requests.RequestException as exc:
        log.warning("Upstream request failed: %s", exc)
        _debug_print(f"[DeepSeek DEBUG] 请求异常：{exc!r}")
        traceback.print_exc()
        return _json_error(
            "无法连接 DeepSeek API，请检查网络或 DEEPSEEK_API_URL 配置。", 502
        )

    _debug_print(
        f"[DeepSeek DEBUG] 上游耗时 {time.monotonic() - upstream_started:.2f}s"
    )
    _debug_response(upstream)

    if upstream.status_code >= 400:
        detail = _upstream_message(upstream, api_key)
        log.warning("DeepSeek API HTTP %s: %s", upstream.status_code, detail)
        if upstream.status_code in (401, 403):
            message = "API Key 无效或没有访问权限，请检查 .env 中的 DEEPSEEK_API_KEY。"
            return _json_error(message, 502, detail=detail)
        if upstream.status_code == 429:
            return _json_error("请求过于频繁或账户额度不足，请稍后再试。", 429, detail=detail)
        if upstream.status_code == 400:
            return _json_error("DeepSeek 拒绝了请求，请检查模型名称或图片内容。", 502, detail=detail)
        return _json_error("DeepSeek 服务暂时不可用，请稍后重试。", 502, detail=detail)

    try:
        data = upstream.json()
    except ValueError:
        _debug_print("[DeepSeek DEBUG] response body 不是合法 JSON。")
        traceback.print_exc()
        return _json_error("DeepSeek 返回了无法解析的响应。", 502)

    _debug_parsed_message(data)

    try:
        raw_text = _extract_content(data)
    except ValueError as exc:
        _debug_print(f"[DeepSeek DEBUG] 解析 choices 失败：{exc}")
        return _json_error(str(exc), 502)

    choices = data.get("choices") if isinstance(data.get("choices"), list) else []
    first_choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    finish_reason = first_choice.get("finish_reason")
    message = first_choice.get("message") if isinstance(first_choice.get("message"), dict) else {}
    reasoning_content = message.get("reasoning_content")

    if not raw_text:
        _debug_print("[DeepSeek DEBUG] 最终 content 为空，请对照上方原始 response 排查。")
        if finish_reason == "length":
            message_text = (
                "DeepSeek 输出达到长度上限（finish_reason=length），没有返回可用内容。"
                "请裁剪表格截图，或调大 .env 里的 MAX_OUTPUT_TOKENS。"
            )
        elif finish_reason == "content_filter":
            message_text = "DeepSeek 因内容安全策略过滤了输出（finish_reason=content_filter）。"
        elif finish_reason == "insufficient_system_resource":
            message_text = "DeepSeek 系统推理资源不足，生成被中断，请稍后重试。"
        elif isinstance(reasoning_content, str) and reasoning_content.strip():
            message_text = (
                "模型只返回了思考内容（reasoning_content），没有返回最终 content。"
                "本次请求已关闭思考模式，请重试；并查看终端里的原始 response。"
            )
        else:
            message_text = (
                "DeepSeek 未返回识别内容（content 为空）。"
                "请查看启动 app.py 的终端中的原始 response，判断是模型未识别还是请求被截断。"
            )
        return _json_error(
            message_text,
            502,
            detail=f"finish_reason={finish_reason!r}",
        )

    text, format_ok = normalize_model_output(raw_text, report_date)
    warnings = []
    if not format_ok:
        warnings.append("模型返回的格式可能不完整，请核对楼栋与人数后再使用。")
    if finish_reason == "length":
        warnings.append("模型输出可能被截断（finish_reason=length），请核对末尾内容。")

    _debug_print("[DeepSeek DEBUG] 归一化后的最终文本：")
    _debug_print(text)

    return jsonify(
        {
            "ok": True,
            "text": text,
            "report_date": report_date,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "format_ok": format_ok,
            "warning": " ".join(warnings) if warnings else None,
        }
    )


if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "5000"))
    app.run(host='0.0.0.0', port=8080, debug=False)

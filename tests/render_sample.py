"""Render a real signin card sample through the user's astrbot t2i service.

Endpoint discovered from http://192.168.31.54:8999/openapi.json:
    POST /text2img/generate
    body: {"html": ..., "options": ScreenshotOptions, "json": bool}
    ScreenshotOptions: type (jpeg|png), quality, full_page, omit_background,
                       viewport_width, viewport_height, device_scale_factor_level, ...

Outputs the rendered image to ./out/ so it can be inspected.
"""
import base64
import io
import json
import os
import random
import sys
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

T2I_BASE = os.environ.get("ASTRBOT_T2I", "http://192.168.31.54:8999")
T2I_URL = f"{T2I_BASE}/text2img/generate"
OUT_DIR = PLUGIN_DIR / "out"

FONT_PATH = PLUGIN_DIR / "MiSans-Medium.ttf"

# Stub astrbot so main.py imports (we only need its pure helpers + TMPL)
import types
for name in ["astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.star",
             "astrbot.api.message_components", "astrbot.core", "astrbot.core.utils",
             "astrbot.core.utils.astrbot_path"]:
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["astrbot.api"].logger = type("L", (), {
    "info": lambda s, *a: None, "warning": lambda s, *a: None, "error": lambda s, *a: None})()
sys.modules["astrbot.api"].AstrBotConfig = dict


class _F:
    class EventMessageType:
        GROUP_MESSAGE = 1
        PRIVATE_MESSAGE = 2

    @staticmethod
    def event_message_type(*a, **k):
        return lambda f: f


sys.modules["astrbot.api.event"].filter = _F
sys.modules["astrbot.api.event"].AstrMessageEvent = object
sys.modules["astrbot.api.event"].MessageEventResult = object
sys.modules["astrbot.api.star"].Context = object
sys.modules["astrbot.api.star"].Star = object
sys.modules["astrbot.api.star"].register = lambda *a, **k: (lambda c: c)
sys.modules["astrbot.core.utils.astrbot_path"].get_astrbot_data_path = lambda: "/tmp/on_signin_render"

import main as plugin_main


def fetch_bytes(url: str, timeout: int = 90) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "image/avif,image/webp,image/png,image/*,*/*;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _fmt(v):
    if v is None or v == "":
        return ""
    if isinstance(v, str):
        return v
    return str(int(v)) if float(v).is_integer() else ("%g" % v)


def to_data_uri(data: bytes, max_size: int, quality: int) -> str:
    from PIL import Image
    img = Image.open(io.BytesIO(data))
    w, h = img.size
    if max(w, h) > max_size:
        ratio = max_size / max(w, h)
        img = img.resize((max(1, int(w * ratio)), max(1, int(h * ratio))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=quality, optimize=True)
    return f"data:image/webp;base64,{base64.b64encode(buf.getvalue()).decode('utf-8')}"


def call_t2i(html: str, options: dict, want_json: bool = False):
    payload = {"html": html, "options": options, "json": want_json}
    req = urllib.request.Request(
        T2I_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:2000]
        raise RuntimeError(f"t2i HTTP {e.code}: {body}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"t2i unreachable ({T2I_URL}): {e}")

    ctype = ""
    try:
        ctype = resp.headers.get("Content-Type", "")
    except Exception:
        pass

    if want_json or "application/json" in ctype:
        return json.loads(raw.decode("utf-8"))
    return raw


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    data = json.loads((PLUGIN_DIR / "data" / "ournotes_data.json").read_text(encoding="utf-8"))
    songs = data["songs"]
    cards = data["cards"]

    # 固定随机种子，保证样例可复现
    random.seed(20261017)

    # 卡面可用命令行参数指定：SSR 卡 asset_id 或留空按 BD→SSR→SR 挑
    want = sys.argv[1] if len(sys.argv) > 1 else ""
    if want:
        card = next(c for c in cards if str(c["asset_id"]) == want)
    else:
        bd = [c for c in cards if c["rarity_name"] == "BD"]
        ssr = [c for c in cards if c["rarity_name"] == "SSR"]
        sr = [c for c in cards if c["rarity_name"] == "SR"]
        card = (bd or ssr or sr)[0]
    print(f"🃏 选用卡面: {card['rarity_name']} {card['character_name']} 「{card['subtitle']}」 (asset_id={card['asset_id']})")

    # 下载立绘与背景
    print("⬇️ 下载立绘 / 背景 ...")
    char_uri = to_data_uri(
        fetch_bytes(f"https://assets.bdon.moe/zh-Hans/MemberCard/{card['asset_id']}/member_character/member_character.webp"),
        max_size=1440, quality=80)
    bg_uri = to_data_uri(
        fetch_bytes(f"https://assets.bdon.moe/zh-Hans/MemberCard/{card['asset_id']}/member_background/member_background.webp"),
        max_size=1440, quality=80)
    print(f"   立绘 data-uri {len(char_uri)//1024} KB, 背景 data-uri {len(bg_uri)//1024} KB")

    # 选两首难度相近的歌
    picks = plugin_main.select_song_with_difficulty(songs)
    print("🎵 推荐歌曲:")
    for p in picks:
        print(f"   {p['title']}  [{p['difficulty_label']} {_fmt(p['difficulty_level'])}]  artist={p.get('artist') or p.get('composer')}")

    covers = []
    for p in picks:
        uri = ""
        if p.get("jacket"):
            try:
                uri = to_data_uri(
                    fetch_bytes(f"https://assets.bdon.moe/zh-Hans/Image/Jacket/{p['jacket']}/{p['jacket']}.webp"),
                    max_size=200, quality=60)
            except Exception as e:
                print(f"   ⚠️ 封面下载失败 {p['jacket']}: {e}")
        covers.append(uri)

    font_b64 = ""
    if FONT_PATH.exists():
        font_b64 = base64.b64encode(FONT_PATH.read_bytes()).decode("utf-8")
        print(f"🔤 字体 {len(font_b64)//1024} KB (base64)")

    rarity_class = {"SR": "sr", "SSR": "ssr", "BD": "bd"}.get(card["rarity_name"], "sr")

    render_data = {
        "name": "慵懒午睡",
        "base64_font": font_b64,
        "b64bg": bg_uri,
        "b64char": char_uri,
        "b64ok": "", "b64yi": "", "b64ji": "",
        "fortune_level": "大吉",
        "fortune_desc": "最最顺!😎",
        "fortune_color": "good",
        "yi_text": "打歌",
        "ji_text": "熬夜追更",
        "streak_days": 7,
        "signed_dates": [datetime.now().day],
        "song_name": picks[0].get("artist") or picks[0].get("composer", ""),
        "song_name_cn": picks[0]["title"],
        "song_cover_base64": covers[0],
        "song_difficulty_type": picks[0]["difficulty_type"],
        "song_difficulty_level": _fmt(picks[0]["difficulty_level"]),
        "song_difficulty_label": picks[0]["difficulty_label"],
        "song2_name": picks[1].get("artist") or picks[1].get("composer", ""),
        "song2_name_cn": picks[1]["title"],
        "song2_cover_base64": covers[1],
        "song2_difficulty_type": picks[1]["difficulty_type"],
        "song2_difficulty_level": _fmt(picks[1]["difficulty_level"]),
        "song2_difficulty_label": picks[1]["difficulty_label"],
        "show_song": True,
        "card_rarity_name": card["rarity_name"],
        "card_rarity_class": rarity_class,
        "card_character_name": card["character_name"],
        "card_subtitle": card["subtitle"],
    }

    # jinja2 渲染（与插件一致）
    import jinja2
    html = jinja2.Template(plugin_main.TMPL).render(**render_data)
    print(f"📄 HTML {len(html)//1024} KB")

    options = {
        "quality": 30,
        "type": "jpeg",
        "full_page": True,
        "omit_background": False,
        "viewport_width": 800,
        "viewport_height": 720,
        "device_scale_factor_level": "high",
    }

    print(f"🚀 调用 t2i 服务 {T2I_URL} ...")
    result = call_t2i(html, options, want_json=True)
    if not isinstance(result, dict):
        print("⚠️ 返回不是 JSON 对象：", repr(result)[:500])
        return 1
    print(f"📦 返回: code={result.get('code')} message={result.get('message')!r}")
    if result.get("code") != 0:
        print("⚠️ t2i 返回错误：", json.dumps(result, ensure_ascii=False)[:1500])
        return 1

    data = result.get("data") or {}
    img_path = data.get("id") or data.get("path") or data.get("filename") or data.get("url")
    if not img_path:
        print("⚠️ 未找到图片标识，完整返回：", json.dumps(result, ensure_ascii=False)[:1500])
        return 1

    # 服务返回的 id 形如 "data/rendered_xxx.jpeg"，取图时要去掉这个前缀：
    # GET /text2img/data/rendered_xxx.jpeg
    if isinstance(img_path, str) and img_path.startswith("http"):
        img_url = img_path
    else:
        rel = str(img_path).lstrip("/")
        if rel.startswith("data/"):
            rel = rel[len("data/"):]
        img_url = f"{T2I_BASE}/text2img/data/{rel}"
    print(f"⬇️ 下载渲染结果 {img_url}")
    img_bytes = fetch_bytes(img_url)
    print(f"   {len(img_bytes)//1024} KB, magic={img_bytes[:4]!r}")
    is_image = (img_bytes[:3] == bytes([0xFF, 0xD8, 0xFF])
                or img_bytes[:4] == bytes([0x89, 0x50, 0x4E, 0x47])
                or img_bytes[:4] == b"RIFF"
                or img_bytes[:4] == b"GIF8")
    if not is_image:
        print("⚠️ 返回的不是图片：", img_bytes[:300])
        return 1
    out_path = OUT_DIR / f"signin_{card['rarity_name'].lower()}_{card['asset_id']}.jpg"
    out_path.write_bytes(img_bytes)
    print(f"✅ 已保存 {out_path} ({len(img_bytes)//1024} KB)")

    from PIL import Image
    im = Image.open(io.BytesIO(img_bytes))
    print(f"🖼️  图片尺寸: {im.size} {im.mode}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

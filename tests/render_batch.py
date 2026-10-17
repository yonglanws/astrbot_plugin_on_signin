"""随机渲染 10 张签到卡片样例

每张随机抽一个角色卡面，带上该角色的贴纸（宜/忌保证不同），
通过 astrbot t2i 服务渲染后存到 out/random/ 下。

用法：
    python tests/render_batch.py [数量]
"""
import asyncio
import base64
import io
import json
import os
import random
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

T2I_BASE = os.environ.get("ASTRBOT_T2I", "http://192.168.31.54:8999")
T2I_URL = f"{T2I_BASE}/text2img/generate"
OUT_DIR = PLUGIN_DIR / "out" / "random"
FONT_PATH = PLUGIN_DIR / "MiSans-Medium.ttf"

# ---- astrbot stub（只为 import main.py 的模板与常量）----
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
sys.modules["astrbot.core.utils.astrbot_path"].get_astrbot_data_path = lambda: "/tmp/on_signin_batch"

import main as plugin_main
import jinja2


def fetch_bytes(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "image/avif,image/webp,image/png,image/*,*/*;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_asset(urls, timeout: int = 30) -> bytes:
    """多源下载：按顺序尝试，全部失败才抛异常（与插件逻辑一致）"""
    last = None
    for u in urls:
        try:
            return fetch_bytes(u, timeout)
        except Exception as e:
            last = e
    raise last


def card_urls(asset_id: int, kind: str):
    """主源 haneoka（扁平 png），备选 bdon.moe（嵌套 webp）"""
    return [
        f"https://haneoka.org/assets/intl/Assets/AddressableResources/MemberCard/{asset_id}/{kind}.png",
        f"https://assets.bdon.moe/zh-Hans/MemberCard/{asset_id}/{kind}/{kind}.webp",
    ]


def jacket_url(jacket: str):
    return [
        f"https://haneoka.org/assets/intl/Assets/AddressableResources/Image/Jacket/{jacket}.png",
        f"https://assets.bdon.moe/zh-Hans/Image/Jacket/{jacket}/{jacket}.webp",
    ]


def to_data_uri(data: bytes, max_size: int, quality: int) -> str:
    from PIL import Image
    img = Image.open(io.BytesIO(data))
    w, h = img.size
    if max(w, h) > max_size:
        ratio = max_size / max(w, h)
        img = img.resize((max(1, int(w * ratio)), max(1, int(h * ratio))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=quality)
    return f"data:image/webp;base64,{base64.b64encode(buf.getvalue()).decode('utf-8')}"


def pick_stickers(manifest, character_id):
    """按角色抽贴纸：>=3 张三个位置全不同；2 张保证宜/忌不同；1 张三者共用"""
    entry = manifest.get(str(character_id))
    if not entry or not entry.get("stickers"):
        return "", "", "", 0
    uris = []
    for st in entry["stickers"]:
        f = PLUGIN_DIR / "data" / "stickers" / st["file"]
        if f.exists():
            uris.append(to_data_uri(f.read_bytes(), max_size=320, quality=72))
    if not uris:
        return "", "", "", 0
    if len(uris) >= 3:
        random.shuffle(uris)
        return uris[0], uris[1], uris[2], len(uris)
    if len(uris) == 2:
        return random.choice(uris), uris[0], uris[1], len(uris)
    return uris[0], uris[0], uris[0], len(uris)


async def render_one(idx, card, songs, font_b64, manifest, rng):
    picks = rng.sample(songs, 2) if len(songs) >= 2 else songs * 2
    # 保证两首难度接近
    picks = plugin_main.select_song_with_difficulty(songs)[:2]

    char_uri = to_data_uri(fetch_asset(card_urls(card['asset_id'], "member_character")), 1440, 80)
    bg_uri = to_data_uri(fetch_asset(card_urls(card['asset_id'], "member_background")), 1440, 80)
    covers = []
    for p in picks:
        try:
            covers.append(to_data_uri(fetch_asset(jacket_url(p['jacket'])), 200, 60))
        except Exception:
            covers.append("")
    while len(covers) < 2:
        covers.append("")

    st_ok, st_yi, st_ji, st_n = pick_stickers(manifest, card.get("character_id"))

    for p in picks:
        p = dict(p)
    picks = [dict(p) for p in picks]
    for p in picks:
        p["difficulty_level"] = plugin_main._format_level(p.get("difficulty_level"))
        p["name_cn"] = p.get("title") or ""
        p["name"] = p.get("artist") or ""

    fortunes = [("大吉", "最最顺!😎", "good"), ("中吉", "努力就发光😋", "good"),
                ("小吉", "小小的幸运😆", "good"), ("末吉", "转机就在眼前😉", "neutral"),
                ("半吉", "明天会更开心哦😍", "neutral"), ("凶", "困难是成长的礼物🎁", "bad")]
    fl, fd, fc = rng.choice(fortunes)
    yi = rng.choice(["打歌", "冲榜", "听OST", "练习乐器", "整理收藏"])
    ji = rng.choice(["熬夜追更", "过度氪金", "沉迷抽卡", "挖坑不填", "忽视健康"])
    name = rng.choice(["慵懒午睡", "测试用户", "Miku", "Soyo", "爱音"])

    render_data = {
        "name": name,
        "base64_font": font_b64,
        "b64bg": bg_uri, "b64char": char_uri,
        "b64ok": st_ok, "b64yi": st_yi, "b64ji": st_ji,
        "fortune_level": fl, "fortune_desc": fd, "fortune_color": fc,
        "yi_text": yi, "ji_text": ji,
        "streak_days": rng.randint(1, 99),
        "signed_dates": [datetime.now().day],
        "song_name": picks[0]["name"], "song_name_cn": picks[0]["name_cn"],
        "song_cover_base64": covers[0],
        "song_difficulty_type": picks[0]["difficulty_type"],
        "song_difficulty_level": picks[0]["difficulty_level"],
        "song_difficulty_label": picks[0]["difficulty_label"],
        "song2_name": picks[1]["name"], "song2_name_cn": picks[1]["name_cn"],
        "song2_cover_base64": covers[1],
        "song2_difficulty_type": picks[1]["difficulty_type"],
        "song2_difficulty_level": picks[1]["difficulty_level"],
        "song2_difficulty_label": picks[1]["difficulty_label"],
        "show_song": True,
        "card_rarity_name": card["rarity_name"],
        "card_rarity_class": {"SR": "sr", "SSR": "ssr", "BD": "bd"}[card["rarity_name"]],
        "card_character_name": card["character_name"],
        "card_subtitle": card["subtitle"],
    }

    html = jinja2.Template(plugin_main.TMPL).render(**render_data)
    opts = {"quality": 30, "type": "jpeg", "full_page": True, "omit_background": False,
            "viewport_width": 800, "viewport_height": 720, "device_scale_factor_level": "high"}
    req = urllib.request.Request(T2I_URL, data=json.dumps(
        {"html": html, "options": opts, "json": True}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    res = json.loads(urllib.request.urlopen(req, timeout=300).read().decode())
    pid = (res["data"]["id"]).lstrip("/")
    if pid.startswith("data/"):
        pid = pid[5:]
    img = fetch_bytes(f"{T2I_BASE}/text2img/data/{pid}", timeout=90)

    out = OUT_DIR / f"{idx:02d}_{card['rarity_name'].lower()}_{card['asset_id']}.jpg"
    out.write_bytes(img)
    print(f"  [{idx:02d}] {card['rarity_name']:<3} {card['character_name'][:14]:<16} "
          f"贴纸{st_n}张 宜忌{'异' if st_yi != st_ji else '同❌'} -> {out.name} ({len(img)//1024} KB)")
    return out


async def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    data = json.loads((PLUGIN_DIR / "data" / "ournotes_data.json").read_text(encoding="utf-8"))
    songs, cards = data["songs"], data["cards"]
    font_b64 = base64.b64encode(FONT_PATH.read_bytes()).decode() if FONT_PATH.exists() else ""
    manifest = {}
    mp = PLUGIN_DIR / "data" / "stickers.json"
    if mp.exists():
        manifest = json.loads(mp.read_text(encoding="utf-8"))

    rng = random.Random()
    print(f"🎲 随机渲染 {n} 张（卡池 {len(cards)} 张 SR+/BD）")
    chosen = rng.sample(cards, min(n, len(cards)))
    for idx, card in enumerate(chosen, 1):
        await render_one(idx, card, songs, font_b64, manifest, rng)
    print(f"\n✅ 全部完成，输出目录: {OUT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())

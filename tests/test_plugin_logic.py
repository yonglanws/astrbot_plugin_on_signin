"""Standalone harness: exercises the OurNotes plugin's pure logic without AstrBot.

Verifies:
1. Difficulty model is hard/expert only (no master/append)
2. Card pool is SR+/BD only
3. Cloud asset URLs resolve (member_character / member_background / jacket)
4. Daily-state round trip through SQLite
5. Difficulty-similarity song selection
"""
import base64
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

# Stub out astrbot before importing main.py
import types

astrbot_pkg = types.ModuleType("astrbot")
api_pkg = types.ModuleType("astrbot.api")
event_mod = types.ModuleType("astrbot.api.event")
star_mod = types.ModuleType("astrbot.api.star")
logger_mod = types.ModuleType("astrbot.api")
comp_mod = types.ModuleType("astrbot.api.message_components")
core_utils = types.ModuleType("astrbot.core.utils.astrbot_path")


class _Logger:
    def info(self, *a, **k): print("[INFO]", *a)
    def warning(self, *a, **k): print("[WARN]", *a)
    def error(self, *a, **k): print("[ERROR]", *a)


class filter:
    class EventMessageType:
        GROUP_MESSAGE = 1
        PRIVATE_MESSAGE = 2

    @staticmethod
    def event_message_type(*a, **k):
        def deco(fn): return fn
        return deco


class AstrMessageEvent: pass
class MessageEventResult: pass
class Context: pass
class Star:
    def __init__(self, context=None): pass
class AstrBotConfig(dict):
    def get(self, k, d=None): return dict.get(self, k, d)


def register(*a, **k):
    def deco(cls): return cls
    return deco


event_mod.filter = filter
event_mod.AstrMessageEvent = AstrMessageEvent
event_mod.MessageEventResult = MessageEventResult
star_mod.Context = Context
star_mod.Star = Star
star_mod.register = register
logger_mod.logger = _Logger()
logger_mod.AstrBotConfig = AstrBotConfig
comp_mod.Image = object
comp_mod.Plain = object
core_utils.get_astrbot_data_path = lambda: tempfile.mkdtemp(prefix="on_signin_test_")

astrbot_pkg.api = api_pkg
api_pkg.event = event_mod
api_pkg.star = star_mod
api_pkg.logger = logger_mod.logger
api_pkg.AstrBotConfig = AstrBotConfig
api_pkg.message_components = comp_mod
sys.modules["astrbot"] = astrbot_pkg
sys.modules["astrbot.api"] = api_pkg
sys.modules["astrbot.api.event"] = event_mod
sys.modules["astrbot.api.star"] = star_mod
sys.modules["astrbot.api.message_components"] = comp_mod
sys.modules["astrbot.core"] = types.ModuleType("astrbot.core")
sys.modules["astrbot.core.utils"] = types.ModuleType("astrbot.core.utils")
sys.modules["astrbot.core.utils.astrbot_path"] = core_utils

import main as plugin_main

FAILURES = []
PASSES = []


def check(name, cond, detail=""):
    if cond:
        PASSES.append(name)
        print(f"  ✅ {name}")
    else:
        FAILURES.append(f"{name}: {detail}")
        print(f"  ❌ {name} — {detail}")


print("=" * 70)
print("TEST 1: Difficulty model is hard/expert only")
print("=" * 70)
check("DIFFICULTY_ORDER has only hard+expert",
      set(plugin_main.DIFFICULTY_ORDER.keys()) == {"hard", "expert"},
      str(plugin_main.DIFFICULTY_ORDER))
check("no 'master' in difficulty order", "master" not in plugin_main.DIFFICULTY_ORDER)
check("no 'append' in difficulty order", "append" not in plugin_main.DIFFICULTY_ORDER)
check("labels map hard->HARD, expert->EXPERT",
      plugin_main.DIFFICULTY_LABEL_MAP == {"hard": "HARD", "expert": "EXPERT"},
      str(plugin_main.DIFFICULTY_LABEL_MAP))

print()
print("=" * 70)
print("TEST 2: Dataset — songs and cards")
print("=" * 70)
data = json.loads((PLUGIN_DIR / "data" / "ournotes_data.json").read_text(encoding="utf-8"))
songs = data["songs"]
cards = data["cards"]
check("songs non-empty", len(songs) > 0, f"{len(songs)} songs")
check("cards non-empty", len(cards) > 0, f"{len(cards)} cards")
check("every song has hard+expert",
      all("hard" in s["difficulties"] and "expert" in s["difficulties"] for s in songs))
check("every song difficulty has only hard/expert keys",
      all(set(s["difficulties"].keys()) == {"hard", "expert"} for s in songs))
check("no song has master/append difficulty",
      not any("master" in s["difficulties"] or "append" in s["difficulties"] for s in songs))
check("no R cards in pool (rarity >= 3)",
      all(c["rarity"] >= 3 for c in cards),
      str(sorted({c["rarity"] for c in cards})))
check("BD card present", any(c["rarity_name"] == "BD" for c in cards))
check("every card has asset_id", all(c.get("asset_id") for c in cards))
check("every song has jacket", all(s.get("jacket") for s in songs))
check("every song has title", all(s.get("title") for s in songs))
check("every card has character_name", all(c.get("character_name") for c in cards))

# 难度数值范围合理
hs = [s["difficulties"]["hard"]["display"] for s in songs]
es = [s["difficulties"]["expert"]["display"] for s in songs]
check("hard levels in sane range", 5 <= min(hs) and max(hs) <= 30, f"{min(hs)}-{max(hs)}")
check("expert levels in sane range", 5 <= min(es) and max(es) <= 30, f"{min(es)}-{max(es)}")
check("expert >= hard for every song", all(e >= h for h, e in zip(hs, es)))

print()
print("=" * 70)
print("TEST 3: Difficulty-similarity song selection")
print("=" * 70)
picked = plugin_main.select_song_with_difficulty(songs)
check("returns 2 songs", len(picked) == 2, str(len(picked)))
check("both picks have difficulty_type in {hard, expert}",
      all(p["difficulty_type"] in ("hard", "expert") for p in picked),
      str([p["difficulty_type"] for p in picked]))
check("both picks have a numeric difficulty_level",
      all(isinstance(p["difficulty_level"], (int, float)) for p in picked))
check("labels are HARD/EXPERT",
      all(p["difficulty_label"] in ("HARD", "EXPERT") for p in picked),
      str([p["difficulty_label"] for p in picked]))
check("picks are distinct songs", picked[0]["id"] != picked[1]["id"])
levels = [p["difficulty_level"] for p in picked]
check("similarity within threshold (same type ±5 / cross ±7)",
      abs(levels[0] - levels[1]) <= 7, str(levels))

# 展示的难度必须存在于该歌曲的难度表里（不能出现"选的时候相似、显示的时候不相似"）
by_id = {s["id"]: s for s in songs}
def displayed_difficulty_is_real(pick):
    entry = by_id[pick["id"]]["difficulties"].get(pick["difficulty_type"])
    return entry is not None and entry["display"] == pick["difficulty_level"]
check("displayed difficulty exists in that song's table",
      all(displayed_difficulty_is_real(p) for p in picked),
      str([(p["id"], p["difficulty_type"], p["difficulty_level"]) for p in picked]))

# 多次抽样：相似度约束必须始终成立（不允许出现超阈值配对）
violations = []
for _ in range(120):
    pair = plugin_main.select_song_with_difficulty(songs)
    lv = [x["difficulty_level"] for x in pair]
    if abs(lv[0] - lv[1]) > 7:
        violations.append(lv)
check("120 samples all within similarity threshold", not violations, str(violations[:3]))

# 两种难度都应有机会被抽到（hard 与 expert 都要能出现）
seen_types = set()
for _ in range(200):
    pair = plugin_main.select_song_with_difficulty(songs)
    for p in pair:
        seen_types.add(p["difficulty_type"])
check("both hard and expert appear across samples", seen_types == {"hard", "expert"}, str(seen_types))

print()
print("=" * 70)
print("TEST 4: characters.json — OurNotes characters & aliases")
print("=" * 70)
chars = json.loads((PLUGIN_DIR / "characters.json").read_text(encoding="utf-8"))
check("25 characters loaded", len(chars) == 25, str(len(chars)))
check("every character has characterId", all(c.get("characterId") for c in chars))
check("every character has name slug", all(c.get("name") for c in chars))
check("every character has aliases", all(c.get("aliases") for c in chars))
check("every character has bandName", all(c.get("bandName") for c in chars))
# 角色名以简体中文为主
check("fullNameChinese is simplified (no 燈/靈/曉 style traditional chars in main field)",
      all("燈" not in c["fullNameChinese"] and "寧" not in c["fullNameChinese"]
          and "櫻" not in c["fullNameChinese"] and "麥" not in c["fullNameChinese"] for c in chars),
      str([c["fullNameChinese"] for c in chars if any(t in c["fullNameChinese"] for t in "燈寧櫻麥")]))
check("tomori is 高松灯 (simplified)",
      next(c for c in chars if c["name"] == "tomori")["fullNameChinese"] == "高松灯")
# 繁体名保留为别名，便于繁体用户检索
check("traditional variants kept as aliases",
      "高松燈" in next(c for c in chars if c["name"] == "tomori")["aliases"]
      and "四宮寧月" in next(c for c in chars if c["name"] == "shizuku")["aliases"])
# 跨游戏检查：不应再出现世界计划角色
# 注意 shizuku 是 OurNotes 的四宫宁月（band 5），不是世界计划的日野森雫，此处不应列入
pjsk_only = {"ick", "saki", "hnm", "shiho", "mnr", "hrk", "airi", "szk", "khn", "an", "akt", "toya",
             "emu", "nene", "rui", "haruka", "knd", "ena", "mzk", "mfy"}
names = {c["name"] for c in chars}
check("no pjsk-only slugs remain", not (names & pjsk_only), str(names & pjsk_only))
check("OurNotes shizuku is 四宫宁月 (band 5), not pjsk 日野森雫",
      next(c for c in chars if c["name"] == "shizuku")["fullNameChinese"] == "四宫宁月")
check("OurNotes characters present",
      {"tomori", "anon", "rana", "soyo", "taki", "doloris", "mortis", "amoris"} <= names,
      str(sorted(names)))
# characters referenced by cards all exist in characters.json
card_char_ids = {c["character_id"] for c in cards}
char_ids = {c["characterId"] for c in chars}
check("all card character_ids exist in characters.json", card_char_ids <= char_ids,
      str(card_char_ids - char_ids))

print()
print("=" * 70)
print("TEST 4b: Character alias resolution (needs _load_character_metadata called)")
print("=" * 70)
# 用一个真实插件实例验证 __init__ 确实调用了 _load_character_metadata
tmp_data = Path(tempfile.mkdtemp(prefix="on_signin_inst_"))
inst = plugin_main.MyPlugin.__new__(plugin_main.MyPlugin)
inst.plugin_dir = PLUGIN_DIR
inst.data_dir = tmp_data
inst.data_file = tmp_data / "signin_data.json"
inst.config_file = tmp_data / "config.json"
inst.db_path = tmp_data / "signin.db"
inst.cache_dir = tmp_data / "cache"
inst.master_dir = tmp_data / "masterdata"
inst.builtin_data_file = PLUGIN_DIR / "data" / "ournotes_data.json"
inst.config = plugin_main.AstrBotConfig({"sign_in_command": "签到"})
inst.custom_config = {}
inst._sync_lock = threading.Lock()
inst._image_cache = {}
inst._image_cache_lock = threading.Lock()
inst._sync_state = {"last_sync": None, "running": False}
inst.db = plugin_main.SigninDatabase(inst.db_path)
inst._load_dataset()
inst._load_character_metadata()
check("character_name_map populated", len(inst.character_name_map) > 0, str(len(inst.character_name_map)))
check("character_alias_map populated", len(inst.character_alias_map) > 0, str(len(inst.character_alias_map)))

# 源码层面确认 __init__ 会调用它（防止再次回归）
src = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
init_body = src[src.index("def __init__(self, context"):src.index("# 数据加载与同步")]
check("__init__ calls _load_character_metadata", "_load_character_metadata()" in init_body)

# 各种输入形式都能解析到正确角色ID
cases = [
    ("ena", None),          # 世界计划角色，应解析不到
    ("tomori", 1), ("anon", 2), ("rana", 3), ("soyo", 4), ("taki", 5),
    ("高松燈", 1), ("千早愛音", 2), ("长崎素世", 4), ("祥子", 10), ("初華", 6),
    ("睦", 7), ("海鈴", 8), ("にゃむ", 9), ("阿拉蕾", 11), ("野乃花", 12),
    ("miku", 22), ("心玖", 22), ("四宮寧月", 25),
]
for query, expected in cases:
    got = inst._resolve_character(query)
    check(f"resolve {query!r} -> {expected}", got == expected, f"got {got}")

# 解析出的角色必须有可用卡面
for query in ["tomori", "anon", "rana", "soyo", "taki", "doloris", "mortis", "timoris",
              "amoris", "oblivionis", "arale", "nonoka", "ritsu", "miyako", "yuno",
              "hotaru", "natsume", "nagi", "mahoro", "houka", "raika", "miku",
              "yomogi", "chieri", "shizuku"]:
    cid = inst._resolve_character(query)
    has_cards = bool(inst._cards_by_character.get(cid))
    check(f"character {query} (id={cid}) has SR+/BD cards", has_cards)

# 指定角色选卡只会选到该角色的卡
for cid in list(inst._cards_by_character.keys())[:10]:
    for _ in range(20):
        c = inst._select_random_card(cid)
        if c["character_id"] != cid:
            check(f"select_random_card({cid}) stays in character", False, str(c))
            break
    else:
        check(f"select_random_card({cid}) stays in character", True)

# 不指定角色时只能选到 SR+ / BD
for _ in range(100):
    c = inst._select_random_card()
    if c["rarity"] < 3:
        check("random card is always SR+/BD", False, str(c))
        break
else:
    check("random card is always SR+/BD", True)
inst.db.close()

print()
print("=" * 70)
print("TEST 4d: difficulty formatting / artist / footer / removed config")
print("=" * 70)
# 19.0 应显示为 "19"，25.5 仍显示 "25.5"
check("_format_level(19.0) == '19'", plugin_main._format_level(19.0) == "19", repr(plugin_main._format_level(19.0)))
check("_format_level(20.5) == '20.5'", plugin_main._format_level(20.5) == "20.5", repr(plugin_main._format_level(20.5)))
check("_format_level(25) == '25'", plugin_main._format_level(25) == "25", repr(plugin_main._format_level(25)))
check("_format_level(29.0) == '29'", plugin_main._format_level(29.0) == "29", repr(plugin_main._format_level(29.0)))
check("_format_level(None) == ''", plugin_main._format_level(None) == "", repr(plugin_main._format_level(None)))
check("_format_level('') == ''", plugin_main._format_level("") == "", repr(plugin_main._format_level("")))
check("_format_level keeps non-integer decimals",
      plugin_main._format_level(25.7) == "25.7" and plugin_main._format_level(26.9) == "26.9")

# 歌曲作者栏应为演唱团体
check("every song has artist", all(s.get("artist") for s in songs),
      str([s["id"] for s in songs if not s.get("artist")]))
check("artist is a band name for banded songs",
      all(any(b in s["artist"] for b in s.get("band_names", [])) for s in songs if s.get("band_names")),
      str([(s["id"], s["artist"], s["band_names"]) for s in songs[:5]]))

# 底部文字限死，不走配置
src_main = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
check("footer hardcoded to 'Designed by 慵懒午睡'",
      "Designed by 慵懒午睡" in src_main and "{{footer_text}}" not in src_main)
check("footer_text config no longer read",
      "config.get('footer_text'" not in src_main and 'config.get("footer_text"' not in src_main)

# 歌曲推荐与签到日期改为始终启用
check("show_song no longer config-gated", "show_song_recommendation" not in src_main)
check("show_calendar no longer config-gated", "show_calendar" not in src_main)
check("render_data sets show_song True unconditionally", '"show_song": True,' in src_main)

# 配置 schema 已移除这三项
schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
for gone in ("footer_text", "show_song_recommendation", "show_calendar"):
    check(f"schema no longer exposes {gone}", gone not in schema, str(sorted(schema)))
check("schema keeps sign_in_command/history_keep_days/auto_sync_masterdata",
      {"sign_in_command", "history_keep_days", "auto_sync_masterdata"} <= set(schema), str(sorted(schema)))

# 立绘层显著放大：高度 >100% 且允许溢出画布
import re as _re
m_char = _re.search(r"\.character-layer img \{(.*?)\}", src_main, _re.S)
check("character-layer img rule found", m_char is not None)
if m_char:
    rule = m_char.group(1)
    h = _re.search(r"height:\s*([\d.]+)%", rule)
    check("character art height exceeds viewport (enlarged)",
          h is not None and float(h.group(1)) > 100, rule.strip()[:200])
    check("character art anchored right/bottom with overflow room",
          "right:" in rule and "bottom:" in rule and "-" in rule, rule.strip()[:200])
check("character layer clips overflow", "overflow: hidden" in src_main)

print()
print("=" * 70)
print("TEST 4c: masterdata sync + dataset rebuild")
print("=" * 70)
sync_dir = Path(tempfile.mkdtemp(prefix="on_signin_sync_"))
inst2 = plugin_main.MyPlugin.__new__(plugin_main.MyPlugin)
inst2.data_dir = sync_dir
inst2.master_dir = sync_dir / "masterdata"
inst2.cache_dir = sync_dir / "cache"
inst2._sync_lock = threading.Lock()
inst2._sync_state = {"last_sync": None, "running": False}
inst2.songs = []
inst2.cards = []

# 用已抓取的 master 表在本地重建数据集（不打网络，验证合并逻辑）
T = "C:/Users/21557/AppData/Local/Temp/"
tables = {}
for fn, key in [("mt.json", "MasterText"), ("mlm.json", "MasterLiveMusic"),
                ("mlms.json", "MasterLiveMusicScore"), ("mmc.json", "MasterMemberCard"),
                ("mc.json", "MasterCharacter"), ("mb2.json", "MasterBand")]:
    p = Path(T) / fn
    if p.exists():
        tables[key] = json.loads(p.read_text(encoding="utf-8"))
if len(tables) == 6:
    ds = inst2._build_dataset(tables)
    check("rebuilt dataset has songs", len(ds["songs"]) == len(songs), f"{len(ds['songs'])} vs {len(songs)}")
    check("rebuilt dataset has same card count", len(ds["cards"]) == len(cards), f"{len(ds['cards'])} vs {len(cards)}")
    check("rebuilt songs only have hard/expert",
          all(set(s["difficulties"]) == {"hard", "expert"} for s in ds["songs"]))
    check("rebuilt cards are all SR+/BD", all(c["rarity"] >= 3 for c in ds["cards"]))
    check("rebuilt songs have titles", all(s["title"] for s in ds["songs"]))
    check("rebuilt cards have character names", all(c["character_name"] for c in ds["cards"]))
    check("rebuilt meta records source repo", "moenotes-masterdata" in ds["meta"]["source"], ds["meta"]["source"])
    check("rebuilt meta records hk-tw-mo path", "hk-tw-mo" in ds["meta"]["source"], ds["meta"]["source"])
else:
    print(f"  ⏭️ 跳过（本地 master 表不完整: {sorted(tables)}）")

# 空表不应崩溃
empty = inst2._build_dataset({"MasterText": {"_allData": []}, "MasterLiveMusic": {"_allData": []},
                              "MasterLiveMusicScore": {"_allData": []}, "MasterMemberCard": {"_allData": []},
                              "MasterCharacter": {"_allData": []}, "MasterBand": {"_allData": []}})
check("empty tables build empty dataset", empty["songs"] == [] and empty["cards"] == [])

print()
print("=" * 70)
print("TEST 5: SQLite round trip (users / checkin / daily state / cleanup)")
print("=" * 70)
db_dir = Path(tempfile.mkdtemp(prefix="on_signin_db_"))
db = plugin_main.SigninDatabase(db_dir / "signin.db")
ctx = "user_test_1"
uid = "10001"

u = db.get_or_create_user(ctx, uid, "测试用户")
check("user created", bool(u) and u["ctx_id"] == ctx, str(u))
check("record_checkin returns True first time", db.record_checkin(ctx, uid, "测试用户") is True)
check("record_checkin returns False on duplicate", db.record_checkin(ctx, uid, "测试用户") is False)
stats = db.get_user_stats(ctx, uid)
check("streak is 1 after one checkin", stats["streak"] == 1, str(stats))
check("total is 1", stats["total"] == 1, str(stats))
check("signed_dates contains today", datetime.now().day in stats["signed_dates"], str(stats))
check("is_signed_today True", stats["is_signed_today"] is True)
check("max_streak is 1", stats["max_streak"] == 1, str(stats))

db.set_nickname(ctx, uid, "昵称甲")
check("nickname round trip", db.get_nickname(ctx, uid) == "昵称甲", db.get_nickname(ctx, uid))

state = {
    "fortune_level": "大吉", "fortune_desc": "x", "fortune_color": "good",
    "yi_activity": "打歌", "ji_activity": "熬夜",
    "card_asset_id": 29, "card_rarity_name": "SR", "card_character_name": "長崎爽世",
    "card_subtitle": "交疊的光彩",
    "song1_name": " composer", "song1_name_cn": "迷星叫", "song1_cover_base64": "AAAA",
    "song1_difficulty_type": "expert", "song1_difficulty_level": 25.0, "song1_difficulty_label": "EXPERT",
    "song2_name": " c2", "song2_name_cn": "壹雫空", "song2_cover_base64": "BBBB",
    "song2_difficulty_type": "hard", "song2_difficulty_level": 21.0, "song2_difficulty_label": "HARD",
}
check("save_daily_state ok", db.save_daily_state(ctx, uid, "测试用户", state) is True)
got = db.get_daily_state(ctx, uid)
check("get_daily_state returns row", got is not None)
check("card fields round trip",
      got and got["card_asset_id"] == 29 and got["card_rarity_name"] == "SR"
      and got["card_character_name"] == "長崎爽世" and got["card_subtitle"] == "交疊的光彩",
      str(got and {k: got[k] for k in ("card_asset_id", "card_rarity_name", "card_character_name", "card_subtitle")}))
check("song difficulty round trip",
      got and got["song1_difficulty_type"] == "expert" and got["song1_difficulty_level"] == 25.0
      and got["song2_difficulty_type"] == "hard",
      str(got and {k: got[k] for k in ("song1_difficulty_type", "song1_difficulty_level", "song2_difficulty_type")}))

# 旧库迁移：构造一个没有卡面列的旧表，验证 ALTER 补齐
old_db = db_dir / "old.db"
conn = sqlite3.connect(str(old_db))
conn.executescript("""
CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, ctx_id TEXT, user_id TEXT, username TEXT,
  last_checkin TEXT, total_checkins INTEGER DEFAULT 0, max_streak INTEGER DEFAULT 0,
  current_streak INTEGER DEFAULT 0, created_at TIMESTAMP, updated_at TIMESTAMP, UNIQUE(ctx_id,user_id));
CREATE TABLE daily_signin_state (id INTEGER PRIMARY KEY AUTOINCREMENT, user_ref INTEGER, signin_date TEXT,
  fortune_level TEXT, fortune_desc TEXT, fortune_color TEXT, yi_activity TEXT, ji_activity TEXT,
  song1_name TEXT, song1_name_cn TEXT, song1_cover_base64 TEXT, song1_difficulty_type TEXT,
  song1_difficulty_level INTEGER, song1_difficulty_label TEXT, song2_name TEXT, song2_name_cn TEXT,
  song2_cover_base64 TEXT, song2_difficulty_type TEXT, song2_difficulty_level INTEGER,
  song2_difficulty_label TEXT, created_at TIMESTAMP, updated_at TIMESTAMP, UNIQUE(user_ref,signin_date));
""")
conn.commit(); conn.close()
old = plugin_main.SigninDatabase(old_db)
cols = {r[1] for r in old._get_conn().execute("PRAGMA table_info(daily_signin_state)").fetchall()}
check("legacy daily_signin_state gained card columns",
      {"card_asset_id", "card_rarity_name", "card_character_name", "card_subtitle"} <= cols,
      str(sorted(cols)))

db.migrate_from_json({"user_old": {"20001": {"username": "旧用户", "last_checkin": "2026-01-01",
                                            "checkin_history": ["2026-01-01", "2026-01-02"]}}})
migrated = db.get_user_stats("user_old", "20001")
check("json migration imported history", migrated["total"] == 2, str(migrated))

n = db.cleanup_old_data(keep_days=365)
check("cleanup_old_data runs", isinstance(n, int), str(n))
db.set_meta("k", "v")
check("meta round trip", db.get_meta("k") == "v")
db.maybe_cleanup(365)
db.maybe_cleanup(365)  # second call should be a no-op (same day)
check("maybe_cleanup idempotent same day", db.get_meta("last_cleanup_date") == datetime.now().strftime("%Y-%m-%d"))
db.close()
old.close()

print()
print("=" * 70)
print("TEST 5b: 云端下载不走事件循环 + 超时/失败不卡死签到")
print("=" * 70)
import asyncio
import concurrent.futures
import inspect

# 1) 下载链路必须是 async + executor，不能同步阻塞
check("_fetch_image is sync (executor-only)", not inspect.iscoroutinefunction(plugin_main.MyPlugin._fetch_image))
check("_fetch_image_async exists", hasattr(plugin_main.MyPlugin, "_fetch_image_async"))
check("_fetch_image_async is a coroutine function",
      inspect.iscoroutinefunction(plugin_main.MyPlugin._fetch_image_async))
check("_get_card_images is async", inspect.iscoroutinefunction(plugin_main.MyPlugin._get_card_images))
check("_get_song_cover is async", inspect.iscoroutinefunction(plugin_main.MyPlugin._get_song_cover))
check("_get_random_song is async", inspect.iscoroutinefunction(plugin_main.MyPlugin._get_random_song))
check("_fetch_image_async delegates to run_in_executor",
      "run_in_executor" in inspect.getsource(plugin_main.MyPlugin._fetch_image_async))

# 2) __init__ 必须创建 executor 与超时配置
src_main = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
check("executor created in __init__", "ThreadPoolExecutor" in src_main)
check("download_timeout configured", "download_timeout" in src_main)
check("terminate shuts executor down", "shutdown(wait=False, cancel_futures=True)" in src_main)

# 3) 事件循环在下载期间必须保持响应（这是"整个 astrbot 卡死"的根因回归测试）
tmp_inst_dir = Path(tempfile.mkdtemp(prefix="on_signin_async_"))
inst3 = plugin_main.MyPlugin.__new__(plugin_main.MyPlugin)
inst3.cache_dir = tmp_inst_dir / "cache"
inst3.download_timeout = 1.5
inst3._executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
inst3.data_dir = tmp_inst_dir
inst3.master_dir = tmp_inst_dir / "masterdata"
inst3._sync_lock = threading.Lock()
inst3._sync_state = {"last_sync": None, "running": False}


class _SlowConn:
    """模拟一个 read 卡住的连接；按 timeout 参数抛 TimeoutError（真实 socket 行为）"""
    def __init__(self, timeout=None):
        self._t = timeout or 12

    def read(self, *a):
        import time
        time.sleep(min(self._t + 0.2, 3.0))  # 超过 timeout 即视为超时
        raise TimeoutError("The read operation timed out")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _slow_urlopen(req, timeout=None):
    return _SlowConn(timeout)


orig_urlopen = plugin_main.urllib.request.urlopen
plugin_main.urllib.request.urlopen = _slow_urlopen
try:
    async def _probe():
        # 并发发起的慢请求不应阻塞事件循环
        t0 = asyncio.get_running_loop().time()
        tasks = [inst3._get_card_images(63), inst3._get_card_images(64)]
        results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=8)
        elapsed = asyncio.get_running_loop().time() - t0
        return results, elapsed

    results, elapsed = asyncio.run(_probe())
    flat = [tuple(r) if isinstance(r, (list, tuple)) else r for r in results]
    check("slow downloads return empty layers instead of raising",
          flat == [("", ""), ("", "")], str(results))
    check("event loop stayed responsive (gather finished, no deadlock)", elapsed < 8, f"{elapsed:.2f}s")
except Exception as e:
    check("slow downloads degrade gracefully", False, repr(e)[:200])
finally:
    plugin_main.urllib.request.urlopen = orig_urlopen

# 4) 单张下载失败不影响另一张（立绘失败仍应有背景）
inst4 = plugin_main.MyPlugin.__new__(plugin_main.MyPlugin)
inst4.cache_dir = tmp_inst_dir / "cache2"
inst4.download_timeout = 2
inst4._executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
inst4.data_dir = tmp_inst_dir
inst4.master_dir = tmp_inst_dir / "masterdata"
inst4._sync_lock = threading.Lock()
inst4._sync_state = {"last_sync": None, "running": False}

# 预置背景缓存，立绘强制失败
(tmp_inst_dir / "cache2").mkdir(parents=True, exist_ok=True)
png = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
(tmp_inst_dir / "cache2" / "card_63_member_background.bin").write_bytes(png)


def _half_urlopen(req, timeout=None):
    url = req.full_url if hasattr(req, "full_url") else str(req)
    if "member_character" in url:
        raise TimeoutError("The read operation timed out")
    return _Ctx(png)


class _Ctx:
    def __init__(self, data): self._d = data
    def read(self, *a): return self._d
    def __enter__(self): return self
    def __exit__(self, *a): return False


orig2 = plugin_main.urllib.request.urlopen
plugin_main.urllib.request.urlopen = _half_urlopen
try:
    ch, bg = asyncio.run(inst4._get_card_images(63))
    check("failed 立绘 degrades to empty", ch == "", repr(ch)[:80])
    check("cached 背景 still renders", bg.startswith("data:image/"), repr(bg)[:60])
except Exception as e:
    check("partial failure handled", False, repr(e)[:200])
finally:
    plugin_main.urllib.request.urlopen = orig2

# 5) 封面失败返回空串而不是抛异常
inst5 = plugin_main.MyPlugin.__new__(plugin_main.MyPlugin)
inst5.cache_dir = tmp_inst_dir / "cache3"
inst5.download_timeout = 1
inst5._executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
inst5.data_dir = tmp_inst_dir
inst5.master_dir = tmp_inst_dir / "masterdata"
inst5._sync_lock = threading.Lock()
inst5._sync_state = {"last_sync": None, "running": False}
plugin_main.urllib.request.urlopen = _slow_urlopen
try:
    cover = asyncio.run(inst5._get_song_cover("jkt_001_100001"))
    check("song cover failure returns empty string", cover == "", repr(cover)[:80])
except Exception as e:
    check("song cover failure handled", False, repr(e)[:200])
finally:
    plugin_main.urllib.request.urlopen = orig_urlopen

# 6) _get_random_song 在封面全失败时仍返回两首歌（不抛异常）
inst6 = plugin_main.MyPlugin.__new__(plugin_main.MyPlugin)
inst6.cache_dir = tmp_inst_dir / "cache4"
inst6.download_timeout = 1
inst6._executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
inst6.data_dir = tmp_inst_dir
inst6.master_dir = tmp_inst_dir / "masterdata"
inst6._sync_lock = threading.Lock()
inst6._sync_state = {"last_sync": None, "running": False}
inst6.songs = songs
inst6.cards = cards
inst6._cards_by_character = {}
plugin_main.urllib.request.urlopen = _slow_urlopen
try:
    picks = asyncio.run(asyncio.wait_for(inst6._get_random_song(), timeout=10))
    check("song picks survive cover failures", len(picks) == 2, str(len(picks)))
    check("cover fields empty on failure", all(p["cover_base64"] == "" for p in picks))
    check("song metadata still present",
          all(p["name_cn"] and p["difficulty_type"] in ("hard", "expert") for p in picks),
          str([(p["name_cn"], p["difficulty_type"]) for p in picks]))
except Exception as e:
    check("song picks survive cover failures", False, repr(e)[:200])
finally:
    plugin_main.urllib.request.urlopen = orig_urlopen

# 7) 空歌曲库不应崩溃
inst7 = plugin_main.MyPlugin.__new__(plugin_main.MyPlugin)
inst7.songs = []
empty_picks = asyncio.run(inst7._get_random_song())
check("empty song library returns 2 placeholders", len(empty_picks) == 2, str(len(empty_picks)))

# 8) 整体时间预算：即使单张请求无限卡住，_get_card_images 也必须在预算内返回
inst8 = plugin_main.MyPlugin.__new__(plugin_main.MyPlugin)
inst8.cache_dir = tmp_inst_dir / "cache8"
inst8.download_timeout = 1.0          # 预算 = 1.0*2+5 = 7s
inst8._executor = concurrent.futures.ThreadPoolExecutor(max_workers=6)
inst8.data_dir = tmp_inst_dir
inst8.master_dir = tmp_inst_dir / "masterdata"
inst8._sync_lock = threading.Lock()
inst8._sync_state = {"last_sync": None, "running": False}


def _hang_urlopen(req, timeout=None):
    class _Hang:
        def read(self, *a):
            import time
            time.sleep(120)   # 远超任何预算
            return b""
        def __enter__(self): return self
        def __exit__(self, *a): return False
    return _Hang()


plugin_main.urllib.request.urlopen = _hang_urlopen
try:
    async def _budget_probe():
        t0 = asyncio.get_running_loop().time()
        r = await inst8._get_card_images(63)
        return r, asyncio.get_running_loop().time() - t0

    res, el = asyncio.run(asyncio.wait_for(_budget_probe(), timeout=30))
    check("hanging download bounded by overall budget", el < 25, f"{el:.1f}s")
    check("bounded download returns empty layers", res == ("", ""), str(res))
except Exception as e:
    check("hanging download bounded by overall budget", False, repr(e)[:200])
finally:
    plugin_main.urllib.request.urlopen = orig_urlopen

check("overall budget derived from download_timeout (covers 2 sources x 2 tries x 2 rounds)",
      "download_timeout * 8" in src_main)

# 8b) 多源：主源全失败时自动切备选源，用户仍能拿到图
import urllib.error

inst8b = plugin_main.MyPlugin.__new__(plugin_main.MyPlugin)
inst8b.cache_dir = tmp_inst_dir / "cache8b"
inst8b.download_timeout = 2
inst8b._executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
inst8b.data_dir = tmp_inst_dir
inst8b.master_dir = tmp_inst_dir / "masterdata"
inst8b._sync_lock = threading.Lock()
inst8b._sync_state = {"last_sync": None, "running": False}

_mirror_calls = []


def _mirror_urlopen(req, timeout=None):
    url = req.full_url if hasattr(req, "full_url") else str(req)
    _mirror_calls.append(url)
    # 主源（haneoka.org）一律连接被重置；备选源（bdon.moe）正常返回
    if "haneoka.org" in url:
        raise OSError("Remote end closed connection without response")
    if "assets.bdon.moe" in url:
        return _Ctx(png)
    raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)


orig_m = plugin_main.urllib.request.urlopen
plugin_main.urllib.request.urlopen = _mirror_urlopen
try:
    ch, bg = asyncio.run(inst8b._get_card_images(63))
    check("mirror fallback yields 立绘", ch.startswith("data:image/"), repr(ch)[:60])
    check("mirror fallback yields 背景", bg.startswith("data:image/"), repr(bg)[:60])
    tried_haneoka = any("haneoka.org" in u for u in _mirror_calls)
    tried_bdon = any("assets.bdon.moe" in u for u in _mirror_calls)
    check("primary source tried first", tried_haneoka, str(_mirror_calls[:4]))
    check("mirror used after primary failed", tried_bdon, str(_mirror_calls[:6]))
    check("mirror URLs are bdon webp paths",
          all(u.endswith(".webp") for u in _mirror_calls if "assets.bdon.moe" in u))
except Exception as e:
    check("mirror fallback works", False, repr(e)[:200])
finally:
    plugin_main.urllib.request.urlopen = orig_m

# 8c) 主源 404 时不应在原源上反复重试，应尽快换源
inst8c = plugin_main.MyPlugin.__new__(plugin_main.MyPlugin)
inst8c.cache_dir = tmp_inst_dir / "cache8c"
inst8c.download_timeout = 2
inst8c._executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
inst8c.data_dir = tmp_inst_dir
inst8c.master_dir = tmp_inst_dir / "masterdata"
inst8c._sync_lock = threading.Lock()
inst8c._sync_state = {"last_sync": None, "running": False}

_404_calls = []


def _urlopen_404(req, timeout=None):
    url = req.full_url if hasattr(req, "full_url") else str(req)
    _404_calls.append(url)
    raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)


plugin_main.urllib.request.urlopen = _urlopen_404
try:
    r = asyncio.run(inst8c._get_card_images(63))
    primary_calls = sum(1 for u in _404_calls if "haneoka.org" in u)
    mirror_calls = sum(1 for u in _404_calls if "assets.bdon.moe" in u)
    # 每张图只请求主源一次、备选源一次：404 不原地重试，直接换源
    check("404 on primary does not retry the same source", primary_calls == 2, f"{primary_calls} calls")
    check("404 falls through to the mirror", mirror_calls == 2, f"{mirror_calls} calls")
    check("all sources exhausted yields empty layers", list(r) == ["", ""], str(r))
except Exception as e:
    check("404 handling", False, repr(e)[:200])
finally:
    plugin_main.urllib.request.urlopen = orig_urlopen

check("ASSET_MIRRORS declares 2 sources",
      len(getattr(plugin_main, "ASSET_MIRRORS", [])) == 2,
      str(len(getattr(plugin_main, "ASSET_MIRRORS", []))))
check("haneoka is the primary source",
      plugin_main.ASSET_MIRRORS[0]["name"] == "haneoka",
      plugin_main.ASSET_MIRRORS[0]["name"])
check("_card_asset_urls returns primary + fallbacks",
      plugin_main.MyPlugin._card_asset_urls(None, 30, "member_character")[1] != ())
check("_jacket_urls returns primary + fallbacks",
      plugin_main.MyPlugin._jacket_urls(None, "jkt_001_100001")[1] != ())

print()
print("=" * 70)
print("TEST 5d: 角色贴纸分配（宜/忌必须不同）")
print("=" * 70)
sticker_manifest_path = PLUGIN_DIR / "data" / "stickers.json"
sticker_dir = PLUGIN_DIR / "data" / "stickers"
check("sticker manifest shipped", sticker_manifest_path.exists(), str(sticker_manifest_path))
check("sticker image dir shipped", sticker_dir.exists() and len(list(sticker_dir.glob("*.webp"))) > 0)

if sticker_manifest_path.exists():
    smanifest = json.loads(sticker_manifest_path.read_text(encoding="utf-8"))
    check("stickers cover all 25 characters", len(smanifest) == 25, str(len(smanifest)))
    check("every character has at least 2 stickers",
          all(len(v.get("stickers", [])) >= 2 for v in smanifest.values()),
          str([k for k, v in smanifest.items() if len(v.get("stickers", [])) < 2]))
    total_stickers = sum(len(v["stickers"]) for v in smanifest.values())
    check("57 stickers total", total_stickers == 57, str(total_stickers))
    check("sticker files all exist",
          all((sticker_dir / s["file"]).exists() for v in smanifest.values() for s in v["stickers"]))
    # 每个角色的贴纸文件名必须互不相同
    check("sticker filenames unique per character",
          all(len({s["file"] for s in v["stickers"]}) == len(v["stickers"]) for v in smanifest.values()))

    # 用真实插件实例验证 _load_stickers + _pick_stickers
    inst_s = plugin_main.MyPlugin.__new__(plugin_main.MyPlugin)
    inst_s.plugin_dir = PLUGIN_DIR
    inst_s.cache_dir = Path(tempfile.mkdtemp(prefix="on_signin_st_")) / "cache"
    inst_s._load_stickers()
    check("stickers loaded for 25 characters", len(inst_s.stickers) == 25, str(len(inst_s.stickers)))
    check("_pick_stickers exists", hasattr(inst_s, "_pick_stickers"))
    check("__init__ calls _load_stickers",
          "_load_stickers()" in src_main[src_main.index("def __init__(self, context"):src_main.index("# 数据加载与同步")])

    # 抽取规则：每个角色抽 300 次
    violations = []
    for cid, pool in inst_s.stickers.items():
        n = len(pool)
        for _ in range(300):
            ok, yi, ji = inst_s._pick_stickers(cid)
            if yi == ji:
                violations.append((cid, "宜=忌")); break
            if n >= 3 and not (ok != yi and ok != ji and yi != ji):
                violations.append((cid, "3张未全不同")); break
            if not all(v in pool for v in (ok, yi, ji)):
                violations.append((cid, "抽出池外")); break
    check("宜/忌 always different across all characters", not violations, str(violations[:5]))

    # 3 张的角色：三个位置必须两两不同
    three = [cid for cid, p in inst_s.stickers.items() if len(p) >= 3]
    check("7 characters have 3 stickers", len(three) == 7, str(three))
    triple_viol = []
    for cid in three:
        for _ in range(200):
            ok, yi, ji = inst_s._pick_stickers(cid)
            if not (ok != yi and ok != ji and yi != ji):
                triple_viol.append(cid); break
    check("3-sticker characters always yield 3 distinct", not triple_viol, str(triple_viol))

    # 2 张的角色：宜/忌不同（成功图标可重复）
    two = [cid for cid, p in inst_s.stickers.items() if len(p) == 2]
    check("18 characters have 2 stickers", len(two) == 18, str(len(two)))
    two_viol = [cid for cid in two for _ in range(200)
                if (lambda r: r[1] == r[2])(inst_s._pick_stickers(cid))]
    check("2-sticker characters always yield distinct 宜/忌", not two_viol, str(two_viol[:5]))

    # 无贴纸角色返回空
    check("unknown character returns empty stickers",
          inst_s._pick_stickers(99999) == ("", "", ""))

    # 渲染数据必须用贴纸，并且模板三个占位符都在
    check("render uses picked stickers", '"b64ok": st_ok' in src_main)
    check("stickers always picked from the card's character",
          "self._pick_stickers(card.get('character_id'))" in src_main)
    check("stickers persisted in daily state",
          "'sticker_ok': st_ok" in src_main and "'sticker_yi': st_yi" in src_main)
    check("daily state stores sticker columns",
          all(c in src_main for c in ("sticker_ok TEXT", '("sticker_ok", "TEXT")')))

    # 立绘仅下移不缩小：高度保持 142%，bottom 负值加大
    import re as _re2
    m2 = _re2.search(r"\.character-layer img \{(.*?)\}", src_main, _re2.S)
    if m2:
        rule2 = m2.group(1)
        h2 = _re2.search(r"height:\s*([\d.]+)%", rule2)
        b2 = _re2.search(r"bottom:\s*(-?[\d.]+)%", rule2)
        check("character height stays enlarged (142%)", h2 and float(h2.group(1)) == 142.0, rule2.strip()[:160])
        check("character shifted down (bottom <= -22%)", b2 and float(b2.group(1)) <= -22.0, rule2.strip()[:160])

    # 贴纸略微缩小
    m3 = _re2.search(r"\.fortune-icon img \{(.*?)\}", src_main, _re2.S)
    if m3:
        sc2 = _re2.search(r"transform:\s*scale\(([\d.]+)\)", m3.group(1))
        check("sticker scale reduced below 1.28", sc2 and float(sc2.group(1)) < 1.28, m3.group(1).strip()[:160])
    m4 = _re2.search(r"\.success-icon img \{(.*?)\}", src_main, _re2.S)
    if m4:
        sc3 = _re2.search(r"transform:\s*scale\(([\d.]+)\)", m4.group(1))
        check("success icon scale reduced below 1.18", sc3 and float(sc3.group(1)) < 1.18, m4.group(1).strip()[:160])

print()
print("=" * 70)
print("TEST 5e: 重复签到行为（卡面/贴纸可变，宜忌固定）")
print("=" * 70)
# 重复签到分支：运势/宜/忌/歌曲从存档读，卡面与贴纸重新抽
check("repeat signin reuses saved fortune",
      "existing_daily_state.get('fortune_level'" in src_main)
check("repeat signin reuses saved 宜", "existing_daily_state.get('yi_activity'" in src_main)
check("repeat signin reuses saved 忌", "existing_daily_state.get('ji_activity'" in src_main)
check("repeat signin reuses saved songs",
      "existing_daily_state.get('song1_name'" in src_main
      and "existing_daily_state.get('song2_name'" in src_main)
check("repeat signin re-rolls the card",
      "existing_daily_state" in src_main and "_select_random_card(specified_character_id)" in src_main)
check("repeat signin honors explicit character query",
      "if card is None and specified_character_id is not None" in src_main)
# 存档分支不再把已存卡面/贴纸塞进 card，否则就变成"固定"而不是"可变换"
check("saved card is not reused verbatim",
      "card = {\n                'asset_id': existing_daily_state" not in src_main)
# 新抽的卡面必须带上 character_id，贴纸才能跟着角色走
check("re-rolled card carries character_id",
      "_secure_random_choice(pool)" in src_main and "_secure_random_choice(self.cards)" in src_main)

# 用真实实例验证：重复签到连续抽到的卡面应当会变化（42 张卡，抽 10 次至少出现 2 种）
inst_r = plugin_main.MyPlugin.__new__(plugin_main.MyPlugin)
inst_r.cards = cards
inst_r._cards_by_character = {}
seen = {inst_r._select_random_card()["asset_id"] for _ in range(10)}
check("repeat signin can vary the card (>=2 distinct in 10 draws)", len(seen) >= 2, str(sorted(seen)))
# 每个卡面都带 character_id，且贴纸按该角色抽取时宜/忌仍不同
for _ in range(50):
    c = inst_r._select_random_card()
    ok, yi, ji = inst_s._pick_stickers(c["character_id"])
    if c["character_id"] in three:
        assert ok != yi and ok != ji and yi != ji, c
    else:
        assert yi != ji, c
check("re-rolled card stickers follow the new character", True)

print()
print("=" * 70)
print("TEST 5c: 字体完整性 + 已编码缓存 + 批量并发下载")
print("=" * 70)
# 1) 字体必须是完整 ttf，不能子集化（子集会丢字形导致回退系统字体）
check("full MiSans font shipped", (PLUGIN_DIR / "MiSans-Medium.ttf").exists())
check("no subset font shipped (would drop glyphs)",
      not (PLUGIN_DIR / "data" / "MiSans-Subset.woff2").exists())
inst_f = plugin_main.MyPlugin.__new__(plugin_main.MyPlugin)
inst_f.plugin_dir = PLUGIN_DIR
inst_f._load_font()
check("font_format is truetype (full font)", inst_f.font_format == "truetype", inst_f.font_format)
check("full font base64 loaded (>1MB)", len(inst_f.base64_font) > 1_000_000,
      f"{len(inst_f.base64_font)//1024} KB")
check("template uses static truetype font-face",
      "format('truetype')" in src_main and "{{font_mime}}" not in src_main)

# 2) 已编码缓存：第二次不再走下载/编码
tmp_ec = Path(tempfile.mkdtemp(prefix="on_signin_ec_"))
inst_e = plugin_main.MyPlugin.__new__(plugin_main.MyPlugin)
inst_e.cache_dir = tmp_ec / "cache"
inst_e.download_timeout = 3
inst_e._executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
inst_e.data_dir = tmp_ec
inst_e.master_dir = tmp_ec / "masterdata"
inst_e._sync_lock = threading.Lock()
inst_e._sync_state = {"last_sync": None, "running": False}

png = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
calls = {"n": 0}


def _counting_urlopen(req, timeout=None):
    calls["n"] += 1
    class _C:
        def read(self, *a): return png
        def __enter__(self): return self
        def __exit__(self, *a): return False
    return _C()


orig3 = plugin_main.urllib.request.urlopen
plugin_main.urllib.request.urlopen = _counting_urlopen
try:
    r1 = asyncio.run(inst_e._get_card_images(63))
    n_after_first = calls["n"]
    r2 = asyncio.run(inst_e._get_card_images(63))
    n_after_second = calls["n"]
    check("first call performs downloads", n_after_first > 0, str(n_after_first))
    check("second call makes zero network requests", n_after_second == n_after_first,
          f"{n_after_first} -> {n_after_second}")
    check("cached result identical", r1 == r2, f"{str(r1)[:40]} vs {str(r2)[:40]}")
    check("encoded cache written to disk",
          (tmp_ec / "cache" / "encoded").exists()
          and len(list((tmp_ec / "cache" / "encoded").glob("*.txt"))) >= 2)
except Exception as e:
    check("encoded cache round trip", False, repr(e)[:200])
finally:
    plugin_main.urllib.request.urlopen = orig3

# 3) 批量并发下载：一次拿回 立绘/背景/两首封面（用全新实例，避免命中前面已写的缓存）
tmp_pf = Path(tempfile.mkdtemp(prefix="on_signin_pf_"))
inst_p = plugin_main.MyPlugin.__new__(plugin_main.MyPlugin)
inst_p.cache_dir = tmp_pf / "cache"
inst_p.download_timeout = 3
inst_p._executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
inst_p.data_dir = tmp_pf
inst_p.master_dir = tmp_pf / "masterdata"
inst_p._sync_lock = threading.Lock()
inst_p._sync_state = {"last_sync": None, "running": False}

calls2 = {"n": 0}
def _counting2(req, timeout=None):
    calls2["n"] += 1
    class _C:
        def read(self, *a): return png
        def __enter__(self): return self
        def __exit__(self, *a): return False
    return _C()

plugin_main.urllib.request.urlopen = _counting2
try:
    res = asyncio.run(inst_p.prefetch_signin_assets(63, ["jkt_001_100001", "jkt_002_100043"]))
    n1 = calls2["n"]
    check("prefetch returns 4 values", len(res) == 4, str(len(res)))
    check("prefetch issues 4 concurrent requests", n1 == 4, str(n1))
    check("prefetch result is 2 card layers + 2 covers",
          all(isinstance(v, str) for v in res) and all(v.startswith("data:image/") for v in res),
          str([v[:20] for v in res]))
    res2 = asyncio.run(inst_p.prefetch_signin_assets(63, ["jkt_001_100001", "jkt_002_100043"]))
    n2 = calls2["n"]
    check("second prefetch fully cached", n2 == n1, f"{n1} -> {n2}")
    check("prefetch cached result identical", res == res2)
except Exception as e:
    check("prefetch batch download", False, repr(e)[:200])
finally:
    plugin_main.urllib.request.urlopen = orig3

# 4) 封面为空 / 不足两首时也要能兜住
plugin_main.urllib.request.urlopen = _counting_urlopen
try:
    r_none = asyncio.run(inst_p.prefetch_signin_assets(63, []))
    check("prefetch with no jackets still returns 4", len(r_none) == 4, str(len(r_none)))
    check("prefetch with no jackets yields empty covers", r_none[2] == "" and r_none[3] == "",
          str(r_none[2:]))
    r_one = asyncio.run(inst_p.prefetch_signin_assets(63, ["jkt_001_100001"]))
    check("prefetch with one jacket fills the other slot", len(r_one) == 4 and r_one[3] == "",
          str([v[:16] for v in r_one]))
except Exception as e:
    check("prefetch edge cases", False, repr(e)[:200])
finally:
    plugin_main.urllib.request.urlopen = orig3

# 5) 批量下载超时同样被预算兜住（不会无限等待）
inst_h = plugin_main.MyPlugin.__new__(plugin_main.MyPlugin)
inst_h.cache_dir = tmp_ec / "cacheH"
inst_h.download_timeout = 1.0
inst_h._executor = concurrent.futures.ThreadPoolExecutor(max_workers=6)
inst_h.data_dir = tmp_ec
inst_h.master_dir = tmp_ec / "masterdata"
inst_h._sync_lock = threading.Lock()
inst_h._sync_state = {"last_sync": None, "running": False}

def _hang_urlopen(req, timeout=None):
    class _Hang:
        def read(self, *a):
            import time
            time.sleep(120)
            return b""
        def __enter__(self): return self
        def __exit__(self, *a): return False
    return _Hang()

plugin_main.urllib.request.urlopen = _hang_urlopen
try:
    async def _p():
        t0 = asyncio.get_running_loop().time()
        r = await inst_h.prefetch_signin_assets(63, ["jkt_001_100001", "jkt_002_100043"])
        return r, asyncio.get_running_loop().time() - t0
    r, el = asyncio.run(asyncio.wait_for(_p(), timeout=30))
    check("prefetch bounded by budget on hang", el < 25, f"{el:.1f}s")
    check("prefetch returns 4 empties on hang", r == ("", "", "", ""), str(r))
except Exception as e:
    check("prefetch bounded by budget on hang", False, repr(e)[:200])
finally:
    plugin_main.urllib.request.urlopen = orig3

# 6) 下载重试（主源失败先原地重试，再换备选源）
check("download retries up to 2 times per source", "[(url, 2)]" in src_main)
check("retry backs off (no hammering)", "time.sleep(0.4" in src_main)
check("HTTP 404 short-circuits to the next source", "404, 403, 410" in src_main)

# 7) 不再发送 keep-alive：实测该 CDN 复用连接会超时
check("no keep-alive header (CDN closes idle conns)", '"Connection": "keep-alive"' not in src_main)

print()
print("=" * 70)
print("TEST 6: Cloud asset URL construction (primary + mirror)")
print("=" * 70)
check("card character URL pattern",
      plugin_main.MyPlugin._card_asset_url(None, 29, "member_character")
      == "https://haneoka.org/assets/intl/Assets/AddressableResources/MemberCard/29/member_character.png",
      plugin_main.MyPlugin._card_asset_url(None, 29, "member_character"))
check("card background URL pattern",
      plugin_main.MyPlugin._card_asset_url(None, 29, "member_background")
      == "https://haneoka.org/assets/intl/Assets/AddressableResources/MemberCard/29/member_background.png")
check("jacket URL pattern",
      plugin_main.MyPlugin._jacket_url(None, "jkt_001_100001")
      == "https://haneoka.org/assets/intl/Assets/AddressableResources/Image/Jacket/jkt_001_100001.png")

prim_ch, fb_ch = plugin_main.MyPlugin._card_asset_urls(None, 29, "member_character")
check("primary is haneoka (faster/more reliable)", prim_ch.startswith("https://haneoka.org"), prim_ch)
check("mirror card URL is bdon webp",
      fb_ch and fb_ch[0] == "https://assets.bdon.moe/zh-Hans/MemberCard/29/member_character/member_character.webp",
      str(fb_ch))
prim_jk, fb_jk = plugin_main.MyPlugin._jacket_urls(None, "jkt_001_100001")
check("mirror jacket URL is bdon webp",
      fb_jk and fb_jk[0] == "https://assets.bdon.moe/zh-Hans/Image/Jacket/jkt_001_100001/jkt_001_100001.webp",
      str(fb_jk))

print()
print("=" * 70)
print("TEST 7: Live cloud asset reachability (all SR+/BD cards)")
print("=" * 70)
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
asset_ids = sorted({c["asset_id"] for c in cards})
bad_char, bad_bg = [], []
for aid in asset_ids:
    for kind, bucket in (("member_character", bad_char), ("member_background", bad_bg)):
        url = f"https://haneoka.org/assets/intl/Assets/AddressableResources/MemberCard/{aid}/{kind}.png"
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=25) as r:
                magic = r.read(8)
            if magic != b"\x89PNG\r\n\x1a\n":
                bucket.append((aid, kind, "not-png"))
        except Exception as e:
            bucket.append((aid, kind, repr(e)[:60]))
check(f"all {len(asset_ids)} card 立绘 downloadable", not bad_char, str(bad_char[:5]))
check(f"all {len(asset_ids)} card 背景 downloadable", not bad_bg, str(bad_bg[:5]))

jacket_bad = []
for s in songs[:12]:
    url = f"https://haneoka.org/assets/intl/Assets/AddressableResources/Image/Jacket/{s['jacket']}.png"
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=25) as r:
            magic = r.read(8)
        if magic != b"\x89PNG\r\n\x1a\n":
            jacket_bad.append((s["jacket"], "not-png"))
    except Exception as e:
        jacket_bad.append((s["jacket"], repr(e)[:60]))
check("sampled 12 jackets downloadable", not jacket_bad, str(jacket_bad[:5]))

# 备选源 bdon.moe 必须同样可用（主源抽风时靠它兜底）
mirror_bad = []
sample_ids = asset_ids[:6] + asset_ids[-6:]
for aid in sample_ids:
    for kind in ("member_character", "member_background"):
        url = f"https://assets.bdon.moe/zh-Hans/MemberCard/{aid}/{kind}/{kind}.webp"
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=25) as r:
                magic = r.read(4)
            if magic != b"RIFF":
                mirror_bad.append((aid, kind, "not-webp"))
        except Exception as e:
            mirror_bad.append((aid, kind, repr(e)[:60]))
check(f"mirror serves {len(sample_ids)*2} card assets", not mirror_bad, str(mirror_bad[:5]))

mirror_jacket_bad = []
for s in songs[:12]:
    j = s["jacket"]
    url = f"https://assets.bdon.moe/zh-Hans/Image/Jacket/{j}/{j}.webp"
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=25) as r:
            magic = r.read(4)
        if magic != b"RIFF":
            mirror_jacket_bad.append((j, "not-webp"))
    except Exception as e:
        mirror_jacket_bad.append((j, repr(e)[:60]))
check("mirror serves sampled 12 jackets", not mirror_jacket_bad, str(mirror_jacket_bad[:5]))

print()
print("=" * 70)
print(f"RESULT: {len(PASSES)} passed, {len(FAILURES)} failed")
print("=" * 70)
if FAILURES:
    for f in FAILURES:
        print("  ❌", f)
    sys.exit(1)
print("ALL CHECKS PASSED")

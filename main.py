import os
import base64
import random
import secrets
import shutil
import sqlite3
import threading
import io
import time
import urllib.request
import urllib.error
from PIL import Image
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api import AstrBotConfig
import astrbot.api.message_components as Comp
import jinja2
import json
from datetime import datetime, timedelta
from pathlib import Path

# OurNotes（梦限大/Mill Sage/MyGO/Ave Mujica）难度体系：最高 HARD（黄）与 EX（红），无 MASTER / APPEND
DIFFICULTY_ORDER = {'hard': 0, 'expert': 1}
DIFFICULTY_LABEL_MAP = {'hard': 'HARD', 'expert': 'EXPERT'}

# haneoka.org（BanG Dream! Our Notes 数据库镜像）：连通性与速度都明显更好，
# 实测 42 张立绘 4 并发 haneoka 约 10~12s 全部成功，bdon 要 16~23s，
# 且 bdon 在部分网络下会 read timeout。所以 haneoka 作主源。
ASSET_BASE = "https://haneoka.org/assets/intl/Assets/AddressableResources"
ASSET_LANG_PATH = "intl/Assets/AddressableResources"
# bdon.moe（webp，体积小一半）作为备选源：主源抽风时兜底，保证用户能拿到图
ASSET_MIRRORS = [
    {
        "name": "haneoka",
        "base": "https://haneoka.org/assets/intl/Assets/AddressableResources",
        "card": "/MemberCard/{id}/{kind}.png",
        "jacket": "/Image/Jacket/{name}.png",
    },
    {
        "name": "bdon",
        "base": "https://assets.bdon.moe/zh-Hans",
        "card": "/MemberCard/{id}/{kind}/{kind}.webp",
        "jacket": "/Image/Jacket/{name}/{name}.webp",
    },
]

# masterdata 同步源（只取繁中服 hk-tw-mo 目录）
MASTERDATA_REPO = "StarMoe-org/moenotes-masterdata"
MASTERDATA_BRANCH = "main"
MASTERDATA_PATH = "hk-tw-mo"
MASTERDATA_FILES = [
    "MasterText.json",
    "MasterLiveMusic.json",
    "MasterLiveMusicScore.json",
    "MasterMemberCard.json",
    "MasterCharacter.json",
    "MasterBand.json",
]

RARITY_NAME = {2: 'R', 3: 'SR', 4: 'SSR', 20: 'BD'}


def _format_level(value):
    """把难度数值格式化为展示文本：19.0 -> "19"，25.5 -> "25.5" """
    if value is None or value == '':
        return ''
    if isinstance(value, str):
        return value
    if float(value).is_integer():
        return str(int(value))
    return ('%g' % value)


async def _empty_cover():
    """占位协程：没有歌曲时返回空封面"""
    return ""


def _secure_random_choice(items):
    """使用加密级随机数生成器进行公平选择"""
    if not items:
        return None
    secure_index = secrets.randbelow(len(items))
    return items[secure_index]


def select_song_with_difficulty(songs, first_difficulty_type=None, first_difficulty_level=None):
    """
    根据难度相似性选择歌曲，使用加密级随机数保证公平性
    相似性标准:
      - 同难度类型: ±5
      - 相邻难度类型: ±7
      - 非相邻难度类型: 不视为相似

    返回的两首歌都带上最终用于展示的难度（difficulty_type / difficulty_level），
    第二首展示的难度就是参与相似度比较的那个难度，避免"选的时候相似、显示的时候不相似"。
    """
    if not songs:
        return []

    first_song = _secure_random_choice(songs)
    if first_song is None:
        return []

    first_diff_info = _extract_best_difficulty(first_song)
    if first_diff_info:
        first_difficulty_type = first_diff_info['type']
        first_difficulty_level = first_diff_info['level']

    similar = _find_similar_songs(songs, first_difficulty_type, first_difficulty_level, exclude_ids=[first_song.get('id')])

    if similar:
        second_song, second_diff_info = _secure_random_choice(similar)
    else:
        logger.warning(f"⚠️ 未找到与难度 {first_difficulty_type}{first_difficulty_level} 相似的歌曲，随机选择")
        candidates = [s for s in songs if s.get('id') != first_song.get('id')]
        if candidates:
            second_song = _secure_random_choice(candidates)
            second_diff_info = _extract_best_difficulty(second_song)
        else:
            second_song = first_song
            second_diff_info = first_diff_info

    result = []
    for song, diff_info in [(first_song, first_diff_info), (second_song, second_diff_info)]:
        song_copy = dict(song)
        if diff_info:
            song_copy['difficulty_type'] = diff_info['type']
            song_copy['difficulty_level'] = diff_info['level']
            song_copy['difficulty_label'] = DIFFICULTY_LABEL_MAP.get(diff_info['type'], diff_info['type'].capitalize())
        else:
            song_copy['difficulty_type'] = None
            song_copy['difficulty_level'] = None
            song_copy['difficulty_label'] = None
        result.append(song_copy)

    return result


def _extract_best_difficulty(song):
    """从歌曲中提取一个难度信息（hard / expert 之间随机，保证两种难度都有机会显示）"""
    difficulties = song.get('difficulties', {})
    if not difficulties:
        return None

    available = []
    for diff_type in ('hard', 'expert'):
        entry = difficulties.get(diff_type)
        if entry and entry.get('display'):
            available.append({'type': diff_type, 'level': entry['display']})

    if not available:
        return None
    return _secure_random_choice(available)


def _find_similar_songs(songs, difficulty_type, difficulty_level, exclude_ids=None):
    """查找与指定难度相似的歌曲，返回 (歌曲, 参与匹配的那个难度) 列表

    同难度类型阈值 ±5，跨难度类型阈值 ±7；只比较数字难度值。
    同时带回匹配到的难度，调用方可以直接用它展示，无需再随机抽一次。
    """
    if exclude_ids is None:
        exclude_ids = []

    if difficulty_level is None:
        return [(s, _extract_best_difficulty(s)) for s in songs if s.get('id') not in exclude_ids]

    similar = []
    same_type_threshold = 5
    cross_type_threshold = 7

    for song in songs:
        song_id = song.get('id')
        if song_id in exclude_ids:
            continue

        difficulties = song.get('difficulties', {})
        if not difficulties:
            continue

        for diff_type, entry in difficulties.items():
            diff_level = entry.get('display', 0)
            level_diff = abs(diff_level - difficulty_level)

            threshold = same_type_threshold if diff_type == difficulty_type else cross_type_threshold
            if level_diff <= threshold:
                similar.append((song, {'type': diff_type, 'level': diff_level}))
                break

    return similar


TMPL = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=2400, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>签到成功</title>
    <style>
        @font-face {
            font-family: 'MiSans';
            src: url(data:font/ttf;base64,{{base64_font}}) format('truetype');
            font-weight: 500;
            font-style: normal;
        }

        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
            font-family: 'MiSans', -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif;
        }

        body {
            height: 2160px;
            width: 2400px;
            display: flex;
            justify-content: center;
            align-items: center;
            padding: 60px;
            position: relative;
            overflow: hidden;
        }

        .bg-layer {
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background-image: url('{{b64bg}}');
            background-repeat: no-repeat;
            background-position: center center;
            background-size: cover;
            z-index: 0;
        }

        /* 立绘层：铺满画布，置于左侧面板之下 */
        /* 立绘层：铺满画布，置于左侧面板之下；立绘按原图比例放大到接近全高 */
        .character-layer {
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            z-index: 1;
            pointer-events: none;
            overflow: hidden;
        }

        /* 立绘层：尺寸保持大幅放大，仅向下偏移，避免顶部切到头顶 */
        .character-layer img {
            position: absolute;
            right: -14%;
            bottom: -22%;
            height: 142%;
            width: auto;
            max-width: 108%;
            object-fit: contain;
            object-position: right bottom;
            filter: drop-shadow(0 12px 28px rgba(0,0,0,0.35));
        }

        .main-wrapper {
            display: flex;
            gap: 40px;
            width: 100%;
            max-width: 2280px;
            height: calc(2160px - 120px);
            align-items: stretch;
            position: relative;
            z-index: 2;
        }

        .divider {
            width: 4px;
            background: linear-gradient(180deg,
                rgba(255,255,255,0) 0%,
                rgba(255,255,255,0.7) 15%,
                rgba(255,255,255,0.7) 85%,
                rgba(255,255,255,0) 100%);
            border-radius: 2px;
            box-shadow: 0 0 30px rgba(255,255,255,0.4);
            flex-shrink: 0;
        }

        .left-panel {
            flex: 0 0 880px;
            background: rgba(255, 255, 255, 0.25);
            backdrop-filter: blur(28px);
            -webkit-backdrop-filter: blur(28px);
            border-radius: 52px;
            padding: 52px 42px;
            box-shadow: 0 8px 24px rgba(0, 0, 0, 0.08), inset 0 1px 0 rgba(255, 255, 255, 0.4);
            text-align: center;
            border: 1px solid rgba(255, 255, 255, 0.35);
            position: relative;
            overflow: hidden;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            align-self: center;
        }

        .right-panel {
            flex: 1;
            display: flex;
            flex-direction: column;
            justify-content: flex-end;
            align-items: flex-end;
            min-width: 0;
        }

        .card-info {
            background: rgba(20, 20, 30, 0.42);
            backdrop-filter: blur(18px);
            -webkit-backdrop-filter: blur(18px);
            border-radius: 28px;
            padding: 26px 34px;
            border: 1px solid rgba(255, 255, 255, 0.22);
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.28);
            max-width: 760px;
            margin-bottom: 12px;
            text-align: left;
        }

        .card-rarity-row {
            display: flex;
            align-items: center;
            gap: 18px;
            margin-bottom: 14px;
        }

        .card-rarity-badge {
            font-size: 34px;
            font-weight: 900;
            color: #1a1a1a;
            padding: 6px 22px;
            border-radius: 14px;
            letter-spacing: 2px;
            line-height: 1.2;
            box-shadow: 0 4px 14px rgba(0,0,0,0.3);
        }

        .card-rarity-badge.sr {
            background: linear-gradient(135deg, #f7d774, #d9a93c);
        }

        .card-rarity-badge.ssr {
            background: linear-gradient(135deg, #ffe9a8, #f5c542 45%, #c99a2e);
            box-shadow: 0 4px 18px rgba(245, 197, 66, 0.45);
        }

        .card-rarity-badge.bd {
            background: linear-gradient(135deg, #ffd6ec, #ff9ecb 50%, #e0569b);
            color: #ffffff;
            text-shadow: 0 2px 6px rgba(0,0,0,0.3);
        }

        .card-character-name {
            font-size: 44px;
            font-weight: 800;
            color: white;
            text-shadow: 0 3px 14px rgba(0, 0, 0, 0.4), 0 1px 3px rgba(0, 0, 0, 0.3);
            line-height: 1.2;
        }

        .card-subtitle {
            font-size: 30px;
            font-weight: 600;
            color: rgba(255, 255, 255, 0.92);
            text-shadow: 0 2px 10px rgba(0, 0, 0, 0.35);
            line-height: 1.35;
            margin-top: 4px;
        }

        .success-icon {
            width: 320px;
            height: 320px;
            margin: 0 auto 26px;
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: visible;
            position: relative;
        }

        .success-icon img {
            width: 100%;
            height: 100%;
            object-fit: contain;
            filter: drop-shadow(0 10px 20px rgba(0,0,0,0.4));
            transform: scale(0.94);
        }

        h1 {
            font-size: 68px;
            font-weight: 800;
            color: white;
            margin-bottom: 22px;
            text-shadow: 0 4px 16px rgba(0, 0, 0, 0.35), 0 2px 4px rgba(0, 0, 0, 0.25);
            letter-spacing: 2px;
            line-height: 1.18;
        }

        .date-time {
            font-size: 44px;
            color: rgba(255, 255, 255, 0.95);
            margin-bottom: 38px;
            padding-bottom: 32px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.25);
            font-weight: 500;
            text-shadow: 0 2px 12px rgba(0, 0, 0, 0.3), 0 1px 3px rgba(0, 0, 0, 0.2);
            line-height: 1.4;
        }

        .fortune-box {
            background: rgba(255, 255, 255, 0.20);
            backdrop-filter: blur(18px);
            border-radius: 32px;
            padding: 0;
            margin-bottom: 34px;
            border: 1px solid rgba(255, 255, 255, 0.28);
            display: flex;
            align-items: stretch;
            box-shadow:
                0 10px 28px rgba(0, 0, 0, 0.22),
                inset 0 1px 0 rgba(255, 255, 255, 0.3);
            overflow: hidden;
            transition: all 0.3s ease;
            min-height: 112px;
        }

        .fortune-level {
            flex: 0 0 210px;
            font-size: 68px;
            font-weight: 800;
            color: white;
            text-shadow: 0 4px 16px rgba(0, 0, 0, 0.35), 0 2px 4px rgba(0, 0, 0, 0.25);
            line-height: 1.2;
            padding: 26px 20px;
            text-align: center;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: background 0.3s ease;
            min-height: 120px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        .fortune-description {
            flex: 1;
            font-size: 46px;
            color: white;
            text-shadow: 0 2px 12px rgba(0, 0, 0, 0.3), 0 1px 3px rgba(0, 0, 0, 0.2);
            line-height: 1.38;
            font-weight: 600;
            padding: 26px 24px;
            text-align: right;
            display: flex;
            align-items: center;
            justify-content: flex-end;
            transition: background 0.3s ease;
            min-height: 120px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            margin-left: 10px;
        }

        .fortune-box.good .fortune-level,
        .fortune-box.good .fortune-description {
            background: rgba(74, 222, 128, 0.25);
        }
        .fortune-box.good {
            border-color: rgba(74, 222, 128, 0.5);
        }

        .fortune-box.bad .fortune-level,
        .fortune-box.bad .fortune-description {
            background: rgba(239, 68, 68, 0.28);
        }
        .fortune-box.bad {
            border-color: rgba(239, 68, 68, 0.55);
        }

        .fortune-box.neutral .fortune-level,
        .fortune-box.neutral .fortune-description {
            background: rgba(255, 193, 7, 0.25);
        }
        .fortune-box.neutral {
            border-color: rgba(255, 193, 7, 0.5);
        }

        .fortune {
            display: flex;
            justify-content: space-around;
            gap: 22px;
            margin-bottom: 36px;
            position: relative;
        }

        .fortune-item {
            flex: 1;
            padding: 44px 24px;
            border-radius: 32px;
            backdrop-filter: blur(22px);
            border: 1px solid rgba(255, 255, 255, 0.35);
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 18px;
            overflow: visible;
            position: relative;
            box-shadow:
                0 8px 24px rgba(0, 0, 0, 0.18),
                inset 0 1px 0 rgba(255, 255, 255, 0.25);
        }

        .fortune-item.good {
            background: rgba(74, 222, 128, 0.35);
            border-color: rgba(74, 222, 128, 0.55);
        }

        .fortune-item.bad {
            background: rgba(239, 68, 68, 0.35);
            border-color: rgba(239, 68, 68, 0.55);
        }

        .fortune-icon {
            width: 180px;
            height: 180px;
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: visible;
            position: relative;
        }

        .fortune-icon img {
            width: 100%;
            height: 100%;
            object-fit: contain;
            filter: drop-shadow(0 6px 14px rgba(0,0,0,0.35));
            transform: scale(1.06);
        }

        .fortune-label {
            font-size: 40px;
            color: rgba(255, 255, 255, 0.95);
            font-weight: 800;
            text-transform: uppercase;
            letter-spacing: 4px;
            text-shadow: 0 2px 12px rgba(0, 0, 0, 0.3), 0 1px 3px rgba(0, 0, 0, 0.2);
        }

        .fortune-value {
            font-size: 50px;
            color: white;
            font-weight: 800;
            text-shadow: 0 4px 16px rgba(0, 0, 0, 0.35), 0 2px 4px rgba(0, 0, 0, 0.25);
            line-height: 1.32;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        /* 左侧歌曲推荐样式 */
        .song-section {
            margin-top: 24px;
            padding: 22px;
            background: rgba(255, 255, 255, 0.18);
            backdrop-filter: blur(16px);
            border-radius: 28px;
            border: 1px solid rgba(255, 255, 255, 0.3);
            box-shadow:
                0 8px 24px rgba(0, 0, 0, 0.18),
                inset 0 1px 0 rgba(255, 255, 255, 0.25);
        }

        .song-section-title {
            font-size: 34px;
            font-weight: 700;
            color: white;
            text-shadow: 0 2px 12px rgba(0, 0, 0, 0.3), 0 1px 3px rgba(0, 0, 0, 0.2);
            margin-bottom: 16px;
            letter-spacing: 2px;
            text-align: left;
        }

        .song-info-left {
            display: flex;
            align-items: center;
            gap: 20px;
            background: rgba(255, 255, 255, 0.15);
            border-radius: 20px;
            padding: 16px 20px;
            border: 1px solid rgba(255, 255, 255, 0.25);
            position: relative;
        }

        .song-info-second {
            margin-top: 14px;
        }

        .song-cover-left {
            width: 120px;
            height: 120px;
            border-radius: 20px;
            object-fit: cover;
            box-shadow: 0 8px 24px rgba(0, 0, 0, 0.42);
            flex-shrink: 0;
        }

        .song-details-left {
            flex: 1;
            display: flex;
            flex-direction: column;
            gap: 6px;
            text-align: left;
            padding-right: 115px;
        }

        .song-difficulty-badge {
            position: absolute;
            right: 18px;
            top: 50%;
            transform: translateY(-50%);
            width: 100px;
            height: 100px;
            border-radius: 18px;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            gap: 6px;
            box-shadow: 0 6px 18px rgba(0, 0, 0, 0.35);
            backdrop-filter: blur(4px);
            -webkit-backdrop-filter: blur(4px);
        }

        .song-difficulty-badge.hard {
            background: rgba(255, 193, 7, 0.35);
            border: 1px solid rgba(255, 193, 7, 0.6);
        }

        .song-difficulty-badge.expert {
            background: linear-gradient(135deg, rgba(244, 67, 54, 0.35), rgba(233, 30, 99, 0.35));
            border: 1px solid rgba(244, 67, 54, 0.55);
        }

        .difficulty-label {
            font-size: 20px;
            font-weight: 800;
            color: white;
            text-shadow: 0 2px 6px rgba(0, 0, 0, 0.5), 0 1px 2px rgba(0, 0, 0, 0.4);
            line-height: 1;
            letter-spacing: 0.5px;
        }

        .difficulty-value {
            font-size: 38px;
            font-weight: 900;
            color: white;
            text-shadow: 0 3px 8px rgba(0, 0, 0, 0.6), 0 1px 4px rgba(0, 0, 0, 0.5);
            line-height: 1;
            letter-spacing: 1px;
        }

        .song-name-left {
            font-size: 26px;
            font-weight: 600;
            color: white;
            text-shadow: 0 2px 10px rgba(0, 0, 0, 0.28), 0 1px 3px rgba(0, 0, 0, 0.18);
            line-height: 1.3;
        }

        .song-name-cn-left {
            font-size: 36px;
            font-weight: 800;
            color: white;
            text-shadow: 0 3px 14px rgba(0, 0, 0, 0.32), 0 1px 3px rgba(0, 0, 0, 0.22);
        }

        .footer {
            margin-top: auto;
            padding-top: 26px;
            font-size: 34px;
            color: rgba(255, 255, 255, 0.88);
            text-shadow: 0 3px 14px rgba(0, 0, 0, 0.32), 0 1px 3px rgba(0, 0, 0, 0.22);
            font-weight: 700;
            letter-spacing: 2.5px;
            line-height: 1.4;
        }

    </style>
</head>
<body>
    <div class="bg-layer"></div>
    <div class="character-layer">
        <img src="{{b64char}}" alt="立绘" onerror="this.style.display='none'">
    </div>
    <div class="main-wrapper">
        <!-- 左侧面板：原有签到内容 -->
        <div class="left-panel">
            <div class="success-icon">
                <img src="{{b64ok}}" alt="成功" onerror="this.style.display='none'">
            </div>

            <h1>{{name}},签到成功!</h1>
            <div class="date-time" id="datetime">2023年10月15日 14:30</div>

            <div class="fortune-box {{fortune_color}}" id="fortuneBox">
                <div class="fortune-level" id="fortuneLevel">{{fortune_level}}</div>
                <div class="fortune-description" id="fortuneDesc">{{fortune_desc}}</div>
            </div>

            <div class="fortune">
                <div class="fortune-item good">
                    <div class="fortune-icon">
                        <img src="{{b64yi}}" alt="宜" onerror="this.style.display='none'">
                    </div>
                    <div class="fortune-label">宜</div>
                    <div class="fortune-value" id="good">{{yi_text}}</div>
                </div>
                <div class="fortune-item bad">
                    <div class="fortune-icon">
                        <img src="{{b64ji}}" alt="忌" onerror="this.style.display='none'">
                    </div>
                    <div class="fortune-label">忌</div>
                    <div class="fortune-value" id="bad">{{ji_text}}</div>
                </div>
            </div>

            {% if show_song %}
            <!-- 今日推荐歌曲 -->
            <div class="song-section">
                <div class="song-section-title">🎵 今日歌曲推荐</div>
                <div class="song-info-left">
                    <img src="{{song_cover_base64}}" alt="封面" class="song-cover-left" onerror="this.src='data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><rect fill=%22%23667eea%22 width=%22100%22 height=%22100%22/><text x=%2250%22 y=%2255%22 font-size=%2240%22 fill=%22white%22 text-anchor=%22middle%22>🎵</text></svg>'">
                    <div class="song-details-left">
                        <div class="song-name-cn-left">{{song_name_cn}}</div>
                        <div class="song-name-left">{{song_name}}</div>
                    </div>
                    {% if song_difficulty_type %}
                    <div class="song-difficulty-badge {{song_difficulty_type}}">
                        <div class="difficulty-label">{{song_difficulty_label}}</div>
                        <div class="difficulty-value">{{song_difficulty_level}}</div>
                    </div>
                    {% endif %}
                </div>
                <div class="song-info-left song-info-second">
                    <img src="{{song2_cover_base64}}" alt="封面" class="song-cover-left" onerror="this.src='data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><rect fill=%22%23667eea%22 width=%22100%22 height=%22100%22/><text x=%2250%22 y=%2255%22 font-size=%2240%22 fill=%22white%22 text-anchor=%22middle%22>🎵</text></svg>'">
                    <div class="song-details-left">
                        <div class="song-name-cn-left">{{song2_name_cn}}</div>
                        <div class="song-name-left">{{song2_name}}</div>
                    </div>
                    {% if song2_difficulty_type %}
                    <div class="song-difficulty-badge {{song2_difficulty_type}}">
                        <div class="difficulty-label">{{song2_difficulty_label}}</div>
                        <div class="difficulty-value">{{song2_difficulty_level}}</div>
                    </div>
                    {% endif %}
                </div>
            </div>
            {% endif %}

            <div class="footer">
                Designed by 慵懒午睡 | 请勿迷信哦
            </div>
        </div>
        <!-- 右侧面板：卡面信息 -->
        <div class="right-panel">
            {% if card_character_name %}
            <div class="card-info">
                <div class="card-rarity-row">
                    <div class="card-rarity-badge {{card_rarity_class}}">{{card_rarity_name}}</div>
                    <div class="card-character-name">{{card_character_name}}</div>
                </div>
                {% if card_subtitle %}
                <div class="card-subtitle">「{{card_subtitle}}」</div>
                {% endif %}
            </div>
            {% endif %}
        </div>
    </div>

    <script>
        function updateDateTime() {
            const now = new Date();
            const options = {
                year: 'numeric',
                month: 'long',
                day: 'numeric',
                hour: '2-digit',
                minute: '2-digit',
                hour12: false,
                timeZone: 'Asia/Shanghai'
            };
            const formattedDate = now.toLocaleString('zh-CN', options);
            document.getElementById('datetime').textContent = formattedDate;
        }

        const goodFortunes = ['追新番', '画同人', '做手书', '参加漫展', '写同人小说', '学习声优', '打游戏', '社区互助','做攻略', '参加社团', '学习绘画', '参加同人展', '分享资源', '关注公益', '练习日语', '整理收藏','看动漫', '听OST', '收集周边', '研究设定', 'cosplay', '配音练习', '剪辑AMV', '制作MAD'];
        const badFortunes = ['沉迷抽卡', '挖坑不填', '刷无用番', '熬夜追更', '沉迷弹幕', '过度氪金', '网暴同好', '弃坑不交', '沉迷游戏','刷无意义弹幕', '过度磕CP', '沉迷虚拟恋爱', '盗版成瘾', '无节制追星', '忽视健康', '网络争吵', '囤积周边','毒唯行为', '引战', '剧透未看番', '滥用表情包', '刷存在感', '跟风黑角色'];

        const fortuneData = [
            { level: '大吉', desc: '最最顺!😎', color: 'good', weight: 5 },
            { level: '吉', desc: '幸福像🎈一样飘起来', color: 'good', weight: 10 },
            { level: '中吉', desc: '努力就发光😋', color: 'good', weight: 15 },
            { level: '小吉', desc: '小小的幸运😆', color: 'good', weight: 20 },
            { level: '半吉', desc: '明天会更开心哦😍', color: 'neutral', weight: 15 },
            { level: '末吉', desc: '转机就在眼前😉', color: 'neutral', weight: 10 },
            { level: '末小吉', desc: '再加把劲哦💪🏻', color: 'neutral', weight: 10 },
            { level: '凶', desc: '困难是成长的礼物🎁', color: 'bad', weight: 8 },
            { level: '小凶', desc: '小心点就好哦❤️', color: 'bad', weight: 7 },
            { level: '半凶', desc: '要小心一点哦..', color: 'bad', weight: 5 },
            { level: '末凶', desc: '要小心一点哦🥺', color: 'bad', weight: 3 },
            { level: '大凶', desc: '要小心一点哦🥺', color: 'bad', weight: 2 }
        ];

        function weightedRandom(items) {
            let totalWeight = 0;
            for (let i = 0; i < items.length; i++) {
                totalWeight += items[i].weight;
            }

            let random = Math.random() * totalWeight;
            let currentWeight = 0;

            for (let i = 0; i < items.length; i++) {
                currentWeight += items[i].weight;
                if (random <= currentWeight) {
                    return items[i];
                }
            }

            return items[items.length - 1];
        }

        function setFortune() {
            const fortuneLevel = document.getElementById('fortuneLevel').textContent.trim();
            const fortuneDesc = document.getElementById('fortuneDesc').textContent.trim();

            if (!fortuneLevel || !fortuneDesc) {
                const randomGood = goodFortunes[Math.floor(Math.random() * goodFortunes.length)];
                const randomBad = badFortunes[Math.floor(Math.random() * badFortunes.length)];
                document.getElementById('good').textContent = randomGood;
                document.getElementById('bad').textContent = randomBad;

                const selectedFortune = weightedRandom(fortuneData);
                document.getElementById('fortuneLevel').textContent = selectedFortune.level;
                document.getElementById('fortuneDesc').textContent = selectedFortune.desc;

                const fortuneBox = document.getElementById('fortuneBox');
                fortuneBox.className = 'fortune-box ' + selectedFortune.color;
            }
        }

        updateDateTime();
        setFortune();
    </script>
</body>
</html>
"""


@register("astrbot_plugin_on_signin", "慵懒午睡", "OurNotes 风格签到插件 - 云端卡面、每日运势、歌曲推荐", "1.0.0")


class MyPlugin(Star):
    """OurNotes 风格签到插件 - 云端卡面、每日运势、歌曲推荐"""

    ASSET_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "image/avif,image/webp,image/png,image/*,*/*;q=0.8",
    }

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self.plugin_dir = Path(__file__).parent

        from astrbot.core.utils.astrbot_path import get_astrbot_data_path
        self.data_dir = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_on_signin"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.data_file = self.data_dir / "signin_data.json"
        self.config_file = self.data_dir / "config.json"
        self.db_path = self.data_dir / "signin.db"
        self.cache_dir = self.data_dir / "cache"
        self.master_dir = self.data_dir / "masterdata"
        # 运行时下载的立绘/背景/封面，插件更新不会丢；插件自带 data/assets/ 作兜底
        self.assets_dir = self.data_dir / "assets"

        # 本地内置数据（插件随附，用于离线兜底）
        self.builtin_data_file = self.plugin_dir / "data" / "ournotes_data.json"

        self._ensure_data_file()
        self._load_custom_config()
        self._load_dataset()

        # 初始化SQLite数据库
        self.db = SigninDatabase(self.db_path)

        # 迁移旧数据（世界计划插件遗留）到新数据库
        try:
            old_data = self._load_data()
            if old_data and isinstance(old_data, dict) and len(old_data) > 0:
                logger.info(f"📦 检测到旧数据格式，开始迁移到SQLite数据库...")
                self.db.migrate_from_json(old_data)
                backup_file = self.data_file.with_suffix('.json.bak')
                shutil.copy2(self.data_file, backup_file)
                logger.info(f"✅ 数据迁移完成，旧文件已备份为: {backup_file.name}")
        except Exception as e:
            logger.warning(f"⚠️ 数据迁移失败（可忽略）: {e}")

        # 启动时在后台执行一次每日清理（删除过期数据并回收空间），不阻塞加载
        self.history_keep_days = int(self.config.get('history_keep_days', 365) or 365)
        threading.Thread(
            target=self.db.maybe_cleanup,
            args=(self.history_keep_days,),
            name="on-signin-db-startup-cleanup",
            daemon=True
        ).start()

        # 后台执行一次 masterdata 同步（每天最多一次），并在插件启动时预热图片缓存
        self._sync_lock = threading.Lock()
        self._image_cache = {}
        self._image_cache_lock = threading.Lock()
        self._sync_state = {'last_sync': None, 'running': False}
        self._load_sync_state()

        # 云端图片下载专用线程池：网络请求一律走 executor，绝不占用事件循环线程，
        # 否则单次下载超时就可能把整个 AstrBot 卡住。
        # 并发压到 2：实测这些 CDN 在 3~4 并发时会频繁 SSL 重置，
        # 2 并发反而更稳（总耗时与串行接近，但配上重试成功率明显更高）。
        import concurrent.futures
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="on-signin-dl"
        )
        # 单次下载超时（秒）。这些 CDN 从国内连经常要 1~2 秒才握手成功，
        # 太短会把本来能成功的请求判死；每个源最多重试 2 次，再自动换备选源。
        self.download_timeout = float(self.config.get('download_timeout', 20) or 20)

        if self.config.get('auto_sync_masterdata', True):
            threading.Thread(
                target=self._maybe_sync_masterdata,
                name="on-signin-masterdata-sync",
                daemon=True
            ).start()

        self._load_font()

        self._load_decorations()
        self._load_stickers()
        self._load_character_metadata()

        logger.info(f"📝 插件配置已加载 - 签到指令: {self.config.get('sign_in_command', '签到')}")

    def _load_font(self):
        """加载完整 MiSANS-Medium.ttf

        不做子集化：子集会丢掉部分字形导致回退到系统字体，牺牲字形完整性。
        字体 base64 只影响 HTML 体积（生成侧），不影响下载耗时。
        """
        self.base64_font = ""
        self.font_format = "truetype"

        full = self.plugin_dir / "MiSans-Medium.ttf"
        if full.exists():
            with open(full, 'rb') as f:
                self.base64_font = base64.b64encode(f.read()).decode('utf-8')
            logger.info(f"✅ 成功加载 MiSans-Medium.ttf "
                        f"(base64 {len(self.base64_font) // 1024} KB)")
        else:
            logger.warning("⚠️ 未找到 MiSans-Medium.ttf 字体文件，将使用系统默认字体")

    # ------------------------------------------------------------------ #
    # 数据加载与同步
    # ------------------------------------------------------------------ #

    def _load_dataset(self):
        """加载 OurNotes 数据集（歌曲 / 卡面），优先使用同步后的数据，其次内置数据"""
        self.songs = []
        self.cards = []
        self._cards_by_character = {}

        candidates = [
            self.data_dir / "ournotes_data.json",
            self.builtin_data_file,
        ]
        for path in candidates:
            if not path.exists():
                continue
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.songs = data.get('songs', []) or []
                self.cards = data.get('cards', []) or []
                self._index_cards()
                logger.info(f"✅ 已加载 OurNotes 数据集: {len(self.songs)} 首歌曲, {len(self.cards)} 张 SR+/生日卡面 (来源: {path.name})")
                if self.songs and self.cards:
                    return
            except Exception as e:
                logger.warning(f"⚠️ 加载数据集失败 {path}: {e}")

        if not self.songs:
            logger.warning("⚠️ 未找到歌曲数据，请检查内置数据或等待 masterdata 同步")

    def _index_cards(self):
        """按角色 ID 索引卡面，便于指定角色签到"""
        self._cards_by_character = {}
        for card in self.cards:
            self._cards_by_character.setdefault(card.get('character_id'), []).append(card)

    def _load_sync_state(self):
        state_file = self.master_dir / "sync_state.json"
        try:
            if state_file.exists():
                with open(state_file, 'r', encoding='utf-8') as f:
                    state = json.load(f)
                self._sync_state['last_sync'] = state.get('last_sync')
        except Exception:
            pass

    def _save_sync_state(self):
        try:
            self.master_dir.mkdir(parents=True, exist_ok=True)
            with open(self.master_dir / "sync_state.json", 'w', encoding='utf-8') as f:
                json.dump({'last_sync': self._sync_state.get('last_sync')}, f)
        except Exception as e:
            logger.warning(f"⚠️ 保存同步状态失败: {e}")

    def _maybe_sync_masterdata(self):
        """每天最多同步一次 masterdata（后台线程执行，不阻塞插件加载）"""
        today = datetime.now().strftime("%Y-%m-%d")
        last = (self._sync_state.get('last_sync') or '')[:10]
        if last == today:
            logger.info(f"📅 masterdata 今日已同步过（{last}），跳过")
            return
        try:
            if self._sync_masterdata(force=False):
                logger.info("✅ masterdata 启动同步完成")
        except Exception as e:
            logger.warning(f"⚠️ masterdata 同步失败（不影响使用内置数据）: {e}")

    def _sync_masterdata(self, force: bool = False) -> bool:
        """从 GitHub 拉取繁中服（hk-tw-mo）master 文件并重建本地数据集"""
        if self._sync_state.get('running'):
            return False
        with self._sync_lock:
            if self._sync_state.get('running'):
                return False
            self._sync_state['running'] = True

        try:
            self.master_dir.mkdir(parents=True, exist_ok=True)
            tables = {}
            for filename in MASTERDATA_FILES:
                local_path = self.master_dir / filename
                url = f"https://api.github.com/repos/{MASTERDATA_REPO}/contents/{MASTERDATA_PATH}/{filename}?ref={MASTERDATA_BRANCH}"
                if not force and local_path.exists():
                    # 增量模式：已有文件直接复用，避免每天重复下载大文件
                    try:
                        with open(local_path, 'r', encoding='utf-8') as f:
                            tables[filename[:-5]] = json.load(f)
                        continue
                    except Exception:
                        pass
                try:
                    req = urllib.request.Request(url, headers={
                        "User-Agent": "Mozilla/5.0",
                        "Accept": "application/vnd.github.raw",
                    })
                    with urllib.request.urlopen(req, timeout=120) as resp:
                        payload = resp.read()
                    data = json.loads(payload.decode('utf-8'))
                    with open(local_path, 'w', encoding='utf-8') as f:
                        f.write(payload.decode('utf-8'))
                    tables[filename[:-5]] = data
                    logger.info(f"⬇️ 已同步 {filename} ({len(payload) // 1024} KB)")
                except Exception as e:
                    logger.warning(f"⚠️ 同步 {filename} 失败: {e}")
                    if local_path.exists():
                        try:
                            with open(local_path, 'r', encoding='utf-8') as f:
                                tables[filename[:-5]] = json.load(f)
                        except Exception:
                            pass

            if not tables.get('MasterLiveMusic') or not tables.get('MasterMemberCard'):
                logger.warning("⚠️ masterdata 不完整，跳过重建数据集")
                return False

            dataset = self._build_dataset(tables)
            out_path = self.data_dir / "ournotes_data.json"
            with open(out_path, 'w', encoding='utf-8') as f:
                json.dump(dataset, f, ensure_ascii=False, indent=1)

            self._sync_state['last_sync'] = datetime.now().isoformat(timespec='seconds')
            self._save_sync_state()

            # 热加载新数据集
            self.songs = dataset.get('songs', [])
            self.cards = dataset.get('cards', [])
            self._index_cards()
            logger.info(f"✅ masterdata 同步完成：{len(self.songs)} 首歌曲, {len(self.cards)} 张卡面")
            # master 里出现新卡面/新封面时，把缺的图片补下载到持久化目录
            self._sync_assets_if_needed(dataset)
            return True
        except Exception as e:
            logger.error(f"❌ masterdata 同步异常: {e}")
            return False
        finally:
            self._sync_state['running'] = False

    def _sync_assets_if_needed(self, dataset: dict):
        """把数据集里缺图的卡面/封面补下载到持久化目录

        只在确实缺文件时才发起下载，已存在的一律跳过，所以日常同步不产生网络开销。
        下载放在后台线程里，避免拖慢签到指令本身。
        """
        missing_cards = []
        for card in dataset.get('cards', []):
            cid = card.get('asset_id')
            if not cid:
                continue
            for kind in ("member_character", "member_background"):
                if not self._find_local_asset(f"cards/{cid}_{kind}"):
                    missing_cards.append((cid, kind))
        missing_jackets = []
        for song in dataset.get('songs', []):
            jk = song.get('jacket') or song.get('jacket_asset')
            if jk and not self._find_local_asset(f"jackets/{jk}"):
                missing_jackets.append(jk)

        total = len(missing_cards) + len(missing_jackets)
        if not total:
            return
        logger.info(f"🧩 检测到 {len(missing_cards)} 个卡面图层与 {len(missing_jackets)} "
                    f"张封面缺失，开始后台补下载")
        import threading
        t = threading.Thread(target=self._download_missing_assets,
                             args=(missing_cards, missing_jackets), daemon=True)
        t.start()
        return t

    def _download_missing_assets(self, missing_cards, missing_jackets):
        """后台补齐缺图；失败的留着不管，下次同步会再试"""
        done = 0
        for cid, kind in missing_cards:
            try:
                url, fallbacks = self._card_asset_urls(cid, kind)
                self._fetch_image(url, f"card_{cid}_{kind}", fallback_urls=fallbacks,
                                  local_names=self._card_local_names(cid, kind),
                                  target_subdir="cards")
                done += 1
            except Exception as e:
                logger.warning(f"⚠️ 补下载立绘/背景失败 card={cid} {kind}: {e}")
        for jk in missing_jackets:
            try:
                url, fallbacks = self._jacket_urls(jk)
                self._fetch_image(url, f"jkt_{jk}", fallback_urls=fallbacks,
                                  local_names=self._jacket_local_names(jk),
                                  target_subdir="jackets")
                done += 1
            except Exception as e:
                logger.warning(f"⚠️ 补下载封面失败 {jk}: {e}")
        if done:
            logger.info(f"✅ 后台补下载完成：{done} 个文件")

    def _build_dataset(self, tables: dict) -> dict:
        """把 master 表合并为插件使用的精简数据集（歌曲名 / 难度 / 卡面）"""
        text_table = tables.get('MasterText', {}).get('_allData', [])
        text = {x.get('_id', ''): x for x in text_table}

        def tc(key, default=''):
            """取简体中文文案，缺失时按 繁体 → 日文 兜底"""
            entry = text.get(key)
            if not entry:
                return default
            return (entry.get('_simplifiedChinese') or entry.get('_traditionalChinese')
                    or entry.get('_japanese') or default)

        music_list = tables.get('MasterLiveMusic', {}).get('_allData', [])
        score_list = tables.get('MasterLiveMusicScore', {}).get('_allData', [])
        card_list = tables.get('MasterMemberCard', {}).get('_allData', [])
        char_list = tables.get('MasterCharacter', {}).get('_allData', [])
        band_list = tables.get('MasterBand', {}).get('_allData', [])

        score = {x['_id']: x for x in score_list}
        charmap = {c['_id']: c for c in char_list}
        band_name = {b['_id']: tc(b['_nameTextID'], '') for b in band_list}

        songs = []
        for s in music_list:
            hard = score.get(s.get('_hardID', 0), {})
            expert = score.get(s.get('_expertID', 0), {})
            vocalists = []
            for vid in s.get('_vocalCharacterIDs', []):
                c = charmap.get(vid)
                if c:
                    vocalists.append({'id': vid, 'name': tc(c['_nameTextID'], '')})
            # 演唱团体：优先乐队名，无乐队时退回主唱名（主唱过多时只取前几个，避免卡片溢出）
            artist_names = [band_name.get(b, '') for b in s.get('_bandIDs', []) if band_name.get(b)]
            if not artist_names:
                artist_names = [v['name'] for v in vocalists if v['name']][:3]
            songs.append({
                'id': s['_id'],
                'title': tc(s['_titleTextID'], ''),
                'title_ja': text.get(s['_titleTextID'], {}).get('_japanese', ''),
                'jacket': s.get('_jacketAssetName', ''),
                'composer': tc(s['_composerTextID'], ''),
                'lyricist': tc(s['_lyricistTextID'], ''),
                'arranger': tc(s['_arrangerTextID'], ''),
                'band_ids': s.get('_bandIDs', []),
                'band_names': [band_name.get(b, '') for b in s.get('_bandIDs', [])],
                'artist': ' / '.join(artist_names),
                'music_type': s.get('_musicType', 0),
                'difficulties': {
                    'hard': {
                        'level': hard.get('_musicScoreLevel', 0),
                        'display': hard.get('_musicScoreDisplayLevel', 0.0),
                    },
                    'expert': {
                        'level': expert.get('_musicScoreLevel', 0),
                        'display': expert.get('_musicScoreDisplayLevel', 0.0),
                    },
                },
                'vocalists': vocalists,
                'sort_order': s.get('_sortOrder', 0),
            })

        cards = []
        for c in card_list:
            rarity = c.get('_rarity', 2)
            if rarity < 3:
                continue  # 只要 SR 以上与生日卡面（BD）
            ch = charmap.get(c.get('_characterID'))
            if not ch:
                continue
            cards.append({
                'asset_id': c['_assetID'],
                'rarity': rarity,
                'rarity_name': RARITY_NAME.get(rarity, '?'),
                'character_id': c['_characterID'],
                'character_name': tc(ch['_nameTextID'], ''),
                'subtitle': tc(c['_subtitleTextID'], ''),
                'start_at': c.get('_startAt', ''),
            })

        return {
            'meta': {
                'source': f"https://github.com/{MASTERDATA_REPO}/tree/{MASTERDATA_BRANCH}/{MASTERDATA_PATH}",
                'server': 'hk-tw (繁中服)',
                'generated_at': datetime.now().isoformat(timespec='seconds'),
                'asset_base': ASSET_BASE,
                'asset_mirrors': [m['name'] for m in ASSET_MIRRORS],
            },
            'songs': songs,
            'cards': cards,
        }

    # ------------------------------------------------------------------ #
    # 云端资产下载与缓存
    # ------------------------------------------------------------------ #

    # 常见图片格式的魔数。用于三处校验：下载回来的数据、磁盘缓存读出来的数据、
    # 以及 Pillow 解码后的成品。任何一处看起来不像图片就当失败处理，绝不写入缓存。
    IMAGE_MAGICS = (
        b"\x89PNG\r\n\x1a\n",   # png
        b"\xff\xd8\xff",        # jpeg
        b"RIFF",                # webp（RIFF....WEBP）
        b"GIF87a", b"GIF89a",
    )

    @classmethod
    def _looks_like_image(cls, data: bytes) -> bool:
        """粗判一段字节是不是图片：至少要有内容且以已知图片魔数开头"""
        if not data or len(data) < 32:
            return False
        if data[:4] == b"RIFF":
            # webp：RIFF<size>WEBP
            return data[8:12] == b"WEBP"
        return any(data.startswith(m) for m in cls.IMAGE_MAGICS if m != b"RIFF")

    @staticmethod
    def _decodes(data: bytes) -> bool:
        """Pillow 完整解码一遍：截断的图在这一步必然报错

        Image.open() 只读文件头，半张图会被当成合法图；load() 才会真正解像素。
        """
        try:
            img = Image.open(io.BytesIO(data))
            img.load()
            return True
        except Exception:
            return False

    # 源健康度跟踪：Cloudflare 限流时会直接重置连接，一次失败不代表资源不存在。
    # 记录"最近一次失败时间 + 连续失败次数"，用于把可疑源排到后面。
    _SOURCE_COOLDOWN = 20.0      # 秒：源失败后的冷却时长

    def _source_health(self) -> dict:
        if not hasattr(self, '_source_state'):
            self._source_state = {}
        return self._source_state

    def _source_key(self, url: str) -> str:
        try:
            from urllib.parse import urlparse
            return urlparse(url).netloc
        except Exception:
            return url

    def _note_source_result(self, url: str, ok: bool):
        """记录一次源访问结果，用于后续排序"""
        state = self._source_health()
        key = self._source_key(url)
        entry = state.setdefault(key, {'fails': 0, 'failed_at': 0.0})
        if ok:
            entry['fails'] = 0
            entry['failed_at'] = 0.0
        else:
            entry['fails'] += 1
            entry['failed_at'] = time.time()

    def _order_sources(self, candidates):
        """把"刚失败过、还在冷却期"的源排到最后；其余保持原顺序"""
        now = time.time()
        state = self._source_health()

        def _penalty(item):
            entry = state.get(self._source_key(item[0]))
            if not entry or entry['fails'] == 0:
                return 0
            if now - entry['failed_at'] > self._SOURCE_COOLDOWN:
                return 0            # 冷却已过，恢复正常优先级
            return 1 + min(entry['fails'], 3)

        return sorted(candidates, key=_penalty)

    def _fetch_image(self, url: str, cache_key: str, ttl_hours: int = 168,
                     fallback_urls=(), local_names=(), target_subdir=None) -> bytes:
        """下载图片并写入本地磁盘缓存；命中缓存直接读文件（内存零占用）

        fallback_urls 是备选源：主源失败（超时/连接重置/404）时按顺序尝试，
        全部失败才抛异常。

        local_names 是预置资源名（不带扩展名），会依次在
        plugin_data/assets/（运行时下载落地的位置）和插件自带 data/assets/ 里找。
        服务器到 CDN 的 TLS 握手和读操作经常超时（实测一张曲绘 6~200s，
        并发竞速仍有约三分之一概率全部失败），所以本地有图就绝不联网。

        target_subdir 非空时，下载成功的图片会额外落一份到
        plugin_data/assets/<subdir>/ 下，下次同步检测到它存在就不再重下。

        注意：这是同步阻塞调用，只允许在 executor 线程里执行（见 _fetch_image_async），
        不能在事件循环线程里直接调用。
        """
        cache_path = self.cache_dir / f"{cache_key}.bin"
        try:
            if cache_path.exists():
                age = datetime.now() - datetime.fromtimestamp(cache_path.stat().st_mtime)
                if age < timedelta(hours=ttl_hours):
                    cached = cache_path.read_bytes()
                    # 上一次可能存进去了半张图（连接读到一半断掉）。校验魔数，
                    # 不合格就删掉重新下载，否则这个 key 会一直返回坏图。
                    if self._looks_like_image(cached):
                        return cached
                    logger.warning(f"🧹 缓存图片损坏，重新下载 {cache_key}（{len(cached)} 字节）")
                    try:
                        cache_path.unlink()
                    except Exception:
                        pass
        except Exception:
            pass

        # 预置资源优先：本地有就绝不联网，这是"一定能拿到图"的保证。
        for name in (local_names or ()):
            data = self._read_local_asset(name)
            if data:
                try:
                    self.cache_dir.mkdir(parents=True, exist_ok=True)
                    tmp = cache_path.with_suffix('.tmp')
                    tmp.write_bytes(data)
                    tmp.replace(cache_path)
                except Exception:
                    pass
                return data

        # 主源 + 备选源依次尝试；每个源最多 2 次（退避 0.4s/0.9s 避开限流窗口）
        # 源级冷却：某个源刚连续失败过（Cloudflare 限流会重置连接），
        # 就把它排到最后，让健康的源先上，避免每张图都先在坏源上耗掉两轮超时。
        candidates = [(url, 2)] + [(u, 2) for u in (fallback_urls or [])]
        ordered = self._order_sources(candidates)
        last_err = None
        data = None
        for candidate_url, attempts in ordered:
            for attempt in range(attempts):
                data = None              # 每次尝试都从零开始，绝不继承上一次的半张图
                try:
                    req = urllib.request.Request(candidate_url, headers=self.ASSET_HEADERS)
                    with urllib.request.urlopen(req, timeout=self.download_timeout) as resp:
                        data = resp.read()
                    # 读到一半断流时 read() 也可能不报错，只返回截断的数据。
                    # 校验不通过就继续重试/换源。
                    if not self._looks_like_image(data):
                        raise OSError(f"响应不是有效图片（{len(data or b'')} 字节）")
                    self._note_source_result(candidate_url, True)
                    break
                except urllib.error.HTTPError as e:
                    # 404/403 说明这个源没有这张图，换源比重试有用
                    last_err = e
                    if e.code in (404, 403, 410):
                        self._note_source_result(candidate_url, True)   # 源是好的，只是没这张图
                        break
                    self._note_source_result(candidate_url, False)
                    if attempt == attempts - 1:
                        break
                    time.sleep(0.4 * (attempt + 1) ** 2)
                except Exception as e:
                    last_err = e
                    self._note_source_result(candidate_url, False)
                    if attempt == attempts - 1:
                        break
                    time.sleep(0.4 * (attempt + 1) ** 2)
            if data:
                break

        if not data:
            raise last_err if last_err else RuntimeError("download failed")

        # 按实际格式落一份到持久化目录，下次同步检测到文件存在就跳过下载
        if target_subdir:
            self._store_local_asset(target_subdir, cache_key, data)

        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = cache_path.with_suffix('.tmp')
            tmp.write_bytes(data)
            tmp.replace(cache_path)
        except Exception as e:
            logger.warning(f"⚠️ 写入图片缓存失败 {cache_key}: {e}")
        return data

    def _store_local_asset(self, subdir: str, key: str, data: bytes):
        """把下载到的图写进 plugin_data/assets/<subdir>/

        key 用 cache_key（card_30_member_character / jkt_jkt_001_100001）。
        落地的文件名必须和 _card_local_names / _jacket_local_names 产生的一致，
        否则下次同步检测不到，会反复重下。
        """
        try:
            assets_dir = getattr(self, 'assets_dir', None)
            if assets_dir is None:
                return
            ext = self._ext_for(data)
            if ext == "bin":
                return
            # cache_key -> 本地文件名（与 local_names 保持一致的来源）
            stem = key
            for prefix in ("card_", "jkt_"):
                if stem.startswith(prefix):
                    stem = stem[len(prefix):]
                    break
            dest = assets_dir / subdir / f"{stem}.{ext}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix('.tmp')
            tmp.write_bytes(data)
            tmp.replace(dest)
        except Exception as e:
            logger.warning(f"⚠️ 写入预置资源失败 {key}: {e}")

    @staticmethod
    def _ext_for(data: bytes) -> str:
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "webp"
        if data.startswith(bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])):
            return "png"
        if data[:3] == bytes([0xFF, 0xD8, 0xFF]):
            return "jpg"
        return "bin"

    def _find_local_asset(self, name: str) -> str:
        """返回本地已存在的预置资源路径（持久化目录优先，其次插件自带），没有则返回 ''"""
        bases = []
        assets_dir = getattr(self, 'assets_dir', None)
        if assets_dir:
            bases.append(assets_dir)
        plugin_dir = getattr(self, 'plugin_dir', None)
        if plugin_dir:
            bases.append(plugin_dir / "data" / "assets")
        for base in bases:
            for ext in ("png", "webp", "jpg", "jpeg"):
                path = base / f"{name}.{ext}"
                try:
                    if path.is_file() and path.stat().st_size > 1024:
                        return str(path)
                except Exception:
                    continue
        return ""

    async def _fetch_image_async(self, url: str, cache_key: str, ttl_hours: int = 168,
                                 fallback_urls=(), local_names=(), target_subdir=None):
        """在 executor 里执行下载，避免阻塞事件循环（否则整个机器人会被卡住）"""
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, self._fetch_image, url, cache_key, ttl_hours,
            fallback_urls, local_names, target_subdir
        )

    def _card_asset_urls(self, asset_id: int, kind: str):
        """返回 (主源URL, 备选源URL列表)，主源来自 ASSET_MIRRORS 第一项"""
        urls = [m["base"] + m["card"].format(id=asset_id, kind=kind) for m in ASSET_MIRRORS]
        return urls[0], urls[1:]

    def _card_local_names(self, asset_id: int, kind: str):
        """预置立绘/背景在 data/assets/cards/ 下的候选文件名（不含扩展名）

        两种源的扩展名不同（haneoka 是 png，bdon 是 webp），所以把两种都列出来，
        由 _read_local_asset 挑第一个真实存在的。
        """
        return [f"cards/{asset_id}_{kind}"]

    def _read_local_asset(self, name: str) -> bytes:
        """读本地预置图片，读不到返回 b''

        查找顺序：plugin_data/assets/（首次运行自动下载落地的位置）优先，
        插件自带 data/assets/ 兜底。两者都没有才走云端。

        截断的文件也能通过魔数检查（PNG 头是完整的），所以必须让 Pillow 真正
        解码一遍；只查头会把半张图当成好图返回，图层照样空白。

        name 可以带扩展名（"cards/30_member_character.png"），也可以不带
        （"cards/30_member_character"），后者会依次试常见图片扩展名。
        """
        if not name:
            return b""
        # 两个目录都可能没初始化（例如只构造了部分属性的测试实例），逐个兜着
        bases = []
        assets_dir = getattr(self, 'assets_dir', None)
        if assets_dir is not None:
            bases.append(assets_dir)
        plugin_dir = getattr(self, 'plugin_dir', None)
        if plugin_dir is not None:
            bases.append(plugin_dir / "data" / "assets")
        if not bases:
            return b""
        known = ("png", "webp", "jpg", "jpeg", "gif")
        if name.lower().endswith(tuple(f".{e}" for e in known)):
            candidates = [base / name for base in bases]
        else:
            candidates = []
            for base in bases:
                candidates.extend(base / f"{name}.{e}" for e in known)
        for path in candidates:
            try:
                if not path.is_file():
                    continue
                data = path.read_bytes()
                if not self._looks_like_image(data):
                    logger.warning(f"🧹 预置图片不是有效图片，已忽略 {path.name}")
                    continue
                if not self._decodes(data):
                    logger.warning(f"🧹 预置图片已截断，已忽略 {path.name}")
                    continue
                return data
            except Exception:
                continue
        return b""

    def _jacket_urls(self, jacket_asset: str):
        """返回 (主源URL, 备选源URL列表)"""
        urls = [m["base"] + m["jacket"].format(name=jacket_asset) for m in ASSET_MIRRORS]
        return urls[0], urls[1:]

    def _jacket_local_names(self, jacket_asset: str):
        """预置曲绘在 data/assets/jackets/ 下的候选文件名（不含扩展名）"""
        return [f"jackets/{jacket_asset}"]

    def _card_asset_url(self, asset_id: int, kind: str, ext: str = "png") -> str:
        """仅主源 URL（兼容旧调用方），带备选源请用 _card_asset_urls"""
        return ASSET_MIRRORS[0]["base"] + ASSET_MIRRORS[0]["card"].format(
            id=asset_id, kind=kind)

    def _jacket_url(self, jacket_asset: str, ext: str = "png") -> str:
        """仅主源 URL（兼容旧调用方），带备选源请用 _jacket_urls"""
        return ASSET_MIRRORS[0]["base"] + ASSET_MIRRORS[0]["jacket"].format(
            name=jacket_asset)

    def _image_to_base64(self, img_data: bytes, max_size: int = 800, quality: int = 75,
                         fmt: str = "WEBP", cache_key: str = None) -> str:
        """将图片数据压缩后转为 base64 data URI

        传入 cache_key 时会把编码结果落到磁盘，下次同一张图直接读字符串，
        省掉一次解码 + 重编码（约 0.3s/张）。

        解码失败（多半是下载只拿到半张图）时直接抛异常，绝不做"用原始数据兜底"：
        坏图一旦写进 encoded 缓存就会长期生效，图层会一直空着。
        """
        if cache_key:
            cached = self._read_encoded_cache(cache_key)
            if cached:
                return cached

        # load() 会让 Pillow 真正解码全部像素；截断的图在这一步就会报错。
        # 只调 Image.open 不 load 的话，截断图会被当成合法图继续往下走。
        img = Image.open(io.BytesIO(img_data))
        img.load()
        w, h = img.size
        if max(w, h) > max_size:
            ratio = max_size / max(w, h)
            new_w, new_h = max(1, int(w * ratio)), max(1, int(h * ratio))
            img = img.resize((new_w, new_h), Image.LANCZOS)
        if fmt == "WEBP":
            mime, save_fmt = "image/webp", "WEBP"
        else:
            mime, save_fmt = "image/jpeg", "JPEG"
        if save_fmt == "JPEG" and img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        # optimize=True 在多字节图片上收益有限但耗时明显，这里按体积取舍
        img.save(buf, format=save_fmt, quality=quality)
        encoded = buf.getvalue()
        # 再解码一次确认输出完整，防止 Pillow 对某些截断图静默产出半张
        Image.open(io.BytesIO(encoded)).load()
        result = f"data:{mime};base64,{base64.b64encode(encoded).decode('utf-8')}"

        if cache_key and result:
            self._write_encoded_cache(cache_key, result)
        return result

    def _read_encoded_cache(self, cache_key: str) -> str:
        """读已编码好的 data URI（存在缓存目录，与原始图片分开存放）

        读到的内容也必须是完整 data URI，否则视为无效，让调用方重新下载。
        """
        try:
            path = self.cache_dir / "encoded" / f"{cache_key}.txt"
            if path.exists():
                value = path.read_text(encoding='utf-8')
                if value.startswith("data:image/") and ";base64," in value:
                    return value
                logger.warning(f"🧹 编码缓存无效，重新下载 {cache_key}")
        except Exception:
            pass
        return ""

    def _write_encoded_cache(self, cache_key: str, value: str):
        try:
            d = self.cache_dir / "encoded"
            d.mkdir(parents=True, exist_ok=True)
            tmp = d / f"{cache_key}.tmp"
            tmp.write_text(value, encoding='utf-8')
            tmp.replace(d / f"{cache_key}.txt")
        except Exception:
            pass

    async def _get_card_images(self, asset_id: int):
        """下载指定卡面的立绘与背景（带本地缓存），返回 base64 data URI

        立绘与背景并行下载；单个失败只让对应图层留空，不影响其余部分渲染。
        整体加一层时间上限，避免 CDN 抽风时把签到流程拖住。

        超时那一刻已经拿到的图层会被保留：一张卡只有一半预置时，
        不该让缺的那半边把已经到手的预置图也一起拖没。
        """
        import asyncio

        async def _one(kind: str, max_size: int, quality: int):
            key = f"card_{asset_id}_{kind}_e{max_size}q{quality}"
            # 已编码过就直接返回，连下载都省掉
            cached = self._read_encoded_cache(key)
            if cached:
                return cached
            try:
                url, fallbacks = self._card_asset_urls(asset_id, kind)
                data = await self._fetch_image_async(
                    url, f"card_{asset_id}_{kind}", fallback_urls=fallbacks,
                    local_names=self._card_local_names(asset_id, kind),
                    target_subdir="cards")
                return self._image_to_base64(data, max_size=max_size, quality=quality,
                                             fmt="WEBP", cache_key=key)
            except Exception as e:
                logger.warning(f"⚠️ 下载{kind}失败 card={asset_id}: {e}")
                return ""

        # 单张已有 download_timeout，这里再给整体一个略宽的上限兜底。
        # 4 张图 2 并发，每张最多 2 个源 x 2 次尝试：预算 = 超时 * 4 * 2 轮 + 余量
        budget = self.download_timeout * 8 + 10
        tasks = [
            asyncio.ensure_future(_one("member_character", 1440, 80)),
            asyncio.ensure_future(_one("member_background", 1440, 80)),
        ]
        try:
            return await asyncio.wait_for(asyncio.gather(*tasks), timeout=budget)
        except asyncio.TimeoutError:
            logger.warning(f"⚠️ 卡面 {asset_id} 下载超时（{budget:.0f}s），本次跳过未完成的图层")
            # gather 被取消时已完成的任务仍有结果，取出来免得白跑一趟
            results = []
            for t in tasks:
                if t.done() and not t.cancelled() and t.exception() is None:
                    results.append(t.result())
                else:
                    results.append("")
            return tuple(results)

    async def prefetch_signin_assets(self, asset_id: int, jackets):
        """一次性并发拉取签到需要的全部图片：立绘 + 背景 + 两首歌曲封面

        分开串行请求会各自付一次 TLS 握手；合成一批并发能把 4 张图的
        总耗时从约 1.0s 压到约 0.45s。每张图自带备选源，单个源抽风时不影响其他图。
        """
        import asyncio

        async def _card(kind: str):
            key = f"card_{asset_id}_{kind}_e1440q80"
            cached = self._read_encoded_cache(key)
            if cached:
                return cached
            try:
                url, fallbacks = self._card_asset_urls(asset_id, kind)
                data = await self._fetch_image_async(
                    url, f"card_{asset_id}_{kind}", fallback_urls=fallbacks,
                    local_names=self._card_local_names(asset_id, kind),
                    target_subdir="cards")
                return self._image_to_base64(data, max_size=1440, quality=80,
                                             fmt="WEBP", cache_key=key)
            except Exception as e:
                logger.warning(f"⚠️ 下载{kind}失败 card={asset_id}: {e}")
                return ""

        async def _cover(jacket: str):
            if not jacket:
                return ""
            key = f"jkt_{jacket}_e200q60"
            cached = self._read_encoded_cache(key)
            if cached:
                return cached
            try:
                url, fallbacks = self._jacket_urls(jacket)
                data = await self._fetch_image_async(
                    url, f"jkt_{jacket}", fallback_urls=fallbacks,
                    local_names=self._jacket_local_names(jacket),
                    target_subdir="jackets")
                return self._image_to_base64(data, max_size=200, quality=60,
                                             fmt="WEBP", cache_key=key)
            except Exception as e:
                logger.warning(f"⚠️ 下载歌曲封面失败 {jacket}: {e}")
                return ""

        jackets = list(jackets or [])[:2]
        while len(jackets) < 2:
            jackets.append("")

        # 4 张图 2 并发，每张最多 2 个源 x 2 次尝试：预算 = 超时 * 4 * 2 轮 + 余量
        budget = self.download_timeout * 8 + 10
        tasks = [
            asyncio.ensure_future(_card("member_character")),
            asyncio.ensure_future(_card("member_background")),
            asyncio.ensure_future(_cover(jackets[0])),
            asyncio.ensure_future(_cover(jackets[1])),
        ]
        try:
            return await asyncio.wait_for(asyncio.gather(*tasks), timeout=budget)
        except asyncio.TimeoutError:
            logger.warning(f"⚠️ 签到图片批量下载超时（{budget:.0f}s），保留已完成的图层")
            # 预置图是毫秒级回来的，云端那几张超时时不能把它们一起丢掉
            results = []
            for t in tasks:
                if t.done() and not t.cancelled() and t.exception() is None:
                    results.append(t.result())
                else:
                    results.append("")
            return tuple(results)

    async def _get_song_cover(self, jacket_asset: str) -> str:
        """下载歌曲封面（带本地缓存 + 已编码缓存）并转为 base64"""
        if not jacket_asset:
            return ""
        key = f"jkt_{jacket_asset}_e200q60"
        cached = self._read_encoded_cache(key)
        if cached:
            return cached
        try:
            url, fallbacks = self._jacket_urls(jacket_asset)
            data = await self._fetch_image_async(
                url, f"jkt_{jacket_asset}", fallback_urls=fallbacks,
                local_names=self._jacket_local_names(jacket_asset),
                target_subdir="jackets")
            return self._image_to_base64(data, max_size=200, quality=60, fmt="WEBP", cache_key=key)
        except Exception as e:
            logger.warning(f"⚠️ 下载歌曲封面失败 {jacket_asset}: {e}")
            return ""

    # ------------------------------------------------------------------ #
    # 角色元数据 / 别名解析
    # ------------------------------------------------------------------ #

    def _load_character_metadata(self):
        """加载角色元数据，构建名称/别名到角色ID的映射"""
        self.character_name_map = {}  # name -> character_id
        self.character_alias_map = {}  # alias -> character_id

        chars_file = self.plugin_dir / "characters.json"
        if not chars_file.exists():
            logger.warning("⚠️ 未找到 characters.json，别名搜索不可用")
            return

        try:
            with open(chars_file, 'r', encoding='utf-8') as f:
                characters = json.load(f)

            for char in characters:
                cid = char.get('characterId')
                name = (char.get('name') or '').lower().strip()
                full_name = (char.get('fullNameChinese') or '').lower().strip()
                full_name_ja = (char.get('fullName') or '').lower().strip()
                full_name_en = (char.get('fullNameEnglish') or '').lower().strip()
                aliases = [a.lower().strip() for a in char.get('aliases', [])]

                if name:
                    self.character_name_map[name] = cid
                if full_name:
                    self.character_name_map[full_name] = cid
                if full_name_ja:
                    self.character_name_map[full_name_ja] = cid
                if full_name_en:
                    self.character_name_map[full_name_en] = cid
                for alias in aliases:
                    if alias:
                        self.character_alias_map[alias] = cid

            logger.info(f"✅ 已加载 {len(self.character_name_map)} 个角色名称，{len(self.character_alias_map)} 个别名")
        except Exception as e:
            logger.error(f"❌ 加载 characters.json 失败: {e}")

    def _resolve_character(self, query: str):
        """根据用户输入的名称或别名解析出角色ID"""
        query = (query or '').lower().strip()
        if not query:
            return None

        if query in self.character_name_map:
            return self.character_name_map[query]

        if query in self.character_alias_map:
            return self.character_alias_map[query]

        for alias, cid in self.character_alias_map.items():
            if alias.startswith(query) or query in alias:
                return cid

        for name, cid in self.character_name_map.items():
            if name.startswith(query) or query in name:
                return cid

        return None

    # ------------------------------------------------------------------ #
    # 兼容旧接口
    # ------------------------------------------------------------------ #

    def _sync_plugin_data(self):
        """旧版插件目录数据同步（OurNotes 版已改为云端下载，无需本地 img/song 资源）"""
        return

    def _load_images_base64(self):
        """旧版本地图片加载（OurNotes 版已改为云端下载）"""
        return

    def _load_decorations(self):
        """加载宜/忌图标（沿用插件内置资源，缺失时留空由前端隐藏）"""
        self.decorations = {'ok': '', 'yi': '', 'ji': ''}
        icon_dir = self.plugin_dir / "data" / "icons"
        mapping = {'ok': 'ok.png', 'yi': 'yi.png', 'ji': 'ji.png'}
        for key, filename in mapping.items():
            path = icon_dir / filename
            if not path.exists():
                continue
            try:
                with open(path, 'rb') as f:
                    self.decorations[key] = self._image_to_base64(f.read(), max_size=300, quality=60, fmt="WEBP")
            except Exception as e:
                logger.warning(f"⚠️ 加载图标失败 {filename}: {e}")

    def _load_stickers(self):
        """加载角色贴纸（BDON 表情包），按角色分组

        每个角色的贴纸数量可能不同（2 或 3 张），抽取时保证宜/忌两张不相同；
        有 3 张时三个位置（成功图标 / 宜 / 忌）也互不相同。
        """
        self.stickers = {}          # characterId -> [data_uri, ...]
        self.sticker_names = {}     # characterId -> [贴纸描述, ...]
        manifest_path = self.plugin_dir / "data" / "stickers.json"
        if not manifest_path.exists():
            logger.info("ℹ️ 未找到角色贴纸 data/stickers.json，宜/忌将使用默认图标")
            return

        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                manifest = json.load(f)
        except Exception as e:
            logger.warning(f"⚠️ 读取贴纸清单失败: {e}")
            return

        sticker_dir = self.plugin_dir / "data" / "stickers"
        loaded = 0
        for cid, entry in manifest.items():
            uris = []
            names = []
            for st in entry.get('stickers', []):
                path = sticker_dir / st.get('file', '')
                if not path.exists():
                    continue
                try:
                    with open(path, 'rb') as f:
                        uris.append(self._image_to_base64(
                            f.read(), max_size=320, quality=72, fmt="WEBP",
                            cache_key=f"sticker_{st['file']}"))
                    names.append(st.get('name', ''))
                except Exception as e:
                    logger.warning(f"⚠️ 加载贴纸失败 {st.get('file')}: {e}")
            if uris:
                self.stickers[int(cid)] = uris
                self.sticker_names[int(cid)] = names
                loaded += len(uris)

        logger.info(f"✅ 已加载角色贴纸：{len(self.stickers)} 个角色，共 {loaded} 张")

    def _pick_stickers(self, character_id):
        """为某个角色抽取互不相同的贴纸：返回 (成功图标, 宜, 忌)

        规则：
          - 有 3 张及以上：三个位置各取一张，互不相同
          - 只有 2 张：宜与忌必须不同，成功图标从中任选（可与宜/忌相同）
          - 只有 1 张：三个位置共用同一张
        没有贴纸时返回空串，由前端隐藏对应图层。
        """
        pool = self.stickers.get(character_id) or []
        if not pool:
            return "", "", ""

        if len(pool) == 1:
            return pool[0], pool[0], pool[0]

        if len(pool) == 2:
            yi, ji = _secure_random_choice(pool), None
            # 保证宜/忌不同
            others = [s for s in pool if s != yi]
            ji = _secure_random_choice(others) if others else yi
            ok = _secure_random_choice(pool)
            return ok, yi, ji

        # 3 张及以上：三个位置都不重复
        shuffled = list(pool)
        # Fisher-Yates，用加密级随机
        for i in range(len(shuffled) - 1, 0, -1):
            j = secrets.randbelow(i + 1)
            shuffled[i], shuffled[j] = shuffled[j], shuffled[i]
        return shuffled[0], shuffled[1], shuffled[2]

    # ------------------------------------------------------------------ #
    # 配置与数据文件
    # ------------------------------------------------------------------ #

    def _ensure_data_file(self):
        if not self.data_file.exists():
            with open(self.data_file, 'w', encoding='utf-8') as f:
                json.dump({}, f)

    def _load_custom_config(self):
        try:
            if self.config_file.exists():
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    self.custom_config = json.load(f)
                logger.info("✅ 成功加载自定义配置文件")
            else:
                self.custom_config = {
                    "daily_quotes": ["每一天都是新的开始，加油！"],
                    "song_descriptions": {}
                }
                logger.warning("⚠️ 未找到自定义配置文件，使用默认配置")
        except Exception as e:
            logger.error(f"加载自定义配置文件失败: {e}")
            self.custom_config = {"daily_quotes": ["每一天都是新的开始，加油！"], "song_descriptions": {}}

    def _load_data(self):
        try:
            if not self.data_file.exists():
                return {}
            with open(self.data_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"加载签到数据失败: {e}")
            return {}

    def _get_context_id(self, event):
        """只使用用户ID作为上下文标识，不再区分群聊/私聊"""
        try:
            user_id = event.get_sender_id()
            if user_id:
                return f"user_{user_id}"
            return "default_ctx"
        except Exception as e:
            logger.warning(f"获取上下文ID失败: {e}")
            return "default_ctx"

    # ------------------------------------------------------------------ #
    # 随机抽取
    # ------------------------------------------------------------------ #

    def _select_random_card(self, character_id=None):
        """从 SR+/生日卡面中随机选择一张；指定角色时只从该角色的卡面中选"""
        if character_id is not None:
            pool = self._cards_by_character.get(character_id, [])
            if not pool:
                return None
            return _secure_random_choice(pool)
        if not self.cards:
            return None
        return _secure_random_choice(self.cards)

    async def _get_random_song(self):
        """使用难度相似性算法选择两首歌曲（封面并行下载）"""
        if not self.songs:
            return [
                {"name": "暂无歌曲", "name_cn": "暂无歌曲", "cover_base64": "",
                 "difficulty_type": None, "difficulty_level": None, "difficulty_label": None},
                {"name": "暂无歌曲", "name_cn": "暂无歌曲", "cover_base64": "",
                 "difficulty_type": None, "difficulty_level": None, "difficulty_label": None}
            ]

        selected_songs = select_song_with_difficulty(self.songs)

        if len(selected_songs) < 2:
            selected_songs = selected_songs + [selected_songs[0] if selected_songs else None] * (2 - len(selected_songs))

        import asyncio
        picks = selected_songs[:2]
        covers = await asyncio.gather(*(
            self._get_song_cover(s.get('jacket', '')) if s else _empty_cover()
            for s in picks
        ))

        result = []
        for song, cover_base64 in zip(picks, covers):
            if song is None:
                result.append({"name": "暂无歌曲", "name_cn": "暂无歌曲", "cover_base64": "",
                               "difficulty_type": None, "difficulty_level": None, "difficulty_label": None})
                continue

            title = song.get('title') or song.get('title_ja') or '未知歌曲'
            artist = song.get('artist') or song.get('composer') or ''

            result.append({
                "name": artist,
                "name_cn": title,
                "id": song.get('id'),
                "cover_base64": cover_base64,
                "difficulty_type": song.get('difficulty_type'),
                "difficulty_level": _format_level(song.get('difficulty_level')),
                "difficulty_label": song.get('difficulty_label')
            })

        while len(result) < 2:
            result.append({"name": "", "name_cn": "暂无歌曲", "cover_base64": "",
                           "difficulty_type": None, "difficulty_level": None, "difficulty_label": None})

        return result

    # ------------------------------------------------------------------ #
    # 运势
    # ------------------------------------------------------------------ #

    def _generate_fortune(self) -> dict:
        fortune_data = [
            { 'level': '大吉', 'desc': '最最顺!😎', 'color': 'good', 'weight': 5 },
            { 'level': '吉', 'desc': '幸福像🎈一样飘起来', 'color': 'good', 'weight': 10 },
            { 'level': '中吉', 'desc': '努力就发光😋', 'color': 'good', 'weight': 15 },
            { 'level': '小吉', 'desc': '小小的幸运😆', 'color': 'good', 'weight': 20 },
            { 'level': '半吉', 'desc': '明天会更开心哦😍', 'color': 'neutral', 'weight': 15 },
            { 'level': '末吉', 'desc': '转机就在眼前😉', 'color': 'neutral', 'weight': 10 },
            { 'level': '末小吉', 'desc': '再加把劲哦💪🏻', 'color': 'neutral', 'weight': 10 },
            { 'level': '凶', 'desc': '困难是成长的礼物🎁', 'color': 'bad', 'weight': 8 },
            { 'level': '小凶', 'desc': '小心点就好哦❤️', 'color': 'bad', 'weight': 7 },
            { 'level': '半凶', 'desc': '要小心一点哦..', 'color': 'bad', 'weight': 5 },
            { 'level': '末凶', 'desc': '要小心一点哦🥺', 'color': 'bad', 'weight': 3 },
            { 'level': '大凶', 'desc': '要小心一点哦🥺', 'color': 'bad', 'weight': 2 }
        ]
        return self._weighted_random(fortune_data)

    def _weighted_random(self, items) -> dict:
        total_weight = sum(item['weight'] for item in items)
        random_val = random.random() * total_weight
        current_weight = 0
        for item in items:
            current_weight += item['weight']
            if random_val <= current_weight:
                return item
        return items[-1]

    def _get_yi_activity(self) -> str:
        good_fortunes = [
            '追新番', '画同人', '做手书', '参加漫展', '写同人小说', '学习声优', '打游戏', '社区互助',
            '做攻略', '参加社团', '学习绘画', '参加同人展', '分享资源', '关注公益', '练习日语', '整理收藏',
            '看动漫', '听OST', '收集周边', '研究设定', 'cosplay', '配音练习', '剪辑AMV',
            '早起锻炼', '阅读好书', '整理房间', '学习新技能', '写日记', '拍照记录', '打歌',
            '冲榜', '陪伴家人', '户外散步', '听音乐', '练习乐器', '烘焙甜点', '种植花草',
            '练习书法', '拼图游戏', '整理相册', '尝试新餐厅',
            '给好友写信', '制定计划', '跑步运动',
            '参观博物馆', '逛书店', '手工DIY', '整理衣橱', '学习摄影',
            '看演唱会', '参加读书会', '整理笔记',
            '做志愿者', '学习舞蹈', '玩桌游',
            '学习烹饪', '品咖啡', '逛花市', '看日出',
            '整理音乐列表',
            '写短篇小说', '学习插花', '制作手账'
        ]
        return random.choice(good_fortunes)

    def _get_ji_activity(self) -> str:
        bad_fortunes = [
            '沉迷抽卡', '挖坑不填', '刷无用番', '熬夜追更', '沉迷弹幕', '过度氪金', '网暴同好', '弃坑不交', '沉迷游戏',
            '刷无意义弹幕', '过度磕CP', '沉迷虚拟恋爱', '盗版成瘾', '无节制追星', '忽视健康', '网络争吵', '囤积周边',
            '毒唯行为', '引战', '剧透未看番', '滥用表情包', '刷存在感', '跟风黑角色'
        ]
        return random.choice(bad_fortunes)

    # ------------------------------------------------------------------ #
    # 指令处理
    # ------------------------------------------------------------------ #

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE | filter.EventMessageType.PRIVATE_MESSAGE)
    async def set_nickname_handler(self, event: AstrMessageEvent):
        """设置签到昵称指令"""
        message_text = event.message_str.strip()
        if not message_text.startswith("设置签到昵称"):
            return

        nickname = message_text[len("设置签到昵称"):].strip()
        if not nickname:
            yield event.plain_result("⚠️ 请提供昵称，用法：设置签到昵称 你的昵称")
            return

        user_id = event.get_sender_id()
        if not user_id:
            yield event.plain_result("⚠️ 无法获取用户ID，设置失败")
            return

        ctx_id = self._get_context_id(event)
        try:
            if self.db.set_nickname(ctx_id, user_id, nickname):
                yield event.plain_result(f"✅ 签到昵称已设置为：{nickname}")
            else:
                yield event.plain_result("⚠️ 设置失败，请稍后重试")
        except Exception as e:
            logger.error(f"❌ 设置签到昵称失败: {e}")
            yield event.plain_result("⚠️ 设置失败，请稍后重试")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE | filter.EventMessageType.PRIVATE_MESSAGE)
    async def sync_masterdata_handler(self, event: AstrMessageEvent):
        """手动同步 masterdata 指令"""
        message_text = event.message_str.strip()
        if message_text not in ("同步卡面数据", "同步ournotes数据", "同步数据"):
            return

        yield event.plain_result("🔄 正在同步 OurNotes master 数据（繁中服），请稍候...")
        try:
            ok = await self._run_sync_in_thread(force=True)
            if ok:
                yield event.plain_result(f"✅ 同步完成：{len(self.songs)} 首歌曲，{len(self.cards)} 张卡面")
            else:
                yield event.plain_result("⚠️ 同步失败，请查看日志（将继续使用现有数据）")
        except Exception as e:
            logger.error(f"❌ 手动同步失败: {e}")
            yield event.plain_result(f"⚠️ 同步失败: {e}")

    async def _run_sync_in_thread(self, force: bool = False) -> bool:
        """在线程池中执行同步，避免阻塞事件循环"""
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._sync_masterdata, force)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE | filter.EventMessageType.PRIVATE_MESSAGE)
    async def listen_signin(self, event: AstrMessageEvent):
        message_text = event.message_str.strip()
        sign_in_cmd = self.config.get('sign_in_command', '签到')

        is_test = message_text == "测试签到"
        char_query = ""

        if not is_test:
            if not message_text.startswith(sign_in_cmd):
                return
            char_query = message_text[len(sign_in_cmd):].strip()

        specified_character_id = None
        if char_query:
            specified_character_id = self._resolve_character(char_query)
            if specified_character_id is not None:
                logger.info(f"🎯 用户指定角色: {char_query} -> characterId={specified_character_id}")
            else:
                logger.warning(f"⚠️ 未找到角色: {char_query}，静默忽略")
                return

        user_id = event.get_sender_id()
        user_name = event.get_sender_name()
        ctx_id = self._get_context_id(event)

        if not user_id:
            logger.warning(f"无法获取用户ID，跳过签到")
            return

        custom_nickname = ""
        try:
            custom_nickname = self.db.get_nickname(ctx_id, user_id)
        except Exception as e:
            logger.warning(f"获取自定义昵称失败: {e}")

        nickname_missing = False
        if custom_nickname:
            display_name = custom_nickname
        elif user_name:
            display_name = user_name
        else:
            display_name = ""
            nickname_missing = True
            logger.warning(f"获取用户昵称失败: user_id={user_id}")

        user_name = display_name

        streak_days = 1
        signed_dates = []

        if not is_test:
            try:
                self.db.maybe_cleanup(self.history_keep_days)
            except Exception as e:
                logger.warning(f"⚠️ 触发每日清理失败: {e}")

            try:
                self.db.record_checkin(ctx_id, user_id, user_name)
            except Exception as e:
                logger.error(f"❌ 数据库记录签到失败: {e}")

            logger.info(f"用户 {user_name}({user_id}) 在 {ctx_id} 签到成功")
        else:
            logger.info(f"用户 {user_name}({user_id}) 在 {ctx_id} 测试签到（不记录，不使用持久化状态）")

        if not is_test:
            try:
                user_stats = self.db.get_user_stats(ctx_id, user_id)
                streak_days = user_stats.get('streak', 0)
            except Exception as e:
                logger.warning(f"从数据库获取连续签到天数失败: {e}")
                streak_days = 1

            try:
                signed_dates = user_stats.get('signed_dates', [])
            except Exception as e:
                logger.warning(f"获取签到日期失败: {e}")
                signed_dates = []

        existing_daily_state = None
        if not is_test:
            existing_daily_state = self.db.get_daily_state(ctx_id, user_id)

        fortune_data = None
        songs = None
        card = None
        yi_text = ""
        ji_text = ""

        if existing_daily_state:
            fortune_data = {
                'level': existing_daily_state.get('fortune_level', ''),
                'desc': existing_daily_state.get('fortune_desc', ''),
                'color': existing_daily_state.get('fortune_color', 'neutral')
            }
            yi_text = existing_daily_state.get('yi_activity', '') or self._get_yi_activity()
            ji_text = existing_daily_state.get('ji_activity', '') or self._get_ji_activity()
            songs = [
                {
                    "name": existing_daily_state.get('song1_name', ''),
                    "name_cn": existing_daily_state.get('song1_name_cn', ''),
                    "cover_base64": existing_daily_state.get('song1_cover_base64', ''),
                    "difficulty_type": existing_daily_state.get('song1_difficulty_type', ''),
                    "difficulty_level": existing_daily_state.get('song1_difficulty_level', 0),
                    "difficulty_label": existing_daily_state.get('song1_difficulty_label', '')
                },
                {
                    "name": existing_daily_state.get('song2_name', ''),
                    "name_cn": existing_daily_state.get('song2_name_cn', ''),
                    "cover_base64": existing_daily_state.get('song2_cover_base64', ''),
                    "difficulty_type": existing_daily_state.get('song2_difficulty_type', ''),
                    "difficulty_level": existing_daily_state.get('song2_difficulty_level', 0),
                    "difficulty_label": existing_daily_state.get('song2_difficulty_label', '')
                }
            ]
            # 卡面（角色、立绘、背景）与贴纸每次重新抽：同一天重复签到也能换个角色看，
            # 贴纸始终跟随新抽到的角色，宜/忌依旧不同。
            # 指定了角色时服从指令（签到 <角色>），否则全库随机。
            card = self._select_random_card(specified_character_id)
            if card is None and specified_character_id is not None:
                logger.warning(f"⚠️ 角色 {specified_character_id} 暂无可用卡面，随机选择")
                card = self._select_random_card()
            if card is None:
                # 重新抽失败时退回存档里的卡面，至少保证能出图
                card = {
                    'asset_id': existing_daily_state.get('card_asset_id', 0),
                    'rarity_name': existing_daily_state.get('card_rarity_name', ''),
                    'character_name': existing_daily_state.get('card_character_name', ''),
                    'subtitle': existing_daily_state.get('card_subtitle', ''),
                }
            logger.info(f"📦 使用已保存的每日运势: {user_name}({user_id})，卡面与贴纸重新抽取")
        else:
            fortune_data = self._generate_fortune()
            yi_text = self._get_yi_activity()
            ji_text = self._get_ji_activity()

            # 先选歌和卡面（纯内存操作，不联网），再一次性并发拉全部图片
            try:
                songs = select_song_with_difficulty(self.songs)
                if len(songs) < 2:
                    songs = songs + [songs[0] if songs else None] * (2 - len(songs))
                songs = songs[:2]
                for s in songs:
                    if s:
                        s['difficulty_level'] = _format_level(s.get('difficulty_level'))
                        s['name_cn'] = s.get('title') or s.get('title_ja') or '未知歌曲'
                        s['name'] = s.get('artist') or s.get('composer') or ''
                        s['cover_base64'] = ''
            except Exception as e:
                logger.warning(f"获取推荐歌曲失败: {e}")
                songs = None

            card = self._select_random_card(specified_character_id)
            if card is None and specified_character_id is not None:
                logger.warning(f"⚠️ 角色 {specified_character_id} 暂无可用卡面，随机选择")
                card = self._select_random_card()

        if card is None:
            logger.error("❌ 无法选择有效卡面，请检查数据同步状态")
            yield event.plain_result("⚠️ 签到失败：卡面数据加载异常，请联系管理员")
            return

        # 立绘 / 背景 / 两首封面合成一批并发下载
        if songs:
            char_b64, bg_b64, songs[0]['cover_base64'], songs[1]['cover_base64'] = \
                await self.prefetch_signin_assets(
                    card['asset_id'],
                    [s.get('jacket', '') if s else '' for s in songs[:2]],
                )
        else:
            char_b64, bg_b64 = await self._get_card_images(card['asset_id'])

        # 角色贴纸：成功图标 / 宜 / 忌，从该角色自己的贴纸里抽，宜忌保证不同。
        # 卡面每次重新抽，贴纸也跟着新角色抽，不会出现"别的角色的贴纸"。
        # 必须在这里取，下面写存档和渲染都要用。
        st_ok, st_yi, st_ji = self._pick_stickers(card.get('character_id'))

        if not is_test and not existing_daily_state:
            state_data = {
                'fortune_level': fortune_data.get('level', '') if fortune_data else '',
                'fortune_desc': fortune_data.get('desc', '') if fortune_data else '',
                'fortune_color': fortune_data.get('color', 'neutral') if fortune_data else 'neutral',
                'yi_activity': yi_text,
                'ji_activity': ji_text,
                'card_asset_id': card.get('asset_id', 0),
                'card_rarity_name': card.get('rarity_name', ''),
                'card_character_name': card.get('character_name', ''),
                'card_subtitle': card.get('subtitle', ''),
                'sticker_ok': st_ok,
                'sticker_yi': st_yi,
                'sticker_ji': st_ji,
            }
            if songs and len(songs) > 0:
                state_data.update({
                    'song1_name': songs[0].get('name', ''),
                    'song1_name_cn': songs[0].get('name_cn', ''),
                    'song1_cover_base64': songs[0].get('cover_base64', ''),
                    'song1_difficulty_type': songs[0].get('difficulty_type', ''),
                    'song1_difficulty_level': songs[0].get('difficulty_level', 0),
                    'song1_difficulty_label': songs[0].get('difficulty_label', ''),
                })
            if songs and len(songs) > 1:
                state_data.update({
                    'song2_name': songs[1].get('name', ''),
                    'song2_name_cn': songs[1].get('name_cn', ''),
                    'song2_cover_base64': songs[1].get('cover_base64', ''),
                    'song2_difficulty_type': songs[1].get('difficulty_type', ''),
                    'song2_difficulty_level': songs[1].get('difficulty_level', 0),
                    'song2_difficulty_label': songs[1].get('difficulty_label', ''),
                })
            try:
                self.db.save_daily_state(ctx_id, user_id, user_name, state_data)
            except Exception as e:
                logger.error(f"❌ 保存每日签到状态失败: {e}")

        ui_settings = self.config.get('ui_settings', {})

        options = {
            "quality": 30,
            "device_scale_factor_level": "high",
            "full_page": True,
            "omit_background": False,
            "type": "jpeg",
            "viewport": {"width": 800, "height": 720}
        }

        song1 = songs[0] if songs and len(songs) >= 1 else {"name": "", "name_cn": "名称：未知歌曲", "cover_base64": "", "difficulty_type": None, "difficulty_level": None, "difficulty_label": None}
        song2 = songs[1] if songs and len(songs) >= 2 else {"name": "", "name_cn": "名称：未知歌曲", "cover_base64": "", "difficulty_type": None, "difficulty_level": None, "difficulty_label": None}

        rarity_class = {'SR': 'sr', 'SSR': 'ssr', 'BD': 'bd'}.get(card.get('rarity_name', ''), 'sr')

        try:
            render_data = {
                "name": user_name or "用户",
                "base64_font": getattr(self, 'base64_font', ''),
                "b64bg": bg_b64,
                "b64char": char_b64,
                "b64ok": st_ok or self.decorations.get('ok', ''),
                "b64yi": st_yi or self.decorations.get('yi', ''),
                "b64ji": st_ji or self.decorations.get('ji', ''),
                "fortune_level": fortune_data.get('level', '') if fortune_data else '',
                "fortune_desc": fortune_data.get('desc', '') if fortune_data else '',
                "fortune_color": fortune_data.get('color', 'neutral') if fortune_data else 'neutral',
                "yi_text": yi_text,
                "ji_text": ji_text,
                "streak_days": max(1, streak_days),
                "signed_dates": signed_dates or [],
                "song_name": song1.get("name", ""),
                "song_name_cn": song1.get("name_cn", "名称：未知歌曲"),
                "song_cover_base64": song1.get("cover_base64", ""),
                "song_difficulty_type": song1.get("difficulty_type") or "",
                "song_difficulty_level": _format_level(song1.get("difficulty_level")),
                "song_difficulty_label": song1.get("difficulty_label") or "",
                "song2_name": song2.get("name", ""),
                "song2_name_cn": song2.get("name_cn", "名称：未知歌曲"),
                "song2_cover_base64": song2.get("cover_base64", ""),
                "song2_difficulty_type": song2.get("difficulty_type") or "",
                "song2_difficulty_level": _format_level(song2.get("difficulty_level")),
                "song2_difficulty_label": song2.get("difficulty_label") or "",
                "show_song": True,
                "card_rarity_name": card.get('rarity_name', ''),
                "card_rarity_class": rarity_class,
                "card_character_name": card.get('character_name', ''),
                "card_subtitle": card.get('subtitle', '')
            }

            url = await self.html_render(TMPL, render_data, options=options)

            url = self._recompress_image_url(url, target_kb=100)

            yield event.image_result(url)

            if nickname_missing:
                yield event.plain_result(
                    "⚠️ 由于QQ官方机器人的限制，获取用户昵称失败，请使用“设置签到昵称 昵称”指令手动设置签到昵称"
                )
        except Exception as e:
            error_type = type(e).__name__
            logger.error(f"❌ 渲染签到卡片失败 [{error_type}]: {e}")
            import traceback
            logger.error(f"堆栈信息:\n{traceback.format_exc()}")
            yield event.plain_result(
                self._build_text_signin(
                    user_name, fortune_data, yi_text, ji_text,
                    songs, streak_days, signed_dates, card
                )
            )

    def _build_text_signin(self, user_name, fortune_data, yi_text, ji_text,
                           songs, streak_days, signed_dates, card):
        """图片渲染失败时，构建文字版签到内容"""
        lines = []
        lines.append(f"🎉 {user_name}，签到成功！")

        if fortune_data:
            level = fortune_data.get('level', '未知')
            desc = fortune_data.get('desc', '')
            lines.append(f"✨ 今日运势：{level} {desc}")

        if yi_text:
            lines.append(f"✅ 宜：{yi_text}")
        if ji_text:
            lines.append(f"❌ 忌：{ji_text}")

        if card:
            rarity = card.get('rarity_name', '')
            cname = card.get('character_name', '')
            sub = card.get('subtitle', '')
            if cname:
                line = f"🃏 今日卡面：{rarity} {cname}"
                if sub:
                    line += f" 「{sub}」"
                lines.append(line)

        if songs and len(songs) > 0:
            lines.append("🎵 今日歌曲推荐：")
            for i, song in enumerate(songs[:2], 1):
                name_cn = song.get('name_cn', '')
                name = song.get('name', '')
                diff_type = song.get('difficulty_type', '')
                diff_level = song.get('difficulty_level', '')
                diff_label = song.get('difficulty_label', '')
                if name_cn or name:
                    song_line = f"  {i}. {name_cn or name}"
                    if diff_type and diff_level:
                        song_line += f" [{diff_label or diff_type.upper()}{diff_level}]"
                    lines.append(song_line)

        return "\n".join(lines)

    def _recompress_image_url(self, url: str, target_kb: int = 100) -> str:
        """对渲染后的图片进行二次压缩，目标大小为 target_kb KB"""
        try:
            if url.startswith("data:"):
                header, b64data = url.split(",", 1)
                img_bytes = base64.b64decode(b64data)
            elif os.path.exists(url):
                with open(url, 'rb') as f:
                    img_bytes = f.read()
            else:
                return url

            original_kb = len(img_bytes) / 1024
            if original_kb <= target_kb:
                logger.info(f"📦 图片 {original_kb:.1f}KB <= 目标 {target_kb}KB，无需二次压缩")
                return url

            img = Image.open(io.BytesIO(img_bytes))
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGB")

            lo, hi = 10, 85
            result_bytes = img_bytes
            while lo <= hi:
                mid = (lo + hi) // 2
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=mid, optimize=True)
                size_kb = buf.tell() / 1024
                if size_kb <= target_kb:
                    result_bytes = buf.getvalue()
                    lo = mid + 1
                else:
                    hi = mid - 1

            final_kb = len(result_bytes) / 1024
            if final_kb > target_kb:
                scale = 0.9
                for _ in range(5):
                    w, h = img.size
                    new_w, new_h = max(1, int(w * scale)), max(1, int(h * scale))
                    resized = img.resize((new_w, new_h), Image.LANCZOS)
                    buf = io.BytesIO()
                    resized.save(buf, format="JPEG", quality=30, optimize=True)
                    result_bytes = buf.getvalue()
                    final_kb = len(result_bytes) / 1024
                    if final_kb <= target_kb:
                        break
                    scale *= 0.9

            final_kb = len(result_bytes) / 1024
            logger.info(f"📦 二次压缩: {original_kb:.1f}KB -> {final_kb:.1f}KB (目标 {target_kb}KB)")
            return f"data:image/jpeg;base64,{base64.b64encode(result_bytes).decode('utf-8')}"
        except Exception as e:
            logger.warning(f"二次压缩失败，使用原始图片: {e}")
            return url

    async def terminate(self):
        if hasattr(self, 'db'):
            try:
                self.db.close()
                logger.info("✅ SQLite数据库连接已关闭")
            except Exception as e:
                logger.warning(f"关闭数据库连接失败: {e}")
        if getattr(self, '_executor', None):
            try:
                self._executor.shutdown(wait=False, cancel_futures=True)
                logger.info("✅ 图片下载线程池已关闭")
            except Exception as e:
                logger.warning(f"关闭下载线程池失败: {e}")
        logger.info("OurNotes 签到插件已停止")


class SigninDatabase:
    """SQLite 数据库管理器 - 高性能签到数据存储"""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._local = threading.local()
        self._lock = threading.Lock()
        self._init_database()
        logger.info(f"✅ SQLite 数据库初始化完成: {db_path.name}")

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
                timeout=10
            )
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn.execute("PRAGMA cache_size=-32000")
        return self._local.conn

    def _init_database(self):
        with self._get_conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ctx_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    username TEXT DEFAULT '',
                    last_checkin TEXT,
                    total_checkins INTEGER DEFAULT 0,
                    max_streak INTEGER DEFAULT 0,
                    current_streak INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(ctx_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS checkin_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_ref INTEGER NOT NULL,
                    checkin_date TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_ref) REFERENCES users(id),
                    UNIQUE(user_ref, checkin_date)
                );

                CREATE TABLE IF NOT EXISTS daily_signin_state (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_ref INTEGER NOT NULL,
                    signin_date TEXT NOT NULL,
                    fortune_level TEXT,
                    fortune_desc TEXT,
                    fortune_color TEXT,
                    yi_activity TEXT,
                    ji_activity TEXT,
                    card_asset_id INTEGER DEFAULT 0,
                    card_rarity_name TEXT,
                    card_character_name TEXT,
                    card_subtitle TEXT,
                    sticker_ok TEXT,
                    sticker_yi TEXT,
                    sticker_ji TEXT,
                    song1_name TEXT,
                    song1_name_cn TEXT,
                    song1_cover_base64 TEXT,
                    song1_difficulty_type TEXT,
                    song1_difficulty_level INTEGER,
                    song1_difficulty_label TEXT,
                    song2_name TEXT,
                    song2_name_cn TEXT,
                    song2_cover_base64 TEXT,
                    song2_difficulty_type TEXT,
                    song2_difficulty_level INTEGER,
                    song2_difficulty_label TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_ref) REFERENCES users(id),
                    UNIQUE(user_ref, signin_date)
                );

                CREATE INDEX IF NOT EXISTS idx_checkin_date
                    ON checkin_history(checkin_date);

                CREATE INDEX IF NOT EXISTS idx_user_ctx
                    ON users(ctx_id, user_id);

                CREATE INDEX IF NOT EXISTS idx_daily_state_date
                    ON daily_signin_state(user_ref, signin_date);

                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
            """)
            try:
                conn.execute("ALTER TABLE users ADD COLUMN nickname TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            # 旧版（世界计划插件）daily_signin_state 没有卡面列，增量补齐
            for column, ddl in [
                ("card_asset_id", "INTEGER DEFAULT 0"),
                ("card_rarity_name", "TEXT"),
                ("card_character_name", "TEXT"),
                ("card_subtitle", "TEXT"),
                ("sticker_ok", "TEXT"),
                ("sticker_yi", "TEXT"),
                ("sticker_ji", "TEXT"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE daily_signin_state ADD COLUMN {column} {ddl}")
                except sqlite3.OperationalError:
                    pass
            conn.commit()

    def migrate_from_json(self, json_data: dict):
        """把旧版 JSON 签到数据导入数据库。

        迁移后需要重算累计次数与连续天数，否则 users 表里的计数器停留在 0。
        """
        for ctx_id, users in json_data.items():
            for user_id, user_data in users.items():
                if not isinstance(user_data, dict):
                    continue

                username = user_data.get('username', 'Unknown')
                last_checkin = user_data.get('last_checkin')
                history = user_data.get('checkin_history', [])

                try:
                    with self._get_conn() as conn:
                        conn.execute(
                            "INSERT OR IGNORE INTO users (ctx_id, user_id, username, last_checkin) VALUES (?, ?, ?, ?)",
                            (ctx_id, user_id, username, last_checkin)
                        )
                        # INSERT OR IGNORE 命中已存在行时 lastrowid 不可靠，改按 (ctx_id, user_id) 查主键
                        row = conn.execute(
                            "SELECT id FROM users WHERE ctx_id = ? AND user_id = ?",
                            (ctx_id, user_id)
                        ).fetchone()
                        if not row:
                            continue
                        user_ref = row['id']

                        if history and isinstance(history, list):
                            for date_str in history:
                                conn.execute(
                                    "INSERT OR IGNORE INTO checkin_history (user_ref, checkin_date) VALUES (?, ?)",
                                    (user_ref, date_str)
                                )

                        # 用迁移进来的历史重算计数器，保证统计与历史一致
                        total = conn.execute(
                            "SELECT COUNT(*) FROM checkin_history WHERE user_ref = ?",
                            (user_ref,)
                        ).fetchone()[0]
                        conn.execute("""
                            UPDATE users SET
                                total_checkins = MAX(total_checkins, ?),
                                current_streak = ?,
                                max_streak = MAX(max_streak, ?),
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                        """, (total, self._calculate_streak(user_ref),
                              self._calculate_max_streak(user_ref), user_ref))

                        conn.commit()
                except Exception as e:
                    logger.warning(f"迁移数据失败: {user_id} - {e}")

    def get_or_create_user(self, ctx_id: str, user_id: str, username: str = "") -> dict:
        with self._get_conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO users (ctx_id, user_id, username) VALUES (?, ?, ?)",
                (ctx_id, user_id, username)
            )

            row = conn.execute(
                "SELECT * FROM users WHERE ctx_id = ? AND user_id = ?",
                (ctx_id, user_id)
            ).fetchone()

            if row and row['username'] != username:
                conn.execute(
                    "UPDATE users SET username = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (username, row['id'])
                )
                conn.commit()

            return dict(row) if row else {}

    def set_nickname(self, ctx_id: str, user_id: str, nickname: str) -> bool:
        with self._get_conn() as conn:
            user = self.get_or_create_user(ctx_id, user_id)
            if not user:
                return False
            conn.execute(
                "UPDATE users SET nickname = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (nickname, user['id'])
            )
            conn.commit()
            return True

    def get_nickname(self, ctx_id: str, user_id: str) -> str:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT nickname FROM users WHERE ctx_id = ? AND user_id = ?",
                (ctx_id, user_id)
            ).fetchone()
            return row['nickname'] if row and row['nickname'] else ''

    def record_checkin(self, ctx_id: str, user_id: str, username: str) -> bool:
        today = datetime.now().strftime("%Y-%m-%d")

        with self._get_conn() as conn:
            user = self.get_or_create_user(ctx_id, user_id, username)

            if not user:
                return False

            existing = conn.execute(
                "SELECT id FROM checkin_history WHERE user_ref = ? AND checkin_date = ?",
                (user['id'], today)
            ).fetchone()

            if existing:
                return False

            conn.execute(
                "INSERT INTO checkin_history (user_ref, checkin_date) VALUES (?, ?)",
                (user['id'], today)
            )

            streak = self._calculate_streak(user['id'])
            total = (user.get('total_checkins', 0) or 0) + 1
            max_streak = self._calculate_max_streak(user['id'])

            conn.execute("""
                UPDATE users SET
                    last_checkin = ?,
                    total_checkins = ?,
                    current_streak = ?,
                    max_streak = MAX(max_streak, ?),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (today, total, streak, max_streak, user['id']))

            conn.commit()
            logger.info(f"✅ 签到记录已保存到数据库: {username}({user_id}) @ {ctx_id}")
            return True

    def _calculate_streak(self, user_id: int) -> int:
        with self._get_conn() as conn:
            dates = sorted([
                r[0] for r in conn.execute(
                    "SELECT checkin_date FROM checkin_history WHERE user_ref = ? ORDER BY checkin_date DESC LIMIT 365",
                    (user_id,)
                ).fetchall()
            ], reverse=True)

            if not dates:
                return 0

            streak = 0
            today = datetime.now().date()

            for i in range(len(dates)):
                expected = (today - timedelta(days=i)).strftime("%Y-%m-%d")
                if expected in dates:
                    streak += 1
                else:
                    break

            return streak

    def _calculate_max_streak(self, user_id: int) -> int:
        with self._get_conn() as conn:
            dates = sorted([r[0] for r in conn.execute(
                "SELECT checkin_date FROM checkin_history WHERE user_ref = ? ORDER BY checkin_date ASC",
                (user_id,)
            ).fetchall()])

            if not dates:
                return 0

            max_streak = 1
            current_streak = 1

            for i in range(1, len(dates)):
                prev = datetime.strptime(dates[i-1], "%Y-%m-%d").date()
                curr = datetime.strptime(dates[i], "%Y-%m-%d").date()

                if (curr - prev).days == 1:
                    current_streak += 1
                    max_streak = max(max_streak, current_streak)
                else:
                    current_streak = 1

            return max_streak

    def get_user_stats(self, ctx_id: str, user_id: str) -> dict:
        user = self.get_or_create_user(ctx_id, user_id)

        if not user:
            return {
                'streak': 0,
                'total': 0,
                'max_streak': 0,
                'signed_dates': [],
                'is_signed_today': False
            }

        today = datetime.now().strftime("%Y-%m-%d")

        with self._get_conn() as conn:
            is_signed = conn.execute(
                "SELECT id FROM checkin_history WHERE user_ref = ? AND checkin_date = ?",
                (user['id'], today)
            ).fetchone() is not None

            signed_dates = [
                int(r[0].split("-")[2]) for r in conn.execute(
                    "SELECT checkin_date FROM checkin_history WHERE user_ref = ? AND checkin_date LIKE ? ORDER BY checkin_date",
                    (user['id'], f"{datetime.now().strftime('%Y-%m')}%")
                ).fetchall() if r[0]
            ]

            return {
                # record_checkin 已把当日签到计入 current_streak / total_checkins，
                # 这里直接返回持久化值，避免二次 +1 导致连续天数与累计次数虚高
                'streak': user.get('current_streak', 0) or 0,
                'total': user.get('total_checkins', 0) or 0,
                'max_streak': user.get('max_streak', 0) or 0,
                'signed_dates': signed_dates,
                'is_signed_today': is_signed
            }

    def save_daily_state(self, ctx_id: str, user_id: str, username: str, state_data: dict) -> bool:
        today = datetime.now().strftime("%Y-%m-%d")

        with self._get_conn() as conn:
            user = self.get_or_create_user(ctx_id, user_id, username)

            if not user:
                return False

            conn.execute("""
                INSERT OR REPLACE INTO daily_signin_state
                    (user_ref, signin_date, fortune_level, fortune_desc, fortune_color,
                     yi_activity, ji_activity,
                     card_asset_id, card_rarity_name, card_character_name, card_subtitle,
                     sticker_ok, sticker_yi, sticker_ji,
                     song1_name, song1_name_cn, song1_cover_base64,
                     song1_difficulty_type, song1_difficulty_level, song1_difficulty_label,
                     song2_name, song2_name_cn, song2_cover_base64,
                     song2_difficulty_type, song2_difficulty_level, song2_difficulty_label,
                     updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (
                user['id'], today,
                state_data.get('fortune_level', ''),
                state_data.get('fortune_desc', ''),
                state_data.get('fortune_color', 'neutral'),
                state_data.get('yi_activity', ''),
                state_data.get('ji_activity', ''),
                state_data.get('card_asset_id', 0),
                state_data.get('card_rarity_name', ''),
                state_data.get('card_character_name', ''),
                state_data.get('card_subtitle', ''),
                state_data.get('sticker_ok', ''),
                state_data.get('sticker_yi', ''),
                state_data.get('sticker_ji', ''),
                state_data.get('song1_name', ''),
                state_data.get('song1_name_cn', ''),
                state_data.get('song1_cover_base64', ''),
                state_data.get('song1_difficulty_type', ''),
                state_data.get('song1_difficulty_level', 0),
                state_data.get('song1_difficulty_label', ''),
                state_data.get('song2_name', ''),
                state_data.get('song2_name_cn', ''),
                state_data.get('song2_cover_base64', ''),
                state_data.get('song2_difficulty_type', ''),
                state_data.get('song2_difficulty_level', 0),
                state_data.get('song2_difficulty_label', '')
            ))

            conn.commit()
            logger.info(f"✅ 每日签到状态已保存: {username}({user_id}) @ {ctx_id}")
            return True

    def get_daily_state(self, ctx_id: str, user_id: str) -> dict:
        today = datetime.now().strftime("%Y-%m-%d")

        with self._get_conn() as conn:
            user = self.get_or_create_user(ctx_id, user_id)

            if not user:
                return None

            row = conn.execute(
                "SELECT * FROM daily_signin_state WHERE user_ref = ? AND signin_date = ?",
                (user['id'], today)
            ).fetchone()

            if not row:
                return None

            return dict(row)

    def get_meta(self, key: str) -> str:
        with self._get_conn() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row[0] if row else None

    def set_meta(self, key: str, value: str):
        with self._get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (key, value)
            )
            conn.commit()

    def cleanup_old_data(self, keep_days: int = 365) -> int:
        """删除过期的每日签到状态和签到历史，返回删除的总行数。

        daily_signin_state 只在当天被读取（get_daily_state 按当天日期查询），
        过期行是纯死数据（含大体积歌曲封面 base64），全部清除；
        checkin_history 保留最近 keep_days 天，足够月历显示和连续签到计算。
        """
        today = datetime.now().strftime("%Y-%m-%d")
        history_cutoff = (datetime.now() - timedelta(days=max(1, keep_days))).strftime("%Y-%m-%d")

        deleted = 0
        with self._get_conn() as conn:
            cur = conn.execute(
                "DELETE FROM daily_signin_state WHERE signin_date < ?",
                (today,)
            )
            deleted += max(0, cur.rowcount)

            cur = conn.execute(
                "DELETE FROM checkin_history WHERE checkin_date < ?",
                (history_cutoff,)
            )
            deleted += max(0, cur.rowcount)

            conn.commit()
        return deleted

    def _vacuum_if_needed(self, min_free_pages: int = 256):
        """在后台线程中执行：有空闲页时回收磁盘空间（256 页 ≈ 1MB）"""
        try:
            conn = self._get_conn()
            free_pages = conn.execute("PRAGMA freelist_count").fetchone()[0]
            if free_pages >= min_free_pages:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("VACUUM")
                logger.info(f"🧹 数据库空间回收完成（VACUUM 释放 {free_pages} 页）")
        except Exception as e:
            logger.warning(f"⚠️ 数据库空间回收失败: {e}")
        finally:
            self.close()

    def maybe_cleanup(self, keep_days: int = 365):
        """每天最多执行一次过期数据清理，删除后异步回收文件空间。

        通过 meta 表记录上次清理日期，进程重启后不会重复执行当天的清理。
        """
        today = datetime.now().strftime("%Y-%m-%d")
        if self.get_meta('last_cleanup_date') == today:
            return

        try:
            deleted = self.cleanup_old_data(keep_days)
            self.set_meta('last_cleanup_date', today)
            if deleted > 0:
                logger.info(f"🧹 数据库每日清理完成，删除 {deleted} 条过期记录")
            threading.Thread(
                target=self._vacuum_if_needed,
                name="on-signin-db-vacuum",
                daemon=True
            ).start()
        except Exception as e:
            logger.warning(f"⚠️ 数据库每日清理失败: {e}")

    def close(self):
        if hasattr(self._local, 'conn') and self._local.conn:
            self._local.conn.close()
            self._local.conn = None

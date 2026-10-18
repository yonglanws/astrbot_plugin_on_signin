#!/usr/bin/env python3
"""把 OurNotes 立绘/背景/曲绘从 CDN 抓到本地。

服务器容器到 Cloudflare 的 TLS 握手和读操作频繁超时（实测一张曲绘要 6~200s，
并发竞速仍有约三分之一概率全部失败），所以改为在能连通 CDN 的机器上预下载，
再送进插件目录。插件读到本地文件就不再走网络。

实测结论（决定了下面的重试策略）：
- 限流表现为 HTTP 429/503，几秒内就返回，重试没用，应当立刻换源；
- 超时表现为 TimeoutError，等很久才失败，重试同一源往往能成（第二次 2.9s 成功）；
- 并发会加剧限流，所以默认单线程串行。

用法:
    python tools/fetch_assets.py --out <目录>
"""
import argparse
import concurrent.futures
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SOURCES = [
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

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://haneoka.org/",
}

MAGICS = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")
# 限流状态码：源是好的，只是现在别打了，重试和换源都不如等一会儿
RATE_LIMIT = (429, 503, 502, 500)
# 资源确实不存在：换源有意义，重试没有
NOT_FOUND = (404, 403, 410)


def looks_like_image(data: bytes) -> bool:
    if not data or len(data) < 32:
        return False
    if data[:4] == b"RIFF":
        return data[8:12] == b"WEBP"
    return any(data.startswith(m) for m in MAGICS)


def ext_for(data: bytes) -> str:
    """按文件头决定扩展名，插件侧读取时不关心具体是哪一种"""
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith(bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])):
        return "png"
    if data[:3] == bytes([0xFF, 0xD8, 0xFF]):
        return "jpg"
    return "bin"



def decode_ok(data: bytes) -> bool:
    """Pillow 完整解码，截断的图在这一步必然报错"""
    try:
        from PIL import Image
    except ImportError:
        return looks_like_image(data)
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        return True
    except Exception:
        return False


def fetch(urls, timeout=30, tries=4, cooldown=2.0):
    """按顺序尝试多个 URL，返回 (数据, 源名)；全部失败返回 (None, 原因)

    单个源内部：超时/连接错误才重试（实测重试常能成），限流码立刻放弃换源。
    """
    last = "unknown"
    for url in urls:
        for attempt in range(tries):
            try:
                req = urllib.request.Request(url, headers=HEADERS)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = resp.read()
                if not looks_like_image(data):
                    raise OSError(f"not an image ({len(data)} bytes)")
                if not decode_ok(data):
                    raise OSError("truncated image")
                return data, url
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}"
                if e.code in RATE_LIMIT:
                    break          # 被限流，换源；重试只会继续撞墙
                if e.code in NOT_FOUND:
                    break          # 这个源没有这张图
            except Exception as e:
                last = f"{type(e).__name__}: {str(e)[:60]}"
            time.sleep(cooldown * (attempt + 1))
    return None, last


def job(spec, out_dir, timeout, tries, cooldown):
    name, urls = spec
    dest = os.path.join(out_dir, name)
    if os.path.exists(dest) and os.path.getsize(dest) > 1024:
        return name, "cached", os.path.getsize(dest), 0.0
    t0 = time.time()
    data, src = fetch(urls, timeout=timeout, tries=tries, cooldown=cooldown)
    if data is None:
        return name, f"FAIL({src})", 0, time.time() - t0
    ext = ext_for(data)
    dest = dest + "." + ext
    if os.path.exists(dest) and os.path.getsize(dest) > 1024:
        return name, "cached", os.path.getsize(dest), 0.0
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dest)
    return name, "ok", len(data), time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="输出目录")
    ap.add_argument("--data", default=os.path.join(ROOT, "data", "ournotes_data.json"))
    ap.add_argument("--workers", type=int, default=1,
                    help="并发数。实测并发会加剧限流，默认 1")
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--tries", type=int, default=4)
    ap.add_argument("--cooldown", type=float, default=2.0)
    ap.add_argument("--rounds", type=int, default=6,
                    help="整体重试几轮。前几轮剩下的顽固文件留到后面再试")
    ap.add_argument("--only", default="", choices=("", "cards", "jackets"),
                    help="只抓某一类，便于和多台机器分工")
    args = ap.parse_args()

    with open(args.data, encoding="utf-8") as f:
        data = json.load(f)

    jobs = []
    jobs = []
    want = args.only          # "" = 全部；"jackets" = 只要封面；"cards" = 只要立绘/背景
    if want in ("", "cards"):
        for card in data.get("cards", []):
            cid = card.get("asset_id")
            if not cid:
                continue
            for kind in ("member_character", "member_background"):
                urls = [s["base"] + s["card"].format(id=cid, kind=kind) for s in SOURCES]
                jobs.append((f"cards/{cid}_{kind}", urls))
    if want in ("", "jackets"):
        jackets = sorted({(s.get("jacket") or s.get("jacket_asset"))
                          for s in data.get("songs", [])
                          if (s.get("jacket") or s.get("jacket_asset"))})
        for jk in jackets:
            urls = [s["base"] + s["jacket"].format(name=jk) for s in SOURCES]
            jobs.append((f"jackets/{jk}", urls))
    else:
        jackets = []

    total = len(jobs)
    print(f"共 {total} 个文件（立绘/背景 {total - len(jackets)}，曲绘 {len(jackets)}）",
          flush=True)

    pending = list(jobs)
    for rnd in range(1, args.rounds + 1):
        if not pending:
            break
        print(f"\n--- 第 {rnd} 轮，剩 {len(pending)} 个 ---", flush=True)
        still = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = [pool.submit(job, spec, args.out, args.timeout,
                                args.tries, args.cooldown) for spec in pending]
            for i, fut in enumerate(concurrent.futures.as_completed(futs), 1):
                name, status, size, dt = fut.result()
                if status.startswith("FAIL"):
                    urls = [u for n, u in jobs if n == name][0]
                    still.append((name, urls))
                    print(f"  [{i}/{len(pending)}] X {name} {status}", flush=True)
                elif status == "ok":
                    print(f"  [{i}/{len(pending)}] + {name} {size // 1024}KB {dt:.1f}s",
                          flush=True)
        pending = still
        if pending and rnd < args.rounds:
            # 限流窗口一般几十秒，退避一下再打
            wait = 20 * rnd
            print(f"  还有 {len(pending)} 个没拿到，等 {wait}s 再试", flush=True)
            time.sleep(wait)

    missing = [n for n, _ in pending]
    print(f"\n完成：成功 {total - len(missing)}/{total}", flush=True)
    if missing:
        print("仍未拿到:", flush=True)
        for m in missing:
            print("  -", m, flush=True)
        with open(os.path.join(args.out, "missing.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(missing))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

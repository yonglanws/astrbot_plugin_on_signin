# OurNotes 签到插件

OurNotes（MyGO!!!!! / Ave Mujica / 夢限大MewType / millsage / 一家Dumb Rock!）风格的签到打卡插件。
**首次运行会自动把立绘、背景、封面下载到持久化目录**，之后签到不依赖外网；master 数据每天自动同步，检测到新卡面/新立绘会补齐对应图片。歌曲名、难度、卡面数据同样自动同步。

## 特性

- **图片自动下载**：首次运行自动拉取全部立绘/背景/封面到 `plugin_data/assets/`，本地已有的一律跳过，不重复下载
- **增量更新**：master 数据同步时检测缺图，新卡面/新封面的图片在后台补齐，失败的留待下次同步重试
- **离线可用**：图片落到持久化目录，更新插件不会丢；签到命中本地图时零网络等待
- **只用 SR 以上与生日卡面**：卡池剔除 R 卡，包含 SSR 与 BD（生日）卡
- **OurNotes 难度体系**：只有 `HARD`（黄）与 `EXPERT`（红），没有 MASTER / APPEND
- **每日自动同步**：从 [`moenotes-masterdata/hk-tw-mo`](https://github.com/StarMoe-org/moenotes-masterdata/tree/main/hk-tw-mo) 拉取歌曲名、难度与新增卡面，一天一次
- **指定角色签到**：`签到 祥子`、`签到 anon`、`签到 初華` 都行
- **每日运势 + 宜忌 + 歌曲推荐**：同一天重复签到会复用当天已抽到的结果；卡面与贴纸会重新抽取，宜忌文案保持不变
- **性能优化**：图片走本地磁盘缓存，SQLite 不存大体积 base64，历史数据每日清理并回收空间

## 安装

```bash
cd AstrBot/data/plugins
git clone https://github.com/yonglanws/astrbot_plugin_on_signin.git
```

依赖：`Pillow`、`jinja2`（见 `requirements.txt`）。

## 使用

```
签到              # 随机卡面签到
签到 祥子          # 指定角色（支持别名 / 罗马字 / 中文名）
测试签到           # 只渲染，不记录、不写入持久化状态
设置签到昵称 小明   # 自定义卡片上显示的名字
同步卡面数据       # 手动同步 master 数据（默认每天自动同步一次）
```

## 数据来源

| 内容 | 来源 |
|------|------|
| 歌曲名 / 难度 / 作曲 | `moenotes-masterdata` 的 `hk-tw-mo/MasterLiveMusic.json` + `MasterLiveMusicScore.json` + `MasterText.json` |
| 卡面 / 角色 | 同仓库 `hk-tw-mo/MasterMemberCard.json` + `MasterCharacter.json` + `MasterBand.json` |
| 立绘 / 背景 / 封面图 | 首次运行自动下载到 `plugin_data/assets/`；插件自带 `data/assets/` 作兜底 |

同步只取繁中服（hk-tw-mo）目录，不做全仓库克隆。首次启动会在后台同步一次，之后每天最多一次；
插件目录内附带一份 `data/ournotes_data.json` 作为离线兜底，同步失败也能正常签到。

> ### ⚠️ 图片是自动下载的，不用手动往目录里塞图
>
> **首次运行插件时，会自动把数据集里全部立绘、背景、封面下载到
> `AstrBot/data/plugin_data/astrbot_plugin_on_signin/assets/`**，后台执行，不影响其他功能。
> 下载在日志里体现为 `🧩 检测到 N 个卡面图层与 M 张封面缺失，开始后台补下载` 和
> `✅ 后台补下载完成：N 个文件`。
>
> 之后每次 master 数据同步（默认每天一次）都会重新检查：**只要 master 里出现了本地没有的
> 卡面或封面，就自动把缺的图补下来**，游戏更新新卡面无需改插件。已经下载过的文件一律跳过，
> 不会重复请求 CDN。
>
> 手动同步会发送 `同步卡面数据`，同样会触发缺图补下载。
>
> 如果机器到 CDN 的网络很差（国内直连 Cloudflare 经常握手/读超时），首次补下载可能要多跑几轮，
> 失败的会留到下次同步继续尝试；也可以先在有代理的机器上用 `tools/fetch_assets.py` 下载，
> 再把文件按下面的目录结构拷过去：
>
> ```bash
> # 在能连通 CDN 的机器上执行，产出 png/webp 文件
> python tools/fetch_assets.py --out assets
> ```
>
> ```
> assets/cards/<assetID>_member_character.png     # 立绘
> assets/cards/<assetID>_member_background.png    # 背景
> assets/jackets/<jacketAssetName>.png            # 歌曲封面
> ```

### 图片寻址顺序与校验

读取顺序：**磁盘缓存 → `plugin_data/assets/` → 插件自带 `data/assets/` → 云端多源**。

每一层都会校验：先查图片魔数，再用 Pillow 完整解码一遍。截断的文件（连接读到一半断掉，
PNG 头是完整的但数据不全）能骗过魔数检查，只有真正解码才会暴露，所以两道都要过。
不合格的文件直接忽略并继续往下找，绝不把坏图渲染出来。

云端部分会尝试 `haneoka.org`（主源）和 `assets.bdon.moe`（备选源）两个源，单源失败自动重试并切换，
并对刚失败过的源做短时间冷却，避免每张图都先在坏源上耗掉超时。批量取图有整体时间上限，
超时的那一刻已经拿到的图层会被保留，不会因为一张图卡住而丢掉其他图。

## 目录说明

| 目录 | 路径 | 说明 |
|------|------|------|
| 插件目录 | `AstrBot/data/plugins/astrbot_plugin_on_signin/` | 代码、字体、`characters.json`、内置兜底数据、`data/assets/` 预置图片 |
| 持久化目录 | `AstrBot/data/plugin_data/astrbot_plugin_on_signin/` | 运行时数据 |

```
plugin_data/astrbot_plugin_on_signin/
├── signin.db                 # SQLite：用户、签到历史、每日状态
├── ournotes_data.json        # 同步后生成的歌曲/卡面数据
├── masterdata/               # 同步下来的 master 表 + sync_state.json
├── assets/                   # 自动下载的立绘/背景/封面（首次运行生成，更新插件不丢）
│   ├── cards/                #   <assetID>_member_character.png / _member_background.png
│   └── jackets/              #   <jacketAssetName>.png
└── cache/                    # 图片磁盘缓存（含已编码 data URI）
```

## 配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `sign_in_command` | string | `签到` | 触发指令 |
| `history_keep_days` | int | `365` | 签到历史保留天数，过期数据每日自动清理 |
| `auto_sync_masterdata` | bool | `true` | 是否每天自动同步 master 数据 |
| `download_timeout` | int | `20` | 单张云端图片下载超时（秒）。有预置图时完全不联网，此项只影响新卡面的兜底下载 |

在 AstrBot WebUI 插件设置页面修改后重载即可。

以下内容固定启用，不提供配置项：今日歌曲推荐、本月签到日期查询、卡片底部文字
（固定为 `Designed by 慵懒午睡 | 请勿迷信哦`）。

## 角色别名

角色名与别名写在插件目录的 `characters.json` 里，包含 25 位角色的简体中文名、繁体名、
日文名、英文名、罗马字简称与乐队信息。主名用简体中文，繁体名保留在 `aliases` 里方便检索。
要加别名直接改这个文件即可，例如：

```json
{
  "characterId": 10,
  "name": "oblivionis",
  "fullNameChinese": "Oblivionis / 丰川祥子",
  "fullNameTraditional": "Oblivionis / 豐川祥子",
  "aliases": ["oblivionis", "祥子", "丰川祥子", "豐川祥子", "オブリビオニス"]
}
```

## 常见问题

**Q: 图片显示不出来？**
A: 检查网络能否访问 `assets.bdon.moe`。单张图片下载失败只让对应图层留空，签到本身照常完成；
全部失败时降级为文字版签到。CDN 偶发超时是正常的，图片有 7 天本地缓存，不会每次重新下载。

**Q: 某张卡面下载超时，机器人没反应了？**
A: 已修复。所有云端请求都走独立线程池，不在事件循环线程里同步等待；单张有
`download_timeout`（默认 12 秒）上限，整个卡面下载还有一层 `download_timeout * 2 + 5` 秒的
整体预算兜底。超时后跳过图片层继续渲染，不会再阻塞其他指令。

**Q: 数据是实时的吗？**
A: master 数据每天同步一次，发送 `同步卡面数据` 可立即同步。图片本身每次按需下载并缓存 7 天。

**Q: 如何备份数据？**
A: 复制持久化目录下的 `signin.db`。

## 开发

```bash
python tests/test_plugin_logic.py    # 逻辑 + SQLite + 云端资产可达性测试
python tests/render_sample.py        # 通过 t2i 服务渲染样例卡片
python tests/render_sample.py 56     # 指定 SSR 卡 asset_id 渲染
```

`tests/render_sample.py` 默认连 `http://192.168.31.54:8999`，可用环境变量 `ASTRBOT_T2I` 覆盖。
不传参数时按 BD → SSR → SR 的顺序挑卡；传卡面 `asset_id` 可指定渲染某一张。

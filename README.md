# OurNotes 签到插件

OurNotes（MyGO!!!!! / Ave Mujica / 夢限大MewType / millsage / 一家Dumb Rock!）风格的签到打卡插件。
卡面立绘与背景全部**云端按需下载**，歌曲名、难度、卡面数据每天自动从繁中服 master 数据同步。

## 特性

- **云端卡面**：立绘与背景来自 `assets.bdon.moe`，不再需要手动往持久化目录塞图
- **只用 SR 以上与生日卡面**：卡池剔除 R 卡，包含 SSR 与 BD（生日）卡
- **OurNotes 难度体系**：只有 `HARD`（黄）与 `EXPERT`（红），没有 MASTER / APPEND
- **每日自动同步**：从 [`moenotes-masterdata/hk-tw-mo`](https://github.com/StarMoe-org/moenotes-masterdata/tree/main/hk-tw-mo) 拉取歌曲名、难度与新增卡面，一天一次
- **指定角色签到**：`签到 祥子`、`签到 anon`、`签到 初華` 都行
- **每日运势 + 宜忌 + 歌曲推荐**：同一天重复签到会复用当天已抽到的结果
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
| 立绘 / 背景 / 封面图 | `https://assets.bdon.moe/zh-Hans/...` |

同步只取繁中服（hk-tw-mo）目录，不做全仓库克隆。首次启动会在后台同步一次，之后每天最多一次；
插件目录内附带一份 `data/ournotes_data.json` 作为离线兜底，同步失败也能正常签到。

### 资产 URL 规则

```
背景:   https://assets.bdon.moe/zh-Hans/MemberCard/<assetID>/member_background/member_background.webp
立绘:   https://assets.bdon.moe/zh-Hans/MemberCard/<assetID>/member_character/member_character.webp
封面:   https://assets.bdon.moe/zh-Hans/Image/Jacket/<jacketAssetName>/<jacketAssetName>.webp
```

## 目录说明

| 目录 | 路径 | 说明 |
|------|------|------|
| 插件目录 | `AstrBot/data/plugins/astrbot_plugin_on_signin/` | 代码、字体、`characters.json`、内置兜底数据 |
| 持久化目录 | `AstrBot/data/plugin_data/astrbot_plugin_on_signin/` | 运行时数据 |

```
plugin_data/astrbot_plugin_on_signin/
├── signin.db                 # SQLite：用户、签到历史、每日状态
├── ournotes_data.json        # 同步后生成的歌曲/卡面数据
├── masterdata/               # 同步下来的 master 表 + sync_state.json
└── cache/                    # 云端图片磁盘缓存（立绘/背景/封面）
```

## 配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `sign_in_command` | string | `签到` | 触发指令 |
| `history_keep_days` | int | `365` | 签到历史保留天数，过期数据每日自动清理 |
| `auto_sync_masterdata` | bool | `true` | 是否每天自动同步 master 数据 |

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
A: 检查网络能否访问 `assets.bdon.moe`。下载失败会自动降级为文字版签到。

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

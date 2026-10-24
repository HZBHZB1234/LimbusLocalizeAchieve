# 自动同步脚本

本目录包含两个独立脚本，由 [`.github/workflows/auto_sync.yml`](../.github/workflows/auto_sync.yml)
每天定时（北京时间 14:17）一起执行，也可手动 `workflow_dispatch` 或在本地运行。

| 脚本 | 作用 | 数据源 |
| --- | --- | --- |
| `update_raw.py` | 同步最新游戏生肉到 `EN/`、`JP/`、`KR/` | status API（与 [LCTA_auto_update](https://github.com/HZBHZB1234/LCTA_auto_update) 相同）+ 游戏 CDN |
| `sync_translation.py` | 同步上游最新译文到 `LLC_zh-CN/` | `LocalizeLimbusCompany/LocalizeLimbusCompany` 最新 Release 资产 `LimbusLocalize_*.zip` |

两者都是**镜像同步**：目标目录与数据源完全一致（新增/更新文件，源中已删除的文件也会删除），
按字节比较，无变化则不产生改动。同步过的 token / Release tag 记录在 `.github/state/`
（随仓库提交），重复运行会自动跳过。

## 生肉链路

1. `GET {RAW_STATUS_URL}` 取 `latest_token.token`；
2. `GET {RAW_URL_TEMPLATE}` 下载 `localize_{en,jp,kr}.zip`；
3. 解压 `LocalizeTemp_{lang}`，去掉文件名的 `{LANG}_` 前缀后镜像同步
   （zip 内 `EN_AbDlg_x.json` → 仓库 `EN/AbDlg_x.json`）。

## 译文链路

1. GitHub API 列出上游正式 Release，取状态文件中 tag 之后（更新）的全部 release；
2. **从旧到新逐个处理**：下载 `LimbusLocalize_*.zip`（游戏安装包布局，译文在
   `LimbusCompany_Data/Lang/LLC_zh-CN`，含 `Info/version.json`（记录版本号）与 `RPGSystem/`），
   镜像同步到仓库 `LLC_zh-CN/`，随后**单独提交——一个 release 一个 commit**
   （`Auto Translation Sync - upstream <tag>`），文件变更与状态文件在同一 commit 中；
3. 首次运行（无状态）只同步最新一个 release 作为基线；单次运行最多处理 10 个，
   剩余留待下次；个别 release 缺 zip 资产时跳过（后续 release 是全量快照，内容不受影响）。

## 配置（环境变量，均有默认值）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `RAW_STATUS_URL` | `https://limbus.lcta.top/api/status` | 生肉 token 状态接口 |
| `RAW_URL_TEMPLATE` | CDN 下载模板 | `{token}` / `{lang}` 占位 |
| `RAW_LANGUAGES` | `en,jp,kr` | 需要同步的语言 |
| `UPSTREAM_REPO` | `LocalizeLimbusCompany/LocalizeLimbusCompany` | 译文上游仓库 |
| `COOKED_ASSET_PREFIX` | `LimbusLocalize_` | Release 资产名前缀（固定取 `*.zip`） |
| `GITHUB_TOKEN` | 无 | 上游 GitHub API 鉴权（Actions 中自动提供） |

## 本地验证

```powershell
python scripts/update_raw.py --dry-run
python scripts/sync_translation.py --dry-run
```

`--dry-run` 会完整执行下载、解压、比较，但**不修改任何文件**、不写状态。

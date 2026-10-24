"""从上游 LocalizeLimbusCompany 的 Release 同步译文（LLC_zh-CN）到仓库。

1. 通过 GitHub API 列出上游正式 Release，找出比状态文件中 tag 更新的所有 release；
2. 按时间从旧到新逐个处理：下载 LimbusLocalize_*.zip（游戏安装包布局，译文位于
   LimbusCompany_Data/Lang/LLC_zh-CN），镜像同步到仓库 LLC_zh-CN/，
   然后单独提交——一个 release 一个 commit；
3. 首次运行（无状态）或状态异常时，只同步最新一个 release 作为基线；
   单次运行最多处理 MAX_RELEASES_PER_RUN 个，剩余留待下次。

每个 release 的文件变更与其状态文件在同一 commit 中。tag 落后于最新时才会同步。

用法：
    python scripts/sync_translation.py [--dry-run] [--repo-root PATH]
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sync_lib as sl  # noqa: E402

DEFAULT_UPSTREAM_REPO = "LocalizeLimbusCompany/LocalizeLimbusCompany"
DEFAULT_ASSET_PREFIX = "LimbusLocalize_"
COOKED_DIR = "LLC_zh-CN"
STATE_REL = Path(".github/state/translation_state.json")
API_ROOT = "https://api.github.com"
# tag 为 YYYYMMDDNN 定长格式，字符串比较即时间先后
TAG_PATTERN = re.compile(r"^\d{10}$")
MAX_RELEASES_PER_RUN = 10


def pick_zip_asset(assets: list) -> dict:
    prefix = os.environ.get("COOKED_ASSET_PREFIX") or DEFAULT_ASSET_PREFIX
    matched = [
        asset
        for asset in assets
        if str(asset.get("name", "")).startswith(prefix)
        and str(asset.get("name", "")).lower().endswith(".zip")
    ]
    if len(matched) != 1:
        names = ", ".join(str(item.get("name")) for item in assets) or "(无资产)"
        raise RuntimeError(
            f"无法唯一确定 Release 资产（{len(matched)} 个匹配 {prefix}*.zip）: {names}"
        )
    return matched[0]


def fetch_stable_releases(upstream: str, github_token: str) -> list[dict]:
    """列出上游正式 Release（GitHub 返回按时间新→旧，最多取 3 页）。"""
    releases: list[dict] = []
    for page in (1, 2, 3):
        data = sl.http_get_json(
            f"{API_ROOT}/repos/{upstream}/releases?per_page=100&page={page}",
            token=github_token,
        )
        if not isinstance(data, list) or not data:
            break
        releases.extend(item for item in data if isinstance(item, dict))
        if len(data) < 100:
            break
    return [
        item
        for item in releases
        if item.get("tag_name")
        and TAG_PATTERN.fullmatch(str(item["tag_name"]))
        and not item.get("draft")
        and not item.get("prerelease")
    ]


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} 失败: {(result.stderr or result.stdout).strip()}"
        )
    return result


def _git_config_get(repo_root: Path, key: str) -> str:
    result = subprocess.run(
        ["git", "config", "--get", key],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def commit_release(repo_root: Path, tag: str) -> bool:
    """提交单个 release 的译文变更，返回是否产生了 commit。"""
    for key, default in (
        ("user.name", "github-actions[bot]"),
        ("user.email", "41898282+github-actions[bot]@users.noreply.github.com"),
    ):
        if not _git_config_get(repo_root, key):
            _git(repo_root, "config", key, default)  # 仅在完全未配置时写仓库本地配置
    status = _git(
        repo_root, "status", "--porcelain", "--", COOKED_DIR, STATE_REL.as_posix()
    ).stdout
    if not status.strip():
        sl.log(f"{tag}: 无待提交内容")
        return False
    _git(repo_root, "add", "--", COOKED_DIR, STATE_REL.as_posix())
    _git(repo_root, "commit", "-m", f"Auto Translation Sync - upstream {tag}")
    sl.log(f"已提交: Auto Translation Sync - upstream {tag}")
    return True


def sync_one_release(release: dict, repo_root: Path, apply: bool) -> str:
    """处理单个 release，返回资产名；apply=False 时只比较不落盘。

    资产异常时抛出 RuntimeError。
    """
    asset = pick_zip_asset(release.get("assets") or [])
    asset_url = str(asset.get("browser_download_url") or "")
    if not asset_url:
        raise RuntimeError("资产缺少 browser_download_url")
    asset_name = str(asset.get("name"))
    with tempfile.TemporaryDirectory(prefix="llc-cooked-sync-") as tmp:
        archive_path = Path(tmp) / asset_name
        # 公开仓库资产无需鉴权；携带 token 反而会在重定向到对象存储时被拒绝
        sl.http_download(asset_url, archive_path)
        extracted = Path(tmp) / "extracted"
        sl.extract_zip_safely(archive_path, extracted)
        source = sl.find_unique_dir(extracted, COOKED_DIR)
        ops = sl.plan_mirror(source, repo_root / COOKED_DIR)
        if apply and ops:
            sl.apply_plan(ops, repo_root / COOKED_DIR)
        label = ("[dry-run] " if not apply else "") + str(release["tag_name"])
        sl.summarize_plan(label, ops)
    return asset_name


def main() -> int:
    parser = argparse.ArgumentParser(description="从上游 Release 同步译文到仓库")
    parser.add_argument(
        "--dry-run", action="store_true", help="只比较并输出结果，不修改文件、不提交"
    )
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args()
    repo_root = Path(args.repo_root).resolve()
    state_path = repo_root / STATE_REL

    upstream = os.environ.get("UPSTREAM_REPO") or DEFAULT_UPSTREAM_REPO
    github_token = os.environ.get("GITHUB_TOKEN") or ""

    sl.log(f"查询上游 Release 列表: {upstream}")
    releases = fetch_stable_releases(upstream, github_token)
    if not releases:
        sl.log("上游没有可用的正式 Release，终止")
        return 1

    tags_desc = sorted({str(item["tag_name"]) for item in releases}, reverse=True)
    latest_tag = tags_desc[0]
    sl.log(f"上游最新 Release: {latest_tag}")

    state = sl.load_state(state_path)
    state_tag = str(state.get("tag") or "")
    if TAG_PATTERN.fullmatch(state_tag):
        pending_tags = sorted(tag for tag in tags_desc if tag > state_tag)
    else:
        if state_tag:
            sl.log(f"状态文件中的 tag 无法识别（{state_tag}），按首次运行处理")
        pending_tags = [latest_tag]

    if not pending_tags:
        sl.log(f"上游 Release 已同步到 {state_tag}，跳过")
        sl.emit_output("changed", "false")
        return 0
    sl.log(f"待同步 release（从旧到新）: {' -> '.join(pending_tags)}")
    if len(pending_tags) > MAX_RELEASES_PER_RUN:
        sl.log(
            f"共 {len(pending_tags)} 个待同步，本次只处理最早的 {MAX_RELEASES_PER_RUN} 个，"
            "其余留待下次"
        )
        if len(releases) >= 300:
            sl.log("警告：release 列表可能被截断，更早的 release 未被获取")
        pending_tags = pending_tags[:MAX_RELEASES_PER_RUN]

    release_map = {str(item["tag_name"]): item for item in releases}

    if args.dry_run:
        sl.log(f"[dry-run] 以下是与最新 release（{latest_tag}）比较的差异预览：")
        sync_one_release(release_map[latest_tag], repo_root, apply=False)
        sl.log("[dry-run] 结束，未修改任何文件")
        return 0

    committed = 0
    for tag in pending_tags:
        try:
            asset_name = sync_one_release(release_map[tag], repo_root, apply=True)
        except Exception as error:
            # 单个 release 失败不拖垮整批：后续 release 是全量快照，内容不受影响
            sl.log(f"警告：{tag} 无法同步，跳过: {error}")
            continue
        sl.save_state(
            state_path, {"tag": tag, "asset": asset_name, "synced_at": sl.now_bj()}
        )
        if commit_release(repo_root, tag):
            committed += 1

    sl.emit_output("changed", "true" if committed else "false")
    sl.log(f"译文同步完成，共提交 {committed} 个 commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

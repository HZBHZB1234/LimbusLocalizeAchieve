"""游戏更新后同步最新生肉（EN / JP / KR）到仓库。

数据链路与 LCTA_auto_update 完全一致：
1. 请求 status API（默认 https://limbus.lcta.top/api/status）拿到最新 raw token；
2. 按 token 下载 localize_{lang}.zip；
3. 解压出 LocalizeTemp_{lang}，去掉文件名中的 {LANG}_ 前缀（如 EN_AbDlg_x.json
   -> AbDlg_x.json），镜像同步到仓库对应语言目录（源中已删除的文件一并删除）。

已同步过的 token 记录在 .github/state/raw_state.json，token 未变化时直接跳过。

用法：
    python scripts/update_raw.py [--dry-run] [--repo-root PATH]
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sync_lib as sl  # noqa: E402

DEFAULT_STATUS_URL = "https://limbus.lcta.top/api/status"
DEFAULT_URL_TEMPLATE = (
    "https://downloadcommon.limbuscompanycdn.org/"
    "{token}/Assets/LocalizePatch/localize_{lang}.zip"
)
LANG_DIRS = {"en": "EN", "jp": "JP", "kr": "KR"}
STATE_REL = Path(".github/state/raw_state.json")


def strip_lang_prefix(name: str, lang: str) -> str:
    prefix = f"{lang.upper()}_"
    return name[len(prefix):] if name.startswith(prefix) else name


def stage_without_prefix(source: Path, staged: Path, lang: str) -> None:
    """把解压出的文件按仓库命名规则（去掉语言前缀）拷贝到暂存目录。"""
    for file_path in source.rglob("*"):
        if not file_path.is_file():
            continue
        rel = file_path.relative_to(source)
        target = staged / rel.parent / strip_lang_prefix(rel.name, lang)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file_path, target)


def sync_language(
    token: str, lang: str, url_template: str, work_root: Path, repo_root: Path
) -> list[tuple]:
    zip_path = work_root / "downloads" / f"localize_{lang}.zip"
    sl.http_download(url_template.format(token=token, lang=lang), zip_path)
    extracted = work_root / "extracted" / lang
    sl.extract_zip_safely(zip_path, extracted)
    source = sl.find_unique_dir(extracted, f"LocalizeTemp_{lang}")
    staged = work_root / "staged" / lang
    stage_without_prefix(source, staged, lang)
    return sl.plan_mirror(staged, repo_root / LANG_DIRS[lang])


def main() -> int:
    parser = argparse.ArgumentParser(description="同步最新游戏生肉到仓库")
    parser.add_argument("--dry-run", action="store_true", help="只比较并输出结果，不修改文件")
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args()
    repo_root = Path(args.repo_root).resolve()

    status_url = os.environ.get("RAW_STATUS_URL") or DEFAULT_STATUS_URL
    url_template = os.environ.get("RAW_URL_TEMPLATE") or DEFAULT_URL_TEMPLATE
    languages = [
        item.strip().lower()
        for item in (os.environ.get("RAW_LANGUAGES") or "en,jp,kr").split(",")
        if item.strip()
    ]

    sl.log(f"请求 status API: {status_url}")
    latest = sl.http_get_json(status_url).get("latest_token") or {}
    token = latest.get("token") or ""
    created_at = latest.get("created_at") or ""
    if not token:
        sl.log("status API 未返回 token，终止")
        return 1
    sl.log(f"最新 raw token: {token}（{created_at}）")

    state = sl.load_state(repo_root / STATE_REL)
    if state.get("token") == token:
        sl.log("token 已同步过，跳过")
        sl.emit_output("changed", "false")
        return 0

    with tempfile.TemporaryDirectory(prefix="llc-raw-sync-") as tmp:
        work_root = Path(tmp)
        for lang in languages:
            ops = sync_language(token, lang, url_template, work_root, repo_root)
            label = LANG_DIRS.get(lang, lang.upper())
            sl.summarize_plan(("[dry-run] " if args.dry_run else "") + label, ops)
            if not args.dry_run:
                sl.apply_plan(ops, repo_root / label)

    if args.dry_run:
        sl.log("dry-run 结束，未修改任何文件")
        return 0

    sl.save_state(
        repo_root / STATE_REL,
        {"token": token, "created_at": created_at, "synced_at": sl.now_bj()},
    )
    sl.emit_output("changed", "true")
    sl.log("生肉同步完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

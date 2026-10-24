"""LimbusLocalizeAchieve 同步脚本共享工具（仅标准库，无第三方依赖）。"""

from __future__ import annotations

import json
import os
import shutil
import time
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

USER_AGENT = "LimbusLocalizeAchieve-sync/1.0"
RETRIES = 3
RETRY_BACKOFF_SECONDS = 5
HTTP_TIMEOUT = 300
SAMPLE_LIMIT = 8


def log(message: str) -> None:
    print(f"[sync] {message}", flush=True)


def now_bj() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def emit_output(name: str, value: str) -> None:
    """写入 GitHub Actions step output；本地运行（无 GITHUB_OUTPUT）时忽略。"""
    output_file = os.environ.get("GITHUB_OUTPUT")
    if not output_file:
        return
    with open(output_file, "a", encoding="utf-8") as handle:
        handle.write(f"{name}={value}\n")


def _build_request(url: str, token: str = "") -> urllib.request.Request:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    return request


def http_get_json(url: str, token: str = ""):
    last_error: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(
                _build_request(url, token), timeout=HTTP_TIMEOUT
            ) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as error:
            last_error = error
            log(f"请求失败（{attempt}/{RETRIES}）: {url}: {error}")
            if attempt < RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    raise RuntimeError(f"请求最终失败: {url}") from last_error


def http_download(url: str, destination: Path, token: str = "") -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(
                _build_request(url, token), timeout=HTTP_TIMEOUT
            ) as response, open(destination, "wb") as handle:
                shutil.copyfileobj(response, handle)
                expected_length = response.headers.get("Content-Length")
            # 连接提前关闭时流式读取不会报错，只会写出截断文件，需按声明长度校验
            if expected_length and destination.stat().st_size != int(expected_length):
                raise RuntimeError(
                    f"下载不完整: 期望 {expected_length} 字节，"
                    f"实际 {destination.stat().st_size} 字节"
                )
            return
        except Exception as error:
            last_error = error
            log(f"下载失败（{attempt}/{RETRIES}）: {url}: {error}")
            if attempt < RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    raise RuntimeError(f"下载最终失败: {url}") from last_error


def extract_zip_safely(archive_path: Path, destination: Path) -> None:
    """解压 ZIP 并防御路径穿越（移植自 LCTA_auto_update 的 archive.py）。"""
    destination.mkdir(parents=True, exist_ok=True)
    destination_root = destination.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        bad_file = archive.testzip()
        if bad_file is not None:
            raise RuntimeError(f"ZIP 校验失败: {bad_file}")
        for member in archive.infolist():
            member_path = (destination / member.filename).resolve()
            if not member_path.is_relative_to(destination_root):
                raise RuntimeError(f"ZIP 包含不安全路径: {member.filename}")
        archive.extractall(destination)


def find_unique_dir(root: Path, name: str) -> Path:
    direct = root / name
    if direct.is_dir():
        return direct
    matches = [path for path in root.rglob(name) if path.is_dir()]
    if len(matches) != 1:
        raise RuntimeError(f"无法唯一定位目录 {name}: 找到 {len(matches)} 个候选")
    return matches[0]


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def save_state(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


# ---- 镜像同步 ----
# 同步后目标目录与源目录完全一致：新增、更新文件，并删除源中已不存在的文件。
# op 形如 ("copy", rel, src_path, dst_path, is_new) 或 ("delete", rel, dst_path)。


def _files_equal(left: Path, right: Path) -> bool:
    left_bytes = left.read_bytes()
    right_bytes = right.read_bytes()
    if left_bytes == right_bytes:
        return True
    # 忽略换行符差异：Windows autocrlf 检出会把工作区变成 CRLF，
    # 按字节比较会造成大面积假差异；JSON 内容本身不关心换行符
    return left_bytes.replace(b"\r\n", b"\n") == right_bytes.replace(b"\r\n", b"\n")


def plan_mirror(src_root: Path, dst_root: Path) -> list[tuple]:
    src_files: dict[str, Path] = {}
    for current, _, files in os.walk(src_root):
        for name in files:
            path = Path(current) / name
            src_files[path.relative_to(src_root).as_posix()] = path
    dst_files: dict[str, Path] = {}
    for current, _, files in os.walk(dst_root):
        for name in files:
            path = Path(current) / name
            dst_files[path.relative_to(dst_root).as_posix()] = path

    ops: list[tuple] = []
    for rel in sorted(src_files):
        dst = dst_root / rel
        if rel not in dst_files:
            ops.append(("copy", rel, src_files[rel], dst, True))
        elif not _files_equal(src_files[rel], dst_files[rel]):
            ops.append(("copy", rel, src_files[rel], dst, False))
    for rel in sorted(dst_files):
        if rel not in src_files:
            ops.append(("delete", rel, dst_files[rel]))
    return ops


def _prune_empty_dirs(root: Path) -> None:
    for current, _, _ in os.walk(root, topdown=False):
        path = Path(current)
        if path == root:
            continue
        try:
            path.rmdir()
        except OSError:
            pass


def apply_plan(ops: list[tuple], dst_root: Path) -> None:
    for op in ops:
        if op[0] == "copy":
            _, _, src, dst, _ = op
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
        else:
            op[2].unlink()
    _prune_empty_dirs(dst_root)


def summarize_plan(label: str, ops: list[tuple]) -> None:
    copies = [op for op in ops if op[0] == "copy"]
    deletes = [op for op in ops if op[0] == "delete"]
    added = [op for op in copies if op[4]]
    updated = [op for op in copies if not op[4]]
    log(f"{label}: 新增 {len(added)}，更新 {len(updated)}，删除 {len(deletes)}")
    for op in added[:SAMPLE_LIMIT]:
        log(f"  + {op[1]}")
    for op in updated[:SAMPLE_LIMIT]:
        log(f"  ~ {op[1]}")
    for op in deletes[:SAMPLE_LIMIT]:
        log(f"  - {op[1]}")

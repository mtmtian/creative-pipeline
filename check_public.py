#!/usr/bin/env python3
"""check_public.py：公开仓库提交前检查。

    python3 check_public.py            # 检查将要提交的文件（git 跟踪 + 已暂存）
    python3 check_public.py FILE...    # 只检查指定文件

拒绝：本机绝对路径（/Users/…、/home/…）、私钥与常见令牌格式、本地配置文件、产物与素材目录。
发现问题时逐条列出并以退出码 1 结束；本仓库公开，内部笔记和素材清单放素材目录，不要提交。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

FORBIDDEN_PATHS = (
    re.compile(r"(^|/)config\.local\.json$"),
    re.compile(r"^(out|work|_batches|_testset_links)/"),
    re.compile(r"\.(mp4|mov|m4v|wav|m4a|srt)$", re.IGNORECASE),
)
FORBIDDEN_CONTENT = (
    ("本机绝对路径", re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+/")),
    ("私钥", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("API 令牌", re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|xox[bp]-[A-Za-z0-9-]{10,})")),
)
SELF = Path(__file__).name


def tracked_files() -> list[str]:
    listed = subprocess.run(["git", "ls-files", "--cached"], capture_output=True, text=True, check=True)
    return [line for line in listed.stdout.splitlines() if line]


def problems_in(path: str) -> list[str]:
    found = [f"{path}: 不应提交到公开仓库"] if any(p.search(path) for p in FORBIDDEN_PATHS) else []
    if found or path == SELF:
        return found
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
        return found
    for number, line in enumerate(text.splitlines(), 1):
        for label, pattern in FORBIDDEN_CONTENT:
            if pattern.search(line):
                found.append(f"{path}:{number}: {label}")
    return found


def main(argv: list[str]) -> int:
    files = argv or tracked_files()
    problems = [problem for path in files for problem in problems_in(path)]
    for problem in problems:
        print(problem)
    if problems:
        print(f"\n{len(problems)} 处不适合公开；改成 config.local.json / 环境变量，或移到素材目录。")
        return 1
    print(f"checked {len(files)} files: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

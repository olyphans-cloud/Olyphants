#!/usr/bin/env python3
"""Periksa isi ZIP rilis: tanpa secret, tanpa state/cache. Keluar kode 1 bila ada pelanggaran.

  python3 scripts/package_check.py olypahantstrade_v5-rc1.zip
"""
import re
import sys
import zipfile

FORBIDDEN_NAMES = [
    (re.compile(r"(^|/)\.env$"), "berkas .env (berisi secret)"),
    (re.compile(r"(^|/)state/(?!\.gitkeep$)[^/]+"), "isi folder state/ (state/log runtime)"),
    (re.compile(r"__pycache__/|\.py[co]$"), "cache Python"),
    (re.compile(r"(^|/)\.git/"), "folder .git"),
    (re.compile(r"\.(pem|key|p12|pfx)$", re.I), "berkas kunci"),
]
SECRET_PATTERNS = [
    (re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{35}\b"), "token bot Telegram"),
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"), "kunci privat"),
    (re.compile(r"(?i)(api[_-]?secret|secret[_-]?key)\s*[:=]\s*['\"][A-Za-z0-9]{20,}['\"]"), "API secret literal"),
]
TEXT_EXT = (".py", ".md", ".json", ".sh", ".txt", ".example", ".cfg", ".toml", ".yml", ".yaml", ".gitignore", ".html")


def check_names(names):
    out = []
    for n in names:
        for rx, why in FORBIDDEN_NAMES:
            if rx.search(n):
                out.append(f"{n}: {why}")
    return out


def check_text(name, text):
    return [f"{name}: {why}" for rx, why in SECRET_PATTERNS if rx.search(text)]


def check_zip(path):
    problems = []
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        problems += check_names(names)
        for n in names:
            if n.endswith("/") or not n.lower().endswith(TEXT_EXT):
                continue
            info = z.getinfo(n)
            if info.file_size > 2_000_000:
                continue
            try:
                text = z.read(n).decode("utf-8", errors="ignore")
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{n}: tidak terbaca ({type(exc).__name__})")
                continue
            problems += check_text(n, text)
    return problems


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    problems = check_zip(sys.argv[1])
    for p in problems:
        print("PELANGGARAN:", p)
    print("BERSIH" if not problems else f"{len(problems)} pelanggaran")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

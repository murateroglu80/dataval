#!/usr/bin/env python3
"""
check_integrity.py — Proje dosyalarinin butunlugunu kontrol eder.

Kullanim:
  python check_integrity.py    # Tum .py dosyalarini tara
"""

import sys
import ast
from pathlib import Path


def check_file(path):
    with open(path, "rb") as f:
        data = f.read()

    null_count = data.count(b"\x00")
    result = {
        "path": path,
        "size": len(data),
        "null_bytes": null_count,
        "syntax_ok": False,
        "error": None,
    }

    if null_count > 0:
        first_pos = data.index(b"\x00")
        result["error"] = f"{null_count} null byte (ilk @{first_pos})"
        return result

    try:
        src = data.decode("utf-8")
        ast.parse(src)
        result["syntax_ok"] = True
    except SyntaxError as e:
        result["error"] = f"SyntaxError: {e}"
    except UnicodeDecodeError as e:
        result["error"] = f"UnicodeDecodeError: {e}"

    return result


def main():
    base = Path(__file__).parent
    py_files = sorted(base.rglob("*.py"))

    print(f"dataval integrity check -- {len(py_files)} dosya\n")

    issues = []
    for f in py_files:
        r = check_file(f)
        rel = str(f.relative_to(base))
        if r["error"]:
            print(f"  HATA  {rel}  -- {r['error']}")
            issues.append(r)
        else:
            print(f"  OK    {rel}  ({r['size']} bytes)")

    print(f"\n{'=' * 50}")
    if issues:
        print(f"SORUN: {len(issues)} dosyada hata var.")
        print("Cozum: git fetch origin && git reset --hard origin/main")
        sys.exit(1)
    else:
        print(f"Tum dosyalar temiz ({len(py_files)} dosya kontrol edildi).")


if __name__ == "__main__":
    main()

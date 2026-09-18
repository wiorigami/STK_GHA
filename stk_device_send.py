#!/usr/bin/env python3
"""
stk_device_send.py — 用裝置憑證送檔案到 Kindle（CI 專用，不需要瀏覽器）

跟 stk_action.py（cookie 版）的差別：
  ┌────────────┬──────────────────────┬──────────────────────────┐
  │            │ cookie 版             │ 裝置憑證版（本檔）         │
  ├────────────┼──────────────────────┼──────────────────────────┤
  │ 憑證       │ session cookie        │ RSA 私鑰 + adp_token      │
  │ 會不會輪替 │ 每次請求都輪替         │ 不會                     │
  │ 需要更新   │ 經常                  │ 幾乎不用                 │
  │ 端點       │ www.amazon.com        │ stkservice.amazon.com    │
  │ Secrets    │ 6 個                  │ 2 個                     │
  └────────────┴──────────────────────┴──────────────────────────┘

需要的 Secrets：
  STK_DEVICE_PRIVATE_KEY   RSA 私鑰（PEM，多行）
  STK_ADP_TOKEN            adp token

取得方式：在本機跑一次 `python3 stk_device_setup.py`

用法：
  python3 stk_device_send.py book.epub
  python3 stk_device_send.py --archive *.epub
  python3 stk_device_send.py --url https://example.com/book.epub
"""

from __future__ import annotations

import argparse
import glob as globmod
import os
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

import requests

# 支援的格式與大小（跟網頁版一致）
MAX_FILE_SIZE = 209_715_200
VALID_EXT = {
    "doc", "docx", "html", "htm", "rtf", "txt",
    "jpeg", "jpg", "png", "bmp", "gif", "epub", "pdf",
}

# 預設作者。
# ⚠️ Amazon 的 API 不接受空字串 —— 沒給 author 會回 400：
#    "Value '' at 'documentMetadata.author' failed to satisfy constraint"
# 所以這裡一定要有非空預設值，或由 --author 指定。
DEFAULT_AUTHOR = "kindle tool"

# 冒充官方桌面 App
CLIENT_INFO = {
    "appName": "ShellExtension",
    "appVersion": "1.1.1.253",
    "os": "MacOSX_10.14.6_x64",
    "osArchitecture": "x64",
}

def _fix_ssl_certs() -> None:
    """修正 macOS 上 Python 的 SSL 憑證問題。

    macOS 的 Python 預設不使用系統憑證庫，呼叫 api.amazon.com 之類的
    端點會出現 CERTIFICATE_VERIFY_FAILED。用 certifi 的憑證包修正。
    """
    try:
        import ssl
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        ssl._create_default_https_context = lambda *a, **k: ctx  # noqa: SLF001
    except ImportError:
        pass


SUMMARY_FILE = os.environ.get("GITHUB_STEP_SUMMARY")


def log(msg: str = "") -> None:
    print(msg, flush=True)


def summary(lines: list[str]) -> None:
    if not SUMMARY_FILE:
        return
    try:
        with open(SUMMARY_FILE, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass


def load_credentials():
    """從環境變數讀裝置憑證，組出 stkclient 的 Client。"""
    from stkclient import Client, model

    key = (os.environ.get("STK_DEVICE_PRIVATE_KEY") or "").strip()
    token = (os.environ.get("STK_ADP_TOKEN") or "").strip()

    missing = [n for n, v in
               [("STK_DEVICE_PRIVATE_KEY", key), ("STK_ADP_TOKEN", token)] if not v]
    if missing:
        raise ValueError(
            "缺少 Secrets：" + ", ".join(missing) + "\n"
            "  請在本機跑一次 python3 stk_device_setup.py 取得這兩個值"
        )

    # 簽章只需要這兩個欄位；其餘留空即可
    info = model.DeviceInfo(
        device_private_key=key,
        adp_token=token,
        device_type="",
        given_name="",
        name="",
        account_pool="Amazon",
        user_directed_id="",
        user_device_name="",
    )
    return Client(info)


def download(url: str, dest_dir: Path) -> Path:
    name = Path(urllib.parse.urlparse(url).path).name or "download"
    dest = dest_dir / name
    log(f"  ↓ 下載 {url}")
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)
    log(f"     -> {dest.name} ({dest.stat().st_size/1e6:.2f} MB)")
    return dest


def resolve(patterns: list[str], urls: list[str], tmp: Path) -> list[Path]:
    out: list[Path] = []
    for pat in patterns:
        p = Path(pat).expanduser()
        if p.is_file():
            out.append(p)
        else:
            ms = [Path(m) for m in globmod.glob(pat, recursive=True) if Path(m).is_file()]
            if ms:
                out.extend(sorted(ms))
            else:
                log(f"  ⚠ 找不到：{pat}")
    for u in urls:
        try:
            out.append(download(u, tmp))
        except Exception as e:  # noqa: BLE001
            log(f"  ✗ 下載失敗 {u}：{e}")
    seen, uniq = set(), []
    for f in out:
        k = str(f.resolve())
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    return uniq


def validate(paths: list[Path]) -> tuple[list[Path], list[tuple[Path, str]]]:
    good, bad = [], []
    for p in paths:
        if not p.is_file():
            bad.append((p, "找不到檔案"))
            continue
        size, ext = p.stat().st_size, p.suffix.lstrip(".").lower()
        if size == 0:
            bad.append((p, "空檔案"))
        elif size > MAX_FILE_SIZE:
            bad.append((p, f"{size/1e6:.1f} MB 超過上限"))
        elif ext not in VALID_EXT:
            bad.append((p, f"格式 .{ext} 不支援"))
        else:
            good.append(p)
    return good, bad


def main(argv: list[str] | None = None) -> int:
    _fix_ssl_certs()

    ap = argparse.ArgumentParser(
        description="用裝置憑證送檔案到 Kindle（CI 專用）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("files", nargs="*", help="檔案路徑或 glob")
    ap.add_argument("--files", dest="files_opt", action="append", default=[],
                    help="檔案路徑或 glob（可重複）")
    ap.add_argument("--url", dest="urls", action="append", default=[],
                    help="遠端檔案網址（可重複）")
    ap.add_argument("--archive", action=argparse.BooleanOptionalAction, default=True,
                    help="送到雲端書庫（預設開啟）")
    ap.add_argument("--device", action="append", default=[],
                    help="指定裝置序號（--no-archive 時才需要）")
    ap.add_argument("--title", default=None, help="覆寫書名")
    ap.add_argument("--author", default=None,
                    help=f"覆寫作者（預設 {DEFAULT_AUTHOR!r}；不可為空）")
    ap.add_argument("--list-devices", action="store_true", help="列出裝置")
    ap.add_argument("--dry-run", action="store_true", help="只驗證憑證，不送出")
    args = ap.parse_args(argv)

    t0 = time.time()
    log("=" * 60)
    log("Send to Kindle — 裝置憑證模式")
    log("=" * 60)

    # ---- 憑證
    try:
        client = load_credentials()
    except ValueError as e:
        log(f"\n✗ {e}")
        summary(["## ❌ Send to Kindle 失敗", "", "```", str(e), "```"])
        return 2
    except Exception as e:  # noqa: BLE001
        log(f"\n✗ 憑證載入失敗：{type(e).__name__}: {e}")
        log("  請確認 STK_DEVICE_PRIVATE_KEY 是完整的 PEM（含 BEGIN/END 兩行）")
        summary(["## ❌ Send to Kindle 失敗", "", f"憑證載入失敗：{e}"])
        return 2

    log("\n✓ 憑證已載入")

    # ---- 列出裝置
    if args.list_devices:
        try:
            devs = client.get_owned_devices()
            log(f"\n裝置清單（{len(devs)} 台）：")
            for d in devs:
                log(f"  - {d.device_name}  [{d.device_serial_number}]")
        except Exception as e:  # noqa: BLE001
            log(f"✗ 取裝置清單失敗：{e}")
            return 2
        return 0

    # ---- 檔案
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        patterns = list(args.files) + list(args.files_opt)
        if not patterns and not args.urls:
            log("\n✗ 沒有指定檔案（用位置參數、--files 或 --url）")
            summary(["## ❌ Send to Kindle 失敗", "", "沒有指定檔案"])
            return 1

        log("\n→ 收集檔案")
        paths = resolve(patterns, args.urls, tmp)
        good, bad = validate(paths)
        for p, why in bad:
            log(f"  ✗ {p.name}：{why}")
        if not good:
            log("\n✗ 沒有可送的檔案")
            summary(["## ❌ Send to Kindle 失敗", "", "沒有可送的檔案"])
            return 1

        if args.dry_run:
            log(f"\n[dry-run] {len(good)} 個檔案通過檢查")
            try:
                devs = client.get_owned_devices()
                log(f"  ✓ 憑證有效，可見裝置 {len(devs)} 台")
            except Exception as e:  # noqa: BLE001
                log(f"  ✗ 憑證驗證失敗：{e}")
                return 2
            log("\n[dry-run] 完成，未送出。")
            return 0

        # ---- 送出
        if not args.archive and not args.device:
            log("\n✗ --no-archive 時必須用 --device 指定裝置")
            return 1

        devices = args.device
        if not args.archive and not devices:
            try:
                devices = [d.device_serial_number for d in client.get_owned_devices()]
                log(f"（未指定裝置，使用全部 {len(devices)} 台）")
            except Exception as e:  # noqa: BLE001
                log(f"✗ 取裝置失敗：{e}")
                return 2

        log("")
        results: list[tuple[str, bool, str]] = []
        for p in good:
            try:
                sku = client.send_file(
                    p, devices,
                    author=(args.author or DEFAULT_AUTHOR).strip() or DEFAULT_AUTHOR,
                    title=(args.title or p.stem)[:250],
                    format=p.suffix.lstrip(".").lower(),
                )
                log(f"  ✓ {p.name}  ({p.stat().st_size/1e6:.2f} MB)  sku={str(sku)[:24]}")
                results.append((p.name, True, ""))
            except Exception as e:  # noqa: BLE001
                msg = f"{type(e).__name__}: {str(e)[:150]}"
                log(f"  ✗ {p.name}：{msg}")
                results.append((p.name, False, msg))
            time.sleep(0.4)

    ok = sum(1 for _, s, _ in results if s)
    total = len(results)
    elapsed = time.time() - t0

    log("")
    log("=" * 60)
    log(f"完成：{ok}/{total} 成功   （{elapsed:.1f} 秒）")
    log("=" * 60)

    icon = "✅" if ok == total else ("⚠️" if ok else "❌")
    lines = [f"## {icon} Send to Kindle：{ok}/{total} 成功", "",
             f"模式：裝置憑證　目標：{'雲端書庫' if args.archive else '指定裝置'}"
             f"　耗時 {elapsed:.1f}s", "",
             "| 檔案 | 結果 |", "|---|---|"]
    for name, success, err in results:
        lines.append(f"| `{name}` | {'✅' if success else '❌ ' + err} |")
    for p, why in bad:
        lines.append(f"| `{p.name}` | ⏭ 跳過：{why} |")
    summary(lines)

    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())

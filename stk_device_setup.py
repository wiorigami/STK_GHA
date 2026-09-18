#!/usr/bin/env python3
"""
stk_device_setup.py — 一次性取得「裝置憑證」（給 GitHub Actions / CI 用）

為什麼需要這個：
  cookie 會輪替，不適合放 CI。但 Amazon 桌面 App 用的是 **裝置憑證**
  （RSA 私鑰 + adp_token），那是長期有效的，不會每次請求就換。
  取得一次，之後 CI 就能一直用，不需要瀏覽器、不需要更新任何東西。

這個流程只能手動跑一次（Amazon 會要求輸入密碼，無法自動化）。

────────────────────────────────────────────────────────────────────
用法（兩步驟，不必開著終端機等）
────────────────────────────────────────────────────────────────────

  步驟 1：產生登入網址
      python3 stk_device_setup.py

      會印出一個網址，並把 PKCE verifier 暫存起來。

  步驟 2：登入後，把導回的網址貼進來
      在瀏覽器打開那個網址 → 登入 → 被導到
      https://www.amazon.com/gp/sendtokindle?openid...authorization_code=...

      然後：
      python3 stk_device_setup.py --redirect '整串網址'

  完成後會印出兩個值，把它們加進 GitHub Secrets。

  想在同一個終端機裡一次做完，用：
      python3 stk_device_setup.py --interactive
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

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


PENDING_FILE = Path.home() / ".stk_oauth_pending.json"
DEVICE_FILE = Path("stk_device.json")
PENDING_TTL = 3600  # 1 小時


def cmd_start() -> int:
    """產生登入網址並暫存 verifier。"""
    from stkclient import OAuth2

    oauth = OAuth2()
    url = oauth.get_signin_url()

    PENDING_FILE.write_text(
        json.dumps({"verifier": oauth._verifier, "created": time.time()}),
        encoding="utf-8",
    )
    PENDING_FILE.chmod(0o600)

    print("=" * 72)
    print("  步驟 1／2 — 取得裝置憑證")
    print("=" * 72)
    print()
    print("【1】複製下面這個網址，在瀏覽器打開，登入你的 Amazon 帳號：")
    print()
    print(url)
    print()
    print("【2】登入後會跳到一個網址，開頭是：")
    print("     https://www.amazon.com/gp/sendtokindle?openid.assoc_handle=...")
    print("     頁面顯示 404 或空白都是正常的")
    print()
    print("【3】把那個網址從網址列【整串】複製下來，然後執行：")
    print()
    print(f"     python3 {Path(__file__).name} --redirect '貼在這裡'")
    print()
    print("─" * 72)
    print("⚠️  Amazon 會要求重新輸入密碼 —— 這是正常的，無法跳過。")
    print(f"⚠️  verifier 暫存在 {PENDING_FILE}（1 小時內有效）")
    print("─" * 72)
    return 0


def cmd_finish(redirect: str) -> int:
    """用暫存的 verifier 完成授權。"""
    from stkclient import OAuth2, api

    if not PENDING_FILE.is_file():
        print("✗ 找不到暫存的 verifier。請先執行：", file=sys.stderr)
        print(f"    python3 {Path(__file__).name}", file=sys.stderr)
        return 1

    try:
        pending = json.loads(PENDING_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("✗ 暫存檔損壞，請重新執行第一步。", file=sys.stderr)
        return 1

    age = time.time() - pending.get("created", 0)
    if age > PENDING_TTL:
        print(f"✗ 暫存的 verifier 已過期（{age/60:.0f} 分鐘前）。請重新執行第一步。",
              file=sys.stderr)
        return 1

    redirect = redirect.strip().strip("'\"")
    if "authorization_code" not in redirect:
        print("✗ 網址裡找不到 authorization_code。", file=sys.stderr)
        print("  請確認貼的是登入後被導到的那個【完整】網址。", file=sys.stderr)
        print("  正確的長相：https://www.amazon.com/gp/sendtokindle?openid.assoc_handle=...", file=sys.stderr)
        print("              ...&openid.oa2.authorization_code=XXXXXXXX&...", file=sys.stderr)
        return 1

    oauth = OAuth2()
    oauth._verifier = pending["verifier"]   # 用第一步存下來的 verifier

    print("→ 解析授權碼…")
    code = api._parse_authorization_code(redirect) if hasattr(api, "_parse_authorization_code") else None
    if not code:
        import urllib.parse
        q = urllib.parse.parse_qs(urllib.parse.urlparse(redirect).query)
        code = q["openid.oa2.authorization_code"][0]
    print(f"  ✓ authorization_code = {code[:12]}...")

    print("→ 交換 access token…")
    print("→ 註冊裝置…")
    try:
        client = oauth.create_client(redirect)
    except Exception as e:  # noqa: BLE001
        print(f"\n✗ 失敗：{type(e).__name__}: {str(e)[:300]}", file=sys.stderr)
        print("\n  常見原因：", file=sys.stderr)
        print("    • 授權碼已經用過（每次都要重新產生）", file=sys.stderr)
        print("    • 授權碼過期了（登入後要盡快貼）", file=sys.stderr)
        print("    • verifier 對不上（第一步跟第二步不是同一輪）", file=sys.stderr)
        print(f"\n  → 重新執行：python3 {Path(__file__).name}", file=sys.stderr)
        return 1

    # 存檔
    with open(DEVICE_FILE, "w", encoding="utf-8") as f:
        client.dump(f)
    DEVICE_FILE.chmod(0o600)
    PENDING_FILE.unlink(missing_ok=True)

    info = json.loads(client.dumps())["device_info"]
    key, token = info["device_private_key"], info["adp_token"]

    print()
    print("=" * 72)
    print("  ✓ 成功！裝置憑證已取得")
    print("=" * 72)
    print()
    print(f"完整資料：{DEVICE_FILE.resolve()}（權限 600，請保管好）")
    print(f"帳號：{info.get('name', '?')}")
    print(f"裝置名稱：{info.get('user_device_name', '?')}")
    print()
    print("─" * 72)
    print("  接下來：把下面兩個值加進 GitHub Secrets")
    print("  repo → Settings → Secrets and variables → Actions → New repository secret")
    print("─" * 72)
    print()
    print("【Secret 1】名稱：STK_DEVICE_PRIVATE_KEY")
    print("           值（整段複製，含 BEGIN / END 兩行）：")
    print()
    print(key)
    print()
    print("【Secret 2】名稱：STK_ADP_TOKEN")
    print("           值：")
    print()
    print(token)
    print()
    print("─" * 72)
    print()
    print("這兩個值長期有效（不像 cookie 會輪替），設定一次就好。")
    print("要撤銷：Amazon → 管理您的內容與裝置 → 裝置 → 刪除對應裝置。")
    print()
    print("驗證看看：python3 stk_device_send.py --list-devices")
    return 0


def cmd_interactive() -> int:
    """舊的互動模式：在同一個終端機裡一路做完。"""
    from stkclient import OAuth2

    oauth = OAuth2()
    url = oauth.get_signin_url()

    print("=" * 72)
    print("  互動模式 — 在同一個終端機完成")
    print("=" * 72)
    print()
    print("【1】在瀏覽器打開這個網址並登入：")
    print()
    print(url)
    print()
    print("【2】登入後把導回的【整串】網址貼到下面，按 Enter")
    print()

    try:
        redirect = input("網址：").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n取消。")
        return 1

    if "authorization_code" not in redirect:
        print("\n✗ 網址裡找不到 authorization_code。")
        return 1

    print("\n→ 交換 token 並註冊裝置…")
    try:
        client = oauth.create_client(redirect)
    except Exception as e:  # noqa: BLE001
        print(f"\n✗ 失敗：{type(e).__name__}: {str(e)[:300]}")
        return 1

    with open(DEVICE_FILE, "w", encoding="utf-8") as f:
        client.dump(f)
    DEVICE_FILE.chmod(0o600)

    info = json.loads(client.dumps())["device_info"]
    print()
    print("=" * 72)
    print("  ✓ 成功！")
    print("=" * 72)
    print(f"資料存到：{DEVICE_FILE.resolve()}")
    print()
    print("【Secret 1】STK_DEVICE_PRIVATE_KEY =")
    print(info["device_private_key"])
    print()
    print("【Secret 2】STK_ADP_TOKEN =")
    print(info["adp_token"])
    return 0


def main(argv: list[str] | None = None) -> int:
    _fix_ssl_certs()

    ap = argparse.ArgumentParser(
        description="取得 Send to Kindle 裝置憑證（給 CI 用）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="沒給參數 = 產生登入網址（步驟 1）",
    )
    ap.add_argument("--redirect", metavar="URL", help="登入後導回的完整網址（步驟 2）")
    ap.add_argument("--interactive", action="store_true", help="在同一個終端機完成（舊模式）")
    args = ap.parse_args(argv)

    try:
        import stkclient  # noqa: F401
    except ImportError:
        print("✗ 需要 stkclient：pip install stkclient", file=sys.stderr)
        return 3

    if args.interactive:
        return cmd_interactive()
    if args.redirect:
        return cmd_finish(args.redirect)
    return cmd_start()


if __name__ == "__main__":
    sys.exit(main())

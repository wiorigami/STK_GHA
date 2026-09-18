# Bear Blog 中文週報 → Kindle（GitHub Actions）

每週六早上自動抓取 Bear Blog 中文文章 → 打包成 EPUB → 送到你的 Kindle。

```
週六 08:00 (台北)
      ↓
抓取 bearblog.dev 中文文章（最多 50 篇）
      ↓
清洗 HTML、內嵌圖片、MathML 轉純文字
      ↓
calibre ebook-convert → EPUB
      ↓
上傳 Artifact（保存 90 天）
      ↓
送到 Kindle（雲端書庫）
```

---

## 部署步驟

### 1. 解壓縮到你的 repo

把這個壓縮檔解開，內容直接放到 repo 根目錄：

```
你的repo/
├── .github/workflows/bear-weekly.yml   ← workflow
├── bear_weekly.py                      ← 產生 EPUB
├── stk_device_send.py                  ← 送到 Kindle
├── stk_device_setup.py                 ← 取得憑證（只用一次）
└── README.md                           ← 本檔
```

```bash
unzip bear-weekly-kindle.zip -d /path/to/your-repo
cd /path/to/your-repo
git add . && git commit -m "add bear weekly + kindle" && git push
```

### 2. 設定 Secrets

repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

| Secret 名稱 | 值 |
|---|---|
| `STK_DEVICE_PRIVATE_KEY` | RSA 私鑰，**完整 PEM 含 `BEGIN`/`END` 兩行** |
| `STK_ADP_TOKEN` | adp token（`{enc:...}{key:...}{iv:...}` 那一長串） |

> 這兩個是 Amazon 桌面 App 用的**裝置憑證**，長期有效、不會輪替。
> 取得方式：在本機跑 `python3 stk_device_setup.py`（見下方）。

### 3. 測試

repo → **Actions** → 左側 **bear-weekly-epub** → **Run workflow**

執行完看：
- **Artifacts** 應該有 `bear-auto-epub`
- 你的 Kindle 書庫應該出現「Bear Blog 中文週報 YYYY-MM-DD」

---

## 之後怎麼運作

| 觸發 | 時機 |
|---|---|
| **自動** | 每週六 08:00（台北時間） |
| **手動** | Actions → bear-weekly-epub → Run workflow |

要改時間，編輯 `.github/workflows/bear-weekly.yml` 的 cron：

```yaml
on:
  schedule:
    - cron: "0 0 * * 6"   # UTC 週六 00:00 = 台北 08:00
```

cron 是 **UTC**，換算：台北時間 − 8 小時。

| 想要的台北時間 | cron |
|---|---|
| 每天 08:00 | `0 0 * * *` |
| 每週六 08:00 | `0 0 * * 6` |
| 每週一 07:30 | `30 23 * * 0` |

---

## 取得裝置憑證（一次性）

只有第一次需要做。憑證長期有效，除非你去 Amazon 把它撤銷。

```bash
pip install stkclient

# 步驟 1：產生登入網址
python3 stk_device_setup.py

# 在瀏覽器打開那個網址 → 登入 → 把導回的完整網址貼回來
python3 stk_device_setup.py --redirect 'https://www.amazon.com/gp/sendtokindle?openid...'
```

腳本會印出兩個值，就是上面要放進 Secrets 的東西。

> ⚠️ **Amazon 會要求重新輸入密碼**，這一步無法自動化。

---

## 調整行為

### 改文章數量 / 來源

編輯 `bear_weekly.py` 開頭的設定：

```python
TARGET = 50                 # 目標文章數
MAX_RANDOM_ATTEMPTS = 60    # 隨機填充最多嘗試次數
BLOG_FEED_PAGES = (0, 1)    # 每個部落格抓幾頁 feed
PER_BLOG_CAP = 3            # 每個部落格最多收錄幾篇
```

### 改書名 / 作者

編輯 workflow 的 `Send to Kindle` 步驟：

```yaml
TITLE="Bear Blog 中文週報 ${STAMP}"
--author "Bear 週報"
```

### 只送到特定裝置（不進雲端書庫）

```yaml
python3 stk_device_send.py \
  --no-archive \
  --device YOUR_DEVICE_SERIAL \
  "${FILES[@]}"
```

裝置序號查法：

```bash
STK_DEVICE_PRIVATE_KEY="..." STK_ADP_TOKEN="..." python3 stk_device_send.py --list-devices
```

---

## 疑難排解

| 症狀 | 原因 / 解法 |
|---|---|
| `缺少 Secrets：STK_DEVICE_PRIVATE_KEY` | 兩個 secret 名稱要完全一致 |
| `憑證載入失敗` | PEM 要完整含 `BEGIN`/`END` 兩行 |
| `documentMetadata.author` 驗證失敗 | author 不能是空字串（workflow 已固定給值） |
| `Fetch & build EPUB` 失敗 | bearblog.dev 連不上，或當週中文文章不足；重跑一次 |
| 收到「警告: 僅收集到 N 篇」 | 正常，文章數不足時會全數打包 |
| Kindle 沒收到 | 檢查書庫的「文件」分類；確認裝置有連網 |
| 之前能用，突然失敗 | 憑證被撤銷 → 重跑 `stk_device_setup.py` 換新的 |

---

## 費用

GitHub Actions 對 **public repo 免費**，private repo 每月有 2000 分鐘額度。
這個 workflow 每次約 5-10 分鐘，一個月 4-5 次，用量很小。

---

## 注意事項

- **裝置憑證等於帳號存取權**，private repo 再用；public repo 不建議
- 這用 Amazon 的**非公開 API**，官方隨時可能改動
- 抓取 bearblog.dev 有禮貌性延遲，請不要調太高頻率
- 僅供推送**你自己的文件**到**你自己的裝置**

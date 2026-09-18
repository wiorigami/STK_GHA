#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bear Blog 中文週報 → EPUB（GitHub Actions 片段，無 SMTP）

流程: 抓取文章 → 內容清洗 → 拼單一 HTML → calibre ebook-convert → EPUB
輸出: output/bear-auto(dd-mm-yy).epub  (交由 Actions artifact 上傳)

資料源(平台 feed 固定 20 條且無分頁, 已查證原始碼):
  1. 4 個 discover feed (zh-trending / zh-newest / all-trending / all-newest)
  2. 中文部落格各自的 /feed/ (每頁10篇, 有分頁)
  3. /discover/random-post/ 隨機填充兜底

僅用 Python 標準庫 + 系統命令(ffmpeg 轉 webp, ebook-convert 轉 epub)。
"""

import base64
import html as html_mod
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from html.parser import HTMLParser

# ---------------- 設定 ----------------
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

OUT_DIR = os.environ.get(
    "BEAR_OUTPUT_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "output"))

FEEDS = {
    "zh_trending": "https://bearblog.dev/discover/feed/?lang=zh",
    "zh_newest":   "https://bearblog.dev/discover/feed/?lang=zh&newest=1",
    "all_trending": "https://bearblog.dev/discover/feed/",
    "all_newest":   "https://bearblog.dev/discover/feed/?newest=1",
}
RANDOM_POST_URL = "https://bearblog.dev/discover/random-post/"

TARGET = 50                 # 目標文章數
MAX_RANDOM_ATTEMPTS = 60    # 隨機填充(兜底)最多嘗試次數
RANDOM_SLEEP = 0.6          # 隨機填充間隔(對伺服器友善)
BLOG_FEED_PAGES = (0, 1)    # 每個中文部落格抓 2 頁 feed(每頁10篇)
PER_BLOG_CAP = 3            # 最終選篇時每個部落格最多收錄篇數(保持多樣性)

ATOM = "http://www.w3.org/2005/Atom"
MATHML = "http://www.w3.org/1998/Math/MathML"

# ---------------- 小工具 ----------------
def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg),
          flush=True)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟隨重定向, 用來抓 random-post 的 Location"""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(url, retries=3, timeout=15):
    """帶瀏覽器 UA 的 GET, 失敗重試 (bearblog 無 UA 會 403)"""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    last = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1 + 2 * i)
    raise last


def fetch_location(url):
    """只取 302 Location, 不跟隨"""
    opener = urllib.request.build_opener(NoRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        opener.open(req, timeout=15)
        return None
    except urllib.error.HTTPError as e:
        return e.headers.get("Location")
    except Exception:  # noqa: BLE001
        return None


def cjk_ratio(s):
    if not s:
        return 0.0
    c = sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")
    return c / len(s)


def is_chinese(title, content_text):
    return cjk_ratio(title) >= 0.4 or cjk_ratio(content_text) >= 0.25


def blog_root_of(url):
    p = urllib.parse.urlsplit(url)
    return "%s://%s" % (p.scheme or "https", p.netloc)


# ---------------- 資料收集 ----------------
def parse_feed(data):
    entries = []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return entries
    for e in root.findall("{%s}entry" % ATOM):
        t = e.find("{%s}title" % ATOM)
        l = e.find("{%s}link" % ATOM)
        p = e.find("{%s}published" % ATOM)
        a = e.find("{%s}author" % ATOM)
        c = e.find("{%s}content" % ATOM)
        title = (t.text or "").strip() if t is not None else ""
        link = l.get("href") if l is not None else ""
        pub = (p.text or "").strip() if p is not None else ""
        author = ""
        if a is not None:
            n = a.find("{%s}name" % ATOM)
            author = (n.text or "").strip() if n is not None else ""
        content = (c.text or "") if c is not None else ""
        if link and title:
            entries.append({
                "url": link, "title": title, "author": author,
                "published": pub, "content": content,
            })
    return entries


def extract_post_page(raw, url):
    """從 Bear 文章頁抽取 標題/部落格名/日期/正文HTML"""
    text = raw.decode("utf-8", "replace")

    def meta(prop):
        m = re.search(r'<meta\s+property="%s"\s+content="([^"]*)"'
                      % re.escape(prop), text, re.I)
        return html_mod.unescape(m.group(1)).strip() if m else ""

    title = meta("og:title")
    blog = meta("og:site_name")
    m = re.search(r'<time\s+datetime="([^"]+)"', text, re.I)
    published = m.group(1) if m else ""

    mm = re.search(r"<main\b[^>]*>(.*?)</main>", text, re.S | re.I)
    body = mm.group(1) if mm else ""
    # 去掉標題 <h1> 與日期區塊 <p><i><time>...</time></i></p>
    body = re.sub(r"<h1\b[^>]*>.*?</h1>", "", body, count=1, flags=re.S | re.I)
    body = re.sub(r"<p\b[^>]*>\s*<i\b[^>]*>\s*<time\b[^>]*>.*?</time>\s*</i>\s*</p>",
                  "", body, count=1, flags=re.S | re.I)
    body = re.sub(r"<p\b[^>]*>.*?<time\b[^>]*>.*?</time>.*?</p>",
                  "", body, count=1, flags=re.S | re.I)
    return title, blog, published, body


def fill_from_blog_feeds(zh, pool):
    """從已知中文文章的部落格 feed 擴充候選(每頁10篇, 有分頁)"""
    blogs = []
    for p in list(zh.values()):
        root = blog_root_of(p["url"])
        if root not in blogs:
            blogs.append(root)
    log("發現 %d 個中文部落格, 抓取其 feed 擴充候選" % len(blogs))
    for root in blogs:
        try:
            for page in BLOG_FEED_PAGES:
                url = "%s/feed/?page=%d" % (root, page)
                data = fetch(url, retries=2, timeout=12)
                for e in parse_feed(data):
                    if e["url"] in pool:
                        continue
                    pool[e["url"]] = e
                    plain = re.sub(r"<[^>]+>", "", e["content"])
                    if is_chinese(e["title"], plain):
                        zh[e["url"]] = e
        except Exception as ex:  # noqa: BLE001
            log("部落格 feed 失敗 %s: %s" % (root, ex))
        time.sleep(0.3)
    return len(zh)


def parse_pub(iso):
    s = (iso or "").strip()
    if not s:
        return datetime.min
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass
    return datetime.min


def pub_date_str(iso):
    d = parse_pub(iso)
    if d is not datetime.min:
        return d.strftime("%Y-%m-%d")
    m = re.match(r"(\d{4}-\d{2}-\d{2})", iso or "")
    return m.group(1) if m else ""


def select_posts(zh, target, cap=PER_BLOG_CAP):
    """按時間新→舊排序, 每部落格最多 cap 篇, 不足則放寬補滿"""
    posts = sorted(zh.values(),
                   key=lambda p: parse_pub(p["published"]), reverse=True)
    by_blog = {}
    chosen = []
    for p in posts:
        b = blog_root_of(p["url"])
        if by_blog.get(b, 0) >= cap:
            continue
        by_blog[b] = by_blog.get(b, 0) + 1
        chosen.append(p)
        if len(chosen) >= target:
            break
    if len(chosen) < target:
        for p in posts:
            if p in chosen:
                continue
            chosen.append(p)
            if len(chosen) >= target:
                break
    return chosen[:target]


# ---------------- MathML → Unicode 線性數學式 ----------------
MATH_RE = re.compile(r"<math\b[^>]*>(.*?)</math>", re.S | re.I)
SUBS = str.maketrans("0123456789", "₀₁₂₃₄₅₆₇₈₉")
SUPS = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")


def mathml_to_text(elem):
    """把 MathML 元素轉成可讀線性文字 (如 k₁y(a)+k₂y′(a)=0)"""
    tag = elem.tag.split("}")[-1]
    if tag in ("mi", "mn", "mtext", "ms", "mo"):
        return "".join(elem.itertext())
    if tag == "mspace":
        return " "
    if tag == "mrow":
        return "".join(mathml_to_text(c) for c in elem)
    if tag == "mfrac":
        kids = [mathml_to_text(c) for c in elem]
        num = kids[0] if len(kids) > 0 else ""
        den = kids[1] if len(kids) > 1 else ""
        return "(%s)/(%s)" % (num, den)
    if tag == "msqrt":
        return "√(" + "".join(mathml_to_text(c) for c in elem) + ")"
    if tag == "mroot":
        kids = [mathml_to_text(c) for c in elem]
        base = kids[0] if kids else ""
        idx = kids[1] if len(kids) > 1 else ""
        return "(%s)^(1/%s)" % (base, idx)
    if tag == "msup":
        kids = [mathml_to_text(c) for c in elem]
        base = kids[0] if kids else ""
        exp = kids[1] if len(kids) > 1 else ""
        if exp in ("′", "″", "‴"):
            return base + exp
        return "%s^%s" % (base, exp)
    if tag == "msub":
        kids = [mathml_to_text(c) for c in elem]
        if len(kids) > 1:
            return "%s_%s" % (kids[0], kids[1])
        return kids[0] if kids else ""
    if tag == "msubsup":
        kids = [mathml_to_text(c) for c in elem]
        s = "%s_%s" % (kids[0], kids[1]) if len(kids) > 1 else (kids[0] if kids else "")
        if len(kids) > 2:
            s += "^%s" % kids[2]
        return s
    if tag in ("munder", "mover", "munderover"):
        return "".join(mathml_to_text(c) for c in elem)
    if tag == "mtable":
        return "\n".join(mathml_to_text(c) for c in elem)
    if tag == "mtr":
        return " ".join(mathml_to_text(c) for c in elem)
    if tag in ("mtd", "mpadded", "mphantom", "mstyle", "merror"):
        return "".join(mathml_to_text(c) for c in elem)
    if tag == "mfenced":
        op = elem.get("open", "(")
        cl = elem.get("close", ")")
        return op + "".join(mathml_to_text(c) for c in elem) + cl
    if tag == "semantics":
        for c in elem:
            return mathml_to_text(c)
        return ""
    if tag in ("annotation", "annotation-xml", "none"):
        return ""
    return "".join(mathml_to_text(c) for c in elem)


def prettify_math(txt):
    """下標/上標數字改用 Unicode 字符: k_1 → k₁, x^2 → x²"""
    txt = re.sub(r"_(\d)", lambda m: m.group(1).translate(SUBS), txt)
    txt = re.sub(r"\^(\d)", lambda m: m.group(1).translate(SUPS), txt)
    return txt


def preprocess_math(raw_html):
    """把 <math> 區塊轉成線性文字, display=block 用區塊包裝"""
    def repl(m):
        open_tag = m.group(0).split(">", 1)[0]
        is_block = 'display="block"' in open_tag
        try:
            root = ET.fromstring(
                '<math xmlns="%s">%s</math>' % (MATHML, m.group(1)))
            txt = prettify_math(mathml_to_text(root)).strip()
        except Exception:  # noqa: BLE001
            return m.group(0)
        if not txt:
            return ""
        txt = html_mod.escape(txt, quote=False)
        if is_block:
            return '<div class="math-block">%s</div>' % txt
        return '<span class="math">%s</span>' % txt
    return MATH_RE.sub(repl, raw_html or "")


# ---------------- HTML 清洗 (生成合法 XHTML) ----------------
VOID_TAGS = {"br", "hr", "img"}
ALLOWED = {"p", "div", "span", "h1", "h2", "h3", "h4", "h5", "h6",
           "ul", "ol", "li", "blockquote", "pre", "code", "em", "strong",
           "b", "i", "u", "del", "s", "a", "img", "br", "hr",
           "figure", "figcaption", "table", "thead", "tbody", "tr", "td", "th",
           "sub", "sup", "mark", "small", "kbd", "q", "cite", "abbr", "ins",
           "dl", "dt", "dd", "address", "section", "article", "aside",
           "details", "summary", "header", "footer"}


class Sanitizer(HTMLParser):
    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.out = []
        self.stack = []
        self.skip = 0          # style/script 內容跳過計數

    def _abs(self, u):
        u = (u or "").strip()
        if not u:
            return ""
        return urllib.parse.urljoin(self.base_url, u)

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in ("style", "script"):
            self.skip += 1
            return
        if self.skip:
            return
        if tag not in ALLOWED:
            return
        d = dict(attrs)
        attr = ""
        if tag == "a":
            href = self._abs(d.get("href"))
            if href.lower().startswith(("http://", "https://")):
                attr = ' href="%s"' % html_mod.escape(href, quote=True)
        elif tag == "img":
            src = self._abs(d.get("src"))
            alt = html_mod.escape(d.get("alt") or "", quote=True)
            if src.lower().startswith(("http://", "https://")):
                attr = ' src="%s"' % html_mod.escape(src, quote=True)
            attr += ' alt="%s"' % alt
        elif tag in ("div", "span"):
            cls = (d.get("class") or "").strip()
            if cls:
                attr = ' class="%s"' % html_mod.escape(cls, quote=True)
        if tag in VOID_TAGS:
            self.out.append("<%s%s/>" % (tag, attr))
            return
        self.out.append("<%s%s>" % (tag, attr))
        self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("style", "script"):
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag not in ALLOWED or tag in VOID_TAGS:
            return
        if tag in self.stack:
            while self.stack:
                t = self.stack.pop()
                self.out.append("</%s>" % t)
                if t == tag:
                    break

    def handle_data(self, data):
        if self.skip:
            return
        data = data.replace("{{ email-signup }}", "")
        self.out.append(html_mod.escape(data, quote=False))

    def finish(self):
        while self.stack:
            t = self.stack.pop()
            self.out.append("</%s>" % t)
        return "".join(self.out)


def sanitize(raw_html, base_url):
    s = Sanitizer(base_url)
    try:
        s.feed(preprocess_math(raw_html or ""))
        s.close()
    except Exception:  # noqa: BLE001
        pass
    return s.finish()


def wrap_bare_text(content):
    """把 content 內裸文字包進 <p>, 避免無樣式的文字塊。

    裸文字 = 片段開頭、或閉合標籤之後的直屬文字
    (標籤內部的文字不算, 避免把 <p>正文</p> 誤判)
    """
    # 沒有裸文字就原樣返回(避免 ET 重序列化改變屬性順序)
    if not re.search(r"(^|</[A-Za-z][A-Za-z0-9]*>)\s*[^<\s]", content):
        return content
    try:
        ET.register_namespace("", "http://www.w3.org/1999/xhtml")
        root = ET.fromstring(
            '<div xmlns="http://www.w3.org/1999/xhtml">%s</div>' % content)
    except Exception:  # noqa: BLE001
        return content
    xns = "{http://www.w3.org/1999/xhtml}"
    # 前導裸文字 (片段開頭)
    if (root.text or "").strip():
        p = ET.Element(xns + "p", {"class": "lead"})
        p.text = root.text
        root.text = ""
        root.insert(0, p)
    # 子元素後方的裸文字
    for i, ch in enumerate(list(root)):
        if (ch.tail or "").strip():
            p = ET.Element(xns + "p")
            p.text = ch.tail
            ch.tail = ""
            root.insert(i + 1, p)
    return "".join(ET.tostring(ch, encoding="unicode", method="xml")
                   for ch in root)


def strip_leading_title(content, title):
    """移除正文開頭與章節標題重複的裸文字"""
    t_esc = html_mod.escape(title or "", quote=False)
    if not t_esc:
        return content
    m = re.match(r"^\s*%s\s*(?=<)" % re.escape(t_esc), content, re.S)
    return content[m.end():] if m else content


# ---------------- 圖片嵌入 ----------------
IMG_EXT_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml"}
MIME_EXT = {v: k for k, v in IMG_EXT_MIME.items()}
IMG_TAG_RE = re.compile(r'<img\s+([^>]*?)/>')
MAX_IMG_BYTES = 10 * 1024 * 1024


def _img_attrs(attr_str):
    src_m = re.search(r'src="([^"]*)"', attr_str)
    alt_m = re.search(r'alt="([^"]*)"', attr_str)
    return (src_m.group(1) if src_m else ""), (alt_m.group(1) if alt_m else "")


def download_image(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    last = None
    for i in range(3):
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                data = r.read()
                ct = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            return data, ct
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def convert_webp_to_jpg(data):
    """用 ffmpeg 把 webp 轉 jpg (Kindle 相容)"""
    wp = None
    jp = None
    try:
        fd, wp = tempfile.mkstemp(suffix=".webp")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        jp = wp + ".jpg"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error",
                        "-i", wp, "-f", "image2", jp],
                       check=True, timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(jp, "rb") as f:
            return f.read(), "image/jpeg"
    finally:
        for p in (wp, jp):
            if p:
                try:
                    os.remove(p)
                except OSError:
                    pass


def resolve_image(src, images, img_map):
    """下載/解碼圖片, 回傳 (local_fname, mime) 或 (None, None)"""
    if src in img_map:
        return img_map[src]
    try:
        if src.startswith("data:image/"):
            m = re.match(r"data:(image/[a-z0-9.+-]+);base64,(.*)", src, re.S)
            if not m:
                return None, None
            mt = m.group(1).lower()
            data = base64.b64decode(m.group(2))
        elif src.lower().startswith(("http://", "https://")):
            data, ct = download_image(src)
            mt = ct if ct.startswith("image/") else ""
            if not mt:
                path = urllib.parse.urlsplit(src).path.lower()
                for ext, mime in IMG_EXT_MIME.items():
                    if path.endswith(ext):
                        mt = mime
                        break
            if not mt or not mt.startswith("image/"):
                return None, None
        else:
            return None, None
        if len(data) > MAX_IMG_BYTES:
            return None, None
        if mt == "image/webp":
            try:
                data, mt = convert_webp_to_jpg(data)
            except Exception:  # noqa: BLE001
                pass  # 轉換失敗則保留 webp 原檔
        ext = MIME_EXT.get(mt, ".img")
        fname = "images/img%03d%s" % (len(images) + 1, ext)
        images.append((fname, data, mt))
        img_map[src] = (fname, mt)
        return fname, mt
    except Exception:  # noqa: BLE001
        return None, None


def embed_images(xhtml, images, img_map):
    """把 <img src> 換成本地嵌入檔; 失敗的圖片直接移除 (屬性順序無關)"""
    def repl(m):
        src_raw, alt_raw = _img_attrs(m.group(1))
        src = html_mod.unescape(src_raw)
        alt = html_mod.unescape(alt_raw)
        fname, _mt = resolve_image(src, images, img_map)
        if not fname:
            log("圖片略過(下載失敗或格式不支援): %s" % src[:80])
            return ""
        return '<img src="%s" alt="%s"/>' % (fname, html_mod.escape(alt, quote=True))
    return IMG_TAG_RE.sub(repl, xhtml)


# ---------------- HTML 拼裝 + Calibre 轉換 ----------------
BOOK_CSS = """
body { font-family: Georgia, "Songti SC", serif; line-height: 1.7; color: #222; margin: 1em; }
.cover-page { text-align: center; margin-top: 25%; page-break-after: always; }
.cover-kicker { letter-spacing: 0.2em; color: #999; font-size: 0.8em; margin-bottom: 1em; }
.cover-title { font-size: 2em; margin: 0.3em 0; }
.cover-date { font-size: 1.3em; color: #444; margin: 0.6em 0; }
.cover-count { color: #666; margin: 0.8em 0; }
.cover-note { color: #999; font-size: 0.8em; margin-top: 2em; line-height: 1.6; }
article.post { margin: 0 0 2em 0; page-break-before: always; }
h1.post-title { font-size: 1.5em; line-height: 1.35; margin: 0.3em 0 0.4em 0; }
p.meta { color: #888; font-size: 0.82em; border-bottom: 1px solid #e2e2e2; padding-bottom: 0.7em; margin-bottom: 1.1em; }
.meta a { color: #888; }
.content p { margin: 0.6em 0; }
.content h2 { font-size: 1.25em; margin: 1.2em 0 0.4em 0; }
.content h3 { font-size: 1.1em; margin: 1em 0 0.3em 0; }
.content blockquote { border-left: 3px solid #d5d5d5; margin: 0.8em 0; padding: 0.1em 1em; color: #555; }
.content pre { background: #f6f7f8; padding: 0.7em; border-radius: 4px; overflow-x: auto; font-size: 0.85em; }
.content img { max-width: 100%; height: auto; }
.content a { color: #1a73a8; text-decoration: none; }
.content ul, .content ol { margin: 0.5em 0; padding-left: 1.6em; }
p.lead { font-weight: bold; color: #333; margin-bottom: 0.2em; }
.math-block { text-align: center; margin: 0.9em 0; overflow-wrap: break-word; }
.math { overflow-wrap: break-word; }
hr { border: none; border-top: 1px solid #e0e0e0; margin: 1.5em 0; }
"""


def build_html_book(posts, stamp_ddmmyy):
    """拼出單一 HTML 文件; 圖片 src 用相對路徑, 回傳 (html_text, images)"""
    n = len(posts)
    parts = ['<!DOCTYPE html>\n<html lang="zh">\n<head>\n<meta charset="utf-8"/>\n',
             "<title>Bear Blog 中文週報 %s</title>\n" % stamp_ddmmyy,
             "<style>%s</style>\n</head>\n<body>\n" % BOOK_CSS]
    parts.append('<div class="cover-page">\n'
                 '<div class="cover-kicker">BEAR BLOG · 中文週報</div>\n'
                 '<h1 class="cover-title">Bear Blog 中文週報</h1>\n'
                 '<p class="cover-date">%s</p>\n'
                 '<p class="cover-count">共 %d 篇文章</p>\n'
                 '<p class="cover-note">內容來源：bearblog.dev/discover/?lang=zh<br/>'
                 '每週六 08:00 自動生成</p>\n'
                 '</div>\n' % (stamp_ddmmyy, n))
    images = []
    img_map = {}
    for p in posts:
        content = sanitize(p["content"], p["url"])
        content = strip_leading_title(content, p["title"])
        content = wrap_bare_text(content)
        content = embed_images(content, images, img_map)
        meta_line = html_mod.escape(
            "%s · %s" % (p["author"] or "Bear Blog", pub_date_str(p["published"])))
        parts.append('<article class="post">\n'
                     '<h1 class="post-title">%s</h1>\n'
                     '<p class="meta">%s · <a href="%s">原文</a></p>\n'
                     '<div class="content">%s</div>\n'
                     '</article>\n' % (
                         html_mod.escape(p["title"]),
                         meta_line,
                         html_mod.escape(p["url"], quote=True),
                         content))
    parts.append("</body>\n</html>")
    return "".join(parts), images


def convert_html_to_epub(html_text, images, out_path):
    """用 calibre 的 ebook-convert 把 HTML 轉成 EPUB"""
    tmpdir = tempfile.mkdtemp(prefix="bearbook_")
    try:
        with open(os.path.join(tmpdir, "book.html"), "w", encoding="utf-8") as f:
            f.write(html_text)
        if images:
            os.makedirs(os.path.join(tmpdir, "images"), exist_ok=True)
            for fname, data, _mt in images:
                with open(os.path.join(tmpdir, fname), "wb") as f:
                    f.write(data)
        cmd = ["ebook-convert",
               os.path.join(tmpdir, "book.html"), out_path,
               "--level1-toc", "//h:h1[contains(@class,'post-title')]",
               "--chapter", "//h:h1[contains(@class,'post-title')]",
               "--base-font-size", "12",
               "--pretty-print"]
        subprocess.run(cmd, check=True, timeout=600,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------- 主流程 ----------------
def main():
    stamp = datetime.now().strftime("%d-%m-%y")
    fname = "bear-auto(%s).epub" % stamp
    os.makedirs(OUT_DIR, exist_ok=True)

    # 1. 四個 discover feed
    pool = {}
    zh = {}
    for name, url in FEEDS.items():
        try:
            data = fetch(url)
            added = 0
            for e in parse_feed(data):
                if e["url"] in pool:
                    continue
                pool[e["url"]] = e
                if name.startswith("zh_"):
                    zh[e["url"]] = e
                    added += 1
                else:
                    plain = re.sub(r"<[^>]+>", "", e["content"])
                    if is_chinese(e["title"], plain):
                        zh[e["url"]] = e
                        added += 1
            log("feed %s: 收錄中文 +%d (池內 %d, 中文累計 %d)"
                % (name, added, len(pool), len(zh)))
        except Exception as ex:  # noqa: BLE001
            log("feed %s 失敗: %s" % (name, ex))

    if not pool:
        log("所有 feed 均失敗, 中止")
        sys.exit(1)

    # 2. 部落格 feed 擴充
    fill_from_blog_feeds(zh, pool)
    log("部落格擴充後中文候選: %d" % len(zh))

    # 3. 隨機填充(兜底)
    attempts = 0
    while len(zh) < TARGET and attempts < MAX_RANDOM_ATTEMPTS:
        attempts += 1
        try:
            loc = fetch_location(RANDOM_POST_URL)
            if not loc:
                time.sleep(RANDOM_SLEEP)
                continue
            loc = urllib.parse.urljoin(RANDOM_POST_URL, loc)
            if loc.startswith("http://"):
                loc = "https://" + loc[7:]
            if loc in pool:
                time.sleep(RANDOM_SLEEP)
                continue
            pool[loc] = None
            raw = fetch(loc)
            title, blog, published, body = extract_post_page(raw, loc)
            if not title or not body.strip():
                time.sleep(RANDOM_SLEEP)
                continue
            plain = re.sub(r"<[^>]+>", "", body)
            if not is_chinese(title, plain):
                time.sleep(RANDOM_SLEEP)
                continue
            zh[loc] = {"url": loc, "title": title, "author": blog,
                       "published": published, "content": body}
            log("隨機填充 +1: %s (嘗試 %d, 現 %d/%d)"
                % (title[:30], attempts, len(zh), TARGET))
        except Exception as ex:  # noqa: BLE001
            log("隨機嘗試 %d 失敗: %s" % (attempts, ex))
        time.sleep(RANDOM_SLEEP)

    posts = select_posts(zh, TARGET)
    if len(posts) < TARGET:
        log("警告: 僅收集到 %d 篇中文文章 (目標 %d), 將全數打包"
            % (len(posts), TARGET))
    if not posts:
        log("沒有文章可打包, 中止")
        sys.exit(1)

    # 4. HTML → EPUB (calibre)
    path = os.path.join(OUT_DIR, fname)
    html_text, images = build_html_book(posts, stamp)
    log("使用 calibre ebook-convert 轉換 (%d 篇, %d 張圖片)"
        % (len(posts), len(images)))
    convert_html_to_epub(html_text, images, path)
    log("EPUB 已寫入 %s (%d bytes)" % (path, os.path.getsize(path)))
    log("===== 完成 =====")


if __name__ == "__main__":
    main()

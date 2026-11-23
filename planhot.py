from html import escape, unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urldefrag, urlencode

from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import re
import subprocess
import sys

# Only links to article pages on the issuing authority's host enter the review queue.
SOURCES = [
    ("上海市规划和自然资源局行业新闻", "上海", "https://ghzyj.sh.gov.cn/hyxw/", "/hyxw/"),
    ("广州市规划和自然资源局工作动态", "广州", "https://ghzyj.gz.gov.cn/zwgk/gzdt/", "/zwgk/gzdt/"),
    ("住房和城乡建设部", "", "https://www.mohurd.gov.cn/gongkai/zc/wjk/", "/gongkai/zc/wjk/art/"),
    ("中国城市规划协会", "", "https://www.cacp.org.cn/", "/zccxghs/"),
    ("上海市规划和自然资源局", "上海", "https://ghzyj.sh.gov.cn/zcwj/cxgh/", "/zcwj/cxgh/"),
    ("广州市规划和自然资源局", "广州", "https://ghzyj.gz.gov.cn/sjb/zw/zcfg/", "/sjb/zw/zcfg/content/"),
    ("深圳市规划和自然资源局", "深圳", "https://pnr.sz.gov.cn/xxgk/zcwj/gfxwj2/", "/"),
    ("陕西省自然资源厅", "陕西", "https://zrzyt.shaanxi.gov.cn/zcfg/zfwj/", "/"),
    ("河南省自然资源厅", "河南", "https://dnr.henan.gov.cn/gk/zc/", "/"),
    ("甘肃省自然资源厅", "甘肃", "https://zrzy.gansu.gov.cn/", "/zrzy/"),
]
TZ = timezone(timedelta(hours=8))
NOTICE = re.compile("废止|失效|清理|替代|代替|停止执行|有效期|到期")
TOPIC = re.compile("城乡规划|国土空间|城市更新|村庄规划|乡村规划|详细规划|控制性详细|规划师|城市设计|城市体检|实景三维|一张图|规划技术")
CATEGORIES = ["政策文件", "标准规范", "注规考试", "规划实践", "规划技术", "研究解读", "其他"]
TODAY = datetime.now(TZ).date()


class Anchors(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.active = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            attrs = dict(attrs)
            self.active = [attrs.get("href") or "", attrs.get("title") or "", []]

    def handle_data(self, data):
        if self.active is not None:
            self.active[2].append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.active is not None:
            href, title, parts = self.active
            self.links.append((href, " ".join((title or "".join(parts)).split())))
            self.active = None


def fetch_page(page):
    result = subprocess.run(["curl", "--silent", "--show-error", "--fail", "--location",
                             "--proto", "=https", "--proto-redir", "=https", "--max-redirs", "3",
                             "--max-time", "24", "--max-filesize", "4000000",
                             "--write-out", "\n%{url_effective}", page], capture_output=True)
    if result.returncode:
        raise ValueError(result.stderr.decode(errors="replace").strip())
    raw, final_url = result.stdout.rsplit(b"\n", 1)
    if urlsplit(final_url.decode()).hostname != urlsplit(page).hostname:
        raise ValueError("列表跨域跳转")
    charset = re.search(br'charset\s*=\s*["\x27]?([\w-]+)', raw[:4096], re.I)
    enc = charset[1].decode() if charset else "utf-8"
    return raw.decode(enc, errors="replace")


def collect(source):
    name, region, page, prefix = source
    try:
        parser = Anchors()
        parser.feed(fetch_page(page))
        notices = {}
        found = 0
        for href, title in parser.links:
            url = urldefrag(urljoin(page, href))[0]
            if urlsplit(url).scheme == "http" and urlsplit(url).hostname == urlsplit(page).hostname:
                url = urlsplit(url)._replace(scheme="https", netloc=urlsplit(page).netloc).geturl()
            path = urlsplit(url).path
            if not url.startswith("https://" + urlsplit(page).netloc + prefix) or not re.search(r"/[^/]*\d[^/]*\.[js]?html?$", path, re.I):
                continue
            if len(title) < 8 or re.search(r"/index(?:_\d+)?\.html?$", path):
                continue
            found += 1
            if not (TOPIC.search(title) or NOTICE.search(title)):
                continue
            category = ("注规考试" if "规划师" in title else "标准规范" if re.search("标准|规范|规程", title)
                        else "规划技术" if re.search("技术|三维|一张图", title) else "政策文件")
            notices[url] = dict(title=title, url=url, source=name, region=region, category=category)
        if not found:
            raise ValueError("未识别到文章链接，需检查列表结构")
        return name, list(notices.values()), ""
    except Exception as exc:
        return name, [], str(exc)


def key(url):
    return hashlib.sha256(url.encode()).hexdigest()[:20]


class Metadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.fields = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            name = attrs.get("name") or attrs.get("property") or ""
            self.fields[name.lower()] = attrs.get("content") or ""


DATE_TEXT = r"(20\d{2})[-年/.](\d{1,2})[-月/.](\d{1,2})"
DOCUMENT = re.compile("废止|失效|征求意见|政策解读|印发|管理办法|实施细则|公示|批前|批后|标准编号|技术规程|规范性文件")
EXAM = re.compile("考试|报名|准考证|资格核查|资格核验|成绩查询")


def read_story(row):
    html = fetch_page(row["url"])
    meta = Metadata()
    meta.feed(html)
    fields = meta.fields
    title = (fields.get("articletitle") or fields.get("og:title") or row["title"]).strip()
    visible = unescape(re.sub(r"<[^>]+>", " ", html))
    if not TOPIC.search(title) or DOCUMENT.search(title) or title[:12] not in visible and title != fields.get("articletitle"):
        return None
    stamp = next((fields[k] for k in ("pubdate", "publishdate", "article:published_time", "firstpublishedtime") if fields.get(k)), "")
    matched = re.match(DATE_TEXT, stamp.strip()) if stamp else re.search(r"(?:发布日期|发布时间|发稿时间)\s*[：:]?\s*" + DATE_TEXT, visible)
    if not matched:
        return None
    issued = date(*map(int, matched.groups()))
    if not 0 <= (TODAY-issued).days < 7:
        return None
    category = "规划技术" if re.search("技术|三维|一张图|数字|智能", title) else "规划实践"
    until = ""
    if EXAM.search(title):
        deadline = re.search(r"(?:报名截止(?:日期|时间)?|报名结束时间)\s*[：:]?\s*" + DATE_TEXT, visible)
        if not deadline or "规划师" not in title:
            return None
        end = date(*map(int, deadline.groups()))
        if end <= TODAY:
            return None
        until = end.isoformat()
        category = "注规考试"
    elif row["category"] in ("政策文件", "标准规范") and not any(word in row["source"] for word in ("新闻", "动态")):
        return None
    return dict(row, title=title, date=issued.isoformat(), category=category,
                reviewed=TODAY.isoformat(), until=until, summary=fields.get("description", "").strip()[:160], withdraw=[])


def update_news(history):
    with ThreadPoolExecutor(max_workers=5) as pool:
        for name, rows, error in pool.map(collect, SOURCES):
            print(name, error or f"{len(rows)} 条线索", flush=True)
            for row in rows[:20]:
                if row["url"] in history or not TOPIC.search(row["title"]):
                    continue
                try:
                    story = read_story(row)
                except (ValueError, LookupError, OSError) as exc:
                    print(name, row["url"], exc, flush=True)
                    continue
                if story:
                    history[row["url"]] = story
    return {url: row for url,row in history.items() if not row.get("until") or row["until"] > TODAY.isoformat()}


def render(history=None):
    page = Path("index.html").read_text(encoding="utf-8")
    match = re.search(r'(<section aria-label="资讯列表" id="articles">)(.*?)(</section>)', page, re.S)
    cards = {}
    for card in re.findall(r"<article\b.*?</article>", match[2], re.S):
        parser = Anchors()
        parser.feed(card)
        cards[parser.links[0][0]] = card
    removed = set()
    rows = [(key(url), row) for url,row in (history or {}).items() if url not in cards]
    for path in sorted(Path("decisions").glob("*.json")):
        row = json.loads(path.read_text(encoding="utf-8-sig"))
        rows.append((path.stem, row))
    for marker, row in rows:
        url = row["url"]
        if marker != key(url) or urlsplit(url).scheme != "https" or urlsplit(url).netloc not in {urlsplit(s[2]).netloc for s in SOURCES}:
            raise ValueError(f"不支持的原文地址 {marker}")
        if not isinstance(row["withdraw"], list) or not all(isinstance(v,str) for v in row["withdraw"]):
            raise ValueError("withdraw 应为旧文网址数组")
        removed.update(row["withdraw"])
        if row["category"] == "撤下":
            if not row["withdraw"]:
                raise ValueError("请填要撤下的原文网址")
            continue
        issued = date.fromisoformat(row["date"])
        if issued > TODAY or row["category"] not in CATEGORIES:
            raise ValueError("请核对发布日期和栏目")
        cat, region, title, source, summary = [escape(row[k]) for k in ("category","region","title","source","summary")]
        reviewed = row.get("reviewed") or ""
        until = row.get("until") or ""
        if reviewed and not issued <= date.fromisoformat(reviewed) <= TODAY:
            raise ValueError("请核对核验日期")
        if until:
            date.fromisoformat(until)
        event = escape(row.get("event") or "")
        link = escape(url)
        cards[url] = f'<article data-reviewed="{reviewed}" data-until="{until}" data-event="{event}" data-category="{cat}" data-regions="{region}"><div class="meta"><span class="category">{cat}</span><span>{region}</span><time datetime="{issued}">{issued}</time></div><h2><a href="{link}" target="_blank" rel="noopener noreferrer">{title}</a></h2><p>{summary}</p><a class="source" href="{link}" target="_blank" rel="noopener noreferrer">原文 · {source}</a></article>'
    cards = [card for url,card in cards.items() if url not in removed]
    cards.sort(key=lambda card: (re.search(r'datetime="([^"]+)"', card) or ["",""])[1], reverse=True)
    page = page[:match.start(2)]+"\n"+"\n".join(cards)+"\n"+page[match.end(2):]
    page = re.sub(r"共 \d+ 条资讯",f"共 {len(cards)} 条资讯",page)
    page = re.sub(r"网页更新于 [\d-]+",f"网页更新于 {TODAY}",page)
    out = Path("public")
    out.mkdir(exist_ok=True)
    (out/"index.html").write_text(page, encoding="utf-8")
    for asset in ("standards.html","standards.json"):
        (out/asset).write_bytes(Path(asset).read_bytes())
    (out/"more").mkdir(exist_ok=True)
    (out/"more"/"index.html").write_text(page, encoding="utf-8")


if __name__ == "__main__":
    if sys.argv[1] == "publish":
        archive = Path("previous-news.json")
        history = json.loads(archive.read_text(encoding="utf-8")) if archive.exists() else {}
        history = update_news(history)
        render(history)
        Path("public/news.json").write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
    elif sys.argv[1] == "render":
        render()
    else:
        raise SystemExit("使用 publish 自动采集发布，或 render 离线生成网页")

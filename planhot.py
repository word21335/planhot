from html import escape
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


def collect(source):
    name, region, page, prefix = source
    try:
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
        parser = Anchors()
        parser.feed(raw.decode(enc, errors="replace"))
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


def gh(*args):
    return subprocess.check_output(["gh", *args], text=True, encoding="utf-8")


def scan(dry):
    repo = os.environ.get("GITHUB_REPOSITORY", "word21335/planhot")
    known = set()
    if not dry:
        pages = gh("api", "--paginate", "--slurp", f"repos/{repo}/issues?state=all&per_page=100")
        for page in json.loads(pages):
            for issue in page:
                if issue["user"]["login"] == "github-actions[bot]":
                    known.update(re.findall(r"<!-- planhot:([a-f0-9]{20}) -->", issue.get("body") or ""))
    homepage = Path("index.html").read_text(encoding="utf-8")
    lines, errors = [], []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for name, rows, error in pool.map(collect, SOURCES):
            print(name, error or f"{len(rows)} 条相关线索", flush=True)
            if error:
                errors.append(name)
            for row in rows:
                url = row["url"]
                marker = key(url)
                if marker in known or escape(url, quote=True) in homepage:
                    continue
                known.add(marker)
                decision = dict(**row, date="请填原文发布日期 YYYY-MM-DD", summary="", withdraw=[])
                params = urlencode({"filename": f"decisions/{marker}.json", "value": json.dumps(decision, ensure_ascii=False, indent=2)})
                label = "疑似效力变更" if NOTICE.search(row["title"]) else "待核对"
                title = escape(row["title"]).replace("[", "［").replace("]", "］").replace("@", "＠")
                lines.append(f"<!-- planhot:{marker} -->\n- **{label}** {title}（{name}） [官方原文](<{url}>) · [审核并发布](https://github.com/{repo}/new/main?{params})")
    if lines and not dry:
        intro = "打开原文核对发布日期、现行效力和考试时限。首次扫描含已有文件。只提交确认有效的条目。填入 date，可补 summary。失效旧条目的原文网址填入 withdraw 数组；只撤下旧文时将 category 改为 撤下。关闭此清单即归档。\n\n"
        # Keep issue bodies below GitHub's length limit.
        for start in range(0, len(lines), 12):
            gh("issue", "create", "--repo", repo, "--title", f"规划资讯待审核 · {TODAY} · {start//12+1}", "--body", intro+"\n\n".join(lines[start:start+12]))
    if errors:
        print("未完成检查：" + "、".join(errors), file=sys.stderr)
        sys.exit(1)


def render():
    page = Path("index.html").read_text(encoding="utf-8")
    match = re.search(r'(<section aria-label="资讯列表" id="articles">)(.*?)(</section>)', page, re.S)
    cards = {}
    for card in re.findall(r"<article\b.*?</article>", match[2], re.S):
        parser = Anchors()
        parser.feed(card)
        cards[parser.links[0][0]] = card
    removed = set()
    for path in sorted(Path("decisions").glob("*.json")):
        row = json.loads(path.read_text(encoding="utf-8-sig"))
        url = row["url"]
        if path.stem != key(url) or urlsplit(url).scheme != "https" or urlsplit(url).netloc not in {urlsplit(s[2]).netloc for s in SOURCES}:
            raise ValueError(f"不支持的原文地址 {path}")
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
        link = escape(url)
        cards[url] = f'<article data-category="{cat}" data-regions="{region}"><div class="meta"><span class="category">{cat}</span><span>{region}</span><time datetime="{issued}">{issued}</time></div><h2><a href="{link}" target="_blank" rel="noopener noreferrer">{title}</a></h2><p>{summary}</p><a class="source" href="{link}" target="_blank" rel="noopener noreferrer">原文 · {source}</a></article>'
    cards = [card for url,card in cards.items() if url not in removed]
    cards.sort(key=lambda card: (re.search(r'datetime="([^"]+)"', card) or ["",""])[1], reverse=True)
    page = page[:match.start(2)]+"\n"+"\n".join(cards)+"\n"+page[match.end(2):]
    page = re.sub(r"共 \d+ 条资讯",f"共 {len(cards)} 条资讯",page)
    page = re.sub(r"网页更新于 [\d-]+",f"网页更新于 {TODAY}",page)
    out = Path("public")
    out.mkdir(exist_ok=True)
    (out/"index.html").write_text(page, encoding="utf-8")
    (out/"more").mkdir(exist_ok=True)
    (out/"more"/"index.html").write_text(page, encoding="utf-8")


if __name__ == "__main__":
    if sys.argv[1] == "render":
        render()
    else:
        scan("--dry-run" in sys.argv)


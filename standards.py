"""Update the public planning standards catalogue from the official SAMR list."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from html import unescape
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlencode, urlsplit

CATALOG = Path("standards.json")
TERMS = ("国土空间", "城市规划", "城乡规划", "城市更新", "村庄规划", "城市用地", "城市居住区", "城市公共交通规划", "城市绿地", "城市综合交通", "城市道路", "城市轨道交通规划", "土地利用", "用地分类", "城市和社区可持续发展", "城市步行", "城市防灾", "公共服务设施", "地理信息公共服务平台")
TODAY = datetime.now(timezone(timedelta(hours=8))).date().isoformat()


def fetch(url, limit=4000000):
    return subprocess.check_output(["curl", "-fsSL", "--proto", "=https", "--proto-redir", "=https",
        "--max-time", "25", "--max-filesize", str(limit), url])


def norm(number):
    import unicodedata
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", number)).upper().replace("—","-").replace("–","-")


def search(term):
    items = {}
    for page in range(1, 101):
        url = "https://openstd.samr.gov.cn/bzgk/gb/std_list?" + urlencode({"p.p1":page-1, "p.p2":term,"p.p90":"circulation_date","p.p91":"desc"})
        try:
            html = fetch(url).decode("utf-8")
        except (OSError, subprocess.SubprocessError, UnicodeError) as e:
            print(term, e)
            break
        before = len(items)
        for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>",html,re.S|re.I):
            hc = re.search(r"showInfo\('([0-9A-Fa-f]{32})'\)",row)
            cols = [unescape(re.sub("<[^>]+>","",v)).strip() for v in re.findall(r"<td\b[^>]*>(.*?)</td>",row,re.S|re.I)]
            if hc and len(cols)>=9 and term in cols[4] and re.fullmatch(r"\d{4}-\d\d-\d\d",cols[7][:10]):
                items[norm(cols[1])] = dict(number=cols[1],title=cols[4],status=cols[6],date=cols[7][:10],source="https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno="+hc[1])
        if len(items)==before or len(re.findall(r"class=\"detail\"",html))<10:
            break
    return items


def update():
    db=json.loads(CATALOG.read_text(encoding="utf-8")) if CATALOG.exists() else {"rows":[],"history":{}}
    known={norm(r["number"]):r for r in db["rows"]}
    pool={}
    with ThreadPoolExecutor(max_workers=5) as workers:
        for group in workers.map(search,TERMS):
            pool.update(group)
    if not pool:
        print("官方目录暂不可用，保留上一版。")
        return
    for key,row in list(known.items()):
        if key in pool and pool[key]["status"]!="现行":
            del known[key]
        elif key in pool:
            row.update(pool[key],checked=TODAY)
    pending={k:v for k,v in pool.items() if v["status"]=="现行" and k not in known}
    # Only the repository owner's submissions can attach shared files.
    repo=os.environ.get("GITHUB_REPOSITORY")
    if repo and os.environ.get("GH_TOKEN"):
        issues=json.loads(subprocess.check_output(["gh","api","--paginate","--slurp",f"repos/{repo}/issues?state=open&per_page=100"]))
        for issue in (i for page in issues for i in page):
            m=re.fullmatch(r"规范上传 (.+?) ([a-f0-9]{64})",issue["title"])
            if not m or issue["user"]["login"].lower()!=repo.split("/")[0].lower(): continue
            key=norm(m[1])
            if key in known: continue
            if key not in pending:
                meta=re.search(r"<!--planhot-standard (.+?) -->",issue.get("body") or "")
                if not meta: continue
                try:
                    info=json.loads(meta[1])
                    source=urlsplit(info["source"])
                    issued=datetime.strptime(info["date"],"%Y-%m-%d").date().isoformat()
                    if source.scheme!="https" or not (source.hostname or "").endswith(".gov.cn") or not 1<=len(info["title"])<=200 or issued>TODAY: continue
                except (KeyError,TypeError,ValueError): continue
                candidate=dict(number=m[1],title=info["title"],date=issued,status="现行",source=info["source"])
            else:
                candidate=pending[key]
            if any(r.get("sha256")==m[2] for r in [*known.values(),*pending.values()]): continue
            file=re.search(r"https://github\.com/user-attachments/files/\d+/[^\s)<>]+\.pdf",issue.get("body") or "",re.I)
            if not file: continue
            try: data=fetch(file[0],20*1024*1024)
            except subprocess.SubprocessError: continue
            if data.startswith(b"%PDF-") and hashlib.sha256(data).hexdigest()==m[2]:
                pending[key]=dict(candidate,download=file[0],sha256=m[2])
    history=db["history"]
    available=max(0,4-sum(d==TODAY for d in history.values()))
    ordered=sorted(pending.items(),key=lambda pair:(bool(pair[1].get("download")),pair[1]["date"],pair[0]),reverse=True)
    for key,row in ordered[:available]:
        known[key]=dict(row,checked=TODAY)
        history[key]=TODAY
    db["rows"]=sorted(known.values(),key=lambda r:(r["date"],r["number"]),reverse=True)
    CATALOG.write_text(json.dumps(db,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(TODAY,len(db["rows"]),"条，候选",len(pending))


if __name__=="__main__":
    update()

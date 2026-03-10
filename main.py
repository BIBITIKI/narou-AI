"""
なろう校正リーダー - FastAPI サーバー
Railway にデプロイして PC なしでスマホから使えるようにする

校正フロー（2段階）:
  Stage 1: gemini-2.5-flash で全話を読んでコンテキストシート生成（1リクエスト）
  Stage 2: gemini-2.5-flash でチャンク単位に校正（コンテキスト付き）
"""
import asyncio, json, os, re, shutil, time, urllib.error, urllib.request
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ============================================================
# 設定
# ============================================================
DATA_DIR   = Path(os.getenv("DATA_DIR", "./data"))
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash"
SCRAPE_DELAY = float(os.getenv("SCRAPE_DELAY", "1.5"))
RATE_INTERVAL = float(os.getenv("RATE_INTERVAL", "7.0"))  # 10req/分 → 6秒/req、余裕で7秒
CHUNK_CHARS  = int(os.getenv("CHUNK_CHARS", "12000"))      # 1話1万文字超でも安全

RAW_DIR  = DATA_DIR / "raw"
PF_DIR   = DATA_DIR / "proofread"
SITE_DIR = DATA_DIR / "site"
for d in [RAW_DIR, PF_DIR, SITE_DIR]:
    d.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="なろう校正リーダー")

# ============================================================
# ジョブ状態管理
# ============================================================
jobs: dict[str, dict] = {}

def set_job(code, status, message, total=0, done=0):
    jobs[code] = {"status": status, "message": message, "total": total, "done": done}

# ============================================================
# スクレイパー
# ============================================================
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; personal-narou-reader/1.0)"}

def fetch_toc(novel_code):
    url = f"https://ncode.syosetu.com/{novel_code}/"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "html.parser")

    title_tag = (soup.select_one(".novel_title") or
                 soup.select_one("h1.p-novel__title") or soup.select_one("h1"))
    novel_title = title_tag.get_text(strip=True) if title_tag else novel_code

    episodes, current_chapter = [], ""
    for item in soup.select(".p-eplist__chapter-title, .p-eplist__sublist"):
        cls = item.get("class", [])
        if "p-eplist__chapter-title" in cls:
            current_chapter = item.get_text(strip=True)
        elif "p-eplist__sublist" in cls:
            link = item.select_one("a.p-eplist__subtitle")
            if link:
                m = re.search(r"/(\d+)/?$", link.get("href", ""))
                if m:
                    episodes.append({"num": int(m.group(1)),
                                     "subtitle": link.get_text(strip=True),
                                     "chapter": current_chapter})
    if not episodes:
        for item in soup.select(".p-eplist__chapter-title, a.p-eplist__subtitle"):
            cls = item.get("class", [])
            if "p-eplist__chapter-title" in cls:
                current_chapter = item.get_text(strip=True)
            elif "p-eplist__subtitle" in cls:
                m = re.search(r"/(\d+)/?$", item.get("href", ""))
                if m:
                    episodes.append({"num": int(m.group(1)),
                                     "subtitle": item.get_text(strip=True),
                                     "chapter": current_chapter})
    if not episodes:
        for item in soup.select(".index_box .chapter_title, .index_box dd.subtitle a"):
            if item.name and "chapter_title" in item.get("class", []):
                current_chapter = item.get_text(strip=True)
            else:
                m = re.search(r"/(\d+)/?$", item.get("href", ""))
                if m:
                    episodes.append({"num": int(m.group(1)),
                                     "subtitle": item.get_text(strip=True),
                                     "chapter": current_chapter})
    return novel_title, episodes

def fetch_body(novel_code, episode_num):
    url = f"https://ncode.syosetu.com/{novel_code}/{episode_num}/"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "html.parser")
    body = soup.select_one("#novel_honbun") or soup.select_one(".p-novel__body")
    if not body:
        raise ValueError(f"本文が見つかりません (episode {episode_num})")
    for rt in body.find_all("rt"): rt.decompose()
    for ruby in body.find_all("ruby"): ruby.unwrap()
    return "\n".join(p.get_text() for p in body.find_all("p"))

# ============================================================
# Gemini API
# ============================================================
def _gemini_request(system_text, user_text, max_output_tokens=8192, timeout=120):
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}")
    payload = json.dumps({
        "system_instruction": {"parts": [{"text": system_text}]},
        "contents": [{"parts": [{"text": user_text}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": max_output_tokens}
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=payload,
                                  headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    candidates = data.get("candidates", [])
    if not candidates:
        raise ValueError(f"candidatesが空: {data}")
    return candidates[0]["content"]["parts"][0]["text"].strip()

async def gemini_with_retry(system_text, user_text, max_output_tokens=8192, timeout=120):
    """レート制限対応・非同期リトライ"""
    for attempt in range(6):
        try:
            # asyncio.to_thread でブロッキングI/Oをオフロード
            return await asyncio.to_thread(
                _gemini_request, system_text, user_text, max_output_tokens, timeout)
        except urllib.error.HTTPError as e:
            err_bytes = e.read()
            err_data = json.loads(err_bytes.decode("utf-8")) if err_bytes else {}
            if e.code == 429:
                wait = RATE_INTERVAL * (attempt + 1) * 3  # 指数バックオフ
                await asyncio.sleep(wait)
            elif e.code == 503:
                await asyncio.sleep(30)
            else:
                msg = err_data.get("error", {}).get("message", str(e))
                raise RuntimeError(f"Gemini APIエラー {e.code}: {msg}")
        except Exception as e:
            if attempt >= 4:
                raise
            await asyncio.sleep(10 * (attempt + 1))
    raise RuntimeError("リトライ上限超過")

# ============================================================
# 校正プロンプト
# ============================================================
SHEET_SYSTEM = """あなたはWeb小説（なろう系）の校正担当編集者です。
与えられた小説の全話テキストを読み込み、校正に必要な「作品コンテキストシート」を作成してください。"""

SHEET_USER = """以下は「{novel_title}」の全話テキストです。

{all_text}

---
上記を読んで、以下の形式で「作品コンテキストシート」を作成してください。

# 作品コンテキストシート：{novel_title}

## 登場人物
（名前・読み・一人称・口調の特徴・関係性を列挙）

## 固有名詞・用語一覧
（地名・組織名・魔法・スキル・アイテム等の正式表記を列挙）

## 文体・語尾の特徴
（地の文のテンポ、よく使われる表現パターン等）

## 注意すべき表記ゆれ
（原文で揺れている表記があれば統一案を提示）

## その他の校正指針
（この作品特有の注意事項）"""

PROOFREAD_SYSTEM = """あなたは日本語Web小説（なろう系）の校正専門の編集者です。
与えられた「作品コンテキストシート」を厳守しながら、文章品質のみを向上させます。

## 校正内容
1. 誤字脱字・助詞の誤用・句読点の修正
2. 同じ語尾の連続回避、長文の分割、読点の過不足修正
3. 冗長な表現の整理、主語省略による読みにくさの解消
4. セリフの自然さ向上（コンテキストシートの口調を厳守）
5. 固有名詞・用語の表記をコンテキストシートに統一

## 絶対禁止
- プロット・設定・キャラクター性格の変更
- 原作にないシーン・説明の追加
- コンテキストシートに反する変更

## 出力形式（複数話は必ずこの形式）
各話を以下のセパレータで区切って出力してください：
===EPISODE_START:{episode_num}===
（校正後の本文）
===EPISODE_END===

説明・コメント・注記は一切不要。本文テキストのみ出力。"""

PROOFREAD_USER = """{context_sheet}

---
上記のコンテキストシートを踏まえて、以下の{count}話を校正してください。

{episodes_text}"""

EPISODE_BLOCK = """===EPISODE_START:{episode_num}===
【話タイトル】{subtitle}
【本文】
{body}
===EPISODE_END==="""

# ============================================================
# チャンク処理
# ============================================================
def build_chunks(episodes, chunk_chars):
    chunks, current, current_chars = [], [], 0
    for ep in episodes:
        ep_chars = len(ep["body"])
        if current_chars + ep_chars > chunk_chars and current:
            chunks.append(current)
            current, current_chars = [], 0
        current.append(ep)
        current_chars += ep_chars
    if current:
        chunks.append(current)
    return chunks

def parse_chunk_response(response, episodes_in_chunk):
    results = {}
    pattern = re.compile(r'===EPISODE_START:(\d+)===\s*(.*?)\s*===EPISODE_END===', re.DOTALL)
    for m in pattern.finditer(response):
        results[int(m.group(1))] = m.group(2).strip()
    if not results and len(episodes_in_chunk) == 1:
        results[episodes_in_chunk[0]["episode_num"]] = response.strip()
    return results

# ============================================================
# サイト生成
# ============================================================
def esc(s):
    return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def extract_terms(episodes):
    terms = set()
    for ep in episodes:
        body = ep.get("body","")
        terms.update(re.findall(r'[ァ-ヶー]{3,}', body))
        terms.update(m.group(1) for m in re.finditer(r'[『「]([^』」]{2,10})[』」]', body)
                     if not m.group(1).endswith(("た","だ","る","い","う")))
    return sorted(t for t in terms if 2 <= len(t) <= 12)

def body_to_html(body, terms):
    sorted_terms = sorted(terms, key=len, reverse=True)
    parts = []
    for line in body.split("\n"):
        s = line.strip()
        h = esc(s) if s else "&nbsp;"
        for t in sorted_terms:
            et = esc(t)
            h = h.replace(et, f'<button class="word-link" data-term="{et}">{et}</button>')
        cls = ' class="dialogue"' if s.startswith(("「","『","…")) else ""
        parts.append(f"<p{cls}>{h}</p>")
    return "\n".join(parts)

EPISODE_TMPL = """\
<!DOCTYPE html><html lang="ja" data-theme="dark"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{subtitle} | {novel_title}</title>
<link rel="stylesheet" href="/static/reader.css"></head><body>
<header id="site-header">
  <a id="header-home" href="/{novel_code}/">目次</a>
  <div id="header-title">{novel_title}　{subtitle}</div>
  <div class="ctrl-group">
    <button class="ctrl-btn size-btn" id="font-down">A-</button>
    <span id="font-size-display">110%</span>
    <button class="ctrl-btn size-btn" id="font-up">A+</button>
    <button class="ctrl-btn" id="theme-toggle">☀️</button>
  </div>
</header>
<div id="main-wrap">
  {chapter_html}
  <h1 id="episode-title">{subtitle}</h1>
  <div id="novel-body">{body_html}</div>
</div>
<div id="nav-bar">
  <a class="nav-btn" href="{prev_href}">{prev_label}</a>
  <a class="nav-btn" href="/{novel_code}/">目次</a>
  <a class="nav-btn" href="{next_href}">{next_label}</a>
</div>
<div id="word-popup">
  <button id="popup-close">✕</button>
  <div id="popup-word"></div>
  <div id="popup-loading">解説を取得中…</div>
  <div id="popup-body" style="display:none"></div>
</div>
<div id="api-panel">
  <label>Gemini Key:</label>
  <input type="password" id="api-key-input" placeholder="AIza...（単語解説に使用）">
  <button id="api-save-btn">保存</button>
  <span id="api-status" class="status-no">未設定</span>
</div>
<script src="/static/reader.js"></script>
<script>initReader({terms_json});</script>
</body></html>"""

INDEX_TMPL = """\
<!DOCTYPE html><html lang="ja" data-theme="dark"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{novel_title} - 目次</title>
<link rel="stylesheet" href="/static/reader.css">
<style>
  .toc-chapter{{margin:24px 0 8px;font-size:.85rem;color:var(--text-muted);border-bottom:1px solid var(--border);padding-bottom:4px}}
  .toc-list{{list-style:none;padding:0}}
  .toc-list li a{{display:block;padding:8px 4px;color:var(--accent);text-decoration:none;font-size:.95rem;border-bottom:1px solid var(--border)}}
  .novel-meta{{font-size:.8rem;color:var(--text-muted);margin-bottom:24px}}
</style></head><body>
<header id="site-header">
  <a id="header-home" href="/">← 作品一覧</a>
  <div id="header-title">{novel_title}</div>
  <div class="ctrl-group"><button class="ctrl-btn" id="theme-toggle">☀️</button></div>
</header>
<div id="main-wrap">
  <h1 id="episode-title">{novel_title}</h1>
  <div class="novel-meta">全{total_episodes}話　校正済み</div>
  {toc_html}
</div>
<script src="/static/reader.js"></script>
<script>initReader([]);</script>
</body></html>"""

TOP_TMPL = """\
<!DOCTYPE html><html lang="ja" data-theme="dark"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>なろう校正リーダー</title>
<link rel="stylesheet" href="/static/reader.css">
<style>
  .card{{background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:20px;margin-bottom:16px}}
  .card h2{{font-size:1rem;margin-bottom:8px;color:var(--text)}}
  .card .meta{{font-size:.8rem;color:var(--text-muted);margin-bottom:12px}}
  .card a.read-link{{color:var(--accent);text-decoration:none;font-size:.9rem}}
  .form-row{{display:flex;gap:8px;margin-top:24px}}
  #novel-input{{flex:1;background:var(--bg3);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:10px 12px;font-size:1rem}}
  #start-btn{{background:var(--accent);border:none;border-radius:6px;color:#fff;cursor:pointer;font-size:1rem;padding:10px 20px;white-space:nowrap}}
  #start-btn:disabled{{opacity:.5;cursor:default}}
  .progress{{margin-top:16px;font-size:.85rem;color:var(--text-muted);display:none}}
  .progress.visible{{display:block}}
  .progress-bar-wrap{{background:var(--bg3);border-radius:4px;height:6px;margin-top:8px}}
  .progress-bar{{background:var(--accent);border-radius:4px;height:6px;transition:width .5s}}
  .status-done{{color:#6a9}}.status-error{{color:#c66}}
  .hint{{font-size:.78rem;color:var(--text-muted);margin-top:8px}}
  .stage-badge{{display:inline-block;font-size:.7rem;background:var(--bg3);border:1px solid var(--border);border-radius:4px;padding:2px 6px;margin-left:6px;color:var(--text-muted)}}
</style></head><body>
<header id="site-header">
  <div id="header-title">なろう校正リーダー</div>
  <div class="ctrl-group"><button class="ctrl-btn" id="theme-toggle">☀️</button></div>
</header>
<div id="main-wrap">
  <div id="novel-list">{novel_cards}</div>
  <div style="border-top:1px solid var(--border);padding-top:24px;margin-top:8px">
    <h2 style="font-size:1rem;margin-bottom:4px">新しい作品を追加</h2>
    <p class="hint">なろうURLの英数字コードを入力（例: n9669bk）</p>
    <div class="form-row">
      <input id="novel-input" type="text" placeholder="n9669bk" autocapitalize="none" autocorrect="off">
      <button id="start-btn">校正開始</button>
    </div>
    <div class="progress" id="progress-area">
      <div id="progress-msg">処理中...<span id="stage-badge" class="stage-badge"></span></div>
      <div class="progress-bar-wrap"><div class="progress-bar" id="progress-bar" style="width:0%"></div></div>
    </div>
  </div>
</div>
<script src="/static/reader.js"></script>
<script>
initReader([]);
const btn = document.getElementById('start-btn');
const input = document.getElementById('novel-input');
const progressArea = document.getElementById('progress-area');
const progressMsg = document.getElementById('progress-msg');
const progressBar = document.getElementById('progress-bar');
const stageBadge = document.getElementById('stage-badge');

btn.addEventListener('click', async () => {{
  const code = input.value.trim().toLowerCase();
  if (!code) return;
  btn.disabled = true;
  progressArea.classList.add('visible');
  progressMsg.textContent = '開始中...';
  stageBadge.textContent = '';
  progressBar.style.width = '0%';
  try {{
    const res = await fetch('/api/start', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{novel_code: code}})
    }});
    const data = await res.json();
    if (!res.ok) {{ showError(data.detail || 'エラー'); return; }}
    pollStatus(code);
  }} catch(e) {{ showError('ネットワークエラー'); }}
}});

function showError(msg) {{
  progressMsg.className = 'status-error';
  progressMsg.textContent = msg;
  btn.disabled = false;
}}

const STAGE_LABELS = {{
  scraping: 'テキスト取得中',
  sheet: 'コンテキストシート生成中',
  proofreading: 'AI校正中',
  generating: 'HTML生成中',
  done: '完了',
  error: 'エラー'
}};

async function pollStatus(code) {{
  try {{
    const res = await fetch('/api/status/' + code);
    const data = await res.json();
    const pct = data.total > 0 ? Math.round(data.done / data.total * 100) : 0;
    progressBar.style.width = Math.max(pct, 3) + '%';
    progressMsg.className = data.status === 'error' ? 'status-error' : '';
    progressMsg.textContent = data.message;
    stageBadge.textContent = STAGE_LABELS[data.status] || data.status;
    if (data.status === 'done') {{
      progressMsg.classList.add('status-done');
      setTimeout(() => {{ location.href = '/' + code + '/'; }}, 1500);
    }} else if (data.status === 'error') {{
      btn.disabled = false;
    }} else {{
      setTimeout(() => pollStatus(code), 3000);
    }}
  }} catch(e) {{
    setTimeout(() => pollStatus(code), 5000);
  }}
}}
</script>
</body></html>"""

def generate_site(novel_code, episodes):
    out_dir = SITE_DIR / novel_code
    out_dir.mkdir(parents=True, exist_ok=True)
    terms = extract_terms(episodes)
    novel_title = episodes[0]["novel_title"] if episodes else novel_code

    for idx, ep in enumerate(episodes):
        num = ep["episode_num"]
        prev_ep = episodes[idx-1] if idx > 0 else None
        next_ep = episodes[idx+1] if idx < len(episodes)-1 else None
        chapter_html = f'<div id="episode-chapter">{esc(ep.get("chapter",""))}</div>' if ep.get("chapter") else ""
        html = EPISODE_TMPL.format(
            novel_code=novel_code, novel_title=esc(novel_title),
            subtitle=esc(ep["subtitle"]), chapter_html=chapter_html,
            body_html=body_to_html(ep["body"], terms),
            prev_href=f"/{novel_code}/{prev_ep['episode_num']:04d}" if prev_ep else "#",
            prev_label="◀ 前の話" if prev_ep else "",
            next_href=f"/{novel_code}/{next_ep['episode_num']:04d}" if next_ep else "#",
            next_label="次の話 ▶" if next_ep else "",
            terms_json=json.dumps(terms, ensure_ascii=False)
        )
        (out_dir / f"{num:04d}.html").write_text(html, encoding="utf-8")

    current_chapter = ""
    toc_parts = []
    for ep in episodes:
        ch = ep.get("chapter","")
        if ch and ch != current_chapter:
            toc_parts.append(f'<div class="toc-chapter">{esc(ch)}</div>')
            current_chapter = ch
        toc_parts.append(
            f'<ul class="toc-list"><li>'
            f'<a href="/{novel_code}/{ep["episode_num"]:04d}">{esc(ep["subtitle"])}</a>'
            f'</li></ul>')

    (out_dir / "index.html").write_text(INDEX_TMPL.format(
        novel_title=esc(novel_title), novel_code=novel_code,
        total_episodes=len(episodes), toc_html="\n".join(toc_parts)
    ), encoding="utf-8")

    meta = {"novel_code": novel_code, "novel_title": novel_title,
            "total_episodes": len(episodes),
            "episodes": [{"num": e["episode_num"], "subtitle": e["subtitle"]} for e in episodes]}
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

# ============================================================
# バックグラウンドパイプライン
# ============================================================
async def run_pipeline(novel_code: str):
    try:
        # ── Step 1: スクレイプ ──────────────────────────
        set_job(novel_code, "scraping", "目次を取得中...", 0, 0)
        novel_title, episode_list = await asyncio.to_thread(fetch_toc, novel_code)
        if not episode_list:
            set_job(novel_code, "error", f"話が見つかりませんでした（コード: {novel_code}）"); return

        total = len(episode_list)
        raw_dir = RAW_DIR / novel_code
        raw_dir.mkdir(parents=True, exist_ok=True)

        for i, ep in enumerate(episode_list, 1):
            num = ep["num"]
            cache = raw_dir / f"{num:04d}.json"
            if not cache.exists():
                body = await asyncio.to_thread(fetch_body, novel_code, num)
                data = {"novel_code": novel_code, "novel_title": novel_title,
                        "episode_num": num, "subtitle": ep["subtitle"],
                        "chapter": ep["chapter"], "body": body}
                cache.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                await asyncio.sleep(SCRAPE_DELAY)
            set_job(novel_code, "scraping", f"テキスト取得中... ({i}/{total}話)", total, i)

        meta = {"novel_code": novel_code, "novel_title": novel_title, "total_episodes": total,
                "episodes": [{"num": e["num"], "subtitle": e["subtitle"], "chapter": e["chapter"]}
                             for e in episode_list]}
        (raw_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        # ── Step 2: コンテキストシート生成 ──────────────
        pf_dir = PF_DIR / novel_code
        pf_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(raw_dir / "meta.json", pf_dir / "meta.json")
        sheet_path = pf_dir / "_context_sheet.md"

        episodes_raw = sorted(
            [json.loads(f.read_text(encoding="utf-8")) for f in raw_dir.glob("*.json") if f.name != "meta.json"],
            key=lambda e: e["episode_num"])

        if not sheet_path.exists():
            set_job(novel_code, "sheet", "コンテキストシートを生成中...", total, 0)
            MAX_CHARS_PER_EP = 3000
            parts = []
            for ep in episodes_raw:
                excerpt = ep["body"][:MAX_CHARS_PER_EP]
                if len(ep["body"]) > MAX_CHARS_PER_EP: excerpt += "…（以下略）"
                parts.append(f"【{ep['subtitle']}】\n{excerpt}")
            user_text = SHEET_USER.format(novel_title=novel_title, all_text="\n\n".join(parts))
            try:
                sheet = await gemini_with_retry(SHEET_SYSTEM, user_text, max_output_tokens=4096, timeout=180)
            except Exception:
                sheet = f"# 作品コンテキストシート：{novel_title}\n\n（シート生成に失敗しました）"
            sheet_path.write_text(sheet, encoding="utf-8")
            # レート制限対策
            await asyncio.sleep(RATE_INTERVAL * 2)
        
        context_sheet = sheet_path.read_text(encoding="utf-8")

        # ── Step 3: チャンク校正 ─────────────────────────
        pending = [ep for ep in episodes_raw
                   if not (pf_dir / f"{ep['episode_num']:04d}.json").exists()]

        if pending:
            chunks = build_chunks(pending, CHUNK_CHARS)
            total_chunks = len(chunks)
            done_chunks = 0

            for ci, chunk in enumerate(chunks, 1):
                chunk_chars = sum(len(ep["body"]) for ep in chunk)
                ep_nums = [ep["episode_num"] for ep in chunk]
                set_job(novel_code, "proofreading",
                        f"校正中... #{ep_nums[0]}〜#{ep_nums[-1]} ({ci}/{total_chunks}チャンク)",
                        total_chunks, done_chunks)

                episodes_text = "\n\n".join(
                    EPISODE_BLOCK.format(episode_num=ep["episode_num"],
                                         subtitle=ep["subtitle"], body=ep["body"])
                    for ep in chunk)
                user_text = PROOFREAD_USER.format(
                    context_sheet=context_sheet, count=len(chunk), episodes_text=episodes_text)
                estimated_output = min(int(chunk_chars * 1.2 / 0.75), 65536)

                try:
                    response = await gemini_with_retry(
                        PROOFREAD_SYSTEM, user_text,
                        max_output_tokens=estimated_output, timeout=120)
                    parsed = parse_chunk_response(response, chunk)
                except Exception:
                    parsed = {}  # 失敗時は原文で保存

                for ep in chunk:
                    num = ep["episode_num"]
                    proofread_body = parsed.get(num, ep["body"])
                    result = {**ep, "body_original": ep["body"], "body": proofread_body,
                              "proofread": bool(parsed.get(num)), "model": GEMINI_MODEL}
                    (pf_dir / f"{num:04d}.json").write_text(
                        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

                done_chunks += 1
                if ci < total_chunks:
                    await asyncio.sleep(RATE_INTERVAL)

        # ── Step 4: サイト生成 ───────────────────────────
        set_job(novel_code, "generating", "HTMLを生成中...", total, total)
        pf_files = sorted(
            [json.loads(f.read_text(encoding="utf-8")) for f in pf_dir.glob("*.json") if f.name != "meta.json"],
            key=lambda e: e["episode_num"])
        await asyncio.to_thread(generate_site, novel_code, pf_files)

        set_job(novel_code, "done", f"「{novel_title}」全{total}話の校正が完了しました！", total, total)

    except Exception as e:
        set_job(novel_code, "error", f"エラー: {str(e)}")

# ============================================================
# API エンドポイント
# ============================================================
class StartRequest(BaseModel):
    novel_code: str

@app.post("/api/start")
async def start_job(req: StartRequest, background_tasks: BackgroundTasks):
    code = req.novel_code.strip().lower()
    if not re.match(r'^[a-z0-9]+$', code):
        raise HTTPException(status_code=400, detail="小説コードは英数字のみです（例: n9669bk）")
    if not GEMINI_KEY:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY が設定されていません")
    if jobs.get(code, {}).get("status") in ("scraping", "sheet", "proofreading", "generating"):
        raise HTTPException(status_code=409, detail="すでに処理中です")
    set_job(code, "scraping", "開始中...", 0, 0)
    background_tasks.add_task(run_pipeline, code)
    return {"ok": True, "novel_code": code}

@app.get("/api/status/{novel_code}")
async def get_status(novel_code: str):
    code = novel_code.strip().lower()
    if code not in jobs:
        if (SITE_DIR / code / "index.html").exists():
            return {"status": "done", "message": "校正済み", "total": 0, "done": 0}
        return {"status": "unknown", "message": "ジョブが見つかりません", "total": 0, "done": 0}
    return jobs[code]

@app.get("/api/novels")
async def list_novels():
    novels = []
    for meta_file in SITE_DIR.glob("*/meta.json"):
        try:
            novels.append(json.loads(meta_file.read_text(encoding="utf-8")))
        except Exception:
            pass
    return novels

# ============================================================
# 読書ページ
# ============================================================
@app.get("/", response_class=HTMLResponse)
async def top_page():
    novels = []
    for meta_file in SITE_DIR.glob("*/meta.json"):
        try:
            novels.append(json.loads(meta_file.read_text(encoding="utf-8")))
        except Exception:
            pass

    # 処理中のジョブも表示
    in_progress = {code: job for code, job in jobs.items()
                   if job["status"] not in ("done", "error")}

    cards = ""
    for n in sorted(novels, key=lambda x: x.get("novel_title","")):
        code = n["novel_code"]
        cards += (f'<div class="card">'
                  f'<h2><a href="/{code}/">{esc(n["novel_title"])}</a></h2>'
                  f'<div class="meta">全{n["total_episodes"]}話</div>'
                  f'<a class="read-link" href="/{code}/">読む →</a>'
                  f'</div>')
    for code, job in in_progress.items():
        if not any(n.get("novel_code") == code for n in novels):
            pct = int(job["done"]/job["total"]*100) if job["total"] else 0
            cards += (f'<div class="card">'
                      f'<h2>{esc(code)}</h2>'
                      f'<div class="meta">処理中... {pct}%</div>'
                      f'</div>')
    if not cards:
        cards = '<p style="color:var(--text-muted);font-size:.9rem">まだ作品がありません。下のフォームから追加してください。</p>'

    return HTMLResponse(TOP_TMPL.format(novel_cards=cards))

@app.get("/{novel_code}/", response_class=HTMLResponse)
async def novel_index(novel_code: str):
    f = SITE_DIR / novel_code / "index.html"
    if not f.exists():
        raise HTTPException(status_code=404, detail="作品が見つかりません")
    return HTMLResponse(f.read_text(encoding="utf-8"))

@app.get("/{novel_code}/{episode_num}", response_class=HTMLResponse)
async def episode_page(novel_code: str, episode_num: str):
    f = SITE_DIR / novel_code / f"{int(episode_num):04d}.html"
    if not f.exists():
        raise HTTPException(status_code=404, detail="話が見つかりません")
    return HTMLResponse(f.read_text(encoding="utf-8"))

app.mount("/static", StaticFiles(directory="static"), name="static")

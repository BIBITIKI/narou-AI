"""
なろう小説パイプライン - ローカル実行版

LLMの役割分担:
  Ollama (quality="fast") → Step0(固有名詞スキャン) / Step3(話単位の校正)
  Gemini (quality="smart") → Step1(文体評価) / Step2(ブロック分析) / Step4(語句解説)

  Gemini無料枠(RPD=20)の1作品あたり消費:
    Step1: 1回
    Step2: 数回（ブロック数 ≒ 総話数/30）
    Step4: 40回
    合計: ~50回 → RPD=20を3日分消費
    → --skip-glossary で Step4 を後日実行できる

使い方:
  # 全話処理（Ollama必須、Gemini推奨）
  OLLAMA_MODEL=qwen2.5:7b GEMINI_API_KEY=xxx python scripts/pipeline.py n9999zz

  # 動作確認（先頭3話）
  python scripts/pipeline.py n9999zz --max-ep 3

  # Step4(語句解説)を後日実行
  python scripts/pipeline.py n9999zz --glossary-only

  # 新話のみ更新
  python scripts/pipeline.py n9999zz --update-only
"""

import json, re, sys, time, argparse, collections, logging, os, subprocess, statistics
import urllib.request, urllib.error
from pathlib import Path
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

# ── LLM設定 ───────────────────────────────────────────────────────────────────
OLLAMA_URL   = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "")      # 例: qwen2.5:7b
GEMINI_KEY   = os.environ.get("GEMINI_API_KEY", "")
GEMINI_URL   = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_PRO   = "gemini-2.5-flash"       # Step1/2/4 担当（全体把握・解説生成）
GEMINI_FLASH = "gemini-2.5-flash-lite"  # Geminiのみ使用時の高頻度フォールバック

USE_OLLAMA = bool(OLLAMA_MODEL)
USE_GEMINI = bool(GEMINI_KEY)

# ── その他設定 ────────────────────────────────────────────────────────────────
BASE_URL    = "https://ncode.syosetu.com"
ROOT_DIR    = Path(__file__).parent.parent
DATA_DIR    = ROOT_DIR / "docs" / "data"
CRAWL_DELAY = 1.5
SCAN_SAMPLE = 5   # 平均文字数算出のサンプル話数

CTX_CHARS         = 1_000_000
BODY_BUDGET_RATIO = 0.40
OVERLAP_RATIO     = 0.20
MIN_BLOCK         = 3
MIN_OVERLAP       = 1
STYLE_SAMPLE      = 20
STYLE_QUALITY_THRESHOLD = 0.55

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NarouReaderBot/1.0; personal use)",
    "Accept-Language": "ja,en;q=0.9",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger()

# ── LLM呼び出し ───────────────────────────────────────────────────────────────

def call_ollama(prompt, max_tokens=4096):
    payload = json.dumps({
        "model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
        "options": {"num_predict": max_tokens, "temperature": 0.3},
    }).encode()
    req = urllib.request.Request(f"{OLLAMA_URL}/api/generate", data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read()).get("response", "")

def call_gemini(prompt, model=None, max_tokens=4096, retries=3):
    if not GEMINI_KEY:
        raise RuntimeError("GEMINI_API_KEY が未設定です")
    model = model or GEMINI_PRO
    url   = f"{GEMINI_URL}/{model}:generateContent?key={GEMINI_KEY}"
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.3},
    }).encode()
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, data=payload,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read())
            parts = data.get("candidates",[{}])[0].get("content",{}).get("parts",[])
            return "".join(p.get("text","") for p in parts)
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            if e.code == 429:
                wait = 60 * attempt
                log.warning(f"[gemini] 429 → {wait}s 待機")
                time.sleep(wait)
            elif attempt == retries:
                raise RuntimeError(f"Gemini {e.code}: {body[:200]}")
            else:
                time.sleep(10 * attempt)
    return ""

def call_llm(prompt, quality="fast", max_tokens=4096):
    """
    quality="fast"  → Ollama担当（Step0/3: 話単位の高頻度処理）
    quality="smart" → Gemini担当（Step1/2/4: 全体把握・解説生成）

    両方未設定の場合はエラー。
    Ollamaのみ設定時は全StepをOllamaで処理（品質は落ちる）。
    Geminiのみ設定時は全StepをGeminiで処理（RPD消費に注意）。
    """
    if quality == "fast":
        if USE_OLLAMA:
            return call_ollama(prompt, max_tokens)
        elif USE_GEMINI:
            # GeminiのみのときはFlashを使いRPMスロットリング
            time.sleep(12)  # RPM=5 → 12秒/req
            return call_gemini(prompt, model=GEMINI_FLASH, max_tokens=max_tokens)
        else:
            raise RuntimeError("OLLAMA_MODEL か GEMINI_API_KEY を設定してください")
    else:  # smart
        if USE_GEMINI:
            return call_gemini(prompt, model=GEMINI_PRO, max_tokens=max_tokens)
        elif USE_OLLAMA:
            log.warning("[LLM] Gemini未設定のためOllamaでsmart処理（品質低下の可能性）")
            return call_ollama(prompt, max_tokens)
        else:
            raise RuntimeError("OLLAMA_MODEL か GEMINI_API_KEY を設定してください")

def parse_json(raw):
    m = re.search(r'\{[\s\S]+\}', raw.strip())
    if m:
        try: return json.loads(m.group())
        except: pass
    return {}

# ── ウィンドウ計算 ────────────────────────────────────────────────────────────
def calc_window(avg_chars_per_ep):
    token_per_ep = avg_chars_per_ep * 1.5
    block_size   = max(MIN_BLOCK, int(CTX_CHARS * BODY_BUDGET_RATIO / max(token_per_ep, 1)))
    # Ollamaはコンテキスト長が短めなので上限を設ける
    if USE_OLLAMA and not USE_GEMINI:
        block_size = min(block_size, 15)
    overlap_size = max(MIN_OVERLAP, int(block_size * OVERLAP_RATIO))
    stride       = block_size - overlap_size
    log.info(f"[window] 平均{avg_chars_per_ep:,}字/話 → ブロック{block_size}話")
    return block_size, overlap_size, stride

def make_blocks(episodes, block_size, stride):
    blocks, i = [], 0
    while i < len(episodes):
        blocks.append(episodes[i:i + block_size])
        if i + block_size >= len(episodes): break
        i += stride
    return blocks

# ── スクレイプ ────────────────────────────────────────────────────────────────
def safe_get(session, url):
    for attempt in range(1, 4):
        try:
            time.sleep(CRAWL_DELAY)
            r = session.get(url, timeout=20)
            r.raise_for_status()
            return r
        except requests.HTTPError:
            if r.status_code == 429: time.sleep(10 * attempt)
            elif attempt == 3: return None
        except requests.RequestException:
            if attempt == 3: return None
            time.sleep(5)
    return None

def parse_meta(html):
    soup = BeautifulSoup(html, "lxml")
    def g(sel):
        t = soup.select_one(sel)
        return t.get_text(strip=True) if t else ""
    return {
        "title":    g(".novel_title"),
        "author":   g(".novel_writername a") or g(".novel_writername"),
        "synopsis": g("#novel_ex"),
        "genre":    g(".genre a") or g(".genre"),
        "tags":     [t.get_text(strip=True) for t in soup.select(".keyword a")],
    }

def parse_toc(html):
    soup = BeautifulSoup(html, "lxml")
    episodes, chapter, no = [], "", 0
    box = soup.select_one(".index_box")
    if not box: return episodes
    for el in box.children:
        if not hasattr(el, "get"): continue
        if "chapter_title" in el.get("class", []):
            chapter = el.get_text(strip=True); continue
        if el.name == "dl" and "novel_sublist2" in el.get("class", []):
            a = el.select_one("dd.subtitle a")
            if not a: continue
            no += 1
            href   = a.get("href", "")
            url    = BASE_URL + href if href.startswith("/") else href
            dt     = el.select_one("dt")
            posted = ""
            if dt:
                t = dt.select_one("time")
                posted = t["datetime"] if t and t.get("datetime") else dt.get_text(strip=True)
            episodes.append({"episode_no": no, "chapter": chapter,
                "title": a.get_text(strip=True), "url": url, "posted_at": posted})
    return episodes

def parse_body(html):
    soup  = BeautifulSoup(html, "lxml")
    parts = []
    for sid in ["novel_p", "novel_honbun", "novel_a"]:
        sec = soup.select_one(f"#{sid}")
        if sec:
            for br in sec.find_all("br"): br.replace_with("\n")
            parts.append(sec.get_text())
    body   = "\n\n".join(p.strip() for p in parts if p.strip())
    posted = updated = ""
    info   = soup.select_one(".novel_writetime")
    if info:
        ts = info.select("time")
        if ts: posted = ts[0].get("datetime", ts[0].get_text(strip=True))
        if len(ts) >= 2: updated = ts[1].get("datetime", ts[1].get_text(strip=True))
    title = soup.select_one(".novel_subtitle")
    return {"title": title.get_text(strip=True) if title else "",
            "body": body, "posted_at": posted, "updated_at": updated}

def scan_avg_chars(ncode):
    """先頭SCAN_SAMPLE話をスクレイプして平均文字数を返す"""
    session = requests.Session()
    session.headers.update(HEADERS)
    r = safe_get(session, f"{BASE_URL}/{ncode}/")
    if not r: return 3000
    episodes = parse_toc(r.text)
    if not episodes:
        return max(1, len(parse_body(r.text).get("body", "")))
    counts = []
    for ep in episodes[:SCAN_SAMPLE]:
        r2 = safe_get(session, ep["url"])
        if not r2: continue
        ed = parse_body(r2.text)
        if ed.get("body"): counts.append(len(ed["body"]))
    return int(statistics.mean(counts)) if counts else 3000

def scrape(ncode, existing_nos=None, max_ep=None):
    session = requests.Session()
    session.headers.update(HEADERS)
    r = safe_get(session, f"{BASE_URL}/{ncode}/")
    if not r: raise RuntimeError("TOC取得失敗")
    meta     = parse_meta(r.text)
    episodes = parse_toc(r.text)
    if not episodes:
        ed = parse_body(r.text)
        return meta, [{"episode_no":1,"chapter":"","title":ed["title"] or meta["title"],
            "url":f"{BASE_URL}/{ncode}/","posted_at":ed["posted_at"],
            "body":ed["body"],"updated_at":""}], 1
    total = len(episodes)
    if existing_nos:
        episodes = [ep for ep in episodes if ep["episode_no"] not in existing_nos]
        log.info(f"[scrape] 新話 {len(episodes)} 件")
    if max_ep:
        episodes = episodes[:max_ep]
    for i, ep in enumerate(episodes, 1):
        log.info(f"[scrape] [{i}/{len(episodes)}] 第{ep['episode_no']}話")
        r2 = safe_get(session, ep["url"])
        if not r2: ep["body"] = ep["updated_at"] = ""; continue
        ed = parse_body(r2.text)
        ep["body"] = ed["body"]; ep["updated_at"] = ed["updated_at"]
        if ed["posted_at"]: ep["posted_at"] = ed["posted_at"]
    meta["total_episodes"] = total
    return meta, episodes, total

# ── Step0: 固有名詞スキャン（Ollama）────────────────────────────────────────
SCAN_PROMPT = """以下の小説本文を分析し、JSONのみ返答（前置き不要）。
## 本文（第{ep_no}話「{title}」）
{body}

{{"episode_no":{ep_no},"proper_nouns":[{{"term":"...","variants":["..."],"correct":"...","kind":"人物|地名|スキル|魔物|組織|その他"}}],"writing_issues":[{{"wrong":"...","correct":"...","kind":"typo|particle|conjugation"}}]}}"""

def step0_scan(episodes):
    log.info(f"[Step0] 固有名詞スキャン（{len(episodes)}話）← Ollama担当")
    noun_counter, noun_map, all_issues = collections.Counter(), {}, []
    for i, ep in enumerate(episodes, 1):
        log.info(f"  [{i}/{len(episodes)}] 第{ep['episode_no']}話")
        prompt = SCAN_PROMPT.format(ep_no=ep["episode_no"],
            title=ep.get("title",""), body=ep.get("body","")[:3000])
        try:
            obj = parse_json(call_llm(prompt, quality="fast", max_tokens=1024))
            for pn in obj.get("proper_nouns", []):
                correct = pn.get("correct", pn.get("term", ""))
                noun_counter[correct] += 1
                for v in pn.get("variants", []): noun_map[v] = correct
                noun_map[correct] = correct
            all_issues.extend(obj.get("writing_issues", []))
        except Exception as e:
            log.warning(f"  scan error: {e}")
    seen, unique = set(), []
    for iss in all_issues:
        key = (iss.get("wrong",""), iss.get("correct",""))
        if key not in seen: seen.add(key); unique.append(iss)
    char_counts = [len(ep.get("body","")) for ep in episodes if ep.get("body")]
    avg_chars   = int(statistics.mean(char_counts)) if char_counts else 3000
    return {"proper_nouns": [{"term":t,"freq":f} for t,f in noun_counter.most_common()],
            "noun_variants": noun_map, "writing_issues": unique,
            "avg_chars_per_ep": avg_chars}

# ── Step1: 文体評価（Gemini）─────────────────────────────────────────────────
BASIC_PRINCIPLES = {
    "style_summary": "日本語小説の基本原則",
    "sentence_endings": ["〜た。","〜だ。","〜ている。"],
    "emotion_style": "感情を直接書かず行動・身体反応・情景で示す",
    "dialogue_balance": "会話3〜4行に対し地の文1〜2行",
    "characteristic_phrases": [],
    "do_not_change": ["作者固有の口癖・記号・キャラの口調"],
    "source": "basic_principles", "quality_score": 0.0,
}

def score_style_quality(bodies_text):
    scores = {}
    endings = re.findall(r'[^。！？\n]{1,}[。！？]', bodies_text)
    if endings:
        tail_counter = collections.Counter(s[-3:] for s in endings if len(s) >= 3)
        top3_ratio   = sum(v for _, v in tail_counter.most_common(3)) / max(len(endings), 1)
        scores["diversity"] = max(0.0, 1.0 - top3_ratio)
    else:
        scores["diversity"] = 0.0
    TELL = r'(嬉しかった|悲しかった|怖かった|恥ずかしかった|驚いた|感動した|ほっとした)'
    scores["show_rate"] = max(0.0, 1.0-(len(re.findall(TELL,bodies_text))/max(len(endings),1))*3)
    sent_lens = [len(s) for s in endings]
    scores["length_variance"] = min(1.0, statistics.stdev(sent_lens)/max(statistics.mean(sent_lens),1)) \
                                 if len(sent_lens) >= 3 else 0.0
    dialogues  = re.findall(r'「[^」]+」', bodies_text)
    dial_ratio = sum(len(d) for d in dialogues) / max(len(bodies_text), 1)
    scores["dialogue_balance"] = 1.0 if 0.15 <= dial_ratio <= 0.50 \
                                  else max(0.0, 1.0-abs(dial_ratio-0.325)*4)
    total = sum(scores.values()) / len(scores)
    log.info(f"[Step1] 品質スコア: {total:.2f}")
    return total

STYLE_LEARN_PROMPT = """「{title}」後期（第{start}〜{end}話）の文体を分析し校正スタイルガイドを作成。
## 本文
{bodies}
JSONのみ: {{"style_summary":"...","sentence_endings":["..."],"emotion_style":"...","dialogue_balance":"...","characteristic_phrases":["..."],"do_not_change":["..."]}}"""

def step1_evaluate_style(title, episodes):
    log.info("[Step1] 文体評価 ← Gemini担当（全体把握）")
    sample      = episodes[-STYLE_SAMPLE:] if len(episodes) >= STYLE_SAMPLE else episodes
    bodies_text = "\n".join(ep.get("body","") for ep in sample)
    quality_score = score_style_quality(bodies_text)
    if quality_score >= STYLE_QUALITY_THRESHOLD:
        bodies_str = "\n\n---\n\n".join(
            f"【第{ep['episode_no']}話】\n{ep.get('body','')}" for ep in sample)
        obj = parse_json(call_llm(STYLE_LEARN_PROMPT.format(
            title=title, start=sample[0]["episode_no"],
            end=sample[-1]["episode_no"], bodies=bodies_str),
            quality="smart", max_tokens=2048))
        obj["source"] = "learned"; obj["quality_score"] = quality_score
        return obj
    result = dict(BASIC_PRINCIPLES); result["quality_score"] = quality_score
    return result

# ── Step2: ブロック分析（Gemini）─────────────────────────────────────────────
BLOCK_PROMPT = """小説「{title}」の編集長として第{start}〜{end}話の校正指示書を作成。
## 文体ガイド
{style_guide}
## 固有名詞索引（変更禁止）
{proper_nouns}
## 前ブロック要約
{prev_summary}
## 本文
{bodies}
JSONのみ: {{"block_summary":"...","fix_rules":[{{"wrong":"...","correct":"...","reason":"..."}}],"preserve_rules":[{{"pattern":"...","reason":"..."}}],"rewrite_instructions":{{"priority_fixes":["..."],"tone_target":"..."}}}}"""

def step2_analyze_blocks(title, episodes, raw_index, style_guide, block_size, stride):
    log.info("[Step2] ブロック分析 ← Gemini担当（整合性・指示書生成）")
    blocks = make_blocks(episodes, block_size, stride)
    instructions, summaries, ep_to_block = {}, {}, {}
    for bi, block in enumerate(blocks):
        for ep in block: ep_to_block[ep["episode_no"]] = bi
    pn_str    = json.dumps(raw_index.get("proper_nouns",[])[:50], ensure_ascii=False)
    style_str = json.dumps(style_guide, ensure_ascii=False)
    for bi, block in enumerate(blocks):
        start = block[0]["episode_no"]; end = block[-1]["episode_no"]
        log.info(f"  ブロック{bi+1}/{len(blocks)}: 第{start}〜{end}話")
        bodies = "\n\n---\n\n".join(
            f"【第{ep['episode_no']}話】\n{ep.get('body','')}" for ep in block)
        try:
            obj = parse_json(call_llm(BLOCK_PROMPT.format(
                title=title, start=start, end=end, style_guide=style_str,
                proper_nouns=pn_str, prev_summary=summaries.get(bi-1,"（初ブロック）"),
                bodies=bodies), quality="smart", max_tokens=4096))
            instructions[bi] = obj; summaries[bi] = obj.get("block_summary","")
            time.sleep(1.0)
        except Exception as e:
            log.warning(f"  ブロック{bi+1} 失敗: {e}")
            instructions[bi] = {}; summaries[bi] = ""
    return instructions, summaries, ep_to_block

# ── Step3: 話単位の校正（Ollama）─────────────────────────────────────────────
REWRITE_PROMPT = """日本語小説校正者として指示書に従い本文を書き直す。
絶対ルール: 固有名詞変更禁止/セリフ変更禁止/ストーリー変更禁止/最小限修正
## 修正ルール: {fix_rules}
## 保持ルール: {preserve_rules}
## 文体目標: {rewrite_instructions}
## 文脈（この話で起きていること）: {block_summary}
## 本文（第{ep_no}話「{title}」）
{body}
書き直した本文のみ返す。"""

def step3_rewrite(episodes, instructions, summaries, ep_to_block, max_tokens_ep):
    log.info(f"[Step3] 話単位の校正（{len(episodes)}話）← Ollama担当")
    last_bi = max(instructions.keys(), default=0)
    for i, ep in enumerate(episodes, 1):
        log.info(f"  [{i}/{len(episodes)}] 第{ep['episode_no']}話")
        bi   = ep_to_block.get(ep["episode_no"], last_bi)
        inst = instructions.get(bi, {})
        try:
            result = call_llm(REWRITE_PROMPT.format(
                ep_no=ep["episode_no"], title=ep.get("title",""),
                body=ep.get("body",""),
                fix_rules=json.dumps(inst.get("fix_rules",[]), ensure_ascii=False),
                preserve_rules=json.dumps(inst.get("preserve_rules",[]), ensure_ascii=False),
                rewrite_instructions=json.dumps(inst.get("rewrite_instructions",{}), ensure_ascii=False),
                block_summary=summaries.get(bi,""),
            ), quality="fast", max_tokens=max_tokens_ep)
            ep["body"] = result.strip() or ep["body"]
        except Exception as e:
            log.warning(f"  失敗（原文使用）: {e}")
    return episodes

# ── Step4: 語句解説（Gemini）─────────────────────────────────────────────────
SKIP_TERMS = {"名前","レベル","職業","ステータス","サラリーマン","ポジティブ","ヨーロッパ","ファンタジー"}
TERM_PATS  = [(r'[ァ-ヶー一-龯々]+＝[ァ-ヶー一-龯々]+', None),(r'【([^】]+)】', 1),
              (r'《([^》]+)》', 1),(r'「([ァ-ヶー]{3,})」', 1),(r'[ァ-ヶー]{4,}', None)]
GLOSS_PROMPT = """小説「{title}」用語解説。第{ep_no}話時点の読者向けに「{term}」を200字以内で解説。
第{ep_no}話より後の情報は含めない。文脈: {ctx}
JSONのみ: {{"kind":"人物|地名|スキル|職業|魔物|組織|その他","desc":"解説","first_ep":{first_ep}}}"""

def step4_glossary(title, episodes):
    log.info("[Step4] 語句解説生成 ← Gemini担当（知識・説明生成）")
    all_text = "\n".join(e["body"] for e in episodes)
    cnt = collections.Counter()
    for pat, grp in TERM_PATS:
        for m in re.finditer(pat, all_text):
            cnt[m.group(grp) if grp else m.group()] += 1
    terms = [t for t,_ in cnt.most_common(40) if t not in SKIP_TERMS]
    log.info(f"  語句候補: {len(terms)}件（Gemini RPD消費: 約{len(terms)*2}回）")
    glossary = {}
    for term in terms:
        first_ep = next((ep["episode_no"] for ep in episodes if term in ep.get("body","")), None)
        if first_ep is None: continue
        glossary[term] = {"first_ep": first_ep, "by_ep": {}}
        for ep_no in sorted({first_ep, episodes[-1]["episode_no"]}):
            ep = next((e for e in episodes if e["episode_no"] == ep_no), None)
            if ep is None: continue
            ctx = []
            for e in episodes:
                if e["episode_no"] > ep_no: break
                for mm in re.finditer(re.escape(term), e.get("body","")):
                    s=max(0,mm.start()-50); en=min(len(e["body"]),mm.end()+50)
                    ctx.append(f"第{e['episode_no']}話:…{e['body'][s:en].replace(chr(10),'　')}…")
                    if len(ctx)>=2: break
                if len(ctx)>=2: break
            if not ctx: continue
            try:
                obj = parse_json(call_llm(GLOSS_PROMPT.format(
                    title=title, term=term, ep_no=ep_no,
                    ctx="\n".join(f"- {c}" for c in ctx), first_ep=first_ep),
                    quality="smart", max_tokens=400))
                if obj: glossary[term]["by_ep"][str(ep_no)] = obj
                time.sleep(1.0)  # Gemini RPM=5 対策
            except Exception as e:
                log.warning(f"  [{term}]: {e}")
    log.info(f"[Step4] 完了: {len(glossary)}件")
    return glossary

# ── 出力・Git ─────────────────────────────────────────────────────────────────
def update_index(ncode, meta):
    index_path = DATA_DIR / "index.json"
    index = []
    if index_path.exists():
        try: index = json.loads(index_path.read_text(encoding="utf-8"))
        except: pass
    entry = {"ncode": ncode, "title": meta.get("title",""),
             "author": meta.get("author",""),
             "total_episodes": meta.get("total_episodes", 0),
             "updated_at": datetime.now(timezone.utc).isoformat()}
    index = [e for e in index if e.get("ncode") != ncode]
    index.append(entry)
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"[index] index.json 更新: {len(index)}作品")

def git_push(ncode):
    try:
        subprocess.run(["git","add","docs/data/"], cwd=ROOT_DIR, check=True)
        if subprocess.run(["git","diff","--cached","--quiet"], cwd=ROOT_DIR).returncode != 0:
            subprocess.run(["git","commit","-m",
                f"data: update {ncode} [{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC]"],
                cwd=ROOT_DIR, check=True)
            subprocess.run(["git","push","origin","HEAD"], cwd=ROOT_DIR, check=True)
            log.info("[git] push完了 → Cloudflare Pagesが自動デプロイします")
        else:
            log.info("[git] 変更なし")
    except subprocess.CalledProcessError as e:
        log.error(f"[git] 失敗: {e}")

# ── メイン ────────────────────────────────────────────────────────────────────
def run(ncode, update_only=False, max_ep=None, skip_glossary=False, glossary_only=False):
    if not USE_OLLAMA and not USE_GEMINI:
        log.error("LLMが設定されていません。OLLAMA_MODEL または GEMINI_API_KEY を設定してください")
        sys.exit(1)

    llm_info = []
    if USE_OLLAMA: llm_info.append(f"Ollama({OLLAMA_MODEL}): Step0/3")
    if USE_GEMINI: llm_info.append(f"Gemini({GEMINI_PRO}): Step1/2/4")
    if USE_OLLAMA and not USE_GEMINI: llm_info.append("※Gemini未設定: Step1/2/4もOllamaで処理")
    log.info(f"[LLM] {' / '.join(llm_info)}")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / f"{ncode}.json"

    existing = {}
    if out_path.exists():
        with open(out_path, encoding="utf-8") as f:
            existing = json.load(f)

    # --glossary-only: 既存データに語句解説だけ追加して終了
    if glossary_only:
        if not existing:
            log.error("既存データがありません。先に通常処理を実行してください")
            sys.exit(1)
        log.info("=== Step4のみ実行（語句解説追加）===")
        all_eps  = existing.get("episodes", [])
        glossary = step4_glossary(existing["meta"]["title"], all_eps)
        existing["glossary"] = glossary
        existing["generated_at"] = datetime.now(timezone.utc).isoformat()
        out_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
        update_index(ncode, existing["meta"])
        git_push(ncode)
        return out_path

    existing_nos = {ep["episode_no"] for ep in existing.get("episodes",[])} if update_only else None

    # 平均文字数（キャッシュ済みなら再スキャン不要）
    avg_chars = existing.get("pipeline_meta",{}).get("avg_chars_per_ep")
    if not avg_chars:
        log.info("[scan] 平均文字数を計測中（先頭5話）...")
        avg_chars = scan_avg_chars(ncode)
    log.info(f"[scan] 平均 {avg_chars:,}字/話")
    max_tokens_ep = min(8192, max(4096, int(avg_chars * 1.5 * 1.1)))

    log.info("=== スクレイプ ===")
    meta, new_eps, total = scrape(ncode, existing_nos, max_ep)
    if not new_eps:
        log.info("対象話なし"); return out_path

    title = meta.get("title") or existing.get("meta",{}).get("title","")

    log.info("=== Step0: 固有名詞スキャン（Ollama）===")
    raw_index = step0_scan(new_eps)
    block_size, _, stride = calc_window(avg_chars)

    log.info("=== Step1: 文体評価（Gemini）===")
    style_guide = step1_evaluate_style(title, existing.get("episodes",[]) + new_eps)

    log.info("=== Step2: ブロック分析（Gemini）===")
    instructions, summaries, ep_to_block = step2_analyze_blocks(
        title, new_eps, raw_index, style_guide, block_size, stride)

    log.info("=== Step3: 話単位の校正（Ollama）===")
    new_eps = step3_rewrite(new_eps, instructions, summaries, ep_to_block, max_tokens_ep)

    all_eps = existing.get("episodes",[]) + [
        {"episode_no":ep["episode_no"],"chapter":ep.get("chapter",""),
         "title":ep.get("title",""),"posted_at":ep.get("posted_at",""),
         "body":ep.get("body","")}
        for ep in new_eps
    ]
    all_eps.sort(key=lambda e: e["episode_no"])

    glossary = existing.get("glossary", {})
    if not skip_glossary:
        log.info("=== Step4: 語句解説（Gemini）===")
        glossary = step4_glossary(title, all_eps)
    else:
        log.info("=== Step4: スキップ（--skip-glossary）→ 後日 --glossary-only で実行可 ===")

    result = {
        "ncode": ncode,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pipeline_meta": {
            "avg_chars_per_ep": avg_chars,
            "max_tokens_ep":    max_tokens_ep,
            "llm_ollama":       OLLAMA_MODEL or "未使用",
            "llm_gemini":       GEMINI_PRO if USE_GEMINI else "未使用",
        },
        "meta": {
            "title":          title,
            "author":         meta.get("author") or existing.get("meta",{}).get("author",""),
            "synopsis":       meta.get("synopsis") or existing.get("meta",{}).get("synopsis",""),
            "genre":          meta.get("genre") or existing.get("meta",{}).get("genre",""),
            "tags":           meta.get("tags") or existing.get("meta",{}).get("tags",[]),
            "total_episodes": len(all_eps),
        },
        "style_guide": style_guide,
        "glossary":    glossary,
        "episodes":    all_eps,
    }
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"=== 保存: {out_path} ({len(all_eps)}話, 語句{len(glossary)}件) ===")
    update_index(ncode, result["meta"])
    git_push(ncode)
    return out_path

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="なろう小説校正パイプライン")
    p.add_argument("ncode",                                    help="Nコード 例: n9999zz")
    p.add_argument("--max-ep",        type=int, default=None, help="最大処理話数（動作確認用）")
    p.add_argument("--update-only",   action="store_true",    help="新話のみ処理")
    p.add_argument("--skip-glossary", action="store_true",    help="Step4をスキップ（Gemini RPD節約）")
    p.add_argument("--glossary-only", action="store_true",    help="Step4のみ実行（後日追加用）")
    args = p.parse_args()
    run(args.ncode, args.update_only, args.max_ep, args.skip_glossary, args.glossary_only)

"""
なろう小説パイプライン - 2パス + 動的Sliding Window設計

  Step0: Flash  全話スキャン → 固有名詞索引・平均文字数算出
  Step1: 2.5Pro 後期サンプル → 文体品質スコアリング → 参考採用 or 基本原則にフォールバック
  Step2: 2.5Pro 順方向・動的オーバーラップで指示書を累積生成
  Step3: Flash  最終指示書+ブロック要約で各話校正
  Step4: Flash  語句解説生成
  完了後: GitHubに自動push
"""

import json, re, sys, time, argparse, collections, logging, os, subprocess, math, statistics
import urllib.request, urllib.error
from pathlib import Path
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

# ── 設定 ──────────────────────────────────────────────────────────────────────
BASE_URL     = "https://ncode.syosetu.com"
GEMINI_URL   = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_PRO   = "gemini-2.5-pro-preview-03-25"
GEMINI_FLASH = "gemini-2.0-flash"

ROOT_DIR  = Path(__file__).parent.parent
DATA_DIR  = ROOT_DIR / "data"
CRAWL_DELAY = 1.5

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GIT_TOKEN  = os.environ.get("GIT_TOKEN", "")
GIT_REPO   = os.environ.get("GIT_REPO", "")

# 動的ウィンドウ計算定数
CTX_CHARS        = 1_000_000 * 1.5   # Gemini 1M token ≈ 150万文字
BODY_BUDGET_RATIO = 0.40             # コンテキストのうち本文に使う割合
OVERLAP_RATIO     = 0.20             # ブロックサイズに対するオーバーラップ比
MIN_BLOCK         = 5
MIN_OVERLAP       = 2
STYLE_SAMPLE      = 30               # 後期から取るサンプル話数

# 後期文体品質スコアの閾値（これ以上なら参考採用）
STYLE_QUALITY_THRESHOLD = 0.55

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NarouReaderBot/1.0; personal use)",
    "Accept-Language": "ja,en;q=0.9",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger()

# ── 動的ウィンドウ計算 ────────────────────────────────────────────────────────

def calc_window(avg_chars_per_ep):
    """平均文字数からブロックサイズとオーバーラップを動的算出"""
    body_budget  = CTX_CHARS * BODY_BUDGET_RATIO
    block_size   = max(MIN_BLOCK, int(body_budget / avg_chars_per_ep))
    overlap_size = max(MIN_OVERLAP, int(block_size * OVERLAP_RATIO))
    stride       = block_size - overlap_size
    log.info(f"[window] 平均{avg_chars_per_ep:,}字/話 → ブロック{block_size}話 / オーバーラップ{overlap_size}話 / stride{stride}話")
    return block_size, overlap_size, stride

def make_blocks(episodes, block_size, stride):
    """スライディングウィンドウでブロック分割"""
    blocks, i = [], 0
    while i < len(episodes):
        blocks.append(episodes[i:i + block_size])
        if i + block_size >= len(episodes):
            break
        i += stride
    return blocks

# ── HTTP ──────────────────────────────────────────────────────────────────────

def safe_get(session, url):
    for attempt in range(1, 4):
        try:
            time.sleep(CRAWL_DELAY)
            r = session.get(url, timeout=20)
            r.raise_for_status()
            return r
        except requests.HTTPError:
            if r.status_code == 429:
                time.sleep(10 * attempt)
            elif attempt == 3:
                return None
        except requests.RequestException:
            if attempt == 3:
                return None
            time.sleep(5)
    return None

# ── スクレイプ ────────────────────────────────────────────────────────────────

def parse_meta(html):
    soup = BeautifulSoup(html, "html.parser")
    def g(sel):
        t = soup.select_one(sel)
        return t.get_text(strip=True) if t else ""
    return {
        "title":  g(".novel_title"),
        "author": g(".novel_writername a") or g(".novel_writername"),
        "synopsis": g("#novel_ex"),
        "genre":  g(".genre a") or g(".genre"),
        "tags":   [t.get_text(strip=True) for t in soup.select(".keyword a")],
    }

def parse_toc(html):
    soup = BeautifulSoup(html, "html.parser")
    episodes, chapter, no = [], "", 0
    box = soup.select_one(".index_box")
    if not box:
        return episodes
    for el in box.children:
        if not hasattr(el, "get"):
            continue
        if "chapter_title" in el.get("class", []):
            chapter = el.get_text(strip=True)
            continue
        if el.name == "dl" and "novel_sublist2" in el.get("class", []):
            a = el.select_one("dd.subtitle a")
            if not a:
                continue
            no += 1
            href = a.get("href", "")
            url  = BASE_URL + href if href.startswith("/") else href
            dt   = el.select_one("dt")
            posted = ""
            if dt:
                t = dt.select_one("time")
                posted = t["datetime"] if t and t.get("datetime") else dt.get_text(strip=True)
            episodes.append({
                "episode_no": no, "chapter": chapter,
                "title": a.get_text(strip=True), "url": url, "posted_at": posted,
            })
    return episodes

def parse_body(html):
    soup  = BeautifulSoup(html, "html.parser")
    parts = []
    for sid in ["novel_p", "novel_honbun", "novel_a"]:
        sec = soup.select_one(f"#{sid}")
        if sec:
            for br in sec.find_all("br"):
                br.replace_with("\n")
            parts.append(sec.get_text())
    body   = "\n\n".join(p.strip() for p in parts if p.strip())
    posted = updated = ""
    info   = soup.select_one(".novel_writetime")
    if info:
        ts = info.select("time")
        if ts:
            posted = ts[0].get("datetime", ts[0].get_text(strip=True))
        if len(ts) >= 2:
            updated = ts[1].get("datetime", ts[1].get_text(strip=True))
    title = soup.select_one(".novel_subtitle")
    return {
        "title":      title.get_text(strip=True) if title else "",
        "body":       body,
        "posted_at":  posted,
        "updated_at": updated,
    }

def scrape(ncode, max_episodes=None, existing_nos=None):
    session = requests.Session()
    session.headers.update(HEADERS)
    toc_url = f"{BASE_URL}/{ncode}/"
    log.info(f"[scrape] {toc_url}")
    r = safe_get(session, toc_url)
    if not r:
        raise RuntimeError(f"TOC取得失敗: {toc_url}")
    meta     = parse_meta(r.text)
    episodes = parse_toc(r.text)
    if not episodes:
        ed = parse_body(r.text)
        return meta, [{"episode_no":1,"chapter":"","title":ed["title"] or meta["title"],
                       "url":toc_url,"posted_at":ed["posted_at"],"body":ed["body"],"updated_at":""}]
    if max_episodes:
        episodes = episodes[:max_episodes]
    if existing_nos:
        episodes = [ep for ep in episodes if ep["episode_no"] not in existing_nos]
        log.info(f"[scrape] 新話 {len(episodes)} 件")
    for i, ep in enumerate(episodes, 1):
        log.info(f"[scrape] [{i}/{len(episodes)}] 第{ep['episode_no']}話")
        r = safe_get(session, ep["url"])
        if not r:
            ep["body"] = ep["updated_at"] = ""; continue
        ed = parse_body(r.text)
        ep["body"] = ed["body"]; ep["updated_at"] = ed["updated_at"]
        if ed["posted_at"]: ep["posted_at"] = ed["posted_at"]
    meta["total_episodes"] = len(episodes)
    return meta, episodes

# ── Gemini API ────────────────────────────────────────────────────────────────

def call_gemini(prompt, model=GEMINI_FLASH, max_tokens=8192, retries=3):
    if not GEMINI_KEY:
        raise RuntimeError("GEMINI_API_KEY が未設定です")
    url     = f"{GEMINI_URL}/{model}:generateContent?key={GEMINI_KEY}"
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
                wait = 30 * attempt
                log.warning(f"[gemini] 429 → {wait}s 待機")
                time.sleep(wait)
            elif attempt == retries:
                raise RuntimeError(f"Gemini {e.code}: {body[:200]}")
            else:
                time.sleep(5 * attempt)
    return ""

def call_pro(prompt, max_tokens=8192):
    return call_gemini(prompt, model=GEMINI_PRO, max_tokens=max_tokens)

def call_flash(prompt, max_tokens=4096):
    return call_gemini(prompt, model=GEMINI_FLASH, max_tokens=max_tokens)

def parse_json(raw):
    m = re.search(r'\{[\s\S]+\}', raw.strip())
    if m:
        try: return json.loads(m.group())
        except: pass
    return {}

# ── Step0: 全話スキャン（Flash）────────────────────────────────────────────────

SCAN_PROMPT = """以下の小説本文を分析し、JSONのみ返答してください（前置き不要）。

## 本文（第{ep_no}話「{title}」）
{body}

{{"episode_no":{ep_no},"proper_nouns":[{{"term":"...","variants":["..."],"correct":"...","kind":"人物|地名|スキル|魔物|組織|その他"}}],"writing_issues":[{{"wrong":"...","correct":"...","kind":"typo|particle|conjugation"}}],"style_markers":{{"sentence_endings":["..."],"special_symbols":["..."]}}}}"""

def step0_scan(episodes):
    log.info(f"[Step0] 全話スキャン（{len(episodes)}話）")
    noun_counter, noun_map, all_issues = collections.Counter(), {}, []

    for i, ep in enumerate(episodes, 1):
        log.info(f"  [{i}/{len(episodes)}] 第{ep['episode_no']}話")
        prompt = SCAN_PROMPT.format(
            ep_no=ep["episode_no"],
            title=ep.get("title",""),
            body=ep.get("body","")[:3000])
        try:
            obj = parse_json(call_flash(prompt, max_tokens=1024))
            for pn in obj.get("proper_nouns", []):
                correct = pn.get("correct", pn.get("term", ""))
                noun_counter[correct] += 1
                for v in pn.get("variants", []): noun_map[v] = correct
                noun_map[correct] = correct
            all_issues.extend(obj.get("writing_issues", []))
            time.sleep(0.2)
        except Exception as e:
            log.warning(f"  scan error: {e}")

    seen, unique_issues = set(), []
    for iss in all_issues:
        key = (iss.get("wrong",""), iss.get("correct",""))
        if key not in seen:
            seen.add(key); unique_issues.append(iss)

    # 平均文字数算出
    char_counts = [len(ep.get("body","")) for ep in episodes if ep.get("body")]
    avg_chars   = int(statistics.mean(char_counts)) if char_counts else 5000
    log.info(f"[Step0] 平均文字数: {avg_chars:,}字/話")

    result = {
        "proper_nouns":   [{"term":t,"freq":f} for t,f in noun_counter.most_common()],
        "noun_variants":  noun_map,
        "writing_issues": unique_issues,
        "avg_chars_per_ep": avg_chars,
    }
    log.info(f"[Step0] 固有名詞{len(result['proper_nouns'])}件, 誤字{len(unique_issues)}件")
    return result

# ── Step1: 後期文体品質評価 + 参考採用判定（2.5 Pro）──────────────────────────

# 日本語小説の基本原則（後期品質が低い場合のフォールバック）
BASIC_PRINCIPLES = {
    "style_summary": "日本語小説の基本原則",
    "sentence_endings": ["〜た。","〜だ。","〜ている。","〜だった。（連続を避ける）"],
    "emotion_style": "感情を直接書かず行動・身体反応・情景で示す（Show don't tell）",
    "dialogue_balance": "会話3〜4行に対し地の文1〜2行を目安にバランスを取る",
    "characteristic_phrases": [],
    "do_not_change": ["作者固有の口癖・記号・キャラの口調"],
    "target_quality": "読者が映像として想起できる描写、テンポよく読める文体",
    "source": "basic_principles",
    "quality_score": 0.0,
}

def score_style_quality(bodies_text):
    """
    後期文体の品質を0〜1でスコアリング。
    閾値以上なら参考採用、未満なら基本原則にフォールバック。
    """
    scores = {}

    # 1. 文末多様性（上位3パターンが全文の60%以下なら良い）
    endings = re.findall(r'[^。！？\n]{1,}[。！？]', bodies_text)
    if endings:
        tail_counter = collections.Counter(s[-3:] for s in endings if len(s) >= 3)
        top3_ratio   = sum(v for _, v in tail_counter.most_common(3)) / max(len(endings), 1)
        scores["diversity"] = max(0.0, 1.0 - top3_ratio)
    else:
        scores["diversity"] = 0.0

    # 2. Show率（感情直書きの少なさ）
    TELL_PATTERNS = r'(嬉しかった|悲しかった|怖かった|恥ずかしかった|驚いた|感動した|ほっとした)'
    tell_count  = len(re.findall(TELL_PATTERNS, bodies_text))
    total_sents = max(len(endings), 1)
    scores["show_rate"] = max(0.0, 1.0 - (tell_count / total_sents) * 3)

    # 3. 文長分散（文の長短バリエーション）
    sent_lens = [len(s) for s in endings]
    if len(sent_lens) >= 3:
        cv = statistics.stdev(sent_lens) / max(statistics.mean(sent_lens), 1)
        scores["length_variance"] = min(1.0, cv)
    else:
        scores["length_variance"] = 0.0

    # 4. 会話バランス（15〜50%が理想）
    dialogues  = re.findall(r'「[^」]+」', bodies_text)
    dial_ratio = sum(len(d) for d in dialogues) / max(len(bodies_text), 1)
    if 0.15 <= dial_ratio <= 0.50:
        scores["dialogue_balance"] = 1.0
    else:
        scores["dialogue_balance"] = max(0.0, 1.0 - abs(dial_ratio - 0.325) * 4)

    total = sum(scores.values()) / len(scores)
    log.info(f"[Step1] 文体品質スコア: {total:.2f} "
             f"(多様性{scores['diversity']:.2f} / Show率{scores['show_rate']:.2f} / "
             f"文長分散{scores['length_variance']:.2f} / 会話{scores['dialogue_balance']:.2f})")
    return total, scores

STYLE_LEARN_PROMPT = """あなたは日本語小説の編集者です。
「{title}」の後期エピソード（第{start}〜{end}話）の文体を分析し、校正の参考スタイルガイドを作成してください。

注意: これは「参考情報」です。品質が高い部分は活かし、低い部分は日本語小説の基本原則で補います。

## 後期エピソード本文
{bodies}

JSONのみ返答：
{{"style_summary":"文体の特徴を一言で","sentence_endings":["実際に使われている自然な文末パターン"],"emotion_style":"感情描写のアプローチ","dialogue_balance":"会話と地の文のバランスの特徴","characteristic_phrases":["この作者らしい表現・語彙"],"do_not_change":["絶対に変えてはいけないパターン（口癖・記号等）"],"strengths":["この文体で優れている点"],"weaknesses":["この文体で改善が必要な点"]}}"""

def step1_evaluate_style(title, episodes):
    """後期文体を品質評価し、参考採用するか基本原則にフォールバックするか決定"""
    log.info("[Step1] 後期文体品質評価")
    sample    = episodes[-STYLE_SAMPLE:] if len(episodes) >= STYLE_SAMPLE else episodes
    bodies_text = "\n".join(ep.get("body","") for ep in sample)

    quality_score, score_detail = score_style_quality(bodies_text)

    if quality_score >= STYLE_QUALITY_THRESHOLD:
        log.info(f"[Step1] スコア{quality_score:.2f} ≥ 閾値{STYLE_QUALITY_THRESHOLD} → 後期文体を参考採用")
        bodies_str = "\n\n---\n\n".join(
            f"【第{ep['episode_no']}話「{ep.get('title','')}」】\n{ep.get('body','')}"
            for ep in sample)
        obj = parse_json(call_pro(STYLE_LEARN_PROMPT.format(
            title=title,
            start=sample[0]["episode_no"],
            end=sample[-1]["episode_no"],
            bodies=bodies_str), max_tokens=2048))
        obj["source"]        = "learned_from_later_episodes"
        obj["quality_score"] = quality_score
        obj["score_detail"]  = score_detail
        log.info(f"[Step1] 採用: {obj.get('style_summary','')}")
        return obj
    else:
        log.info(f"[Step1] スコア{quality_score:.2f} < 閾値{STYLE_QUALITY_THRESHOLD} → 基本原則にフォールバック")
        result = dict(BASIC_PRINCIPLES)
        result["quality_score"] = quality_score
        result["score_detail"]  = score_detail
        # do_not_change だけは後期から抽出（口癖・記号は品質に関係なく保持すべき）
        bodies_str = "\n\n---\n\n".join(
            f"【第{ep['episode_no']}話「{ep.get('title','')}」】\n{ep.get('body','')}"
            for ep in sample)
        preserve_prompt = f"""以下の小説本文から「絶対に変えてはいけない表現パターン」のみ抽出してください。
（作者固有の口癖・特殊記号・キャラ固有の口調など）
JSONのみ: {{"do_not_change":["パターン1","パターン2"]}}

## 本文
{bodies_str[:5000]}"""
        preserve_obj = parse_json(call_flash(preserve_prompt, max_tokens=512))
        if preserve_obj.get("do_not_change"):
            result["do_not_change"] = preserve_obj["do_not_change"]
        return result

# ── Step2: ブロック分析（2.5 Pro）─────────────────────────────────────────────

BLOCK_PROMPT = """あなたは日本語小説「{title}」の編集長です。
以下の情報を元に、ブロック（第{start}〜{end}話）の校正指示書を作成してください。

## 文体ガイド（{style_source}）
{style_guide}

## 固有名詞索引（変更禁止）
{proper_nouns}

## 前ブロックまでの確定指示書
{prev_inst}

## 前ブロック要約
{prev_summary}

## このブロックの本文
{bodies}

JSONのみ返答：
{{"block_summary":"主要な出来事・人物変化（次ブロックへの引き継ぎ）","new_proper_nouns":[{{"term":"...","kind":"...","desc":"定義"}}],"fix_rules":[{{"wrong":"...","correct":"...","reason":"..."}}],"preserve_rules":[{{"pattern":"...","reason":"..."}}],"rewrite_instructions":{{"style_gap":"このブロックの文体と目標のギャップ","priority_fixes":["優先修正ポイント（優先度順）"],"tone_target":"トーン調整目標"}},"context_notes":"次ブロック校正者への引き継ぎ情報"}}"""

def step2_analyze_blocks(title, episodes, raw_index, style_guide, block_size, stride):
    log.info("[Step2] ブロック分析開始")
    blocks       = make_blocks(episodes, block_size, stride)
    instructions, summaries, ep_to_block = {}, {}, {}

    for bi, block in enumerate(blocks):
        for ep in block:
            ep_to_block[ep["episode_no"]] = bi  # 後のブロックで上書き（最後のブロックが優先）

    pn_str     = json.dumps(raw_index.get("proper_nouns",[])[:50], ensure_ascii=False)
    style_str  = json.dumps(style_guide, ensure_ascii=False)
    style_src  = "後期実例から学習" if style_guide.get("source") == "learned_from_later_episodes" \
                 else "日本語小説基本原則（後期品質が基準未満のためフォールバック）"

    for bi, block in enumerate(blocks):
        start = block[0]["episode_no"]; end = block[-1]["episode_no"]
        log.info(f"  ブロック{bi+1}/{len(blocks)}: 第{start}〜{end}話（{len(block)}話）")
        bodies = "\n\n---\n\n".join(
            f"【第{ep['episode_no']}話「{ep.get('title','')}」】\n{ep.get('body','')}"
            for ep in block)
        try:
            obj = parse_json(call_pro(BLOCK_PROMPT.format(
                title=title, start=start, end=end,
                style_guide=style_str, style_source=style_src,
                proper_nouns=pn_str,
                prev_inst=json.dumps(instructions.get(bi-1,{}), ensure_ascii=False) if bi>0 else "（初ブロック）",
                prev_summary=summaries.get(bi-1,"（初ブロック）"),
                bodies=bodies), max_tokens=4096))
            instructions[bi] = obj
            summaries[bi]    = obj.get("block_summary","")
            log.info(f"    修正{len(obj.get('fix_rules',[]))}件 / 保持{len(obj.get('preserve_rules',[]))}件")
            time.sleep(1.0)
        except Exception as e:
            log.warning(f"  ブロック{bi+1} 失敗: {e}")
            instructions[bi] = {}; summaries[bi] = ""

    log.info(f"[Step2] 完了: {len(blocks)}ブロック")
    return instructions, summaries, ep_to_block

# ── Step3: 話単位校正（Flash）─────────────────────────────────────────────────

REWRITE_PROMPT = """日本語小説の校正者として、以下の指示書に従い本文を書き直してください。

## 絶対ルール
- 固有名詞（人名・地名・スキル名等）は指示書の表記のまま。絶対に変えない
- セリフの内容・口調は変えない
- ストーリーの展開・事実は変えない
- 必要最小限の修正に留める

## 修正ルール
{fix_rules}

## 保持ルール（変えてはいけない）
{preserve_rules}

## 文体目標
{rewrite_instructions}

## 文脈情報（このブロックで起きていること）
{block_summary}

## 本文（第{ep_no}話「{title}」）
{body}

書き直した本文のみ返してください。前置き・説明不要。"""

def step3_rewrite(episodes, instructions, summaries, ep_to_block):
    log.info(f"[Step3] 校正（{len(episodes)}話）")
    last_bi = max(instructions.keys(), default=0)
    for i, ep in enumerate(episodes, 1):
        log.info(f"  [{i}/{len(episodes)}] 第{ep['episode_no']}話")
        bi   = ep_to_block.get(ep["episode_no"], last_bi)
        inst = instructions.get(bi, {})
        try:
            result = call_flash(REWRITE_PROMPT.format(
                ep_no=ep["episode_no"], title=ep.get("title",""),
                body=ep.get("body",""),
                fix_rules=json.dumps(inst.get("fix_rules",[]), ensure_ascii=False),
                preserve_rules=json.dumps(inst.get("preserve_rules",[]), ensure_ascii=False),
                rewrite_instructions=json.dumps(inst.get("rewrite_instructions",{}), ensure_ascii=False),
                block_summary=summaries.get(bi,""),
            ), max_tokens=4096)
            ep["body"] = result.strip() or ep["body"]
            time.sleep(0.3)
        except Exception as e:
            log.warning(f"  失敗（原文使用）: {e}")
    log.info("[Step3] 完了")
    return episodes

# ── Step4: 語句解説（Flash）──────────────────────────────────────────────────

SKIP_TERMS = {"名前","レベル","職業","ステータス","サラリーマン","ポジティブ","ヨーロッパ","ファンタジー"}
TERM_PATS  = [
    (r'[ァ-ヶー一-龯々]+＝[ァ-ヶー一-龯々]+', None),
    (r'【([^】]+)】', 1), (r'《([^》]+)》', 1),
    (r'「([ァ-ヶー]{3,})」', 1), (r'[ァ-ヶー]{4,}', None),
]
GLOSS_PROMPT = """小説「{title}」の用語解説係。第{ep_no}話時点の読者向けに「{term}」を200字以内で解説。
第{ep_no}話より後の情報は含めない。文脈: {ctx}
JSONのみ: {{"kind":"人物|地名|スキル|職業|魔物|組織|その他","desc":"解説","first_ep":{first_ep}}}"""

def step4_glossary(title, episodes):
    log.info("[Step4] 語句解説生成")
    all_text = "\n".join(e["body"] for e in episodes)
    cnt = collections.Counter()
    for pat, grp in TERM_PATS:
        for m in re.finditer(pat, all_text):
            cnt[m.group(grp) if grp else m.group()] += 1
    terms = [t for t,_ in cnt.most_common(40) if t not in SKIP_TERMS]
    log.info(f"  語句候補: {len(terms)}件")
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
                obj = parse_json(call_flash(GLOSS_PROMPT.format(
                    title=title, term=term, ep_no=ep_no,
                    ctx="\n".join(f"- {c}" for c in ctx), first_ep=first_ep), max_tokens=400))
                if obj: glossary[term]["by_ep"][str(ep_no)] = obj
                time.sleep(0.3)
            except Exception as e:
                log.warning(f"  [{term}]: {e}")
    log.info(f"[Step4] 完了: {len(glossary)}件")
    return glossary

# ── Git push ──────────────────────────────────────────────────────────────────

def git_push(ncode):
    if not GIT_TOKEN or not GIT_REPO:
        log.info("[git] skip"); return
    log.info("[git] push開始")
    try:
        subprocess.run(["git","config","user.email","bot@narou-reader"], cwd=ROOT_DIR, check=True)
        subprocess.run(["git","config","user.name","narou-reader-bot"],  cwd=ROOT_DIR, check=True)
        subprocess.run(["git","remote","set-url","origin",
                        f"https://{GIT_TOKEN}@github.com/{GIT_REPO}.git"],
                       cwd=ROOT_DIR, check=True)
        subprocess.run(["git","add",f"data/{ncode}_reading.json"], cwd=ROOT_DIR, check=True)
        if subprocess.run(["git","diff","--cached","--quiet"], cwd=ROOT_DIR).returncode != 0:
            subprocess.run(["git","commit","-m",
                f"data: update {ncode} [{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC]"],
                cwd=ROOT_DIR, check=True)
            subprocess.run(["git","push","origin","HEAD"], cwd=ROOT_DIR, check=True)
            log.info("[git] push完了")
        else:
            log.info("[git] 変更なし")
    except subprocess.CalledProcessError as e:
        log.error(f"[git] 失敗: {e}")

# ── メイン ────────────────────────────────────────────────────────────────────

def run(ncode, max_episodes=None, update_only=False):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / f"{ncode}_reading.json"

    existing = {}
    if out_path.exists():
        with open(out_path, encoding="utf-8") as f:
            existing = json.load(f)

    existing_nos = (
        {ep["episode_no"] for ep in existing.get("episodes",[])} if update_only else None
    )

    log.info("=== スクレイプ ===")
    meta, new_eps = scrape(ncode, max_episodes, existing_nos)
    if not new_eps and update_only:
        log.info("新話なし。終了。"); return out_path

    title = meta.get("title") or existing.get("meta",{}).get("title","")

    log.info("=== Step0: 全話スキャン（Flash）===")
    raw_index = step0_scan(new_eps)

    # 動的ウィンドウサイズ算出
    avg_chars  = raw_index["avg_chars_per_ep"]
    block_size, overlap_size, stride = calc_window(avg_chars)

    log.info("=== Step1: 後期文体評価（2.5 Pro）===")
    all_eps_for_style = existing.get("episodes",[]) + new_eps
    style_guide = step1_evaluate_style(title, all_eps_for_style)

    log.info("=== Step2: ブロック分析（2.5 Pro）===")
    instructions, summaries, ep_to_block = step2_analyze_blocks(
        title, new_eps, raw_index, style_guide, block_size, stride)

    log.info("=== Step3: 話単位校正（Flash）===")
    new_eps = step3_rewrite(new_eps, instructions, summaries, ep_to_block)

    all_eps = existing.get("episodes",[]) + [
        {"episode_no":ep["episode_no"],"chapter":ep.get("chapter",""),
         "title":ep.get("title",""),"posted_at":ep.get("posted_at",""),
         "body":ep.get("body","")}
        for ep in new_eps
    ]
    all_eps.sort(key=lambda e: e["episode_no"])

    log.info("=== Step4: 語句解説生成（Flash）===")
    glossary = step4_glossary(title, all_eps)

    result = {
        "ncode":        ncode,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "meta": {
            "title":          title,
            "author":         meta.get("author") or existing.get("meta",{}).get("author",""),
            "synopsis":       meta.get("synopsis") or existing.get("meta",{}).get("synopsis",""),
            "genre":          meta.get("genre") or existing.get("meta",{}).get("genre",""),
            "tags":           meta.get("tags") or existing.get("meta",{}).get("tags",[]),
            "total_episodes": len(all_eps),
        },
        "pipeline_meta": {
            "avg_chars_per_ep": avg_chars,
            "block_size":       block_size,
            "overlap_size":     overlap_size,
            "style_source":     style_guide.get("source",""),
            "style_quality":    style_guide.get("quality_score",0),
        },
        "style_guide": style_guide,
        "glossary":    glossary,
        "episodes":    all_eps,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    log.info(f"=== 完了: {out_path} ({len(all_eps)}話, 語句{len(glossary)}件) ===")
    git_push(ncode)
    return out_path

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("ncode")
    p.add_argument("--max-episodes", type=int)
    p.add_argument("--update-only", action="store_true")
    args = p.parse_args()
    run(args.ncode, args.max_episodes, args.update_only)

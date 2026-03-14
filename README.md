# なろうリーダー

ローカルで校正 → git push → Cloudflare Pages でスマホ閲覧

## LLMのセットアップ

### Ollama（話単位の校正担当・無料）

https://ollama.com/download からインストール後:

```bash
# RTX 4060 Ti 8GBの場合 → 7b推奨（9GBの14bはVRAMに乗らない）
ollama pull qwen2.5:7b

# 確認
ollama run qwen2.5:7b "日本語テスト"
```

### Gemini API（文体評価・語句解説担当・無料枠あり）

https://aistudio.google.com/apikey でAPIキーを取得。
無料枠: RPD=20（1日20回）→ 1作品あたり3日で語句解説まで完了

## セットアップ

```bash
pip install -r requirements.txt
```

## 作品を追加する

```bash
# 環境変数を設定
export OLLAMA_MODEL=qwen2.5:7b
export GEMINI_API_KEY=AIzaSy...

# 動作確認（先頭3話のみ）
python scripts/pipeline.py n9999zz --max-ep 3

# 全話処理（語句解説はGemini RPD=20を3日分消費するため翌々日以降に追加）
python scripts/pipeline.py n9999zz --skip-glossary

# 2〜3日後に語句解説を追加
python scripts/pipeline.py n9999zz --glossary-only

# 新話のみ更新
python scripts/pipeline.py n9999zz --update-only
```

## Cloudflare Pages 設定

1. https://dash.cloudflare.com にアクセス（無料アカウント作成）
2. 「Workers & Pages」→「Create application」→「Pages」→「Connect to Git」
3. GitHubでログイン → プライベートリポジトリを選択
4. ビルド設定:
   - Framework preset: None
   - Build command: （空欄）
   - Build output directory: docs
5. 「Save and Deploy」→ URLが発行されるのでスマホでブックマーク

git pushするたびに自動デプロイされます。

## ファイル構成

```
docs/
  index.html        ビューア本体（静的・サーバー不要）
  data/
    .gitkeep        空ファイル（ディレクトリをGitで管理するため）
    index.json      作品一覧（自動生成）
    n9999zz.json    校正済み作品データ（自動生成）
scripts/
  pipeline.py       メインスクリプト
requirements.txt
```

## LLMの役割分担

| Step | 処理内容 | 担当 |
|------|----------|------|
| Step0 | 固有名詞スキャン | Ollama（高頻度） |
| Step1 | 文体評価 | Gemini（全体把握） |
| Step2 | ブロック分析・指示書生成 | Gemini（整合性） |
| Step3 | 話単位の校正 | Ollama（高頻度） |
| Step4 | 語句解説生成 | Gemini（知識・説明） |

# セットアップ手順

上から順番にコマンドを打つだけです。所要時間: 約20分。

---

## 事前準備（ブラウザで）

1. GitHubアカウント作成 → https://github.com/signup
2. Railwayアカウント作成（GitHubでログイン） → https://railway.app

---

## 1. GitHubにリポジトリを作る

ブラウザで:
```
https://github.com/new
  - Repository name: narou-reader
  - Private にする（作品データが入るため）
  - 「Create repository」
```

---

## 2. ローカルでGitを初期化してpush

```bash
cd narou-reader

git init
git add .
git commit -m "first commit"

# YOUR_USERNAME を自分のGitHubユーザー名に変える
git remote add origin https://github.com/YOUR_USERNAME/narou-reader.git
git branch -M main
git push -u origin main
```

---

## 3. GitHub Personal Access Token を発行する

RailwayのサーバーからGitHubにpushするために必要です。

```
https://github.com/settings/tokens/new
  - Note: narou-reader
  - Expiration: No expiration
  - Scopes: ✅ repo（にチェック）
  - 「Generate token」
  - 表示されたトークン（ghp_xxxx...）をコピーして保存
```

---

## 4. Railwayにデプロイする

ブラウザで:
```
https://railway.app/new
  →「Deploy from GitHub repo」
  → narou-reader を選択
  →「Deploy Now」
```

2〜3分でデプロイ完了。

---

## 5. 環境変数を設定する

Railwayダッシュボード → サービス → 「Variables」タブ:

```
GEMINI_API_KEY     =  AIzaSy...（Google AI Studioで取得）
GIT_TOKEN          =  ghp_xxxxxxxxxx        ← 手順3で取得したトークン
GIT_REPO           =  YOUR_USERNAME/narou-reader
```

---

## 6. URLを発行する

```
Railwayダッシュボード → サービス → 「Settings」→「Networking」
→「Generate Domain」
→ https://narou-reader-xxxx.railway.app  ← これがアクセス先
```

ブックマーク推奨。

---

## 7. Railway CLI をインストールする

```bash
npm install -g @railway/cli
railway login
railway link   # ターミナルでプロジェクトを選択する
```

---

## 8. 最初の作品を処理する

```bash
# 動作確認（3話だけ）
railway run python scripts/pipeline.py n9999zz --max-episodes 3

# 問題なければ全話
railway run python scripts/pipeline.py n9999zz
```

処理完了後、自動でGitHubにpushされ、Railwayが再デプロイします。
完了したらブラウザでアクセスすると作品が表示されます。

---

## 作品を追加したいとき

```bash
railway run python scripts/pipeline.py n新しいNコード
```

スマホのターミナルアプリ（iSH / a-Shellなど）からでも実行可能です。

---

## cronで毎日自動更新（新話チェック）

Railwayダッシュボードで別サービスを追加:
```
「+ New」→「Empty Service」→ 名前: cron
同じGitHubリポジトリを選択
Variables に GEMINI_API_KEY / GIT_TOKEN / GIT_REPO / NAROU_NCODES を設定
  NAROU_NCODES = n9999zz,n1234ab   ← カンマ区切りで全作品

Settings → Deploy → Start Command:
  python scripts/cron.py

Settings → Cron Schedule:
  0 17 * * *   （毎日JST深夜2時 = UTC 17時）
```

---

## ファイル構成

```
narou-reader/
├── server.js          ビューア配信サーバー
├── package.json
├── requirements.txt
├── nixpacks.toml      Railway ビルド設定（Node + Python）
├── static/
│   └── index.html     ビューア本体
├── scripts/
│   ├── pipeline.py    スクレイプ→校正→AI改善→語句解説→git push
│   └── cron.py        毎日の自動更新
└── data/              処理済みJSON（GitHubに保存される）
```

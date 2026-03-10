# なろう校正リーダー（サーバー版）

PCなしでスマホから校正・読書ができるWebアプリです。

## Railway へのデプロイ手順

### 1. GitHubリポジトリを作成してpush

```bash
git init
git add .
git commit -m "initial commit"
git branch -M main
git remote add origin https://github.com/<ユーザー名>/<リポジトリ名>.git
git push -u origin main
```

### 2. Railwayでデプロイ

1. https://railway.app にアクセス（GitHubアカウントでログイン）
2. 「New Project」→「Deploy from GitHub repo」
3. 作成したリポジトリを選択
4. デプロイ完了後、「Settings」→「Environment Variables」から環境変数を追加：

| 変数名 | 値 |
|--------|-----|
| `GEMINI_API_KEY` | `AIzaSy...（取得したキー）` |

5. 「Settings」→「Domains」→「Generate Domain」でURLを発行

### 3. 使い方

発行されたURL（例: `https://xxx.railway.app`）にスマホでアクセス。

1. 小説コードを入力して「校正開始」
2. バックグラウンドで処理（進捗バーで確認できる）
3. 完了したら自動で目次ページへ移動

---

## 注意事項

- Railwayの無料枠は**月500時間**（1サービスなら約21日分）
- データは Railway のサーバーに保存されますが、**再デプロイするとリセット**されます
- 永続化したい場合は Railway の Volume を追加してください（月 $0.25/GB）

## ローカルで動かす場合

```bash
pip install -r requirements.txt
export GEMINI_API_KEY=AIzaSy...
uvicorn main:app --reload
```

`http://localhost:8000` でアクセスできます。

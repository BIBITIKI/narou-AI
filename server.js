const express    = require('express');
const path       = require('path');
const fs         = require('fs');
const { spawn }  = require('child_process');

const app  = express();
const PORT = process.env.PORT || 3000;
const DATA = path.join(__dirname, 'data');

const processing = new Map(); // ncode -> { startedAt }

app.use(express.json());
app.use(express.static(path.join(__dirname, 'static')));

// 作品一覧
app.get('/api/list', (req, res) => {
  try {
    const files = fs.existsSync(DATA)
      ? fs.readdirSync(DATA).filter(f => f.endsWith('_reading.json'))
      : [];
    const list = files.map(f => {
      const d = JSON.parse(fs.readFileSync(path.join(DATA, f), 'utf-8'));
      return {
        ncode:          d.ncode,
        title:          d.meta?.title  || '',
        author:         d.meta?.author || '',
        total_episodes: d.meta?.total_episodes || d.episodes?.length || 0,
        generated_at:   d.generated_at || '',
        processing:     processing.has(d.ncode),
      };
    });
    // 処理中でまだJSONがない作品も先頭に追加
    for (const [ncode] of processing.entries()) {
      if (!list.find(l => l.ncode === ncode)) {
        list.unshift({ ncode, title: `校正処理中... (${ncode})`,
                       author: '', total_episodes: 0, generated_at: '', processing: true });
      }
    }
    res.json(list);
  } catch(e) {
    res.status(500).json({ error: e.message });
  }
});

// 作品データ取得
app.get('/api/novel/:ncode', (req, res) => {
  const f = path.join(DATA, `${req.params.ncode}_reading.json`);
  if (!fs.existsSync(f)) return res.status(404).json({ error: 'not found' });
  res.sendFile(f);
});

// パイプライン起動
app.post('/api/process', (req, res) => {
  const ncode = (req.body.ncode || '').toLowerCase().trim();
  if (!/^n[0-9a-z]+$/.test(ncode))
    return res.status(400).json({ error: 'Nコードの形式が正しくありません（例: n9999zz）' });
  if (processing.has(ncode))
    return res.json({ status: 'already_running', ncode });

  processing.set(ncode, { startedAt: new Date().toISOString() });

  const py = spawn('python', ['scripts/pipeline.py', ncode], {
    env: { ...process.env },
    cwd: __dirname,
  });
  py.on('close', code => {
    processing.delete(ncode);
    console.log(`[pipeline] ${ncode} done (exit=${code})`);
  });

  res.json({ status: 'started', ncode });
});

// 処理状況確認
app.get('/api/status/:ncode', (req, res) => {
  const ncode = req.params.ncode;
  const f = path.join(DATA, `${ncode}_reading.json`);
  res.json({
    ncode,
    status: processing.has(ncode) ? 'processing'
          : fs.existsSync(f)      ? 'done'
          :                         'not_found',
  });
});

app.listen(PORT, () => console.log(`narou-reader port ${PORT}`));

function initReader(terms) {
  const STATE = {
    apiKey: localStorage.getItem('gemini_api_key') || '',
    theme: localStorage.getItem('theme') || 'dark',
    fontSize: parseInt(localStorage.getItem('font_size') || '110'),
    cache: {},
  };

  applyTheme(STATE.theme);
  document.getElementById('theme-toggle')?.addEventListener('click', () => {
    STATE.theme = STATE.theme === 'dark' ? 'light' : 'dark';
    localStorage.setItem('theme', STATE.theme);
    applyTheme(STATE.theme);
  });
  function applyTheme(t) {
    document.documentElement.dataset.theme = t;
    const btn = document.getElementById('theme-toggle');
    if (btn) btn.textContent = t === 'dark' ? '☀️' : '🌙';
  }

  applySize(STATE.fontSize);
  document.getElementById('font-up')?.addEventListener('click', () => changeSize(10));
  document.getElementById('font-down')?.addEventListener('click', () => changeSize(-10));
  function changeSize(d) {
    STATE.fontSize = Math.max(70, Math.min(200, STATE.fontSize + d));
    localStorage.setItem('font_size', STATE.fontSize);
    applySize(STATE.fontSize);
  }
  function applySize(p) {
    document.documentElement.style.setProperty('--font-size', p / 100 + 'rem');
    const el = document.getElementById('font-size-display');
    if (el) el.textContent = p + '%';
  }

  const apiInput = document.getElementById('api-key-input');
  const apiStatus = document.getElementById('api-status');
  if (apiInput && STATE.apiKey) {
    apiInput.value = STATE.apiKey;
    if (apiStatus) { apiStatus.textContent = '設定済み ✓'; apiStatus.className = 'status-ok'; }
  }
  document.getElementById('api-save-btn')?.addEventListener('click', () => {
    const k = apiInput?.value.trim();
    if (!k) return;
    STATE.apiKey = k;
    localStorage.setItem('gemini_api_key', k);
    STATE.cache = {};
    if (apiStatus) { apiStatus.textContent = '設定済み ✓'; apiStatus.className = 'status-ok'; }
  });

  document.getElementById('novel-body')?.addEventListener('click', e => {
    const btn = e.target.closest('.word-link');
    if (btn) showPopup(btn, btn.dataset.term);
  });
  document.getElementById('popup-close')?.addEventListener('click', closePopup);
  document.addEventListener('click', e => {
    const popup = document.getElementById('word-popup');
    if (popup && !popup.contains(e.target) && !e.target.closest('.word-link')) closePopup();
  });

  function showPopup(anchor, term) {
    document.querySelectorAll('.word-link.active').forEach(el => el.classList.remove('active'));
    anchor.classList.add('active');
    const popup = document.getElementById('word-popup');
    if (!popup) return;
    document.getElementById('popup-word').textContent = term;
    const bodyEl = document.getElementById('popup-body');
    const loadingEl = document.getElementById('popup-loading');
    bodyEl.style.display = 'none';
    bodyEl.textContent = '';
    loadingEl.style.display = 'block';
    popup.classList.add('visible');
    positionPopup(anchor, popup);

    if (STATE.cache[term]) { showContent(STATE.cache[term]); return; }
    fetchExplanation(term, STATE.apiKey)
      .then(t => { STATE.cache[term] = t; showContent(t); })
      .catch(err => showContent('⚠️ ' + err.message));
  }

  function showContent(text) {
    const bodyEl = document.getElementById('popup-body');
    const loadingEl = document.getElementById('popup-loading');
    if (loadingEl) loadingEl.style.display = 'none';
    if (bodyEl) { bodyEl.textContent = text; bodyEl.style.display = 'block'; }
  }

  function positionPopup(anchor, popup) {
    const r = anchor.getBoundingClientRect();
    const vpW = window.innerWidth, vpH = window.innerHeight;
    const popW = Math.min(320, vpW * 0.9);
    let top = r.bottom + 8, left = r.left;
    if (left + popW > vpW - 8) left = vpW - popW - 8;
    if (left < 8) left = 8;
    if (top + 160 > vpH) top = r.top - 168;
    popup.style.top = top + 'px';
    popup.style.left = left + 'px';
    popup.style.maxWidth = popW + 'px';
  }

  function closePopup() {
    document.getElementById('word-popup')?.classList.remove('visible');
    document.querySelectorAll('.word-link.active').forEach(el => el.classList.remove('active'));
  }
}

async function fetchExplanation(term, apiKey) {
  if (!apiKey) throw new Error('Gemini APIキーが未設定です（画面下部に入力してください）');
  const prompt = `なろう系ライトノベルの用語「${term}」について、作品内での意味・役割を2〜3文で簡潔に解説してください。解説のみ出力してください。`;
  const res = await fetch(
    `https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key=${apiKey}`,
    { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ contents: [{ parts: [{ text: prompt }] }], generationConfig: { temperature: 0.3, maxOutputTokens: 200 } }) }
  );
  if (!res.ok) { const e = await res.json().catch(() => ({})); throw new Error(e?.error?.message || `APIエラー(${res.status})`); }
  const d = await res.json();
  return d?.candidates?.[0]?.content?.parts?.[0]?.text?.trim() || '解説を取得できませんでした';
}

// Browser-side embedding + in-database hybrid retrieval demo.
// All embedding happens in this tab (Transformers.js / ONNX Runtime Web).
// The server never sees raw text without its vector, and search ranking is
// produced by a single seekdb statement (DBMS_HYBRID_SEARCH.SEARCH).

import { pipeline, env } from './vendor/transformers.min.js';

const MODEL_ID = 'Xenova/all-MiniLM-L6-v2';

env.allowLocalModels = true;
env.allowRemoteModels = false;
env.localModelPath = './models/';
env.backends.onnx.wasm.wasmPaths = '/vendor/';
env.backends.onnx.wasm.numThreads = 1;

const el = (id) => document.getElementById(id);
const metrics = { loadMs: null, embedMs: [], searchMs: [], device: null, dtype: null };

let extractor = null;
let extractorKey = '';
let backend = { device: 'wasm', dtype: 'q8' };

const SAMPLE_DOCS = [
  {
    doc_id: 'remote-control',
    title: '遥控器原型评审',
    source: 'meeting-notes/2026-03-11.md',
    text: [
      '产品经理：遥控器原型目前只有三个按键，分别是电源、音量加、音量减，外壳还在打样阶段。',
      '硬件工程师：遥控器使用 2.4G 无线模块，空旷环境有效距离约 12 米，实测待机功耗 0.4 毫瓦。',
      '电池方案：两节 AAA 电池可以撑大约两周，低电量时指示灯会闪烁三次提醒。',
    ].join('\n'),
  },
  {
    doc_id: 'video-pipeline',
    title: '视频管线性能优化记录',
    source: 'engineering/2026-04-02.md',
    text: [
      '视频管线运行在 GPU 上，解码、色彩转换、缩放三个阶段串行执行，单帧平均耗时 6.8 毫秒。',
      '当渲染队列积压超过 4 帧时，管线会丢弃过期帧，保证画面延迟不超过 120 毫秒。',
      '优化后又把纹理上传改成批量提交，帧率从 48fps 提升到 59fps，掉帧率下降到 0.3%。',
    ].join('\n'),
  },
  {
    doc_id: 'seekdb-handbook',
    title: 'seekdb 混合检索备忘',
    source: 'docs/seekdb-hybrid.md',
    text: [
      'seekdb 可以把全文索引与向量索引放在同一张表上，全文索引用 WITH PARSER SPACE，向量索引用 HNSW。',
      'DBMS_HYBRID_SEARCH.SEARCH 一次调用即可完成关键词打分与向量距离计算，并把两路分数相加后排序。',
      '表不能自带主键，库内部通过隐藏列 __pk_increment 对齐两路候选，因此建模时不要声明 PRIMARY KEY。',
    ].join('\n'),
  },
];

function chunkText(text, maxChars = 420) {
  const paragraphs = text.split(/\n+/).map((p) => p.trim()).filter(Boolean);
  const chunks = [];
  for (const paragraph of paragraphs) {
    if (paragraph.length <= maxChars) {
      chunks.push(paragraph);
      continue;
    }
    let start = 0;
    while (start < paragraph.length) {
      chunks.push(paragraph.slice(start, start + maxChars));
      start += maxChars;
    }
  }
  return chunks.length ? chunks : [text.slice(0, maxChars)];
}

async function detectDevice(preference) {
  if (preference === 'wasm') return { device: 'wasm', fallback: false };
  const gpuAvailable = typeof navigator !== 'undefined' && !!navigator.gpu;
  if (!gpuAvailable) {
    if (preference === 'gpu') throw new Error('当前浏览器没有 WebGPU，无法强制启用 GPU 后端');
    return { device: 'wasm', fallback: true };
  }
  try {
    const adapter = await navigator.gpu.requestAdapter();
    if (!adapter) throw new Error('no adapter');
    return { device: 'webgpu', fallback: false };
  } catch (err) {
    if (preference === 'gpu') throw err;
    return { device: 'wasm', fallback: true };
  }
}

async function ensureModel(devicePreference, dtype) {
  const target = await detectDevice(devicePreference);
  const key = `${target.device}|${dtype}`;
  if (extractor && extractorKey === key) return backend;
  if (target.fallback) {
    const notice = el('fallbackNotice');
    notice.style.display = 'block';
    notice.textContent =
      '当前环境不支持 WebGPU（或适配器获取失败），已自动降级到 WASM 后端；核心流程仍可完整运行。';
    el('devicePill').classList.add('warn');
  }
  const started = performance.now();
  extractor = await pipeline('feature-extraction', MODEL_ID, {
    device: target.device,
    dtype,
    progress_callback: (p) => {
      if (p.status === 'progress' && p.file) {
        el('modelVal').textContent = `加载 ${p.file} ${p.progress ? p.progress.toFixed(1) : 0}%`;
      }
    },
  });
  metrics.loadMs = Math.round(performance.now() - started);
  extractorKey = key;
  backend = { device: target.device, dtype };
  metrics.device = target.device;
  metrics.dtype = dtype;
  el('deviceVal').textContent = target.device === 'webgpu' ? 'WebGPU（GPU）' : 'WASM（CPU）';
  el('dtypeVal').textContent = dtype === 'fp32' ? 'FP32 基线' : 'INT8 (q8)';
  el('modelVal').textContent = `${metrics.loadMs} ms（冷启动）`;
  el('devicePill').classList.toggle('warn', target.device !== 'webgpu');
  el('devicePill').classList.toggle('live', target.device === 'webgpu');
  return backend;
}

async function embed(texts) {
  const started = performance.now();
  const output = await extractor(texts, { pooling: 'mean', normalize: true });
  const vectors = output.tolist();
  const ms = performance.now() - started;
  metrics.embedMs.push({ count: texts.length, ms: Math.round(ms), perItem: +(ms / texts.length).toFixed(2) });
  return vectors;
}

async function api(path, payload) {
  const response = await fetch(path, {
    method: payload ? 'POST' : 'GET',
    headers: { 'Content-Type': 'application/json' },
    body: payload ? JSON.stringify(payload) : undefined,
  });
  if (!response.ok) throw new Error(`${path} -> HTTP ${response.status}: ${await response.text()}`);
  return response.json();
}

async function refreshStats() {
  try {
    const stats = await api('/api/stats');
    el('dbVal').textContent = `${stats.chunks} chunks / ${stats.batches} batch`;
    el('dbPill').className = 'pill live';
    el('corpusVal').textContent = stats.per_batch
      .map((item) => `b${item.batch}:${item.chunks}`)
      .join('  ') || '-';
    return stats;
  } catch (err) {
    el('dbVal').textContent = '连接失败';
    el('dbPill').className = 'pill warn';
    throw err;
  }
}

function parseCorpus(raw) {
  const blocks = raw.split(/^\s*##\s+/m).map((b) => b.trim()).filter(Boolean);
  return blocks.map((block, index) => {
    const firstLine = block.split('\n')[0].trim();
    const body = block.slice(firstLine.length).trim();
    return {
      doc_id: `doc-${index + 1}-${firstLine.replace(/\s+/g, '-').slice(0, 20)}`,
      title: firstLine,
      source: `browser-input/${firstLine.slice(0, 40)}`,
      text: body || firstLine,
    };
  });
}

async function runImport() {
  const btn = el('importBtn');
  btn.disabled = true;
  el('importLog').textContent = '准备中…';
  try {
    await ensureModel(deviceFromSelect(), el('dtypeSelect').value);
    const batch = Number(el('batchSelect').value);
    const docs = parseCorpus(el('corpus').value);
    if (!docs.length) throw new Error('语料为空：用 “## 标题” 开头区分文档');
    let chunkCount = 0;
    const payload = { batch, docs: [], replace_batch: true };
    for (const doc of docs) {
      const chunks = chunkText(doc.text);
      chunkCount += chunks.length;
      el('importLog').textContent = `编码 ${doc.title}（${chunks.length} 段）…`;
      const vectors = await embed(chunks);
      payload.docs.push({
        doc_id: doc.doc_id,
        title: doc.title,
        source: doc.source,
        chunks: chunks.map((text, i) => ({ chunk_id: `${doc.doc_id}-c${i}`, text, vector: vectors[i] })),
      });
    }
    el('importLog').textContent = `写入 seekdb：${payload.docs.length} 文档 / ${chunkCount} 段…`;
    const result = await api('/api/import', payload);
    el('importLog').textContent =
      `已写入 batch ${result.batch}：+${result.inserted} 段，库内共 ${result.chunks} 段（${result.batches} 批）` +
      `｜编码 ${metrics.embedMs.at(-1)?.ms ?? 0} ms`;
    await refreshStats();
  } catch (err) {
    el('importLog').textContent = `导入失败：${err.message}`;
  } finally {
    btn.disabled = false;
  }
}

async function runSearch() {
  const btn = el('searchBtn');
  btn.disabled = true;
  const query = el('query').value.trim();
  try {
    if (!query) throw new Error('请输入查询');
    await ensureModel(deviceFromSelect(), el('dtypeSelect').value);
    const vectors = await embed([query]);
    const started = performance.now();
    const response = await api('/api/search', {
      query_text: query,
      query_vector: vectors[0],
      k: Number(el('topk').value),
    });
    const ms = Math.round(performance.now() - started);
    metrics.searchMs.push(ms);
    el('searchLog').textContent =
      `${response.hits.length} 条结果｜库内检索 ${ms} ms｜编码 ${metrics.embedMs.at(-1)?.perItem ?? 0} ms/条`;
    el('sqlDump').textContent = response.generated_sql || '（未返回 SQL）';
    renderHits(response.hits);
  } catch (err) {
    el('searchLog').textContent = `检索失败：${err.message}`;
    el('results').innerHTML = '<div class="empty">检索失败（确认 seekdb 已启动、后端在线）。</div>';
  } finally {
    btn.disabled = false;
  }
}

function renderHits(hits) {
  const container = el('results');
  if (!hits.length) {
    container.innerHTML = '<div class="empty">没有命中：检查是否已导入语料。</div>';
    return;
  }
  container.innerHTML = hits
    .map((hit) => {
      const content = (hit.content || '').replace(/</g, '&lt;');
      return `<article class="hit">
        <div class="head">
          <span class="score">${hit.score.toFixed(4)}</span>
          <b>${(hit.title || hit.doc_id || '').replace(/</g, '&lt;')}</b>
          <span class="meta">batch ${hit.batch} · ${(hit.source || '').replace(/</g, '&lt;')} · ${hit.chunk_id}</span>
        </div>
        <p>${content}</p>
      </article>`;
    })
    .join('');
}

function deviceFromSelect() {
  const value = el('deviceSelect').value;
  return value === 'auto' ? 'auto' : value === 'gpu' ? 'gpu' : 'wasm';
}

function loadSample() {
  el('corpus').value = SAMPLE_DOCS.map((doc) => `## ${doc.title}\n${doc.text}`).join('\n\n');
  el('importLog').textContent = `已载入 ${SAMPLE_DOCS.length} 篇示例文档，点击“导入到 seekdb”即可写入。`;
}

function boot() {
  loadSample();
  el('loadModelBtn').addEventListener('click', async () => {
    el('modelVal').textContent = '加载中…';
    try {
      await ensureModel(deviceFromSelect(), el('dtypeSelect').value);
    } catch (err) {
      el('modelVal').textContent = `加载失败：${err.message}`;
    }
  });
  el('importBtn').addEventListener('click', runImport);
  el('sampleBtn').addEventListener('click', loadSample);
  el('statsBtn').addEventListener('click', () => refreshStats().catch(() => {}));
  el('searchBtn').addEventListener('click', runSearch);
  el('query').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') runSearch();
  });
  refreshStats().catch(() => {});
  window.__stepfun = { metrics, embed, ensureModel, backend: () => backend };
}

boot();

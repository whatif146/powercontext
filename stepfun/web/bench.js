import { pipeline, env } from './vendor/transformers.min.js';

const MODEL_ID = 'Xenova/all-MiniLM-L6-v2';
const DIM = 384;
env.allowLocalModels = true;
env.allowRemoteModels = false;
env.localModelPath = './models/';
env.backends.onnx.wasm.wasmPaths = '/vendor/';
env.backends.onnx.wasm.numThreads = 1;

const QUERIES = [
  '遥控器有几个按键', '遥控器待机功耗是多少', '电池能用多久', '视频管线单帧耗时',
  '掉帧策略是什么', '帧率优化了多少', 'seekdb 怎么建全文索引', 'DBMS_HYBRID_SEARCH 怎么用',
  '向量索引用什么算法', '表为什么不能带主键',
];
const CHUNKS = [
  '遥控器原型目前只有三个按键，分别是电源、音量加、音量减。',
  '遥控器使用 2.4G 无线模块，空旷环境有效距离约 12 米，待机功耗 0.4 毫瓦。',
  '两节 AAA 电池可以撑大约两周，低电量时指示灯闪烁三次。',
  '视频管线在 GPU 上串行执行三个阶段的单帧平均耗时 6.8 毫秒。',
  '渲染队列积压超过 4 帧时丢弃过期帧，画面延迟不超过 120 毫秒。',
  '纹理上传改成批量提交后帧率从 48fps 提升到 59fps，掉帧率 0.3%。',
  'seekdb 全文索引用 WITH PARSER SPACE，向量索引用 HNSW。',
  'DBMS_HYBRID_SEARCH.SEARCH 一次调用完成关键词与向量两路打分再排序。',
  '表不能自带主键，库内通过隐藏列 __pk_increment 对齐候选。',
  '混合检索的分数是 keyword_score 与 semantic_score 相加。',
  '视频管线的优化还包括把色彩转换合并到缩放阶段。',
  '遥控器外壳仍在打样，最终量产时间待定。',
  '低电量提醒逻辑由指示灯闪烁三次实现。',
  '文本分块长度设置为 420 个字符以内。',
  '浏览器端用 Transformers.js 计算 embedding，不调用云端推理。',
];

function cosine(a, b) {
  let dot = 0, na = 0, nb = 0;
  for (let i = 0; i < a.length; i += 1) { dot += a[i] * b[i]; na += a[i] * a[i]; nb += b[i] * b[i]; }
  return dot / (Math.sqrt(na) * Math.sqrt(nb) + 1e-9);
}

function topK(queryVec, docVecs, k = 5) {
  return docVecs
    .map((vec, i) => ({ i, score: cosine(queryVec, vec) }))
    .sort((a, b) => b.score - a.score)
    .slice(0, k)
    .map((item) => item.i);
}

function median(values) {
  const sorted = [...values].sort((a, b) => a - b);
  return +(sorted[Math.floor(sorted.length / 2)]).toFixed(2);
}

function sizeMB(bytes) { return (bytes / 1024 / 1024).toFixed(2); }

async function measure(dtype, device) {
  const started = performance.now();
  const extractor = await pipeline('feature-extraction', MODEL_ID, { device, dtype });
  const loadMs = Math.round(performance.now() - started);
  const chunkVectors = (await extractor(CHUNKS, { pooling: 'mean', normalize: true })).tolist();
  // warm-up: the first call pays kernel/thread-pool setup costs
  for (let w = 0; w < 3; w += 1) {
    await extractor([CHUNKS[w]], { pooling: 'mean', normalize: true });
  }
  const latencies = [];
  for (let i = 0; i < 20; i += 1) {
    const t = performance.now();
    await extractor([CHUNKS[i % CHUNKS.length]], { pooling: 'mean', normalize: true });
    latencies.push(Math.round((performance.now() - t) * 100) / 100);
  }
  const batchStart = performance.now();
  await extractor(CHUNKS.slice(0, 3), { pooling: 'mean', normalize: true });
  const batch3Ms = Math.round((performance.now() - batchStart) * 100) / 100;
  const queryVectors = [];
  for (const q of QUERIES) {
    const out = await extractor([q], { pooling: 'mean', normalize: true });
    queryVectors.push(out.tolist()[0]);
  }
  return { dtype, device, loadMs, chunkVectors, queryVectors, latencies, batch3Ms };
}

async function retrievalLatency() {
  const timings = [];
  const probe = await fetch('/api/stats').then((r) => r.json()).catch(() => null);
  if (!probe || !probe.chunks) return null;
  const random = new Array(DIM).fill(0).map((_, i) => Math.sin(i));
  for (let i = 0; i < 5; i += 1) {
    const t = performance.now();
    const res = await fetch('/api/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query_text: QUERIES[i % QUERIES.length], query_vector: random, k: 5 }),
    });
    await res.json();
    timings.push(Math.round((performance.now() - t) * 100) / 100);
  }
  const sorted = [...timings].sort((a, b) => a - b);
  return { samples: timings, p50: sorted[Math.floor(sorted.length / 2)], max: sorted[sorted.length - 1] };
}

async function fileSizes() {
  const paths = ['models/Xenova/all-MiniLM-L6-v2/onnx/model_quantized.onnx', 'models/Xenova/all-MiniLM-L6-v2/onnx/model.onnx'];
  const out = {};
  for (const path of paths) {
    try {
      const res = await fetch(path, { method: 'HEAD' });
      const len = res.headers.get('content-length');
      out[path] = len ? Number(len) : null;
    } catch { out[path] = null; }
  }
  return out;
}

async function pickDevice() {
  if (!navigator.gpu) return 'wasm';
  try {
    const adapter = await navigator.gpu.requestAdapter();
    return adapter ? 'webgpu' : 'wasm';
  } catch {
    return 'wasm';
  }
}

async function main() {
  const out = document.getElementById('out');
  const device = await pickDevice();
  const int8 = await measure('q8', device);
  const fp32 = await measure('fp32', device);
  const sizes = await fileSizes();
  const agreement = QUERIES.map((_, qi) => {
    const a = topK(int8.queryVectors[qi], int8.chunkVectors);
    const b = topK(fp32.queryVectors[qi], fp32.chunkVectors);
    return a.filter((idx) => b.includes(idx)).length / a.length;
  });
  const recall = agreement.reduce((x, y) => x + y, 0) / agreement.length;
  const retrieval = await retrievalLatency();
  const report = {
    device,
    model: 'Xenova/all-MiniLM-L6-v2 @ 751bff37182d3f1213fa05d7196b954e230abad9',
    fileSizesMB: {
      int8: sizes['models/Xenova/all-MiniLM-L6-v2/onnx/model_quantized.onnx'] ? sizeMB(sizes['models/Xenova/all-MiniLM-L6-v2/onnx/model_quantized.onnx']) : null,
      fp32: sizes['models/Xenova/all-MiniLM-L6-v2/onnx/model.onnx'] ? sizeMB(sizes['models/Xenova/all-MiniLM-L6-v2/onnx/model.onnx']) : null,
    },
    loadMs: { int8: int8.loadMs, fp32: fp32.loadMs },
    encodeMsPerText: {
      int8: median(int8.latencies),
      fp32: median(fp32.latencies),
    },
    batch3Ms: { int8: int8.batch3Ms, fp32: fp32.batch3Ms },
    top5Agreement: +(recall * 100).toFixed(1) + '%',
    retrieval,
    perQueryAgreement: agreement.map((x) => +(x * 100).toFixed(0)),
  };
  out.textContent = JSON.stringify(report, null, 2);
  window.__bench = report;
  return report;
}

document.getElementById('run').addEventListener('click', () => {
  document.getElementById('out').textContent = '测量中…（首次加载模型需要几秒）';
  main().catch((err) => { document.getElementById('out').textContent = '失败：' + err.message; });
});

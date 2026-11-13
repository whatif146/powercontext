#!/usr/bin/env bash
# Fetch every pinned asset the demo needs, so the app runs fully offline afterwards.
# Nothing here is an inference service: it is static JS + ONNX weights.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENDOR="$ROOT/web/vendor"
MODEL_DIR="$ROOT/web/models/Xenova/all-MiniLM-L6-v2"
TRANSFORMERS_VERSION="3.7.2"
MODEL_REVISION="751bff37182d3f1213fa05d7196b954e230abad9"
mkdir -p "$VENDOR" "$MODEL_DIR/onnx" "$ROOT/assets/manifests"

fetch() { # url dest
  local url="$1" dest="$2"
  echo "fetch $url"
  curl -fsSL --retry 3 -o "$dest" "$url"
}

fetch "https://cdn.jsdelivr.net/npm/@huggingface/transformers@${TRANSFORMERS_VERSION}/dist/transformers.min.js" "$VENDOR/transformers.min.js"
fetch "https://cdn.jsdelivr.net/npm/@huggingface/transformers@${TRANSFORMERS_VERSION}/dist/ort-wasm-simd-threaded.jsep.mjs" "$VENDOR/ort-wasm-simd-threaded.jsep.mjs"
fetch "https://cdn.jsdelivr.net/npm/@huggingface/transformers@${TRANSFORMERS_VERSION}/dist/ort-wasm-simd-threaded.jsep.wasm" "$VENDOR/ort-wasm-simd-threaded.jsep.wasm"

for file in config.json tokenizer.json tokenizer_config.json special_tokens_map.json vocab.txt; do
  fetch "https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/${MODEL_REVISION}/${file}" "$MODEL_DIR/${file}"
done
fetch "https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/${MODEL_REVISION}/onnx/model_quantized.onnx" "$MODEL_DIR/onnx/model_quantized.onnx"
fetch "https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/${MODEL_REVISION}/onnx/model.onnx" "$MODEL_DIR/onnx/model.onnx"

MANIFEST="$ROOT/assets/manifests/model-files.txt"
: > "$MANIFEST"
for path in "$MODEL_DIR"/*.json "$MODEL_DIR"/*.txt "$MODEL_DIR"/onnx/*.onnx "$VENDOR"/*; do
  size=$(stat -c '%s' "$path")
  sha=$(sha256sum "$path" | cut -d' ' -f1)
  echo "$(realpath --relative-to="$ROOT" "$path") $size $sha" >> "$MANIFEST"
done
echo "--- asset sizes (bytes) ---"
sort -k2 -n "$MANIFEST"
echo "manifest: $MANIFEST"

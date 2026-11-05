#!/bin/bash
# GPU Pod start command. gpu_power.py passes this file's text to `bash -c`, so nothing needs to be copied to the
# volume first. Reading the 53 GB of models from the network volume made each scene about 4x slower, and copying
# them off the volume took over an hour, so download them straight to the Pod's own disk from Hugging Face
# (about 5 minutes measured in CA-MTL-3) and point ComfyUI there. Inputs and outputs stay on local disk too, so
# user photos and clips never reach the persistent volume and vanish with the Pod.
set -u
LOCAL=/local
COMFY=/workspace/runpod-slim/ComfyUI
LOG=/workspace/audigo/gpu_boot.log
mkdir -p "$LOCAL/models" "$LOCAL/io/input" "$LOCAL/io/output" /workspace/audigo
echo "$(date -u +%FT%TZ) boot $(hostname)" > "$LOG"
# The password gate comes up first; until ComfyUI is ready it answers 502, which the worker treats as not ready.
COMFYUI_IO_DIR="$LOCAL/io" python3 /workspace/audigo/gate.py >> /workspace/audigo/gate.log 2>&1 &
start=$(date +%s)
if timeout 1500 python3 /workspace/download_models.py --models-dir "$LOCAL/models" > /workspace/audigo/model_download_local.log 2>&1; then
  # is_default puts the local copies first, so ComfyUI loads them instead of the volume's files with the same name.
  printf 'audigo_local:\n  base_path: %s/models\n  is_default: true\n  checkpoints: checkpoints\n  text_encoders: text_encoders\n  diffusion_models: diffusion_models\n  vae: vae\n' \
    "$LOCAL" > "$COMFY/extra_model_paths.yaml"
  echo "$(date -u +%FT%TZ) models on local disk in $(( $(date +%s) - start ))s" >> "$LOG"
else
  # Never block the Pod on the download: fall back to the (slower) models on the volume.
  rm -f "$COMFY/extra_model_paths.yaml"
  echo "$(date -u +%FT%TZ) local model download failed after $(( $(date +%s) - start ))s; using the volume's models" >> "$LOG"
fi
printf '%s\n' '# Written by gpu_boot.sh' "--input-directory $LOCAL/io/input" "--output-directory $LOCAL/io/output" \
  > /workspace/runpod-slim/comfyui_args.txt
exec /start.sh

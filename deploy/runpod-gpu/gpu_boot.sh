#!/bin/bash
# GPU Pod start command. gpu_power.py passes this file's text to `bash -c`, so nothing needs to be copied to the
# volume first. Reading the 53 GB of models from the network volume made each scene about 4x slower, and copying
# them off the volume took over an hour, so download them straight to the Pod's own disk from Hugging Face
# (about 5 minutes measured in CA-MTL-3) and point ComfyUI there. Inputs and outputs stay on local disk too, so
# user photos and clips never reach the persistent volume and vanish with the Pod.
set -u
# A serverless worker mounts the network volume at /runpod-volume, a Pod at /workspace. The image and the
# volume's ComfyUI install expect /workspace, so point it at the volume there.
# The RunPod model cache also appears under /runpod-volume, so only a real network volume (with ComfyUI on it)
# replaces /workspace.
if [ -d /runpod-volume/runpod-slim ] && ! mountpoint -q /workspace; then
  # Leave /workspace first: a process whose working directory was deleted fails on os.getcwd().
  cd / && rm -rf /workspace && ln -s /runpod-volume /workspace
fi
cd /workspace
LOCAL=/local
COMFY=/workspace/runpod-slim/ComfyUI
LOG=/workspace/audigo/gpu_boot.log
mkdir -p "$LOCAL/models" "$LOCAL/io/input" "$LOCAL/io/output" /workspace/audigo
echo "$(date -u +%FT%TZ) boot $(hostname)" > "$LOG"
# The password gate comes up first; until ComfyUI is ready it answers 502, which the worker treats as not ready.
# gpu_power.start_command() writes the current gate to /local/gate.py ahead of this script.
GATE=/local/gate.py; [ -f "$GATE" ] || GATE=/workspace/audigo/gate.py
COMFYUI_IO_DIR="$LOCAL/io" python3 "$GATE" >> /workspace/audigo/gate.log 2>&1 &
start=$(date +%s)
# gpu_power.start_command() writes the current downloader to /local too.
DOWNLOAD=/local/download_models.py; [ -f "$DOWNLOAD" ] || DOWNLOAD=/workspace/download_models.py
# RunPod model cache (endpoint "Model" = AUDIGO_MODEL_REPO): the six files are already on the host, laid out in
# ComfyUI folders, so nothing is downloaded and the worker starts in seconds. Without it, download as before.
CACHE_REPO=${AUDIGO_MODEL_REPO:-SSaBaSSaBa/audigo-video-models}
CACHE=$(ls -d /runpod-volume/huggingface-cache/hub/models--${CACHE_REPO/\//--}/snapshots/*/ 2>/dev/null | head -1)
if [ -n "$CACHE" ] && python3 "$DOWNLOAD" --check-sizes --models-dir "$CACHE" >> "$LOG" 2>&1; then
  printf 'audigo_cache:\n  base_path: %s\n  is_default: true\n  checkpoints: checkpoints\n  text_encoders: text_encoders\n  diffusion_models: diffusion_models\n  vae: vae\n' \
    "${CACHE%/}" > "$LOCAL/extra_model_paths.yaml"
  rm -f "$COMFY/extra_model_paths.yaml" 2>/dev/null
  echo "$(date -u +%FT%TZ) models from the RunPod model cache in $(( $(date +%s) - start ))s" | tee -a "$LOG"
elif timeout 1500 python3 "$DOWNLOAD" --models-dir "$LOCAL/models" > /workspace/audigo/model_download_local.log 2>&1; then
  # is_default puts the local copies first, so ComfyUI loads them instead of the volume's files with the same name.
  printf 'audigo_local:\n  base_path: %s/models\n  is_default: true\n  checkpoints: checkpoints\n  text_encoders: text_encoders\n  diffusion_models: diffusion_models\n  vae: vae\n' \
    "$LOCAL" > "$LOCAL/extra_model_paths.yaml"
  # Passed with --extra-model-paths-config below: without a volume, ComfyUI's folder exists only after /start.sh.
  rm -f "$COMFY/extra_model_paths.yaml" 2>/dev/null
  echo "$(date -u +%FT%TZ) models on local disk in $(( $(date +%s) - start ))s" | tee -a "$LOG"
else
  # Without a network volume there is no slower copy to fall back to: fail so RunPod replaces the worker.
  if [ ! -f /workspace/download_models.py ]; then
    echo "$(date -u +%FT%TZ) model download failed and no volume copy exists; exiting" | tee -a "$LOG"
    exit 1
  fi
  # Never block the Pod on the download: fall back to the (slower) models on the volume.
  rm -f "$COMFY/extra_model_paths.yaml"
  echo "$(date -u +%FT%TZ) local model download failed after $(( $(date +%s) - start ))s; using the volume's models" >> "$LOG"
fi
touch "$LOCAL/ready"  # the gate's /ping answers 200 once ComfyUI is up too
mkdir -p /workspace/runpod-slim
EXTRA=(); [ -f "$LOCAL/extra_model_paths.yaml" ] && EXTRA=("--extra-model-paths-config $LOCAL/extra_model_paths.yaml")
printf '%s\n' '# Written by gpu_boot.sh' "--input-directory $LOCAL/io/input" "--output-directory $LOCAL/io/output" "${EXTRA[@]}" \
  > /workspace/runpod-slim/comfyui_args.txt
exec /start.sh

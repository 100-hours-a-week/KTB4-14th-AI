#!/bin/bash
# GPU Pod start command (kept on the model volume at /workspace/audigo/gpu_boot.sh).
# Reading the 53 GB of models from the network volume made every scene about 4x slower than on a Pod with
# local disk, so copy them to the Pod's own disk first and point ComfyUI there; inputs and outputs stay local
# too, so user photos and clips never reach the persistent volume and vanish with the Pod.
set -u
LOCAL=/local
VOLUME_MODELS=/workspace/runpod-slim/ComfyUI/models
LOG=/workspace/audigo/gpu_boot.log
mkdir -p "$LOCAL/models" "$LOCAL/io/input" "$LOCAL/io/output"
echo "$(date -u +%FT%TZ) boot" >> "$LOG"
# The password gate comes up first; until ComfyUI is ready it answers 502, which the worker treats as not ready.
COMFYUI_IO_DIR="$LOCAL/io" python3 /workspace/audigo/gate.py >> /workspace/audigo/gate.log 2>&1 &
start=$(date +%s)
for folder in checkpoints text_encoders diffusion_models vae; do
  mkdir -p "$LOCAL/models/$folder"
  for file in "$VOLUME_MODELS/$folder"/*.safetensors; do
    [ -f "$file" ] && cp -n "$file" "$LOCAL/models/$folder/" &
  done
done
wait
echo "$(date -u +%FT%TZ) models copied to local disk in $(( $(date +%s) - start ))s" >> "$LOG"
# is_default puts the local copies first, so ComfyUI loads them instead of the volume's files with the same name.
cat > /workspace/runpod-slim/ComfyUI/extra_model_paths.yaml <<YAML
audigo_local:
  base_path: $LOCAL/models
  is_default: true
  checkpoints: checkpoints
  text_encoders: text_encoders
  diffusion_models: diffusion_models
  vae: vae
YAML
printf '%s\n' '# Written by /workspace/audigo/gpu_boot.sh' "--input-directory $LOCAL/io/input" "--output-directory $LOCAL/io/output" \
  > /workspace/runpod-slim/comfyui_args.txt
exec /start.sh

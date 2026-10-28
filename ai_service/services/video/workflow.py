"""공식 FLF2V 템플릿에서 API 그래프를 구성하고 실행 서버의 노드·입력 형식과 대조한다."""
import json
from pathlib import Path

from ai_service.services.video.config import VideoSettings
from ai_service.services.video.errors import VideoError

WORKFLOWS = Path(__file__).with_name("workflows")


def build_workflow(settings: VideoSettings, *, start: str, end: str, frames: int,
                   fps: float, prompt: str, seed: int, prefix: str) -> dict:
    graph = json.loads((WORKFLOWS / "wan2_2_flf2v.api.json").read_text())
    graph["80"]["inputs"]["image"] = start
    graph["89"]["inputs"]["image"] = end
    graph["81"]["inputs"].update(width=1280, height=720, length=frames, batch_size=1)
    graph["90"]["inputs"]["text"] = prompt
    graph["86"]["inputs"]["fps"] = fps
    graph["83"]["inputs"]["filename_prefix"] = prefix
    graph["76"]["inputs"]["unet_name"] = settings.high_model
    graph["77"]["inputs"]["unet_name"] = settings.low_model
    graph["72"]["inputs"].update(clip_name=settings.text_encoder, device=settings.text_encoder_device)
    graph["79"]["inputs"]["vae_name"] = settings.vae
    graph["84"]["inputs"]["noise_seed"] = seed
    return graph


def _expand_inputs(schema: dict, values: dict, prefix="") -> dict:
    """ComfyUI 0.35.0의 DynamicCombo 입력을 format.codec처럼 펼쳐진 API 형식으로 대조한다."""
    expanded = {}
    for group in ("required", "optional"):
        for name, spec in schema.get(group, {}).items():
            key = prefix + name
            expanded[key] = (group == "required", spec)
            if spec[0] == "COMFY_DYNAMICCOMBO_V3":
                options = spec[1]["options"]
                selected = next((o for o in options if o["key"] == values.get(key)), None)
                if selected:
                    expanded.update(_expand_inputs(selected["inputs"], values, key + "."))
    return expanded


def validate_workflow(graph: dict, object_info: dict):
    """노드·모델 파일·필수 입력이 없거나 연결이 잘못되면 GPU 작업 제출 전에 중단한다."""
    for node_id, node in graph.items():
        kind, values = node["class_type"], node["inputs"]
        if kind not in object_info:
            raise VideoError("comfyui_node_missing", node=kind)
        fields = _expand_inputs(object_info[kind].get("input", {}), values)
        for key, (required, spec) in fields.items():
            if required and key not in values:
                raise VideoError("comfyui_input_missing", node=kind, field=key)
            if key not in values:
                continue
            value = values[key]
            if isinstance(value, list):
                if len(value) != 2 or value[0] not in graph:
                    raise VideoError("workflow_link_invalid", node=kind, field=key)
                continue
            choices = spec[0]
            if choices == "COMFY_DYNAMICCOMBO_V3":
                choices = [o["key"] for o in spec[1]["options"]]
            # 모델 목록과 달리 입력 사진은 업로드한 뒤에만 서버 목록에 나타난다.
            if isinstance(choices, list) and not (kind == "LoadImage" and key == "image") and value not in choices:
                raise VideoError("comfyui_value_unavailable", node=kind, field=key)
            limits = spec[1] if len(spec) > 1 else {}
            if isinstance(value, (int, float)) and (value < limits.get("min", value) or value > limits.get("max", value)):
                raise VideoError("comfyui_value_out_of_range", node=kind, field=key)
        if set(values) - set(fields):
            raise VideoError("comfyui_input_unsupported", node=kind)
    if object_info["SaveVideo"].get("output_node") is not True:
        raise VideoError("comfyui_output_node_invalid")

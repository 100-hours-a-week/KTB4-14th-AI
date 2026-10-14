"""고정된 로컬 multilingual-e5-small ONNX 추론."""
from pathlib import Path
from ai_service.core.errors import ServiceUnavailable

MODEL_DIMENSION = 384

class E5Encoder:
    """고정된 로컬 ONNX 모델로 마스킹 평균 풀링과 L2 정규화를 수행한다."""

    def __init__(self, model_dir: Path):
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer

            model_path = model_dir / "model_O4.onnx"
            tokenizer_path = model_dir / "tokenizer.json"
            if not model_path.is_file() or not tokenizer_path.is_file():
                raise FileNotFoundError("E5 model assets are missing")
            self.tokenizer = Tokenizer.from_file(str(tokenizer_path))
            self.tokenizer.enable_truncation(max_length=512)
            self.tokenizer.enable_padding()
            options = ort.SessionOptions()
            options.intra_op_num_threads = 2
            self.session = ort.InferenceSession(
                str(model_path), sess_options=options, providers=["CPUExecutionProvider"]
            )
            self.input_names = {item.name for item in self.session.get_inputs()}
        except Exception as exc:
            raise ServiceUnavailable(reason="music_model_load_failed", detail={"error": type(exc).__name__}) from exc

    def encode(self, texts: list[str]):
        import numpy as np

        encoded = self.tokenizer.encode_batch(texts)
        ids = np.asarray([item.ids for item in encoded], dtype=np.int64)
        mask = np.asarray([item.attention_mask for item in encoded], dtype=np.int64)
        values = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.input_names:
            values["token_type_ids"] = np.asarray([item.type_ids for item in encoded], dtype=np.int64)
        hidden = self.session.run(None, values)[0]
        if hidden.ndim != 3 or hidden.shape[-1] != MODEL_DIMENSION:
            raise ValueError("unexpected E5 output shape")
        weights = mask[..., None]
        vectors = (hidden * weights).sum(axis=1) / np.maximum(weights.sum(axis=1), 1)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        return vectors / np.maximum(norms, 1e-12)

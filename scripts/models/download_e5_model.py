"""Download and verify the exact E5 ONNX assets used by the music API."""
from __future__ import annotations

import hashlib
from pathlib import Path
import sys
from urllib.request import urlopen


MODEL_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
ASSETS = {
    "onnx/model_O4.onnx": (
        "model_O4.onnx",
        "4654c156f3e4171abc9c716cdb771bf9116455d15ac1aab364aeeede0e3205b0",
    ),
    "tokenizer.json": (
        "tokenizer.json",
        "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39",
    ),
}


def verify(path: Path, expected: str) -> bool:
    if not path.is_file():
        return False
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == expected


def download(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for remote, (filename, checksum) in ASSETS.items():
        target = destination / filename
        if verify(target, checksum):
            continue
        temporary = target.with_suffix(target.suffix + ".download")
        url = f"https://huggingface.co/intfloat/multilingual-e5-small/resolve/{MODEL_REVISION}/{remote}"
        try:
            with urlopen(url, timeout=60) as response, temporary.open("wb") as output:
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    output.write(chunk)
            if not verify(temporary, checksum):
                raise RuntimeError(f"checksum mismatch for {filename}")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    download(Path(sys.argv[1] if len(sys.argv) > 1 else "model"))

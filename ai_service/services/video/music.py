"""사용자 선택 음원과 자체 합성한 긍정적인 배경음악 프리셋을 준비한다."""
from pathlib import Path
import wave

import numpy as np

MUSIC_CATALOG = [
    {'id': 'AUTO', 'name': '추억 분위기에 맞추기', 'description': '추억 정보와 연출 분위기로 긍정적인 음악 선택'},
    {'id': 'WARM', 'name': '따뜻한 기억', 'description': '잔잔한 피아노 계열의 장조 배경음악'},
    {'id': 'BRIGHT', 'name': '즐거운 하루', 'description': '경쾌한 플럭과 가벼운 리듬의 장조 배경음악'},
    {'id': 'CALM', 'name': '햇살 속 풍경', 'description': '밝고 차분한 패드·멜로디 배경음악'},
    {'id': 'NONE', 'name': '음악 없음', 'description': '무음 영상'},
]


def choose_preset(selection, memory_style: str, context: str) -> str:
    if selection.preset != 'AUTO':
        return selection.preset
    if any(word in context.lower() for word in ('생일', '축하', '축제', '파티', 'birthday', 'celebration')):
        return 'BRIGHT'
    if any(word in context.lower() for word in ('가족', '부모', '가정', 'family')):
        return 'WARM'
    return {'WARM': 'WARM', 'LIVELY': 'BRIGHT', 'CINEMATIC': 'CALM'}[memory_style]


def render_preset(destination: Path, preset: str, duration: float) -> None:
    """외부 음원을 복제하지 않는 자체 합성 데모 음악. 실제 피아노 녹음이나 AI 작곡 모델은 아니다."""
    sample_rate = 22050
    audio = np.zeros((round(duration * sample_rate), 2), dtype=np.float32)
    tempo = {'WARM': 88, 'BRIGHT': 112, 'CALM': 76}[preset]
    beat = 60 / tempo
    chords = ((60, 64, 67), (65, 69, 72), (67, 71, 74), (60, 64, 67))

    def note(midi, start, length, gain, pan=0.5, pad=False):
        first = round(start * sample_rate)
        count = min(round(length * sample_rate), len(audio) - first)
        if count <= 0:
            return
        t = np.arange(count, dtype=np.float32) / sample_rate
        frequency = 440 * 2 ** ((midi - 69) / 12)
        if pad:
            tone = np.sin(2 * np.pi * frequency * t) + 0.18 * np.sin(2 * np.pi * frequency * 2.002 * t)
            envelope = np.minimum(t / 0.22, 1) * np.minimum((count / sample_rate - t) / 0.5, 1)
        else:
            tone = sum(weight * np.sin(2 * np.pi * frequency * harmonic * t)
                       for harmonic, weight in ((1, 1), (2, 0.32), (3, 0.12), (4, 0.04)))
            envelope = (1 - np.exp(-80 * t)) * np.exp(-t * (3.8 if preset == 'BRIGHT' else 2.3))
            envelope *= np.minimum((count / sample_rate - t) / 0.04, 1)
        signal = tone * envelope * gain
        audio[first:first + count, 0] += signal * np.sqrt(1 - pan)
        audio[first:first + count, 1] += signal * np.sqrt(pan)

    melody = (0, 4, 7, 4, 2, 5, 9, 7)
    bars = int(np.ceil(duration / (4 * beat)))
    for bar in range(bars):
        chord = chords[bar % len(chords)]
        for index, pitch in enumerate(chord):
            note(pitch - 12, bar * 4 * beat, 4 * beat, 0.08, 0.3 + index * 0.2, pad=True)
        for step in range(8):
            start = (bar * 4 + step * 0.5) * beat
            note(chord[step % 3], start, 1.6, 0.10 if preset != 'CALM' else 0.04,
                 0.35 if step % 2 else 0.65)
        for step in range(4):
            note(72 + melody[(bar * 4 + step) % len(melody)], (bar * 4 + step) * beat,
                 1.8, 0.10 if preset != 'CALM' else 0.055)
        if preset == 'BRIGHT':
            for step in range(4):
                note(36, (bar * 4 + step) * beat, 0.16, 0.08)
    peak = float(np.max(np.abs(audio)))
    if peak > 0:
        audio *= 0.72 / peak
    fade = min(round(0.3 * sample_rate), len(audio) // 2)
    audio[:fade] *= np.linspace(0, 1, fade, dtype=np.float32)[:, None]
    audio[-fade:] *= np.linspace(1, 0, fade, dtype=np.float32)[:, None]
    with wave.open(str(destination), 'wb') as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes((np.clip(audio, -1, 1) * 32767).astype('<i2').tobytes())

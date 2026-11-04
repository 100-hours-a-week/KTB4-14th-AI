# 추억영상 품질 검사·음악 선택·연출

## OpenCV가 실제로 검사하는 것

이전 구현은 OpenCV로 EXIF 회전·반전, 리사이즈와 여백 처리만 수행했다.
이번 변경은 여백·배경 효과를 넣기 **전**의 사진에서 다음 점수를 계산한다.

| 항목 | 방법 | 현재 경고 기준 |
| --- | --- | --- |
| 흐림 의심 | 긴 변 최대 1024픽셀 분석 영상의 그레이스케일 Laplacian 분산 | 60 미만 BLURRY, 60~120 미만 SOFT, 120 이상 SHARP |
| 어두움 | 밝기 20 미만 픽셀의 비율 | 65% 초과 UNDEREXPOSED |
| 과도한 밝음 | 밝기 235 초과 픽셀의 비율 | 65% 초과 OVEREXPOSED |
| 낮은 대비 | 밝기 95백분위수−5백분위수 | 40 미만 LOW_CONTRAST |
| 낮은 해상도 | 방향 보정 후 원본 크기 | 너비 640 또는 높이 360 미만 |

점수는 `report.json`의 `preprocessing[].quality`에 기록한다.
Laplacian은 [OpenCV의 경계·미분 연산](https://docs.opencv.org/4.x/d5/db5/tutorial_laplace_operator.html)이다.
분산이 낮다는 이유만으로 사진이 나쁘다고 확정할 수 없다. 하늘·보케·의도적인 흐림·노이즈·질감에 영향을 받는다.
현재 임계값은 초기 휴리스틱이며 서비스 사진으로 보정한 분류 정확도나 확률이 아니다.
인물 인식·감정 분류·흐림 복원 기능도 아니다.

기본 `quality_policy=WARN`은 점수와 경고를 반환하고 사진 순서를 유지한다.
사진을 자동 삭제하거나 건너뛰지 않는다. `REJECT`를 선택하면 BLURRY·UNDEREXPOSED·OVEREXPOSED 사진을
GPU 요청 전에 `photo_quality_rejected`로 거부하며 점수와 사진 인덱스를 보관한다.

## 사용자가 음악을 선택하는 입력

AI Python 서비스와 CLI는 다음 선택을 지원한다.

| 선택값 | 의미 |
| --- | --- |
| AUTO | 추억 정보와 선택된 연출 스타일로 프리셋 결정 |
| WARM | 따뜻한 기억: 피아노 계열 장조 음악 |
| BRIGHT | 즐거운 하루: 경쾌한 플럭·리듬 장조 음악 |
| CALM | 햇살 속 풍경: 밝고 차분한 패드·멜로디 |
| NONE | 명시적인 무음 |
| music.file | 사용자가 고른 실제 음원 파일. 프리셋보다 우선 |

AUTO는 `memory_context`에 생일·축하·축제·파티가 있으면 BRIGHT, 가족·부모가 있으면 WARM을 고른다.
그 외에는 WARM→WARM, LIVELY→BRIGHT, CINEMATIC→CALM으로 선택한다.
이는 **전달된 추억 정보 기반의 규칙**이며 사진의 감정을 OpenCV로 추론하는 기능이 아니다.
직접 선택한 프리셋·파일은 AUTO 판단으로 바꾸지 않는다.

기본 프리셋은 이 프로젝트가 합성하는 간단한 배경음악 샘플이다. 외부 곡이나 실제 악기 녹음을 제공하는 카탈로그는 아니다.
서비스용 음원을 쓰려면 백엔드가 사용자가 고른 `music_track_id`를 실제 음원 자산으로 해석하고
AI 서버가 읽을 수 있도록 준비한 로컬 파일 경로를 `music.file`로 전달한다.
기존 음악 추천 API의 YouTube 링크를 음원 파일로 간주하거나 자동 추출하지 않는다.

이 저장소에는 프론트엔드와 영상 접수 HTTP API가 없다. 여기서 구현한 것은 선택 카탈로그와 생성 입력·합성이다.
사용자용 선택 화면, 트랙 ID 관리와 파일 업로드·저장소 연결은 프론트엔드·백엔드의 연동 작업이다.

```json
{
  "photos": ["/prepared/start.jpg", "/prepared/end.jpg"],
  "duration_seconds": 5,
  "memory_style": "LIVELY",
  "memory_context": "친구들과 떠난 밝고 즐거운 여행",
  "music": {"preset": "AUTO", "volume": 0.65, "start_seconds": 0},
  "photo_fit": "blurred_background",
  "quality_policy": "WARN"
}
```

5초 요청은 `ClipRequest`, 10~180초 최종 요청은 `VideoRequest`를 사용한다.
사용자 파일은 100MB 이하로 제한하고 오디오 스트림·시작 시점을 GPU 요청 전에 검증한다.
지정 시점 이후 구간을 잘라 준비하고, 짧으면 반복해 영상 길이에 맞춘다.
AAC 스테레오를 합성하고 음량 정규화·사용자 볼륨·음악의 시작과 끝 페이드·피크 제한을 적용한다.
영상의 장면 전환을 음악 페이드로 대신하지 않는다.

## 추억영상 연출

- `story_mode=PHOTO_MOTION`은 원본 사진의 줌·가로 이동·컷 편집을 실제 FFmpeg로 렌더링한다.
  GPU나 생성 모델을 호출하지 않으며 사진 안에 새로운 인물·물체를 만들지 않는다.
  확대율 1.02~1.06의 완만한 이동으로 가장자리는 조금 잘린다. 흐린 배경·색감·선택 음악은 함께 적용한다.
  사진당 최소 1.5초를 배정하며 프레임을 나눠 총 길이를 유지한다.
  이 모드의 기록은 `execution_mode=photo_motion`, `gpu_inference_executed=false`로 AI 생성 측정과 분리한다.
  최대 120장·180초를 지원한다. AI 모드의 입력 상한 91장은 유지한다.
- `story_mode=MONTAGE`는 사진마다 같은 사진을 시작·종료 조건으로 사용해 독립된 짧은 AI 숏을 만든 뒤
  컷으로 연결한다. 사진 두 장·5초이면 2.5초·41프레임 숏 두 개다. 다른 장소 사이의 액체 같은 변형을 줄이려는 편집 구조다.
  단순 페이드·정지 사진 늘리기로 길이를 채우지 않는다. GPU 요청은 두 개이며 실제 지연 시간은 별도 측정해야 한다.
- 기존 5분 측정과의 호환을 위해 기본값은 `TRANSITION`이다. 새 몽타주는 `--story-mode MONTAGE`로 명시한다.
- WARM / LIVELY / CINEMATIC에 따라 따뜻한 기억·즐거운 여행·차분한 영화 같은 긍정적 분위기를 지시한다.
- 클립마다 천천히 전진하거나 옆으로 이동하는 카메라와 전경 시차를 지시한다.
- 얼굴·손·랜드마크 보존, 액체처럼 변형되는 전환·디졸브·이중 노출 방지 조건을 전달한다.
- 기본 `photo_fit=blurred_background`는 원본 주 피사체 전체를 보존하고 빈 영역을 어둡게 흐린 사진 배경으로 채운다.
  `letterbox`로 이전 검은 여백을 선택할 수 있다.
- 최종 인코딩에서 스타일에 따른 색온도·채도·대비와 선택적 약한 비네트를 적용한다.

AI가 프롬프트를 완전히 지킨다는 보장은 없다. 특히 서로 다른 장소·인물 사이의 영상은
추가 장면이나 변형이 생길 수 있어 최종 영상의 육안 확인이 필요하다.
실제 프롬프트는 각 `workflow-*.json`에 저장한다.

## 로컬 실행

```bash
# 원본 사진 기반 줌·이동·컷 편집 + 음악. GPU 없이 실행
uv run --frozen --extra video python -m scripts.video.run smoke \
  --photos /actual/start.jpg /actual/end.jpg --workspace artifacts/video/photo-motion \
  --story-mode PHOTO_MOTION --memory-style LIVELY --memory-context '친구들과 떠난 여행' --music AUTO

# 선택지와 기본 샘플 미리듣기 만들기
uv run --frozen --extra video python -m scripts.video.run music-list \
  --preview-dir artifacts/video/music-previews

# 추억 연출·음악을 포함한 새 AI 생성
uv run --frozen --extra video python -m scripts.video.run smoke --profile fast \
  --photos /actual/start.jpg /actual/end.jpg --workspace artifacts/video/memory \
  --story-mode MONTAGE --memory-style LIVELY --memory-context '친구들과 떠난 여행' --music AUTO

# 음악·색감만 교체: 기존 생성 클립을 사용하므로 GPU 재생성 없음
uv run --frozen --extra video python -m scripts.video.run remix \
  --video artifacts/video/fast/17cf65eba1474c0f8eaec3cc3f9153fb/clip-0000.mp4 \
  --workspace artifacts/video/memory-remix --memory-style LIVELY --music BRIGHT

# 사용자가 선택한 파일과 시작 시점 적용
uv run --frozen --extra video python -m scripts.video.run remix \
  --video /actual/clip.mp4 --music-file /actual/selected.mp3 \
  --music-start 12 --music-volume 0.65 --workspace artifacts/video/user-music
```

`remix`는 기존 파일을 보존하고 새 폴더에 저장한다. 색감의 중복 적용을 피하려면
이미 후처리한 `final.mp4`보다 원래 생성한 `clip-0000.mp4`를 입력한다.

## 현재 검증

OpenCV의 선명/흐림·과소/과다 노출 검사, 배경 효과 전 점수 일관성, 음악 프리셋 선택,
사용자 음원 반복과 실제 소리 샘플, AAC 스테레오·명시적 무음, GPU 요청 전 거부와 프롬프트를 자동 검사한다.

기존 실제 AI 생성 클립의 음악·LIVELY 색감 후처리를 0.894초에 완료했다.
결과: `artifacts/video/memory-remix/138c62e7d13644189e09ab485e2b591d/final.mp4`.
5.000초·1280×720·H.264와 AAC 스테레오 1개를 확인했다. GPU를 다시 호출하지 않았다.

2026-10-29 영상 테스트 37개와 전체 회귀 테스트 369개를 통과했다(1개 건너뜀).
실제 사진 두 장으로 `PHOTO_MOTION`을 **1.933초**에 완료했다.
결과: `artifacts/video/photo-motion/6e469659a22a4f2c82c49e0cda647f48/final.mp4`.
5.000초·1280×720·H.264·AAC 스테레오를 확인했고 디코딩한 오디오 RMS는 0.08056으로 무음이 아니다.
사진 순서와 줌·이동·컷을 프레임으로 확인했다. 음악은 AUTO→BRIGHT 자체 합성 샘플이다.
이는 GPU AI 생성 속도가 아니라 실제 사진 편집·음악 합성 속도이며 500편 처리량을 검증한 수치는 아니다.

원본 입력 폴더가 비어 있어 같은 사진의 이전 전처리 파일에서 알려진 검은 여백만 제거해 사용했다.
입력은 540×720과 720×720이며 원본 고해상도 사진으로의 재검증은 아니다.
첫 사진은 SOFT·LOW_RESOLUTION, 두 번째는 SHARP·노출 NORMAL로 기록됐다.

### 2026-10-29 새 L40S AI 몽타주 검증 결과: 미통과

사용자 승인 후 기존 `syhkvtb28srrmm`를 보존하고 새 `0w2ili446gj0y7`에 모델 6개를 준비했다.
모델 준비 후 `fast`·MONTAGE·LIVELY·두 장·5초 요청 1건을 제출했다.
첫 2.5초 숏은 내려받았지만 **사진에 없는 아이들이 새로 나타났다**. 원본 보존 품질 기준에 미달한다.
두 번째 숏은 준비 포함 전체 제한에 따른 취소로 완료되지 않아 5초 최종 AI 몽타주가 없다.
이 실행으로 AI 몽타주의 5분 완료를 입증하지 못했다. 이전 TRANSITION 184.206초와도 구분해야 한다.

별도 감시 프로세스가 전체 시작 후 563.16초에 새 Pod를 중지했고 `desiredStatus=EXITED`를 확인했다.
기존 Pod도 EXITED로 보존했다. 중지는 삭제가 아니며 디스크 요금 여부는 GPU 가동 상태와 별개다.
원격 모델·중간 결과는 로컬 최종 결과로 간주하지 않는다.
실행 기록: `artifacts/video/memory-validation/{live-result,pod-budget,visual-review}.json`,
첫 숏·실패 기록: `artifacts/video/memory-live/680a55562dbd408791a0e167b86b4910/`.

### 2026-10-29 재개 시 확인한 인프라 문제

기존 Pod 시작 요청이 HTTP 500으로 거절됐다. 제공자 메시지는 기존 호스트에 시작할 수 있는 빈 GPU가 없다는 내용이다.
계정 충전은 반영됐지만 Pod 상태는 EXITED로 남았다.
기존 Pod 설정은 `containerDiskInGb=150`, `volumeInGb=0`이다.

[RunPod의 공식 이전 정책](https://docs.runpod.io/pods/troubleshooting/pod-migration)에 따르면
자동 이전은 볼륨 디스크를 복사하며 컨테이너 디스크는 복사하지 않는다.
현재 구성에서는 `/workspace`라는 경로만 사용해도 영구 볼륨에 저장된다는 뜻이 아니다.
새 Pod에서 모델이 비어 있었던 현상과 일치한다. 새 GPU 검증 전 실제 모델 파일 존재 여부를 다시 확인해야 한다.
영구 볼륨은 추가하지 않았다. 새 Pod 생성·모델 준비는 위의 명시적 승인으로 한 번 수행했다.

### 2026-10-29 Leon 폴더 전체 110장 PHOTO_MOTION 참고 측정

**이 측정은 사용자가 요청한 First–Last Frame AI 테스트가 아니다.**
사용자는 FLF 생성을 요청했으며 이 사진 편집 방식으로 대체한 것은 요청과 맞지 않았다.
아래 65.517초를 FLF 성능·요청 완료의 근거로 사용하면 안 된다.
FLF는 `story_mode=TRANSITION`으로 서로 다른 시작·종료 사진을 연결한다.

바탕화면 `샘플사진 Leon`의 JPG 108장과 DNG 2장을 모두 사용했다. 원본은 변경하지 않았다.
DNG는 macOS `sips`로 품질 90·최대 변 6000픽셀의 JPEG 변환본을 만들어 입력했다.
촬영 EXIF 시각순으로 배치하고 시각이 없는 2장은 파일명순으로 마지막에 배치했다.
`PHOTO_MOTION`, LIVELY, 음악 AUTO→BRIGHT, WARN, 흐린 배경 설정으로 실제 MP4를 한 번 생성했다.

| 측정 범위 | 소요 시간 |
| --- | ---: |
| 입력 조사·EXIF·DNG 변환 | 1.752초 |
| 디코딩·OpenCV 품질 검사·사진 전처리 | 14.995초 |
| 110개 사진의 움직임 렌더링 | 31.909초 |
| 최종 영상·음악 인코딩 | 16.240초 |
| 파이프라인 전체 | 63.760초 |
| 입력 준비 포함 전체 | **65.517초 = 약 1분 6초** |

출력은 **180초·1280×720·16fps·H.264·AAC 스테레오·35,435,340바이트**다.
110개 사진 인덱스가 순서대로 포함되고 총 2880프레임임을 확인했다.
영상 앞·중간·끝(1·89·177초)에서 오디오 RMS가 각각 0.0936·0.0853·0.0813으로 소리가 있음을 확인했다.
15초 간격 프레임 모음의 화면 방향과 사진 표시를 육안 검사했다.

OpenCV 결과는 SHARP 99장, SOFT 7장, BLURRY 4장이다. UNDEREXPOSED 경고는 별도로 1장이다.
총 12장에 경고가 있었고 WARN 정책에 따라 제외 없이 110장을 모두 포함했다.
자연스러운 야간·풍경 사진도 이 기준에서 경고될 수 있으므로 휴리스틱 결과를 정답으로 해석하면 안 된다.

이는 한 번의 실제 로컬 사진 편집 측정이다. 짧은 영상 회귀 테스트가 렌더링과 일부 겹쳤으며
격리된 성능 벤치마크나 GPU AI 생성 속도를 뜻하지 않는다. 영상 테스트 38개를 통과했다.
구현·테스트 개발 시간과 완성 후 오디오·화면 QA 시간은 65.517초에 포함하지 않았다.

영상: `artifacts/video/leon-benchmark/jobs/f43bc56757ce4a3496617ab9f1aaafe4/final.mp4`.
측정: `artifacts/video/leon-benchmark/summary.json`.
사진별 품질·파일명: `artifacts/video/leon-benchmark/photo-quality.csv`.
입력 순서·변환 기록: `artifacts/video/leon-benchmark/inputs.json`.

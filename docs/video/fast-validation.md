# 사진 두 장·5초 고속 생성 검증

2026-10-28 한국시간 기준, L40S 48GB / ComfyUI 0.35.0 / PyTorch 2.10.0+cu128에서 검사한다.

## 변경한 경로

고정된 공식 템플릿의 LightX2V 분기를 `--profile fast`로 선택한다.
High/low LoRA 각각 strength 1, shift 5, CFG 1, 4단계(2+2)를 사용한다.
1280×720, 81프레임, 사진 두 장의 시작·종료 조건을 유지한다.
standard는 기존 20단계 경로다. fast의 출력 품질은 별도 육안 확인이 필요하다.

## 사전 검사

- 영상 관련 자동 테스트 26개와 전체 회귀 테스트 358개 통과, 1개 건너뜀. 공식 템플릿의 비활성 고속 분기와 LoRA·CFG·단계·연결을 대조했다.
- 현재 RunPod의 실제 ComfyUI 경로는 `/workspace/runpod-slim/ComfyUI`다.
- 공식 고정 리비전의 LightX2V LoRA 두 개를 `models/loras/`에 준비했다. 각각 1,226,977,424바이트다.
- 원격 `check --profile fast`가 `preflight_passed`를 반환했다. 기본 모델 4개와 LoRA 2개의 존재를 확인했다.
- 사용자가 기존 standard 작업 중단 후 fast 테스트를 승인했다. 다른 작업의 제출·중단은 수행하지 않는다.

## 실제 성능

실제 GPU 생성 1건에서 **184.206초(약 3분 4초)**로 목표 300초를 충족했다.

| 항목 | 측정값 |
| --- | --- |
| 작업 ID | `17cf65eba1474c0f8eaec3cc3f9153fb` |
| 시작·완료(KST) | 2026-10-28 19:09:24 ~ 19:12:28 |
| 입력 | 기존 테스트와 같은 `start_clean.jpg`, `end_clean.jpg`, seed 0 |
| 프롬프트 | Smooth cinematic camera movement, realistic travel video |
| 실행 | 실제 RunPod L40S, `fast`, 4단계, CFG 1, LoRA 1.0 |
| 출력 | 1280×720, H.264, yuv420p, 16fps, 5.000초, 1,322,761바이트 |
| 전처리·탐색·업로드 후 생성 시작 | 13.603초 |
| 서버 생성 실행 시간 | 163.324초 |
| 다운로드 시작 | 요청 후 180.828초 |
| 최종 MP4 저장 완료 | 요청 후 184.206초 |
| 목표 충족 | `latency_target_met: true` |

로컬 결과는 `artifacts/video/fast/17cf65eba1474c0f8eaec3cc3f9153fb/final.mp4`,
전체 측정은 같은 폴더의 `report.json`, 요청 그래프는 `workflow-0000.json`이다.
`progress.jsonl`에는 조회 시점의 실제 ComfyUI 단계 이벤트를 별도로 남겼다.
모델 설치·기존 작업 중단 대기 시간은 새 생성 요청의 측정에 포함하지 않는다.
전처리, 원격 탐색, 업로드, 생성, 다운로드, FFmpeg 최종 인코딩은 모두 포함한다.

이 측정은 앞서 standard 작업이 실행된 **이미 가동 중인 Pod**에서 수행한 단일 요청이다.
동일 프롬프트의 일부 노드 결과를 서버가 캐시했을 수 있다. 재시작 직후 첫 생성, 다른 프롬프트·사진,
다른 GPU, 동시 요청, 10~180초 영상의 소요시간을 보장하지 않는다.
사진 사이의 장면·인물 변화에 따른 시각 품질도 별도로 판단해야 한다. Mock 시간은 성능 근거에 쓰지 않았다.

현재 로컬 `.env`의 `VIDEO_GENERATION_PROFILE=fast`를 적용했다. 이후 CLI에서 `--profile`을 생략해도
fast를 사용하며 `--profile standard`로 이전 경로를 명시할 수 있다. 실제 `.env`와 영상·사진·모델은 Git에 넣지 않는다.

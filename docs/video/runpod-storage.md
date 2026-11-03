# RunPod 영상 저장·조회

별도 AWS S3 버킷 없이 RunPod Network Volume의 S3 호환 게이트웨이를 사용한다.
GPU가 중지돼도 게이트웨이로 결과를 읽는다. 일반 RUNPOD_API_KEY가 아닌 별도의
RunPod S3 access key/secret이 필요하다. 키를 채팅·Git·결과 JSON에 넣지 않는다.

## 설정

`.env`에 RUNPOD_STORAGE_VOLUME_ID, RUNPOD_STORAGE_REGION,
RUNPOD_S3_ACCESS_KEY_ID, RUNPOD_S3_SECRET_ACCESS_KEY를 설정한다.
S3 키는 RunPod 콘솔 Credentials → S3 API keys에서 발급한다.

현재 이 대화에서 생성·조회 검증한 볼륨은 `ioqbto3qji`, US-WA-1, Standard 100GB다.
2026-11-03 재조회에서 해당 지역에는 24GB MIG만 표시돼 기존 80GB 모델용 Pod는 만들지 않았다.
같은 지역의 GPU 재고는 새 Pod 생성 전에 다시 확인해야 한다. 기존 Pod는 보존한다.

## 기존 완성 영상 업로드

```bash
python -m scripts.video.storage publish --video /results/final.mp4
```

실제 MP4를 ffprobe로 확인하고 썸네일을 생성한다. 새 publication job ID를 부여하고
`jobs/{job_id}/final.mp4`, `thumbnail.jpg`를 업로드한다. 두 파일을 게이트웨이에서
다시 읽어 크기·SHA-256을 검증한 뒤에만 `result.json` 완료 마커를 기록·재조회한다.
실패한 업로드는 완료 마커 없이 남을 수 있으며 자동 삭제하지 않는다.
완료 결과에는 사용자 로컬 경로나 프롬프트·인증정보를 포함하지 않는다.

## 생성 후 자동 업로드

```bash
python -m scripts.video.run memory --input-dir /photos --music-file /music/selected.wav --publish-to-runpod
```

키 누락은 GPU 생성 전에 거부한다. preview/mock 결과는 이 옵션으로 게시할 수 없다.
업로드가 실패해도 생성된 로컬 영상은 보존하므로 publish 명령으로 재생성 없이 다시 업로드한다.
게시 ID는 생성 단계의 기존 작업 ID와 별개다. 백엔드는 자신의 작업 ID와 반환된 게시 ID를
연결해서 저장해야 한다. 재게시하면 새 ID가 생성된다.

## 조회·다운로드

```bash
python -m scripts.video.storage inspect --job-id PUBLICATION_ID
python -m scripts.video.storage download --job-id PUBLICATION_ID --output /results/downloaded.mp4
```

다운로드 시 완료 마커의 job ID·볼륨·지역·파일 경로를 검사하고 파일 크기·해시를 검증한다.
기존 로컬 파일을 덮어쓰지 않는다. 모델 디렉터리 등 임의의 파일 경로를 조회할 수 없다.
이 명령과 `VolumeResults` 클래스는 CPU 백엔드에서 실행 가능하다. 계정 S3 키 권한은
클래스의 경로 검사보다 넓을 수 있으므로 사용자에게 키나 SDK를 직접 노출하지 않는다.

## 연동 범위와 검증 상태

백엔드는 사용자·여행 소유권을 확인한 후 저장된 게시 ID로 VolumeResults.inspect/download를
호출하면 된다. 웹 동영상 Range 스트리밍 API, 사용자 인증·DB 매핑, 결과 큐 통지는 아직 별도
백엔드 연동 대상이다. 썸네일과 MP4 보관은 파일 저장이며 관계형 DB를 대체하지 않는다.
임시 파일 자동 삭제는 실제 저장·백엔드 완료 처리가 확인되기 전에는 적용하지 않는다.

자동 테스트는 메모리 게이트웨이로 경로 격리·업로드 실패·해시 손상·완료 마커·덮어쓰기 방지를
검증한다. 2026-11-03 실제 RunPod S3 키를 설정하고 기존 30초 완성 영상을 게시했다.
게시 ID는 `e2edfce2b7e54be8884fcf44801e3f95`다. MP4·썸네일을 업로드하고 원격 전체 읽기
검증 후 완료 마커를 게시·재조회했으며, 별도 다운로드 명령 경로도 검증했다.
영상은 45,360,451바이트, 30초, 1920×1080, 24fps, 스테레오 AAC이며
SHA-256은 `f13bcfdbbc924c875a664e426e7fa587748513b389e2f26dc699370d8f250010`으로
기존 완성본과 일치한다. 검증 중 기존 Pod 3개는 MCP 재조회에서 모두 EXITED였다.
이번 검증은 새 GPU 생성이 아니라 기존 완성 영상의 영구 보관·조회 검증이다.
GPU Pod 연결·모델 배치와 백엔드 HTTP/큐 연동은 미완료다.

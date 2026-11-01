# 추억 영상: 여행 전체를 기억하는 30초 이야기

2026-10-29 최신 요구는 단순히 풍경을 움직이는 영상이 아니라 다시 꺼내 보고 싶은 여행의 기억이다. 이전 정원·폭포·열차 영상은 기술적으로 움직였지만 이야기와 감정이 부족해 사용자가 거절했다. 렌더 성공과 사용자 요구 충족을 구분한다.

## 현재 구성

- OpenCV는 초점·노출·해상도·유사도를 검사한다. 의미나 감정을 이해하는 분류기는 아니다.
- 보통 규모의 입력은 품질을 통과한 여행 전체를 이미지 분석에 전달한다. 대규모 입력은 시간 구간별 대표와 주변 사진으로 최대 128개 후보를 만든다. 마지막 정원 구간에만 후보가 몰리지 않게 한다.
- 이미지 분석은 사진에 실제로 있는 경험에서 출발/기대, 한 끼·한 모금 같은 작은 기억, 대표 경험, 분위기 있는 하이라이트, 귀가/여운을 고른다. 새 동행자·관계·행사를 만들지 않는다.
- 기본 참고 사진 상한은 12장(최대 6개 장면의 First–Last 쌍)이다. 장면별 duration_seconds와 memory_reason을 기록하고 전체 길이가 정확히 30초인지 GPU 제출 전에 검사한다.
- LTX-2.3 distilled FP8 단일 샘플러로 721프레임·24fps를 생성한다. 기본 768×448 생성 후 1280×720으로 확대/크롭한다. 가이드 강도는 0.7이다. 장면 경계는 인접 프레임에 배치해 컷 전환을 지시한다.
- 생성용 긍정 프롬프트에는 보이는 장소, 구체적인 행동, 카메라 동선을 간결하게 적는다. 사진·액자·콜라주 관련 금지어를 반복 나열하지 않는다. 한국인 조건은 원본 인물의 외형·옷·인원을 유지하는 범위에서 적용한다.
- 한 곡의 긍정적인 기악곡을 도입→고조→해결로 지시하고 실제 장소 소리를 낮게 겹친다. 사용자 음원 파일과 NONE도 지원한다. 사용자가 고른 파일이 있으면 해당 파일을 사용한다.
- 제목과 closing_line은 AI 프레임 생성 지시와 분리해 로컬에서 작게 합성한다. 사진을 삽입하거나 정지 사진 줌 영상으로 대체하지 않는다. 한국어 글꼴은 macOS Apple SD Gothic Neo 또는 Linux Noto Sans CJK/KR이 필요하다.
- 최종 길이·음원·코덱 검사 후 실제 시각 품질과 이야기 흐름을 별도로 검토한다. 인물 삽입, 액자 이미지, 장면 변형, 긴 겹침은 시각 검토 대상이다. 테스트 통과만으로 감정이나 사용자 만족을 보증하지 않는다.

## 실행

```bash
uv sync --frozen --extra video --extra dev
.venv/bin/python -m scripts.video.run memory \
  --input-dir '/actual/travel-photos' --workspace artifacts/video/memory \
  --duration 30 --memory-style WARM --preview-only \
  --memory-context '여행 전체에서 작은 기억과 하이라이트, 귀가의 여운을 담은 추억 영화'
# 실제 생성은 --preview-only 제거
```

미리보기는 GPU 없이 OpenAI 이미지 분석을 호출한다. OPENAI_API_KEY 또는 기존 OPEN_API_KEY 별칭을 사용한다. VIDEO_STORY_MODEL을 생략하면 기존 OPENAI_MODEL을 사용한다. TravelStoryboard를 직접 넣으면 검토한 계획을 재사용한다. 제목·엔딩·음악 방향·장면별 길이는 storyboard.json과 report.json에 남긴다.

30초는 자체 실행 실험이며 호스팅 API 공식 지원 길이를 30초라고 주장하는 것이 아니다. 3분이나 5초 클립 연결로 바꾸지 않는다. 작업 후 GPU를 중지한다. 총예산 320,000원 범위에서 운영한다.

## 이전 검증 이력

- 20초/6장 시험은 대부분 정지·겹침이었고, 13.4초 편집본도 거절됐다. artifacts/video/ltx-live/에 보존한다.
- 30초 다중 장면 시험 중 액자 인물이나 단체 사진을 생성한 결과는 미통과로 기록했다.
- 간결한 행동 프롬프트의 3장면/30초 결과는 움직임과 소리가 있었지만, 사용자가 단순한 풍경 영상으로 평가해 거절했다. artifacts/video/revisit30-live/delivery.json에 사용자 평가를 기록했다. 당시 최종 1회 600.269초는 렌더/동작 측정이며 추억 영상 완성의 증거가 아니다.
- 새 전체 여행 구성과 실행 기록은 artifacts/video/memory-story-v2/에 기록한다.

## 공식 근거

- [LTX-2.3 FLF 템플릿](https://github.com/Comfy-Org/workflow_templates/blob/14220b8bd3e86baa852959f17039613191e66656/templates/video_ltx2_3_flf2v.json)
- [LTX 행동·카메라 프롬프트 안내](https://docs.ltx.io/open-source-model/usage-guides/prompting-guide)
- [OpenAI 이미지 입력](https://developers.openai.com/api/docs/guides/images-vision)
- [OpenAI 구조화 출력](https://developers.openai.com/api/docs/guides/structured-outputs)

모델 고정 revision과 그래프 변환 근거는 ai_service/services/video/ltx.py의 LTX_PROVENANCE에 기록한다. 실제 object_info로 노드와 모델을 사전 검사한다.

## 이번 전달본의 생성·편집 범위

`artifacts/video/memory-story-v2/다시-그날의-제주-30초.mp4`는 검토한 **AI 영상 구간을 편집한** 30초 버전이다. CLI의 단일 원본을 무검토 완성품으로 취급하지 않는다.

- 새 30초 전체 여행 원본 1회: 343.357초. 원본에는 장소/식탁 혼합이 있어 그대로 전달하지 않았다.
- 실제 생선구이 식탁을 유지한 30초 원본: 232.448초.
- 원래 야간 정원과 조명 나무를 유지한 30초 원본: 241.478초.
- 이전에 생성한 해안길 AI 원본의 정상 구간도 재사용했다. 5초 확산 생성 요청을 여러 번 연결한 결과가 아니다.
- 최종 구간은 비행기 2초, 식탁 2초, 음식 디테일 3.5초, 음료 1.5초, 해안길 6초, 야간 정원 9초, 공항 노을 6초다. 합계 720프레임·24fps·30초이며 속도 변경이나 정지 사진 삽입을 쓰지 않았다.
- 제목/엔딩은 그래픽으로 작게 얹고, 한 곡의 음원에 도입→고조→마무리의 음량 흐름을 적용했다.
- `Media.assemble_memory_film`은 검토한 FilmTake의 원본 프레임 구간을 받아 컷 편집한다. 이번 구간 선택은 시각 검토를 거친 편집 결정이며 완전 자동 편집 품질을 검증한 것은 아니다.
- 전달 기록 `delivery.json`, 원본 구간 `film/film-edit.json`, 최종 프레임 검토 `film-qa.json`에 출처와 한계를 기록한다.
- 영상 테스트 59개 통과. 타임라인 총길이, 실제 프레임 컷 경계, 음원 고조와 제목 합성 경로를 검사했다. 모델 출력의 감정적 가치는 사용자 평가 대상이다. 검증 후 신규 GPU Pod의 EXITED를 확인했다.

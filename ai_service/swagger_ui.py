"""Swagger UI의 안내 문구를 한글로 표시하고 API 데이터는 원문을 유지한다."""

import json

from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse


_LABELS = {
    "Code": "상태 코드", "Description": "설명", "Links": "링크", "No links": "없음",
    "Successful Response": "요청 성공", "Bad Request": "잘못된 요청",
    "Unauthorized": "인증 실패", "Internal Server Error": "서버 내부 오류",
    "Service Unavailable": "일시적으로 서비스 이용 불가", "Validation Error": "요청 검증 오류",
    "Parameters": "매개변수", "No parameters": "경로·쿼리 매개변수 없음",
    "Request body": "요청 본문", "Request bodyrequired": "요청 본문 (필수)",
    "required": "필수", "Required": "필수", "optional": "선택",
    "Examples": "예시", "Examples:": "예시:", "Example Value": "예시 값",
    "Example Description": "예시 설명",
    "Edit Value": "본문 편집", "Schema": "스키마", "Schemas": "스키마 목록",
    "Try it out": "요청 작성", "Execute": "실행", "Executing...": "실행 중…",
    "Clear": "지우기", "Cancel": "취소", "Reset": "초기화",
    "Responses": "응답", "Response": "응답", "Request URL": "요청 주소",
    "Server response": "서버 응답", "Details": "상세 내용", "Undocumented": "명세에 없는 응답",
    "Response body": "응답 본문", "Response headers": "응답 헤더", "Download": "다운로드",
    "Curl": "curl 요청 명령", "Media type": "응답 형식", "Media Type": "응답 형식",
    "Request content type": "요청 본문 형식", "Response content type": "응답 본문 형식",
    "Controls": "설정 대상:", "header.": "헤더", "Accept": "Accept",
    "Authorize": "토큰 설정", "Available authorizations": "사용 가능한 인증 방식",
    "Authorized": "토큰 입력됨", "Value:": "토큰 값:", "Value": "값",
    "Apply credentials": "토큰 적용", "Logout": "토큰 해제", "Close": "닫기",
    "Authorization": "인증", "Authorization failed": "인증 실패",
    "authorization button unlocked": "인증 토큰 설정", "authorization button locked": "인증 토큰 설정됨",
    "Copy path to clipboard": "경로 복사", "Copy to clipboard": "복사",
    "Collapse operation": "API 접기", "Expand operation": "API 펼치기",
    "Expand all": "모두 펼치기", "Collapse all": "모두 접기", "Hide": "숨기기", "Show": "표시",
    "Extensions": "추가 정보", "Field": "항목", "Default value": "기본값",
    "Available values": "허용 값", "Select a definition": "명세 선택", "Select a spec": "명세 선택",
    "Servers": "서버", "Server": "서버", "Filter by tag": "태그 검색",
    "system": "시스템", "Enum": "허용 값", "default": "기본값",
    "Deprecated": "사용 중단", "deprecated": "사용 중단",
    "read-only": "읽기 전용", "write-only": "쓰기 전용",
    "Errors": "오류", "Error": "오류", "Error:": "오류:",
    "Failed to fetch.": "서버 응답을 받지 못했습니다.", "Possible Reasons:": "가능한 원인:",
    "Network Failure": "네트워크 연결 실패", "Not Found": "경로를 찾을 수 없음",
    'URL scheme must be "http" or "https" for CORS request.': 'CORS 요청 주소는 http 또는 https 형식이어야 합니다.',
    "Loading...": "불러오는 중…", "Fetch error": "명세를 불러오지 못했습니다.",
    "Failed to load API definition.": "API 명세를 불러오지 못했습니다.",
    "object": "객체", "array": "배열", "string": "문자열", "integer": "정수",
    "number": "숫자", "boolean": "참·거짓", "null": "값 없음",
}


def korean_swagger_html(*, openapi_url: str, title: str) -> HTMLResponse:
    html = get_swagger_ui_html(openapi_url=openapi_url, title=title).body.decode("utf-8")
    # UI 문구만 치환한다. JSON 예시·curl 명령·편집 입력·스크립트는 건드리지 않는다.
    script = r"""
    <script id="swagger-ko">
    (() => {
      const labels = __LABELS__;
      const protectedNodes = 'pre, code, textarea, input, script, style';
      const translate = value => {
        const key = value.trim();
        return Object.hasOwn(labels, key) ? value.replace(key, labels[key]) : value;
      };
      function localize(root) {
        if (root.nodeType === Node.TEXT_NODE) {
          if (!root.parentElement?.closest(protectedNodes)) {
            const text = translate(root.nodeValue);
            if (text !== root.nodeValue) root.nodeValue = text;
          }
          return;
        }
        if (root.nodeType !== Node.ELEMENT_NODE || root.closest(protectedNodes)) return;
        for (const attribute of ['aria-label', 'title', 'placeholder']) {
          const value = root.getAttribute(attribute);
          if (value) {
            const text = translate(value);
            if (text !== value) root.setAttribute(attribute, text);
          }
        }
        for (const child of root.childNodes) localize(child);
      }
      const observer = new MutationObserver(changes => {
        // 치환 중에는 감시를 멈춰 자체 변경을 다시 처리하지 않는다.
        observer.disconnect();
        for (const change of changes) {
          if (change.type === 'childList') change.addedNodes.forEach(localize);
          else localize(change.target);
        }
        observe();
      });
      function observe() {
        observer.observe(document.body, {
          childList: true, subtree: true, characterData: true,
          attributes: true, attributeFilter: ['aria-label', 'title', 'placeholder']
        });
      }
      localize(document.body);
      observe();
    })();
    </script>
    """.replace("__LABELS__", json.dumps(_LABELS, ensure_ascii=False))
    style = '<style>.swagger-ui h4.opblock-title.parameter__name.required::after { content: "필수" !important; }</style>'
    return HTMLResponse(html.replace("<html>", '<html lang="ko">')
                        .replace("</head>", style + "</head>")
                        .replace("</body>", script + "</body>"))

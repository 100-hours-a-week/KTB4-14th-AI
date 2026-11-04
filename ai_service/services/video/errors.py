"""인증정보나 외부 응답 본문을 노출하지 않는 영상 처리 오류 형식을 정의한다."""


class VideoError(Exception):
    def __init__(self, reason: str, **detail):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail

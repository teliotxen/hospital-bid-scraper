# 병원 입찰 정보 스크래퍼

국내 주요 병원의 입찰 공고 게시판에서 입찰 정보(입찰명, 날짜, URL)를 자동으로 수집하는 FastAPI 기반 REST API 서버입니다.

## 기능

- 전국 42개 병원 입찰 게시판 지원
- 병원별 병렬 비동기 스크래핑
- 지역별 필터링
- EUC-KR 등 한국어 인코딩 자동 감지
- Docker로 간편하게 배포

## 지원 지역

| 지역 | 병원 수 |
|------|---------|
| 서울특별시 | 13개 |
| 경기남부 | 4개 |
| 경기북부/인천 | 5개 |
| 부산/울산/경남 | 7개 |
| 대구/경북 | 4개 |
| 충청권 | 5개 |
| 전라권 | 3개 |
| 강원 | 1개 |

## 빠른 시작

### Docker Compose (권장)

```bash
docker compose up -d
```

서버가 `http://localhost:8000` 에서 실행됩니다.

### 로컬 실행

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## API 엔드포인트

### `GET /hospitals` — 병원 목록 조회

CSV에 등록된 병원 목록을 반환합니다. 스크래핑 없이 즉시 응답합니다.

| 파라미터 | 타입 | 설명 |
|----------|------|------|
| `region` | string (선택) | 지역 필터 (예: `서울특별시`, `경기남부`) |

**예시**

```bash
curl http://localhost:8000/hospitals?region=서울특별시
```

---

### `GET /bids` — 전체 병원 입찰 정보 수집

등록된 모든 병원의 입찰 공고를 병렬로 수집합니다.

| 파라미터 | 타입 | 설명 |
|----------|------|------|
| `region` | string (선택) | 지역 필터 |

**예시**

```bash
curl http://localhost:8000/bids
curl http://localhost:8000/bids?region=부산%2F울산%2F경남
```

---

### `GET /bids/{hospital_name}` — 특정 병원 입찰 정보 수집

병원명으로 단일 병원의 입찰 공고를 수집합니다. 병원명은 CSV의 `병원명` 컬럼과 정확히 일치해야 합니다.

**예시**

```bash
curl http://localhost:8000/bids/서울대학교병원
```

---

### API 문서

서버 실행 후 브라우저에서 확인할 수 있습니다.

- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

## 응답 형식

```json
[
  {
    "hospital": "서울대학교병원",
    "region": "서울특별시",
    "source_url": "https://www.snuh.org/board/B004/list.do",
    "bids": [
      {
        "title": "2024년 의료장비 구매 입찰 공고",
        "date": "2024.03.06",
        "url": "https://www.snuh.org/board/B004/view.do?id=12345"
      }
    ],
    "error": null
  }
]
```

스크래핑 실패 시 `bids`는 빈 배열이 되고 `error` 필드에 사유가 표시됩니다.

## 병원 목록 관리

병원 정보는 [data/hospitals.csv](data/hospitals.csv) 파일로 관리합니다.

| 컬럼 | 설명 |
|------|------|
| `병원명` | 병원 이름 (API 경로명으로 사용) |
| `URL` | 입찰 게시판 URL |
| `지역` | 지역 분류 |
| `table_tag` | 게시판 테이블의 CSS 클래스명 |
| `div_tag` | 게시판 컨테이너의 CSS 클래스명 또는 API 패턴 식별자 |
| `타입 설명` | 스크래핑 방식 메모 |

`table_tag` 또는 `div_tag` 중 하나 이상을 지정해야 스크래핑이 동작합니다.

### 스크래핑 패턴

| 패턴 | 조건 | 방식 | 해당 병원 |
|------|------|------|-----------|
| **A** | `table_tag` 설정 | HTML에서 `<table class="...">` 직접 파싱 | 중앙대학교병원, 서울아산병원, 분당서울대병원, 아주대병원, 인하대병원, 일산백병원, 순천향대부천병원, 동아대병원, 울산대병원, 경상국립대병원, 계명대동산병원, 영남대병원, 충남대병원, 건양대병원, 충북대병원, 순천향대천안병원, 전남대병원, 화순전남대병원 |
| **B** | `div_tag` 설정 (일반) | HTML에서 컨테이너 탐색 후 내부 `<table>` · `<li>` · `<a>` 파싱 | 강북삼성병원, 건국대학교병원, 서울대학교병원, 연세대세브란스병원(총무/구매), 이화여대부속목동병원, 단국대병원, 가천대길병원, 부산대병원, 양산부산대병원, 인제대부산백병원, 고신대복음병원, 경북대병원, 칠곡경북대병원, 조선대병원, 연세대원주세브란스기독병원 |
| **C** | `div_tag = cmc_api:{boardNo}` | Vue.js SPA — JSON API(`/api/article/{boardNo}`) 직접 호출 | 서울성모병원, 부천성모병원 |
| **D** | `div_tag = khmc_api:{boardNo}` | JS 렌더링 SPA — JSON API(`/api/article/{boardNo}.do`) 직접 호출 | 경희의료원 |
| **E** | `div_tag = kumc_api:{boardId}` | Vue.js SPA — boardNo 조회 후 JSON API(`/api/article/{boardNo}`) 직접 호출 | 고려대학교안암병원, 고려대구로병원, 고려대안산병원 |
| **-** | 선택자 없음 | 오류 반환 | 한양대학교병원 |

## 기술 스택

- **FastAPI** — REST API 프레임워크
- **httpx** — 비동기 HTTP 클라이언트
- **BeautifulSoup4 + lxml** — HTML 파싱
- **chardet** — 인코딩 자동 감지
- **Pydantic v2** — 데이터 모델 및 유효성 검사
- **Docker** — 컨테이너 배포

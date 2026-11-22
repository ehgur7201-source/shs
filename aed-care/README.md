# AED Care

단일 HTML 프런트엔드와 Python 표준 라이브러리 기반 SafeMap API 프록시로 구성됩니다.

## 실행

```powershell
python server.py
```

브라우저에서 `http://localhost:5500`을 엽니다.

## SafeMap 설정

1. `.env.example`을 참고해 `.env`에 `SAFEMAP_SERVICE_KEY`를 설정합니다.
2. 키는 `index.html`에 넣지 않습니다.
3. 공공 AED는 브라우저에 저장하지 않고 Supabase DB에서만 조회합니다. 관리자 모드 → 데이터 연동에서 DB 연결 상태와 시도별 건수를 확인합니다.

실제 API 엔드포인트는 `https://safemap.go.kr/openapi2/IF_0004`입니다. `www`가 붙은 호스트에서는 같은 키가 거부될 수 있습니다.

## 공공 AED 데이터베이스 (Supabase)

시민 지도는 Supabase `aed_locations`(SafeMap 62,000건)를 서버를 거쳐 조회합니다. DB 키(`SUPABASE_SERVICE_ROLE_KEY`)는 `server.py`에서만 쓰고 브라우저로 보내지 않습니다.

- `GET /api/aeds?bbox=minLng,minLat,maxLng,maxLat&lat=&lng=&limit=800`: 지도 영역 안의 AED (최대 2000건, `lat`/`lng` 기준 가까운 순)
- `GET /api/aeds/nearest?lat=&lng=&limit=5`: 1km → 30km로 넓혀 가며 가장 가까운 AED
- `GET /api/aeds/points`: 전국 AED 좌표 전체(62,000건, gzip 약 460KB)와 시도별 건수. 서버 시작 시 미리 받아 6시간 캐시(`.cache/aed_points.json`)
- 응답에는 이름·주소·좌표·제조사·모델·거리만 포함되며 담당자 연락처는 포함하지 않습니다.
- 지도는 처음부터 전국을 보여 줍니다. 확대 수준 14 미만에서는 숫자 묶음, 14 이상에서는 개별 위치와 상세 정보(최대 2,000건)를 표시합니다.
- 동기화로 데이터를 바꾼 뒤 바로 반영하려면 `.cache/aed_points.json`을 지우고 서버를 다시 시작하세요.

데이터 갱신: `python scripts\sync_safemap_to_supabase.py --page-size 500` (같은 기기는 갱신, 중복 없음)
점검: `python scripts\check_supabase_table.py`, `python scripts\verify_supabase_data.py`

## 보안 범위

현재 관리자 로그인과 데이터 저장은 시연용입니다. 실제 배포 전 서버 인증, 기관별 권한, 데이터베이스, 비밀관리, 백업을 추가해야 합니다.

## 지도 실행 방식

- Leaflet 1.9.4를 `vendor/leaflet`에서 로컬 제공하므로 CDN 차단의 영향을 받지 않습니다.
- 배경 타일은 OpenStreetMap, OpenStreetMap DE, CARTO 순서로 자동 전환됩니다.
- 모든 외부 타일이 차단돼도 등록된 위도·경도로 좌표 기반 오프라인 지도를 표시합니다.
- 지도 데이터 및 타일 제공자의 이용조건과 저작자 표시를 유지해야 합니다.

## Third-party license

Leaflet 1.9.4 is included under the BSD 2-Clause License. See `vendor/leaflet/LICENSE`.

## Supabase PostgreSQL에 전국 AED 62,000건 적재

Supabase를 선택한 이유는 무료 PostgreSQL, PostGIS 공간 인덱스, 웹 SQL 편집기와 REST API를 함께 제공하기 때문입니다. 현재 SafeMap 500건 기준 원본 JSON 용량으로 추정한 62,000건 전체 원본은 약 30.6MB입니다.

1. `https://supabase.com/dashboard/new`에서 무료 프로젝트를 생성합니다.
2. Dashboard의 SQL Editor에서 `sql/001_supabase_schema.sql` 전체를 실행합니다.
3. Project Settings > API에서 Project URL과 서버 전용 service-role 키를 확인합니다.
4. `.env`의 아래 값을 채웁니다. service-role 키는 브라우저나 `index.html`에 넣지 않습니다.

```text
SUPABASE_URL=https://프로젝트_ID.supabase.co
SUPABASE_SERVICE_ROLE_KEY=서버_전용_키
```

5. 먼저 1페이지만 시험 적재합니다.

```powershell
python scripts/sync_safemap_to_supabase.py --limit-pages 1 --page-size 500
```

6. Supabase Table Editor에서 `aed_locations` 500건을 확인한 뒤 전체를 적재합니다.

```powershell
python scripts/sync_safemap_to_supabase.py --page-size 500
```

전체 62,000건은 SafeMap 500건 단위로 약 124페이지를 조회해 `(source, source_record_id)` 기준으로 업서트합니다. 재실행해도 같은 `objt_id`는 중복 삽입하지 않고 갱신합니다.

시민 지도 공개 조회는 SQL의 `aeds_in_bbox` RPC를 사용하도록 설계했습니다. 담당자 연락처, 원본 payload, 내부 점검 정보는 공개 RPC에 포함되지 않습니다.

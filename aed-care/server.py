"""AED Care local server and SafeMap API proxy.

The SafeMap key is loaded from the SAFEMAP_SERVICE_KEY environment variable
or the local .env file. It is never sent to the browser.
"""

from __future__ import annotations

import gzip
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
UPSTREAM = "https://safemap.go.kr/openapi2/IF_0004"
CACHE_TTL_SECONDS = 300
MAX_ROWS = 500
_cache: dict[tuple[int, int], tuple[float, bytes]] = {}

# Supabase(DB) 조회 설정: service_role 키는 서버에서만 사용하고 브라우저로 보내지 않습니다.
DB_CACHE_TTL_SECONDS = 60
POSTGREST_MAX_ROWS = 1000  # Supabase 기본 응답 최대 행 수
DB_MAX_LIMIT = 2000
DB_DEFAULT_LIMIT = 800
KOREA_LNG = (124.0, 132.0)
KOREA_LAT = (33.0, 39.0)
_db_cache: dict[tuple, tuple[float, list[dict[str, Any]]]] = {}
DB_PUBLIC_FIELDS = (
    "id", "name", "address", "detail_address", "sido", "gugun",
    "latitude", "longitude", "source_maker", "source_model", "distance_m",
)


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv(ROOT / ".env")


def safe_int(value: str | None, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value or default)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def parse_float(value: str | None, minimum: float, maximum: float) -> float | None:
    try:
        parsed = float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
    if parsed is None or parsed != parsed or not (minimum <= parsed <= maximum):
        return None
    return parsed


def supabase_config() -> tuple[str, str] | None:
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url.startswith("https://") or not key:
        return None
    return url, key


class DBError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def supabase_bbox(
    min_lng: float, min_lat: float, max_lng: float, max_lat: float,
    limit: int, user_lng: float | None, user_lat: float | None,
) -> list[dict[str, Any]]:
    """Supabase RPC aeds_in_bbox 호출. 공개 필드만 돌려줍니다."""
    config = supabase_config()
    if not config:
        raise DBError(HTTPStatus.SERVICE_UNAVAILABLE, "DB_NOT_CONFIGURED", "데이터베이스가 서버에 설정되지 않았습니다.")
    cache_key = (
        round(min_lng, 4), round(min_lat, 4), round(max_lng, 4), round(max_lat, 4), limit,
        None if user_lng is None else round(user_lng, 4), None if user_lat is None else round(user_lat, 4),
    )
    cached = _db_cache.get(cache_key)
    if cached and time.time() - cached[0] < DB_CACHE_TTL_SECONDS:
        return cached[1]
    url, key = config
    body = json.dumps(
        {
            "p_min_lng": min_lng, "p_min_lat": min_lat, "p_max_lng": max_lng, "p_max_lat": max_lat,
            "p_limit": limit, "p_user_lng": user_lng, "p_user_lat": user_lat,
        }
    ).encode("utf-8")
    rows: list[Any] = []
    # Supabase는 한 응답에 최대 1000행만 돌려주므로 offset으로 나눠 받습니다.
    for offset in range(0, limit, POSTGREST_MAX_ROWS):
        request = urllib.request.Request(
            f"{url}/rest/v1/rpc/aeds_in_bbox?offset={offset}&limit={min(POSTGREST_MAX_ROWS, limit - offset)}",
            data=body,
            method="POST",
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "AEDCare/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                chunk = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise DBError(HTTPStatus.BAD_GATEWAY, "DB_QUERY_FAILED", f"데이터베이스 조회 실패 (HTTP {exc.code})") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise DBError(HTTPStatus.BAD_GATEWAY, "DB_UNREACHABLE", "데이터베이스에 연결할 수 없습니다.") from exc
        if not isinstance(chunk, list):
            raise DBError(HTTPStatus.BAD_GATEWAY, "DB_BAD_RESPONSE", "데이터베이스 응답 형식이 올바르지 않습니다.")
        rows.extend(chunk)
        if len(chunk) < POSTGREST_MAX_ROWS:
            break
    cleaned = [{k: row.get(k) for k in DB_PUBLIC_FIELDS} for row in rows if isinstance(row, dict)]
    if len(_db_cache) > 500:
        _db_cache.clear()
    _db_cache[cache_key] = (time.time(), cleaned)
    return cleaned


# ---------------------------------------------------------------------------
# 전국 AED 좌표 묶음: 62,000건 좌표를 한 번에 받아 메모리·디스크에 캐시합니다.
# 브라우저는 이 좌표로 전국 지도를 그리고(묶음 표시), 확대하면 /api/aeds 로 상세를 봅니다.
# ---------------------------------------------------------------------------
POINTS_TTL_SECONDS = 6 * 3600
POINTS_PAGE = 1000  # Supabase(PostgREST) 기본 최대 응답 행 수
POINTS_CACHE_FILE = ROOT / ".cache" / "aed_points.json"
_points_lock = threading.Lock()
_points: dict[str, Any] = {"at": 0.0, "json": b"", "gzip": b"", "count": 0}


def supabase_get(path: str, extra_headers: dict[str, str] | None = None) -> tuple[Any, dict[str, str]]:
    config = supabase_config()
    if not config:
        raise DBError(HTTPStatus.SERVICE_UNAVAILABLE, "DB_NOT_CONFIGURED", "데이터베이스가 서버에 설정되지 않았습니다.")
    url, key = config
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json", "User-Agent": "AEDCare/1.0"}
    headers.update(extra_headers or {})
    request = urllib.request.Request(f"{url}/rest/v1/{path}", headers=headers)
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8")), dict(response.headers)
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:
                raise DBError(HTTPStatus.BAD_GATEWAY, "DB_QUERY_FAILED", f"데이터베이스 조회 실패 (HTTP {exc.code})") from exc
            last_exc = exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_exc = exc
        time.sleep(1 + attempt)
    raise DBError(HTTPStatus.BAD_GATEWAY, "DB_UNREACHABLE", "데이터베이스에 연결할 수 없습니다.") from last_exc


def build_points_payload() -> bytes:
    """전국 활성 AED 좌표를 압축 배열로 만듭니다. [위도*1e5, 경도*1e5, 시도번호, ...]"""
    _, headers = supabase_get(
        "aed_locations?select=id&is_active=eq.true&limit=1", {"Prefer": "count=exact"}
    )
    total = int(str(headers.get("Content-Range", "*/0")).split("/")[-1] or 0)
    offsets = list(range(0, total, POINTS_PAGE))

    def fetch(offset: int) -> list[dict[str, Any]]:
        rows, _ = supabase_get(
            f"aed_locations?select=latitude,longitude,sido&is_active=eq.true&order=id&offset={offset}&limit={POINTS_PAGE}"
        )
        return rows if isinstance(rows, list) else []

    with ThreadPoolExecutor(max_workers=8) as pool:
        pages = list(pool.map(fetch, offsets))
    sido_names: list[str] = []
    sido_index: dict[str, int] = {}
    sido_stats: dict[int, list[float]] = {}
    flat: list[int] = []
    for rows in pages:
        for row in rows:
            try:
                lat = float(row["latitude"])
                lng = float(row["longitude"])
            except (KeyError, TypeError, ValueError):
                continue
            name = str(row.get("sido") or "미상").strip() or "미상"
            if name not in sido_index:
                sido_index[name] = len(sido_names)
                sido_names.append(name)
            idx = sido_index[name]
            flat.extend((round(lat * 1e5), round(lng * 1e5), idx))
            stat = sido_stats.setdefault(idx, [0, 0.0, 0.0])
            stat[0] += 1
            stat[1] += lat
            stat[2] += lng
    sido = [
        {"name": sido_names[i], "count": int(s[0]), "lat": round(s[1] / s[0], 5), "lng": round(s[2] / s[0], 5)}
        for i, s in sorted(sido_stats.items(), key=lambda kv: -kv[1][0])
    ]
    payload = {
        "count": len(flat) // 3,
        "expected": total,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "scale": 100000,
        "sidoNames": sido_names,
        "sido": sido,
        "points": flat,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def get_points(force: bool = False) -> dict[str, Any]:
    with _points_lock:
        now = time.time()
        if not force and _points["json"] and now - _points["at"] < POINTS_TTL_SECONDS:
            return _points
        if not force and not _points["json"] and POINTS_CACHE_FILE.exists():
            age = now - POINTS_CACHE_FILE.stat().st_mtime
            if age < POINTS_TTL_SECONDS:
                body = POINTS_CACHE_FILE.read_bytes()
                _points.update(at=now - age, json=body, gzip=gzip.compress(body, 6), count=json.loads(body).get("count", 0))
                return _points
        body = build_points_payload()
        _points.update(at=now, json=body, gzip=gzip.compress(body, 6), count=json.loads(body).get("count", 0))
        try:
            POINTS_CACHE_FILE.parent.mkdir(exist_ok=True)
            POINTS_CACHE_FILE.write_bytes(body)
        except OSError:
            pass
        return _points


def prewarm_points() -> None:
    if not supabase_config():
        return
    try:
        info = get_points()
        print(f"Supabase DB: 전국 AED 좌표 {info['count']}건 준비 완료")
    except DBError as exc:
        print(f"Supabase DB: 전국 좌표 준비 실패 ({exc.message})")


class AEDCareHandler(SimpleHTTPRequestHandler):
    server_version = "AEDCare/1.0"

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        # 로컬 개발·IDE webview 미리보기를 허용합니다. 실제 배포에서는
        # 허용할 도메인을 정한 CSP frame-ancestors 정책을 적용하세요.
        super().end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        # Browser requests never include the SafeMap key. Keep logs concise.
        super().log_message(format, *args)

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def is_private_path(path: str) -> bool:
        decoded = urllib.parse.unquote(path)
        return any(part.startswith(".") for part in decoded.split("/") if part)

    def do_HEAD(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if self.is_private_path(parsed.path):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        super().do_HEAD()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if self.is_private_path(parsed.path):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if parsed.path == "/api/health":
            self.send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "safemapConfigured": bool(os.environ.get("SAFEMAP_SERVICE_KEY", "").strip()),
                    "dbConfigured": supabase_config() is not None,
                    "maxRows": MAX_ROWS,
                },
            )
            return
        if parsed.path == "/api/safemap/aed":
            self.handle_safemap(parsed.query)
            return
        if parsed.path == "/api/aeds":
            self.handle_aeds_bbox(parsed.query)
            return
        if parsed.path == "/api/aeds/nearest":
            self.handle_aeds_nearest(parsed.query)
            return
        if parsed.path == "/api/aeds/points":
            self.handle_aeds_points()
            return
        super().do_GET()

    def handle_aeds_points(self) -> None:
        """GET /api/aeds/points — 전국 AED 좌표 묶음 (gzip 지원)."""
        try:
            info = get_points()
        except DBError as exc:
            self.send_json(exc.status, {"error": exc.code, "message": exc.message})
            return
        use_gzip = "gzip" in (self.headers.get("Accept-Encoding") or "").lower()
        body = info["gzip"] if use_gzip else info["json"]
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, max-age=600")
        self.end_headers()
        self.wfile.write(body)

    def handle_aeds_bbox(self, query: str) -> None:
        """GET /api/aeds?bbox=minLng,minLat,maxLng,maxLat[&limit=&lat=&lng=]"""
        params = urllib.parse.parse_qs(query)
        parts = ((params.get("bbox") or [""])[0]).split(",")
        if len(parts) != 4:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "BAD_BBOX", "message": "bbox=minLng,minLat,maxLng,maxLat 형식이 필요합니다."})
            return
        min_lng = parse_float(parts[0], -180, 180)
        min_lat = parse_float(parts[1], -90, 90)
        max_lng = parse_float(parts[2], -180, 180)
        max_lat = parse_float(parts[3], -90, 90)
        if None in (min_lng, min_lat, max_lng, max_lat):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "BAD_BBOX", "message": "bbox 좌표가 올바르지 않습니다."})
            return
        # 국내 범위로 잘라 RPC 조건(124~132, 33~39)을 맞춥니다.
        min_lng, max_lng = max(min_lng, KOREA_LNG[0]), min(max_lng, KOREA_LNG[1])
        min_lat, max_lat = max(min_lat, KOREA_LAT[0]), min(max_lat, KOREA_LAT[1])
        if min_lng >= max_lng or min_lat >= max_lat:
            self.send_json(HTTPStatus.OK, {"items": [], "count": 0, "limit": 0, "truncated": False})
            return
        limit = safe_int((params.get("limit") or [None])[0], DB_DEFAULT_LIMIT, 1, DB_MAX_LIMIT)
        user_lat = parse_float((params.get("lat") or [None])[0], *KOREA_LAT)
        user_lng = parse_float((params.get("lng") or [None])[0], *KOREA_LNG)
        if user_lat is None or user_lng is None:
            user_lat = user_lng = None
        try:
            rows = supabase_bbox(min_lng, min_lat, max_lng, max_lat, limit, user_lng, user_lat)
        except DBError as exc:
            self.send_json(exc.status, {"error": exc.code, "message": exc.message})
            return
        self.send_json(HTTPStatus.OK, {"items": rows, "count": len(rows), "limit": limit, "truncated": len(rows) >= limit})

    def handle_aeds_nearest(self, query: str) -> None:
        """GET /api/aeds/nearest?lat=&lng=[&limit=5] — 반경을 넓혀 가며 가까운 AED를 찾습니다."""
        params = urllib.parse.parse_qs(query)
        lat = parse_float((params.get("lat") or [None])[0], *KOREA_LAT)
        lng = parse_float((params.get("lng") or [None])[0], *KOREA_LNG)
        if lat is None or lng is None:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "BAD_LOCATION", "message": "국내 위도(lat)·경도(lng)가 필요합니다."})
            return
        limit = safe_int((params.get("limit") or [None])[0], 5, 1, 50)
        rows: list[dict[str, Any]] = []
        try:
            for span in (0.01, 0.03, 0.1, 0.3):  # 약 1km → 30km
                rows = supabase_bbox(
                    max(lng - span, KOREA_LNG[0]), max(lat - span, KOREA_LAT[0]),
                    min(lng + span, KOREA_LNG[1]), min(lat + span, KOREA_LAT[1]),
                    limit, lng, lat,
                )
                if rows:
                    break
        except DBError as exc:
            self.send_json(exc.status, {"error": exc.code, "message": exc.message})
            return
        self.send_json(HTTPStatus.OK, {"items": rows, "count": len(rows)})

    def handle_safemap(self, query: str) -> None:
        service_key = os.environ.get("SAFEMAP_SERVICE_KEY", "").strip()
        if not service_key:
            self.send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "SAFEMAP_NOT_CONFIGURED", "message": "SafeMap API 키가 서버에 설정되지 않았습니다."},
            )
            return

        params = urllib.parse.parse_qs(query, keep_blank_values=False)
        page_no = safe_int((params.get("pageNo") or [None])[0], 1, 1, 100000)
        num_rows = safe_int((params.get("numOfRows") or [None])[0], 100, 1, MAX_ROWS)
        cache_key = (page_no, num_rows)
        cached = _cache.get(cache_key)
        if cached and time.time() - cached[0] < CACHE_TTL_SECONDS:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(cached[1])))
            self.send_header("Cache-Control", "private, max-age=60")
            self.send_header("X-AEDCare-Cache", "HIT")
            self.end_headers()
            self.wfile.write(cached[1])
            return

        upstream_query = urllib.parse.urlencode(
            {
                "serviceKey": service_key,
                "pageNo": page_no,
                "numOfRows": num_rows,
                "returnType": "json",
            }
        )
        request = urllib.request.Request(
            f"{UPSTREAM}?{upstream_query}",
            headers={"Accept": "application/json", "User-Agent": "AEDCare/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
            payload = json.loads(raw.decode("utf-8"))
            header = payload.get("header") or payload.get("response", {}).get("header") or {}
            if str(header.get("resultCode", "")) != "00":
                self.send_json(
                    HTTPStatus.BAD_GATEWAY,
                    {
                        "error": "SAFEMAP_UPSTREAM_ERROR",
                        "resultCode": header.get("resultCode"),
                        "message": header.get("errorMsg") or header.get("resultMsg") or "SafeMap 조회에 실패했습니다.",
                    },
                )
                return
            normalized = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            _cache[cache_key] = (time.time(), normalized)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(normalized)))
            self.send_header("Cache-Control", "private, max-age=60")
            self.send_header("X-AEDCare-Cache", "MISS")
            self.end_headers()
            self.wfile.write(normalized)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            self.send_json(
                HTTPStatus.BAD_GATEWAY,
                {"error": "SAFEMAP_UNREACHABLE", "message": "SafeMap 서버 응답을 확인할 수 없습니다.", "detail": type(exc).__name__},
            )


def main() -> None:
    host = os.environ.get("AEDCARE_HOST", "127.0.0.1")
    port = safe_int(os.environ.get("AEDCARE_PORT"), 5500, 1, 65535)
    handler = partial(AEDCareHandler, directory=str(ROOT))
    server = ThreadingHTTPServer((host, port), handler)
    print(f"AED Care running at http://{host}:{port}")
    print("SafeMap proxy: configured" if os.environ.get("SAFEMAP_SERVICE_KEY") else "SafeMap proxy: not configured")
    print("Supabase DB: configured" if supabase_config() else "Supabase DB: not configured")
    threading.Thread(target=prewarm_points, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

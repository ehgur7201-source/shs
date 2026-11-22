"""Supabase aed_locations 적재 품질 점검 (키는 출력하지 않음)."""
import json
import sys
import urllib.error
import urllib.request

sys.path.insert(0, __import__("os").path.dirname(__file__))
from check_supabase_table import load_env  # noqa: E402

env = load_env()
BASE = env["SUPABASE_URL"].rstrip("/") + "/rest/v1/"
KEY = env["SUPABASE_SERVICE_ROLE_KEY"]


def count(query):
    req = urllib.request.Request(
        BASE + "aed_locations?select=id&limit=1" + query,
        headers={"apikey": KEY, "Authorization": "Bearer " + KEY, "Prefer": "count=exact"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.headers.get("Content-Range", "").split("/")[-1]


def rpc(name, body):
    req = urllib.request.Request(
        BASE + "rpc/" + name,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"apikey": KEY, "Authorization": "Bearer " + KEY, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


print("전체 행:", count(""))
print("이름 빈값:", count("&name=eq."))
print("주소 빈값:", count("&address=eq."))
print("부산 행:", count("&sido=like.*%EB%B6%80%EC%82%B0*"))
print("시도 목록 샘플 수(서울):", count("&sido=like.*%EC%84%9C%EC%9A%B8*"))
try:
    # 부산시청(129.0756, 35.1798) 주변 bbox
    rows = rpc("aeds_in_bbox", {
        "p_min_lng": 129.06, "p_min_lat": 35.17, "p_max_lng": 129.09, "p_max_lat": 35.19,
        "p_limit": 1000, "p_user_lng": 129.0756, "p_user_lat": 35.1798,
    })
    print("aeds_in_bbox(부산시청 주변) 결과:", len(rows))
    if rows:
        r = min(rows, key=lambda x: x.get("distance_m") or 1e12)
        print("  가장 가까운 곳:", {k: r.get(k) for k in ("name", "address", "distance_m", "source_model")})
except urllib.error.HTTPError as e:
    print("RPC 오류:", e.code, e.read()[:200].decode("utf-8", "replace"))

"""Import all SafeMap AED rows into Supabase using PostgREST.

Required .env values:
  SAFEMAP_SERVICE_KEY
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY

The service-role key stays on the server and must never be exposed to a browser.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SAFEMAP_URL = "https://safemap.go.kr/openapi2/IF_0004"


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def request_json(url: str, *, method: str = "GET", headers: dict[str, str] | None = None,
                 payload: Any = None, attempts: int = 4) -> tuple[Any, dict[str, str]]:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_headers = {"Accept": "application/json", "User-Agent": "AEDCare-Sync/1.0"}
    request_headers.update(headers or {})
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, data=body, headers=request_headers, method=method)
            with urllib.request.urlopen(req, timeout=60) as response:
                raw = response.read().decode("utf-8")
                return (json.loads(raw) if raw else None), dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            # 4xx(인증·테이블 없음 등)는 재시도해도 같으므로 즉시 중단한다.
            if 400 <= exc.code < 500:
                raise RuntimeError(f"HTTP {exc.code}: {detail}") from None
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(2 ** attempt)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Request failed after {attempts} attempts: {type(last_error).__name__}")


def safemap_page(service_key: str, page_no: int, page_size: int) -> tuple[list[dict[str, Any]], int]:
    query = urllib.parse.urlencode({
        "serviceKey": service_key,
        "pageNo": page_no,
        "numOfRows": page_size,
        "returnType": "json",
    })
    payload, _ = request_json(f"{SAFEMAP_URL}?{query}")
    header = payload.get("header", {})
    if str(header.get("resultCode", "")) != "00":
        raise RuntimeError(header.get("errorMsg") or header.get("resultMsg") or "SafeMap error")
    body = payload.get("body", {})
    item = body.get("items", {}).get("item", [])
    rows = item if isinstance(item, list) else ([item] if item else [])
    return rows, int(body.get("totalCount") or 0)


def text(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    return "" if value is None else str(value).strip()


def source_id(row: dict[str, Any]) -> str:
    original = text(row, "objt_id")
    if original:
        return original
    fingerprint = "|".join([
        text(row, "org"), text(row, "addres"), text(row, "buildaddress"),
        text(row, "buildplace"), text(row, "lat"), text(row, "lon"),
    ]).lower()
    return "fingerprint:" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()


def map_row(row: dict[str, Any]) -> dict[str, Any] | None:
    try:
        latitude = float(row.get("lat") or row.get("y"))
        longitude = float(row.get("lon") or row.get("x"))
    except (TypeError, ValueError):
        return None
    if not (33 <= latitude <= 39 and 124 <= longitude <= 132):
        return None
    return {
        "source": "safemap",
        "source_record_id": source_id(row),
        "name": text(row, "org") or text(row, "buildplace") or "AED 설치처",
        "address": text(row, "addres"),
        "detail_address": text(row, "buildaddress") or text(row, "buildplace"),
        "sido": text(row, "sido"),
        "gugun": text(row, "gugun"),
        "postal_code_1": text(row, "zipcode1"),
        "postal_code_2": text(row, "zipcode2"),
        "latitude": latitude,
        "longitude": longitude,
        "source_maker": text(row, "mfg"),
        "source_model": text(row, "model"),
        "source_manager": text(row, "manager"),
        "source_manager_tel": text(row, "managertel"),
        "raw_payload": row,
        "is_active": True,
        "last_seen_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def supabase_upsert(base_url: str, service_role_key: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    endpoint = base_url.rstrip("/") + "/rest/v1/aed_locations?on_conflict=source,source_record_id"
    headers = {
        "apikey": service_role_key,
        "Authorization": f"Bearer {service_role_key}",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    request_json(endpoint, method="POST", headers=headers, payload=rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync SafeMap AED data to Supabase")
    parser.add_argument("--page-size", type=int, default=500, choices=[100, 200, 500])
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--limit-pages", type=int, default=0, help="0 means all pages")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    safemap_key = os.environ.get("SAFEMAP_SERVICE_KEY", "").strip()
    supabase_url = os.environ.get("SUPABASE_URL", "").strip()
    service_role_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not safemap_key:
        print("SAFEMAP_SERVICE_KEY is missing", file=sys.stderr)
        return 2
    if not args.dry_run and (not supabase_url or not service_role_key):
        print("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY is missing", file=sys.stderr)
        return 2

    first_rows, total = safemap_page(safemap_key, args.start_page, args.page_size)
    total_pages = math.ceil(total / args.page_size)
    end_page = total_pages if args.limit_pages <= 0 else min(total_pages, args.start_page + args.limit_pages - 1)
    processed = upserted = skipped = 0

    for page in range(args.start_page, end_page + 1):
        rows = first_rows if page == args.start_page else safemap_page(safemap_key, page, args.page_size)[0]
        mapped = [item for item in (map_row(row) for row in rows) if item]
        processed += len(rows)
        skipped += len(rows) - len(mapped)
        if not args.dry_run:
            supabase_upsert(supabase_url, service_role_key, mapped)
        upserted += len(mapped)
        print(f"page {page}/{total_pages}: received={len(rows)} upserted={len(mapped)} total_upserted={upserted}")
        if not rows:
            break

    mode = "DRY RUN" if args.dry_run else "SYNC COMPLETE"
    print(f"{mode}: expected={total} processed={processed} upserted={upserted} skipped={skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

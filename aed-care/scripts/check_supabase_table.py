"""Supabase aed_locations 테이블 존재 여부 및 행 수 확인 (키는 출력하지 않음)."""
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env():
    env = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def main():
    tries = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    env = load_env()
    url = env["SUPABASE_URL"].rstrip("/") + "/rest/v1/aed_locations?select=id&limit=1"
    key = env["SUPABASE_SERVICE_ROLE_KEY"]
    headers = {"apikey": key, "Authorization": "Bearer " + key, "Prefer": "count=exact"}
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=10) as r:
                print("OK", r.status, "Content-Range:", r.headers.get("Content-Range"))
                return 0
        except urllib.error.HTTPError as e:
            print(i, "HTTP", e.code, e.read()[:120].decode("utf-8", "replace"))
        except Exception as e:  # 네트워크 오류
            print(i, "ERR", e)
        if i < tries - 1:
            time.sleep(5)
    return 1


if __name__ == "__main__":
    sys.exit(main())

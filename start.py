from __future__ import annotations

import concurrent.futures
import json
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent
SHERLOCK_DATA = ROOT / "resources" / "sherlock" / "sherlock_project" / "resources" / "data.json"
THEHARVESTER_DIR = ROOT / "resources" / "theHarvester"
OUTPUT_DIR = ROOT / "output"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36"
TIMEOUT = 15


class Spinner:
    def __init__(self, message: str = "Working") -> None:
        self.message = message
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _run(self) -> None:
        frames = ["|", "/", "-", "\\"]
        i = 0
        while not self._stop.is_set():
            print(f"\r{self.message} {frames[i % len(frames)]}", end="", flush=True)
            i += 1
            time.sleep(0.12)
        print(f"\r{self.message} done.{' ' * 12}")

    def __enter__(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)



def load_site_data() -> dict[str, dict[str, Any]]:
    raw = json.loads(SHERLOCK_DATA.read_text(encoding="utf-8"))
    raw.pop("$schema", None)
    return raw


def interpolate(value: Any, username: str) -> Any:
    if isinstance(value, str):
        return value.replace("{}", username)
    if isinstance(value, dict):
        return {k: interpolate(v, username) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate(v, username) for v in value]
    return value


def valid_for_site(username: str, site_info: dict[str, Any]) -> bool:
    regex_check = site_info.get("regexCheck")
    if not regex_check:
        return True
    return re.search(regex_check, username) is not None


def build_headers(site_info: dict[str, Any]) -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT}
    extra = site_info.get("headers")
    if isinstance(extra, dict):
        headers.update(extra)
    return headers


def any_match(marker: Any, text: str) -> bool:
    if not marker:
        return False
    if isinstance(marker, str):
        return marker in text
    if isinstance(marker, list):
        return any(isinstance(item, str) and item in text for item in marker)
    return False


def evaluate_response(site_name: str, username: str, site_info: dict[str, Any], response: requests.Response | None, error: str | None) -> dict[str, Any]:
    profile_url = interpolate(site_info.get("url", ""), username)
    result = {
        "site": site_name,
        "url": profile_url,
        "exists": False,
        "http_status": response.status_code if response is not None else None,
        "error": error,
    }

    if error or response is None:
        return result

    error_type = site_info.get("errorType", "status_code")

    if error_type == "status_code":
        result["exists"] = response.status_code in {200, 201, 202, 203}
    elif error_type == "response_url":
        expected = profile_url.rstrip("/")
        actual = str(response.url).rstrip("/")
        result["exists"] = response.status_code in {200, 301, 302} and actual == expected
    elif error_type == "message":
        text = response.text or ""
        error_msg = site_info.get("errorMsg")
        error_msg2 = site_info.get("errorMsg2")
        if any_match(error_msg, text):
            result["exists"] = False
        elif any_match(error_msg2, text):
            result["exists"] = False
        else:
            result["exists"] = response.status_code == 200
    else:
        result["exists"] = response.status_code == 200

    return result


def probe_site(site_name: str, username: str, site_info: dict[str, Any]) -> dict[str, Any]:
    if not valid_for_site(username, site_info):
        return {
            "site": site_name,
            "url": "",
            "exists": False,
            "http_status": None,
            "error": "illegal_username_for_site",
        }

    profile_url = interpolate(site_info.get("url", ""), username)
    probe_url = interpolate(site_info.get("urlProbe", site_info.get("url", "")), username)
    headers = build_headers(site_info)
    error_type = site_info.get("errorType", "status_code")
    method = site_info.get("request_method")

    if method is None:
        method = "HEAD" if error_type == "status_code" else "GET"

    allow_redirects = error_type != "response_url"

    try:
        if method == "HEAD":
            response = requests.head(probe_url, headers=headers, timeout=TIMEOUT, allow_redirects=allow_redirects)
        elif method == "POST":
            payload = interpolate(site_info.get("request_payload", {}), username)
            response = requests.post(probe_url, headers=headers, timeout=TIMEOUT, allow_redirects=allow_redirects, data=payload)
        else:
            response = requests.get(probe_url, headers=headers, timeout=TIMEOUT, allow_redirects=allow_redirects)
        return evaluate_response(site_name, username, site_info, response, None)
    except requests.RequestException as exc:
        return {
            "site": site_name,
            "url": profile_url,
            "exists": False,
            "http_status": None,
            "error": str(exc),
        }


def username_lookup(username: str, limit: int | None = None) -> list[dict[str, Any]]:
    data = load_site_data()
    items = list(data.items())
    if limit:
        items = items[:limit]

    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(20, max(1, len(items)))) as executor:
        futures = [executor.submit(probe_site, site_name, username, site_info) for site_name, site_info in items]
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())

    results.sort(key=lambda item: (not item["exists"], item["site"].lower()))
    return results


def save_report(username: str, results: list[dict[str, Any]]) -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / f"username-{username}.json"
    path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return path


def print_results(results: list[dict[str, Any]]) -> None:
    found = [r for r in results if r["exists"]]
    print(f"\nFound {len(found)} matching profiles.\n")
    for item in found:
        print(f"[FOUND] {item['site']}: {item['url']}")

    errors = [r for r in results if r.get("error") and r.get("error") != "illegal_username_for_site"]
    if errors:
        print(f"\n{len(errors)} sites returned request errors (not necessarily failures).")


def main() -> None:
    print("=" * 48)
    print("OSINTPUN :: Username")
    print("Manifest-driven public profile lookup")
    print("=" * 48)
    username = input("Username to search: ").strip()
    if not username:
        print("Username is required.")
        return

    subset = input("Limit sites for quick test? (blank = all, number = limit): ").strip()
    limit = int(subset) if subset.isdigit() else None

    print()
    with Spinner("Running username checks"):
        results = username_lookup(username, limit=limit)
    print_results(results)
    report_path = save_report(username, results)
    print(f"\nSaved report: {report_path}")


if __name__ == "__main__":
    main()

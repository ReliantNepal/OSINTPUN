from __future__ import annotations

import concurrent.futures
import json
import re
import shutil
import subprocess
import sys
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

    total = len(items)
    completed = 0
    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(20, max(1, len(items)))) as executor:
        futures = [executor.submit(probe_site, site_name, username, site_info) for site_name, site_info in items]
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
            completed += 1
            print(f"\rProgress: {completed}/{total} sites checked", end="", flush=True)
    print()
    results.sort(key=lambda item: (not item["exists"], item["site"].lower()))
    return results


def save_report(name: str, kind: str, results: Any) -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    suffix = "json" if isinstance(results, (dict, list)) else "txt"
    path = OUTPUT_DIR / f"{kind}-{name}.{suffix}"
    if suffix == "json":
        path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    else:
        path.write_text(str(results), encoding="utf-8")
    return path


def print_username_results(results: list[dict[str, Any]]) -> None:
    found = [r for r in results if r["exists"]]
    errors = [r for r in results if r.get("error") and r.get("error") != "illegal_username_for_site"]
    illegal = [r for r in results if r.get("error") == "illegal_username_for_site"]
    checked = len(results)

    print("\nSummary:")
    print(f"  Checked : {checked}")
    print(f"  Found   : {len(found)}")
    print(f"  Errors  : {len(errors)}")
    print(f"  Skipped : {len(illegal)}")

    if found:
        print("\nFound profiles:\n")
        for item in found:
            print(f"[FOUND] {item['site']}: {item['url']}")
    else:
        print("\nNo matching public profiles found.")

    if errors:
        print(f"\n{len(errors)} sites returned request errors (not necessarily failures).")


def run_username_module() -> None:
    print("=" * 48)
    print("OSINTPUN :: Username")
    print("Manifest-driven public profile lookup")
    print("=" * 48)
    if not SHERLOCK_DATA.exists():
        print(f"Missing Sherlock data file: {SHERLOCK_DATA}")
        print("Please re-download the repo or ensure resources/sherlock is present.")
        return

    username = input("Username to search: ").strip()
    if not username:
        print("Username is required.")
        return

    subset = input("Limit sites for quick test? (blank/all = all, number = limit): ").strip().lower()
    limit = int(subset) if subset.isdigit() else None

    print()
    with Spinner("Running username checks"):
        results = username_lookup(username, limit=limit)
    print_username_results(results)
    report_path = save_report(username, "username", results)
    print(f"\nSaved report: {report_path}")


def run_email_module() -> None:
    print("=" * 48)
    print("OSINTPUN :: Email")
    print("theHarvester wrapper")
    print("=" * 48)
    if not THEHARVESTER_DIR.exists():
        print(f"Missing theHarvester directory: {THEHARVESTER_DIR}")
        return

    target = input("Target domain or email (e.g. example.com or user@example.com): ").strip()
    if not target:
        print("Domain or email is required.")
        return

    domain = target.split("@", 1)[1] if "@" in target else target
    domain = domain.strip()
    if not domain:
        print("Could not determine a valid domain.")
        return

    source = input("Source engine (blank = bing): ").strip() or "bing"
    limit_raw = input("Result limit (blank = 100): ").strip() or "100"
    limit = limit_raw if limit_raw.isdigit() else "100"

    python_cmd = sys.executable or shutil.which("python") or shutil.which("py") or "python"
    cmd = [
        python_cmd,
        "-m",
        "theHarvester.theHarvester",
        "-d", domain,
        "-b", source,
        "-l", limit,
    ]

    print()
    with Spinner("Running theHarvester"):
        result = subprocess.run(cmd, cwd=str(THEHARVESTER_DIR), capture_output=True, text=True)

    stdout = result.stdout or ""
    stderr = result.stderr or ""
    print(stdout)

    emails_found = 0
    for line in stdout.splitlines():
        lower = line.lower()
        if "@" in line and not lower.startswith("[*]"):
            emails_found += 1

    print("\nSummary:")
    print(f"  Target      : {domain}")
    print(f"  Return code : {result.returncode}")
    print(f"  Emails seen : {emails_found}")

    if result.returncode != 0:
        print("theHarvester returned a non-zero exit code.")
        if "ModuleNotFoundError:" in stderr:
            missing = None
            for line in stderr.splitlines():
                if "ModuleNotFoundError:" in line and "No module named" in line:
                    missing = line.split("No module named ", 1)[1].strip().replace("'", "").replace(chr(34), "")
                    break
            if missing:
                print(f"Missing dependency detected: {missing}")
                print("Install theHarvester dependencies with:")
                print(r"  python -m pip install .\resources\theHarvester")
            print(stderr)
        elif stderr:
            print(stderr)

    report_path = save_report(domain, "email", stdout + "\n\nSTDERR:\n" + stderr)
    print(f"  Output file : {report_path.name}")
    print(f"\nSaved report: {report_path}")


def main() -> None:
    print("=" * 48)
    print("OSINTPUN")
    print("Single-file OSINT starter")
    print("=" * 48)
    print("1. Username")
    print("2. Email / Domain")
    choice = input("Select option (1/2): ").strip()
    if choice == "1":
        run_username_module()
        return
    if choice == "2":
        run_email_module()
        return
    print("Invalid choice. Use 1 or 2.")


if __name__ == "__main__":
    main()

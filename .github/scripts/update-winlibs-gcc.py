#!/usr/bin/env python3
"""Update the winlibs-gcc manifests from the upstream winlibs_mingw releases.

Upstream publishes one release per (gcc, mingw-w64, crt, revision) combination,
tagged like `16.2.0posix-14.0.0-ucrt-r1`, with assets named like
`winlibs-x86_64-posix-seh-gcc-16.2.0-mingw-w64ucrt-14.0.0-r1.7z`.

The manifest version drops the `posix` marker: `16.2.0-14.0.0-ucrt-r1`.

Writes `updated`, `summary` and `body_file` to $GITHUB_OUTPUT when running in
GitHub Actions. Exits non-zero only on hard errors, not on "already current".
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

REPO = "brechtsanders/winlibs_mingw"
API = f"https://api.github.com/repos/{REPO}/releases"
PAGES = 3  # releases are listed newest-first; 300 entries is far more than enough

# bucket manifest -> C runtime variant it tracks
MANIFESTS = {
    "bucket/winlibs-gcc.json": "msvcrt",
    "bucket/winlibs-gcc-ucrt.json": "ucrt",
}

TAG_RE = re.compile(
    r"^(?P<gcc>\d+(?:\.\d+)*)posix-(?P<mingw>\d+(?:\.\d+)*)-(?P<crt>msvcrt|ucrt)-r(?P<rev>\d+)$"
)
VERSION_RE = re.compile(
    r"^(?P<gcc>\d+(?:\.\d+)*)-(?P<mingw>\d+(?:\.\d+)*)-(?P<crt>msvcrt|ucrt)-r(?P<rev>\d+)$"
)
SHA256_RE = re.compile(r"\b[0-9a-f]{64}\b")


def log(msg: str) -> None:
    print(msg, flush=True)


def http_get(url: str, accept: str = "application/vnd.github+json", attempts: int = 4) -> bytes:
    req = urllib.request.Request(url, headers={
        "Accept": accept,
        "User-Agent": "lampyridae-winlibs-updater",
    })
    token = os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        req.add_header("Authorization", f"Bearer {token}")
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            if attempt == attempts:
                raise
            log(f"  {url} failed ({err}), retrying in {2 ** attempt}s")
            time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


def sort_key(gcc: str, mingw: str, rev: str) -> tuple:
    return (
        tuple(int(p) for p in gcc.split(".")),
        tuple(int(p) for p in mingw.split(".")),
        int(rev),
    )


def fetch_releases() -> list[dict]:
    releases: list[dict] = []
    for page in range(1, PAGES + 1):
        batch = json.loads(http_get(f"{API}?per_page=100&page={page}"))
        if not batch:
            break
        releases.extend(batch)
    log(f"fetched {len(releases)} releases from {REPO}")
    return releases


def latest_per_crt(releases: list[dict]) -> dict[str, dict]:
    """Newest stable release for each C runtime variant, keyed by crt."""
    best: dict[str, dict] = {}
    for release in releases:
        if release.get("draft") or release.get("prerelease"):
            continue
        m = TAG_RE.match(release.get("tag_name", ""))
        if not m:
            continue
        crt = m.group("crt")
        key = sort_key(m.group("gcc"), m.group("mingw"), m.group("rev"))
        if crt not in best or key > best[crt]["key"]:
            best[crt] = {
                "key": key,
                "tag": release["tag_name"],
                "gcc": m.group("gcc"),
                "mingw": m.group("mingw"),
                "rev": m.group("rev"),
                "assets": {a["name"]: a["browser_download_url"] for a in release.get("assets", [])},
            }
    return best


def fetch_hash(url: str) -> str | None:
    """Read the upstream .sha256 sidecar; None if it is missing or unparsable."""
    try:
        text = http_get(url, accept="text/plain").decode("utf-8", "replace")
    except OSError as err:  # URLError/HTTPError/timeouts all land here
        log(f"  warning: could not fetch {url}: {err}")
        return None
    m = SHA256_RE.search(text.lower())
    if not m:
        log(f"  warning: no sha256 found in {url}")
        return None
    return m.group(0)


def current_key(version: str) -> tuple | None:
    m = VERSION_RE.match(version)
    return sort_key(m.group("gcc"), m.group("mingw"), m.group("rev")) if m else None


def update_manifest(path: str, crt: str, release: dict) -> tuple[str, str] | None:
    """Rewrite the manifest in place. Returns (old_version, new_version) if changed."""
    with open(path, "r", encoding="utf-8", newline="") as fh:
        raw = fh.read()
    newline = "\r\n" if "\r\n" in raw else "\n"
    manifest = json.loads(raw)

    old_version = manifest["version"]
    new_version = f"{release['gcc']}-{release['mingw']}-{crt}-r{release['rev']}"
    if old_version == new_version:
        log(f"{path}: already at {old_version}")
        return None

    old_key = current_key(old_version)
    if old_key is not None and release["key"] < old_key:
        log(f"{path}: upstream {new_version} is older than {old_version}, skipping")
        return None

    asset = (
        f"winlibs-x86_64-posix-seh-gcc-{release['gcc']}"
        f"-mingw-w64{crt}-{release['mingw']}-r{release['rev']}.7z"
    )
    url = release["assets"].get(asset)
    if not url:
        raise SystemExit(f"{path}: release {release['tag']} has no asset named {asset}")

    sidecar = release["assets"].get(f"{asset}.sha256")
    digest = fetch_hash(sidecar) if sidecar else None
    if not digest:
        # Never leave a stale hash behind: a wrong hash breaks every install.
        log(f"  warning: no upstream checksum for {asset}, omitting hash")

    arch = manifest["architecture"]["64bit"]
    arch.pop("hash", None)
    rebuilt = {}
    for key, value in arch.items():
        rebuilt[key] = url if key == "url" else value
        if key == "url" and digest:
            rebuilt["hash"] = digest
    manifest["architecture"]["64bit"] = rebuilt
    manifest["version"] = new_version

    out = json.dumps(manifest, indent=4, ensure_ascii=False) + "\n"
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(out.replace("\n", newline) if newline != "\n" else out)

    log(f"{path}: {old_version} -> {new_version}")
    return old_version, new_version


def set_outputs(changes: dict[str, tuple[str, str]]) -> None:
    out_path = os.environ.get("GITHUB_OUTPUT")
    body_lines = ["Automated update from "
                  f"[{REPO}](https://github.com/{REPO}/releases).", ""]
    for path, (old, new) in sorted(changes.items()):
        body_lines.append(f"- `{path}`: `{old}` -> `{new}`")
    body_lines += ["", "Hashes are taken from the `.sha256` files published with each release."]
    body = "\n".join(body_lines) + "\n"

    summary = ", ".join(
        f"{os.path.basename(p).removesuffix('.json')} {new}" for p, (_, new) in sorted(changes.items())
    )

    if changes:
        with open("pr-body.md", "w", encoding="utf-8") as fh:
            fh.write(body)

    if out_path:
        with open(out_path, "a", encoding="utf-8") as fh:
            fh.write(f"updated={'true' if changes else 'false'}\n")
            fh.write(f"summary={summary}\n")
            fh.write("body_file=pr-body.md\n")

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write(body if changes else "No winlibs-gcc update available.\n")


def main() -> int:
    latest = latest_per_crt(fetch_releases())
    changes: dict[str, tuple[str, str]] = {}
    for path, crt in MANIFESTS.items():
        release = latest.get(crt)
        if not release:
            raise SystemExit(f"no usable {crt} release found upstream")
        log(f"{path}: latest {crt} release is {release['tag']}")
        result = update_manifest(path, crt, release)
        if result:
            changes[path] = result

    set_outputs(changes)
    return 0


if __name__ == "__main__":
    sys.exit(main())

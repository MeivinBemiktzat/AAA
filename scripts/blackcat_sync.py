#!/usr/bin/env python3
import hashlib
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse, quote
from zipfile import ZipFile, ZIP_STORED, ZIP_DEFLATED

import requests

API = "https://black-cat.thechats.click/api/messages"
MEDIA_PROXY = "https://vodos-media.i0527685490.workers.dev/?url="
OUT = Path("downloads")
REPORT = Path("download-report.json")
MAX_PACKAGES = 4
PACKAGE_LIMIT = int(1.8 * 1024**3) - 8 * 1024**2
PAGE_SIZE = 100
ALLOWED_HOSTS = {"black-cat.thechats.click", "cdn.thechats.click"}
VIDEO_TYPES = {"mp4", "mov", "webm", "m4v", "video/mp4", "video/webm", "video/quicktime"}
IMAGE_TYPES = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "avif",
               "image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp", "image/avif"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".m4v"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif"}

session = requests.Session()
session.headers.update({"User-Agent": "VODOS/1.0 (+https://xnet2.github.io/VOD/)"})

def get_messages(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("messages", "data", "items", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    raise RuntimeError("Unexpected API response: could not find a messages array")

def fetch_all_messages():
    all_messages = []
    seen_ids = set()
    seen_pages = set()
    pagination_supported = True
    offset = 0

    while True:
        params = {"offset": offset, "limit": PAGE_SIZE, "direction": "asc"}
        response = session.get(API, params=params, timeout=(20, 90))
        response.raise_for_status()
        page = get_messages(response.json())
        if not page:
            break

        page_ids = []
        for msg in page:
            if not isinstance(msg, dict):
                continue
            mid = str(msg.get("id") or "")
            fingerprint = mid or hashlib.sha256(
                json.dumps(msg, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            page_ids.append(fingerprint)
            if fingerprint not in seen_ids:
                seen_ids.add(fingerprint)
                all_messages.append(msg)

        page_signature = hashlib.sha256("|".join(page_ids).encode()).hexdigest()
        if page_signature in seen_pages:
            pagination_supported = False
            print("Pagination parameters appear to be ignored; the API repeated a page.")
            break
        seen_pages.add(page_signature)

        print(f"Fetched page at offset={offset}: {len(page)} messages; {len(all_messages)} unique so far")
        if len(page) < PAGE_SIZE:
            # Some installations cap responses below the requested limit. Try one more offset;
            # if the server ignores offset, the duplicate-page guard above stops the scan.
            offset += len(page)
        else:
            offset += PAGE_SIZE

        if len(page) == 0:
            break
        if offset > 100000:
            raise RuntimeError("Safety stop: message pagination exceeded 100,000 offset")

    return all_messages, pagination_supported

def media_candidates(messages):
    candidates = []
    seen = set()
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("deleted") or msg.get("isAds"):
            continue
        body = str(msg.get("text") or "")
        found = []
        found.extend(("video", u) for u in re.findall(r"\[video-embedded#\]\(([^)]+)\)", body))
        found.extend(("image", u) for u in re.findall(r"\[image-embedded#\]\(([^)]+)\)", body))

        file_info = msg.get("file") or {}
        if isinstance(file_info, dict):
            filetype = str(file_info.get("filetype") or "").lower().lstrip(".")
            file_url = file_info.get("url")
            if file_url and filetype in VIDEO_TYPES:
                found.append(("video", file_url))
            elif file_url and filetype in IMAGE_TYPES:
                found.append(("image", file_url))

        for kind, raw_url in found:
            url = urljoin("https://black-cat.thechats.click/", str(raw_url).strip())
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
                continue
            key = (kind, url)
            if key in seen:
                continue
            seen.add(key)
            candidates.append({
                "url": url,
                "kind": kind,
                "message_id": str(msg.get("id", "unknown")),
                "timestamp": str(msg.get("timestamp", "")),
            })
    return candidates

def signature_info(data):
    if data.startswith(b"\x00\x00\x00") and len(data) >= 8 and data[4:8] == b"ftyp":
        if b"avif" in data[:64]:
            return "image", ".avif"
        return "video", ".mp4"
    if data.startswith(bytes.fromhex("1a45dfa3")):
        return "video", ".webm"
    if data.startswith(bytes.fromhex("ffd8ff")):
        return "image", ".jpg"
    if data.startswith(bytes.fromhex("89504e470d0a1a0a")):
        return "image", ".png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image", ".gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image", ".webp"
    if data.startswith(b"BM"):
        return "image", ".bmp"
    return None, None

def package_dir(index):
    path = OUT / f"APP-{index}"
    path.mkdir(parents=True, exist_ok=True)
    return path

def write_package(index, files, manifest_rows):
    if not files:
        return None
    archive = Path(f"APP-{index}.zip")
    with ZipFile(archive, "w", allowZip64=True) as z:
        for path in files:
            z.write(path, arcname=path.name, compress_type=ZIP_STORED)
        z.writestr("manifest.json", json.dumps(manifest_rows, ensure_ascii=False, indent=2),
                   compress_type=ZIP_DEFLATED)
    size = archive.stat().st_size
    print(f"Created {archive.name}: {size / 1024**3:.3f} GiB")
    if size >= 2 * 1024**3:
        raise RuntimeError(f"{archive.name} exceeds GitHub's 2 GB release-asset limit")
    for path in files:
        path.unlink(missing_ok=True)
    return {"name": archive.name, "bytes": size, "files": len(files)}

def main():
    OUT.mkdir(exist_ok=True)
    messages, pagination_supported = fetch_all_messages()
    candidates = media_candidates(messages)
    videos = sum(1 for x in candidates if x["kind"] == "video")
    images = sum(1 for x in candidates if x["kind"] == "image")
    print(f"Messages scanned: {len(messages)}")
    print(f"Unique videos: {videos}; unique images: {images}; total media: {len(candidates)}")

    package_index = 1
    current_dir = package_dir(package_index)
    current_bytes = 0
    current_files = []
    current_manifest = []
    package_results = []
    downloaded = {"video": 0, "image": 0}
    skipped = 0
    failures = []
    total_bytes = 0

    for number, item in enumerate(candidates, 1):
        if package_index > MAX_PACKAGES:
            print("All four package slots are full; remaining media is left for a later run.")
            break

        url = item["url"]
        digest = hashlib.sha256((item["kind"] + ":" + url).encode()).hexdigest()[:16]
        suffix = Path(urlparse(url).path).suffix.lower()
        allowed = VIDEO_EXTS if item["kind"] == "video" else IMAGE_EXTS
        if suffix not in allowed:
            suffix = ".mp4" if item["kind"] == "video" else ".jpg"

        print(f"[{number}/{len(candidates)}] Downloading {item['kind']} from message {item['message_id']}")
        temp = OUT / f"{digest}{suffix}.part"
        target = current_dir / f"{item['kind']}_{digest}{suffix}"
        try:
            host = urlparse(url).hostname
            download_url = MEDIA_PROXY + quote(url, safe="") if host == "cdn.thechats.click" else url
            with session.get(download_url, stream=True, timeout=(25, 180)) as response:
                response.raise_for_status()
                content_type = (response.headers.get("Content-Type") or "").lower()
                first = b""
                written = 0
                with temp.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        if not first:
                            first = chunk[:64]
                        handle.write(chunk)
                        written += len(chunk)

            detected_kind, detected_ext = signature_info(first)
            if not detected_kind or detected_kind != item["kind"] or written < 128:
                temp.unlink(missing_ok=True)
                failures.append({"url": url, "kind": item["kind"], "error": "invalid media signature",
                                 "content_type": content_type, "bytes_received": written})
                print(f"Rejected non-media response: {content_type}; first bytes={first[:20]!r}")
                continue

            if written > PACKAGE_LIMIT:
                temp.unlink(missing_ok=True)
                failures.append({"url": url, "kind": item["kind"], "error": "file exceeds 1.8 GiB package limit",
                                 "bytes_received": written})
                continue

            if current_bytes + written > PACKAGE_LIMIT:
                temp.unlink(missing_ok=True)
                result = write_package(package_index, current_files, current_manifest)
                if result:
                    package_results.append(result)
                package_index += 1
                if package_index > MAX_PACKAGES:
                    print("All four package slots are full; remaining media is left for a later run.")
                    break
                current_dir = package_dir(package_index)
                current_bytes = 0
                current_files = []
                current_manifest = []

                # Retry the current item in the next package.
                try:
                    with session.get(download_url, stream=True, timeout=(25, 180)) as response:
                        response.raise_for_status()
                        with temp.open("wb") as handle:
                            for chunk in response.iter_content(chunk_size=1024 * 1024):
                                if chunk:
                                    handle.write(chunk)
                    if temp.stat().st_size > PACKAGE_LIMIT:
                        temp.unlink(missing_ok=True)
                        failures.append({"url": url, "kind": item["kind"], "error": "file exceeds package limit"})
                        continue
                except Exception as exc:
                    temp.unlink(missing_ok=True)
                    failures.append({"url": url, "kind": item["kind"], "error": str(exc)})
                    continue

            final_target = current_dir / f"{item['kind']}_{digest}{detected_ext}"
            temp.replace(final_target)
            size = final_target.stat().st_size
            current_files.append(final_target)
            current_manifest.append({**item, "file": final_target.name, "bytes": size})
            current_bytes += size
            total_bytes += size
            downloaded[item["kind"]] += 1
            print(f"Saved {final_target.name}: {size / 1024**2:.2f} MiB; package {package_index}: {current_bytes / 1024**3:.3f} GiB")
        except Exception as exc:
            temp.unlink(missing_ok=True)
            failures.append({"url": url, "kind": item["kind"], "error": str(exc)})
            print(f"Download failed: {exc}")
            time.sleep(1)

    result = write_package(package_index, current_files, current_manifest)
    if result:
        package_results.append(result)

    report = {
        "messages_scanned": len(messages),
        "pagination_supported": pagination_supported,
        "unique_media": len(candidates),
        "unique_video_urls": videos,
        "unique_image_urls": images,
        "downloaded_videos": downloaded["video"],
        "downloaded_images": downloaded["image"],
        "downloaded_total": sum(downloaded.values()),
        "total_bytes": total_bytes,
        "package_limit_bytes": PACKAGE_LIMIT,
        "packages_created": package_results,
        "failures": failures,
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not package_results:
        raise SystemExit("No valid media files were packaged; inspect download-report.json")

if __name__ == "__main__":
    main()

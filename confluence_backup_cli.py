#!/usr/bin/env python3
# -----------------------------------------------------------------------------
# Confluence Cloud Backup CLI
#
# Based on the AWS Lambda scripts by Aaron Morris (agile-innovations.tech).
# Refactored for CLI/systemd usage on Linux servers.
#
# License: MIT (see LICENSE)
# -----------------------------------------------------------------------------

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


__version__ = "2.0.0"

# Defaults
DEFAULT_POLL_INTERVAL = 30  # seconds
DEFAULT_POLL_TIMEOUT = 7200  # 2 hours
DEFAULT_HTTP_TIMEOUT = 60  # seconds
DEFAULT_DOWNLOAD_TIMEOUT = 1800  # 30 minutes


def create_session(email: str, api_token: str) -> requests.Session:
    """Create an HTTP session with retry/backoff for 429 and 5xx."""
    session = requests.Session()
    session.auth = (email, api_token)
    session.headers.update({
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    retry = Retry(
        total=5,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def trigger_backup(
    session: requests.Session,
    site_url: str,
    include_attachments: bool = True,
) -> None:
    """Trigger a Confluence Cloud backup via the undocumented OBM API."""
    url = f"{site_url}/wiki/rest/obm/1.0/runbackup"
    payload = {
        "cbAttachments": "true" if include_attachments else "false",
        "exportToCloud": "true",
    }
    print(f"Triggering Confluence backup on {site_url} ...")
    resp = session.post(url, json=payload, timeout=DEFAULT_HTTP_TIMEOUT)
    if resp.status_code == 406:
        print("Backup already in progress — skipping trigger, will poll existing job.")
        return
    resp.raise_for_status()
    print("Backup triggered successfully.")


def poll_until_complete(
    session: requests.Session,
    site_url: str,
    poll_interval: int = DEFAULT_POLL_INTERVAL,
    poll_timeout: int = DEFAULT_POLL_TIMEOUT,
) -> str:
    """Poll the backup progress endpoint until COMPLETE. Returns the download path."""
    url = f"{site_url}/wiki/rest/obm/1.0/getprogress.json"
    deadline = time.monotonic() + poll_timeout
    print(f"Polling for backup completion (timeout {poll_timeout}s) ...")

    while True:
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"Backup did not complete within {poll_timeout} seconds."
            )

        resp = session.get(url, timeout=DEFAULT_HTTP_TIMEOUT)
        resp.raise_for_status()
        progress = resp.json()

        status = progress.get("currentStatus", "UNKNOWN")
        alt_status = progress.get("alternativePercentage", "")
        print(f"  Status: {status}  {alt_status}")

        if status == "COMPLETE":
            file_name = progress.get("fileName")
            if not file_name:
                raise RuntimeError(
                    "Backup marked COMPLETE but no fileName returned."
                )
            return file_name

        if status in ("ERROR", "FAILED"):
            raise RuntimeError(f"Backup failed with status: {status}")

        time.sleep(poll_interval)


def download_backup(
    session: requests.Session,
    site_url: str,
    download_path: str,
    output_dir: Path,
    date_str: str,
    max_retries: int = 5,
) -> tuple[Path, int]:
    """Download the backup ZIP to output_dir with resume on broken connections."""
    download_url = f"{site_url}/wiki/download/{download_path}"
    output_dir.mkdir(parents=True, exist_ok=True)

    filename = f"confluence-backup-{date_str}.zip"
    dest = output_dir / filename

    chunk_size = 8 * 1024 * 1024  # 8 MiB

    for attempt in range(1, max_retries + 1):
        # Resume from where we left off if partial file exists
        written = dest.stat().st_size if dest.exists() else 0
        headers = {}
        if written > 0:
            headers["Range"] = f"bytes={written}-"
            print(f"Resuming download from {written / (1024 * 1024):.1f} MiB (attempt {attempt}) ...")
        else:
            print(f"Downloading backup from {download_url} ...")

        try:
            resp = session.get(
                download_url,
                stream=True,
                timeout=DEFAULT_DOWNLOAD_TIMEOUT,
                headers=headers,
            )
            # 416 = Range Not Satisfiable — file is already complete
            if resp.status_code == 416:
                print(f"Download already complete ({written} bytes).")
                return dest, written
            resp.raise_for_status()

            mode = "ab" if written > 0 and resp.status_code == 206 else "wb"
            if mode == "wb":
                written = 0

            with open(dest, mode) as f:
                for chunk in resp.iter_content(chunk_size=chunk_size):
                    f.write(chunk)
                    written += len(chunk)
                    print(
                        f"  Downloaded {written / (1024 * 1024):.1f} MiB ...",
                        end="\r",
                    )

            print(f"\nBackup saved to {dest} ({written} bytes)")
            return dest, written

        except (requests.ConnectionError, requests.ChunkedEncodingError) as exc:
            if attempt == max_retries:
                raise RuntimeError(
                    f"Download failed after {max_retries} attempts: {exc}"
                ) from exc
            wait = min(30 * attempt, 120)
            print(f"\nConnection lost at {written / (1024 * 1024):.1f} MiB. "
                  f"Retrying in {wait}s (attempt {attempt}/{max_retries}) ...")
            time.sleep(wait)


def write_metadata(
    output_dir: Path,
    *,
    site_url: str,
    backup_filename: str,
    size_bytes: int,
    task_file_name: str,
    date_str: str,
) -> Path:
    """Write a metadata.json next to the backup ZIP."""
    meta = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "site_url": site_url,
        "backup_type": "confluence",
        "downloaded_filename": backup_filename,
        "size_bytes": size_bytes,
        "atlassian_task_file": task_file_name,
        "tool": "atlassian-cloud-backups",
        "tool_version": __version__,
        "date": date_str,
    }
    meta_path = output_dir / "metadata.json"
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    print(f"Metadata written to {meta_path}")
    return meta_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Confluence Cloud Backup CLI — trigger, poll, and download.",
    )
    parser.add_argument(
        "--site-url",
        required=True,
        help='Atlassian site URL, e.g. "https://TENANT.atlassian.net"',
    )
    parser.add_argument(
        "--email",
        required=True,
        help="Atlassian account email for API authentication.",
    )
    parser.add_argument(
        "--api-token",
        required=True,
        help="Atlassian API token (use env var to avoid shell history).",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="Directory to store the backup ZIP and metadata.",
    )
    parser.add_argument(
        "--no-attachments",
        action="store_true",
        default=False,
        help="Exclude attachments from the backup.",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=DEFAULT_POLL_INTERVAL,
        help=f"Seconds between progress polls (default {DEFAULT_POLL_INTERVAL}).",
    )
    parser.add_argument(
        "--poll-timeout",
        type=int,
        default=DEFAULT_POLL_TIMEOUT,
        help=f"Max seconds to wait for backup completion (default {DEFAULT_POLL_TIMEOUT}).",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    site_url = args.site_url.rstrip("/")
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    session = create_session(args.email, args.api_token)

    try:
        # 1. Trigger
        trigger_backup(
            session, site_url, include_attachments=not args.no_attachments
        )

        # 2. Poll
        download_path = poll_until_complete(
            session,
            site_url,
            poll_interval=args.poll_interval,
            poll_timeout=args.poll_timeout,
        )

        # 3. Download
        dest, size_bytes = download_backup(
            session, site_url, download_path, args.output_dir, date_str
        )

        # 4. Metadata
        write_metadata(
            args.output_dir,
            site_url=site_url,
            backup_filename=dest.name,
            size_bytes=size_bytes,
            task_file_name=download_path,
            date_str=date_str,
        )

        print("Confluence backup completed successfully.")
        return 0

    except requests.HTTPError as exc:
        print(f"HTTP error: {exc}", file=sys.stderr)
        return 1
    except TimeoutError as exc:
        print(f"Timeout: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"Backup error: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())

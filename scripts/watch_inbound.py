"""
Local Server Inbound Watchdog — Fusion Flow V2
Monitors INBOUND_FOLDER for new files, routes them, and forwards to the portal.

Known files (customer profile matched) → forwarded to ingest service for
structured extraction using the configured field mapping.

Unknown files (no profile match) → forwarded to ingest service as UNMAPPED
for Synovia review.

Usage:
    python scripts/watch_inbound.py [--once]

    --once   Process any pending files in INBOUND_FOLDER and exit (no loop).
             Without --once, the script polls every POLL_INTERVAL seconds.

Environment variables:
    INBOUND_FOLDER        Directory to watch (default: uploads/inbound)
    PORTAL_URL            Main portal base URL (for POST /ingest/receive)
    INGEST_WEBHOOK_KEY    Optional API key (X-API-Key header)
    POLL_INTERVAL         Seconds between scans (default: 30)
    PROCESSED_FOLDER      Move processed files here (default: uploads/processed)
    FAILED_FOLDER         Move failed files here (default: uploads/failed)
"""
import os
import sys
import time
import shutil
import logging
import argparse
try:
    from _console_output import configure_console_output
except ModuleNotFoundError:
    from scripts._console_output import configure_console_output

configure_console_output()
from pathlib import Path

import requests

# ── Logging setup ──────────────────────────────────────────────────────────────
logging.basicConfig(
    format='%(asctime)s [watchdog] %(levelname)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    level=logging.INFO,
)
log = logging.getLogger('watchdog')

# ── Config from env ────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent.parent   # project root

INBOUND_FOLDER    = Path(os.environ.get('INBOUND_FOLDER', _HERE / 'uploads' / 'inbound'))
PROCESSED_FOLDER  = Path(os.environ.get('PROCESSED_FOLDER', _HERE / 'uploads' / 'processed'))
FAILED_FOLDER     = Path(os.environ.get('FAILED_FOLDER', _HERE / 'uploads' / 'failed'))
PORTAL_URL        = os.environ.get('PORTAL_URL', 'http://localhost:10000').rstrip('/')
INGEST_WEBHOOK_KEY = os.environ.get('INGEST_WEBHOOK_KEY', '')
POLL_INTERVAL     = int(os.environ.get('POLL_INTERVAL', '30'))

ALLOWED_EXTENSIONS = {'.pdf', '.png', '.jpg', '.jpeg', '.tiff', '.tif', '.bmp', '.xml', '.csv'}


def ensure_dirs():
    for d in (INBOUND_FOLDER, PROCESSED_FOLDER, FAILED_FOLDER):
        d.mkdir(parents=True, exist_ok=True)


def send_to_portal(filepath: Path):
    """POST the file to the portal receive endpoint. Returns (ok, result_dict)."""
    receive_url = f'{PORTAL_URL}/ingest/receive'
    headers = {}
    if INGEST_WEBHOOK_KEY:
        headers['X-API-Key'] = INGEST_WEBHOOK_KEY

    try:
        with open(filepath, 'rb') as fh:
            resp = requests.post(
                receive_url,
                files={'file': (filepath.name, fh, 'application/octet-stream')},
                data={'source': 'local_watchdog'},
                headers=headers,
                timeout=120,
            )
        resp.raise_for_status()
        return True, resp.json()
    except requests.exceptions.ConnectionError:
        return False, {'error': f'Cannot connect to portal at {receive_url}'}
    except requests.exceptions.Timeout:
        return False, {'error': 'Portal timed out (>120s)'}
    except Exception as e:
        return False, {'error': str(e)}


def process_file(filepath: Path):
    """Route one file — send to portal, move to processed/ or failed/."""
    if filepath.suffix.lower() not in ALLOWED_EXTENSIONS:
        log.info('Skipping unsupported file type: %s', filepath.name)
        return

    log.info('Processing: %s', filepath.name)
    ok, result = send_to_portal(filepath)

    if ok:
        channel  = result.get('channel', 'UNKNOWN')
        customer = result.get('customer_code') or '—'
        unmapped = result.get('is_unmapped', False)
        log.info('  → channel=%s  customer=%s  unmapped=%s', channel, customer, unmapped)

        dest = PROCESSED_FOLDER / filepath.name
        # Avoid name collision
        if dest.exists():
            stem = filepath.stem
            suffix = filepath.suffix
            dest = PROCESSED_FOLDER / f'{stem}_{int(time.time())}{suffix}'
        shutil.move(str(filepath), str(dest))
        log.info('  Moved to processed/: %s', dest.name)
    else:
        err = result.get('error', 'unknown error')
        log.error('  FAILED: %s — %s', filepath.name, err)
        dest = FAILED_FOLDER / filepath.name
        if dest.exists():
            dest = FAILED_FOLDER / f'{filepath.stem}_{int(time.time())}{filepath.suffix}'
        shutil.move(str(filepath), str(dest))
        log.warning('  Moved to failed/: %s', dest.name)


def scan_once():
    """Process all files currently in INBOUND_FOLDER."""
    files = sorted(INBOUND_FOLDER.iterdir(), key=lambda p: p.stat().st_mtime)
    pending = [f for f in files if f.is_file() and not f.name.startswith('.')]
    if not pending:
        log.info('No files in inbound folder.')
        return 0

    log.info('Found %d file(s) to process.', len(pending))
    for fp in pending:
        process_file(fp)
    return len(pending)


def main():
    parser = argparse.ArgumentParser(description='Fusion Flow inbound watchdog')
    parser.add_argument('--once', action='store_true',
                        help='Process existing files and exit (no polling loop)')
    args = parser.parse_args()

    ensure_dirs()
    log.info('Watchdog starting — inbound: %s  portal: %s', INBOUND_FOLDER, PORTAL_URL)

    if args.once:
        processed = scan_once()
        log.info('Done — %d file(s) processed.', processed)
        sys.exit(0)

    log.info('Polling every %ds  (Ctrl+C to stop)', POLL_INTERVAL)
    while True:
        try:
            scan_once()
        except KeyboardInterrupt:
            log.info('Stopped.')
            sys.exit(0)
        except Exception as e:
            log.error('Scan error: %s', e)
        time.sleep(POLL_INTERVAL)


if __name__ == '__main__':
    main()

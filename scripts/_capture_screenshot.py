"""Decode the latest Puppeteer MCP screenshot blob into docs/screenshots/<slug>.png.

Usage:
    python scripts/_capture_screenshot.py <slug>

The MCP saves large screenshot results to a JSON file in the project tool-results
cache. This helper finds the most recent one, extracts the base64 image and
writes it to docs/screenshots/<slug>.png so the product manual can embed it.
"""
import base64
import glob
import json
import os
import sys


CACHE_DIR = os.path.join(
    os.path.expanduser('~'),
    '.claude', 'projects',
    'C--Users-it-synoviasupport-Desktop-dev-Fusion-Flow-V2-BKD',
)


def main():
    if len(sys.argv) < 2:
        sys.exit('Usage: _capture_screenshot.py <slug>')
    slug = sys.argv[1]
    pattern = os.path.join(CACHE_DIR, '*', 'tool-results',
                           'mcp-puppeteer-puppeteer_screenshot-*.txt')
    candidates = sorted(glob.glob(pattern), key=os.path.getmtime)
    if not candidates:
        sys.exit('No puppeteer screenshot blob found')
    src = candidates[-1]
    with open(src, 'r', encoding='utf-8') as fh:
        payload = json.load(fh)
    image_text = ''
    for item in payload if isinstance(payload, list) else [payload]:
        text = item.get('text', '') if isinstance(item, dict) else ''
        if text.startswith('data:image/'):
            image_text = text
            break
    if not image_text:
        sys.exit(f'Did not find image blob in {src}')
    raw = image_text.split(',', 1)[1]
    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           'docs', 'screenshots')
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f'{slug}.png')
    with open(out_path, 'wb') as fh:
        fh.write(base64.b64decode(raw))
    size = os.path.getsize(out_path)
    print(f'OK {slug}.png ({size} bytes) from {os.path.basename(src)}')


if __name__ == '__main__':
    main()

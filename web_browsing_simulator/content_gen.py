#!/usr/bin/env python3
"""
Generates a randomized corpus of synthetic "web pages" on disk so the browsing
simulator has realistic, varied traffic to fetch instead of a handful of fixed
canned requests. Bytes are random and meaningless -- only the size/type/count
distribution matters, since this is used to exercise the WiFi link, not to
render anything.

Regenerating the corpus on every process start is intentional: it keeps test
sessions from looking identical run to run, and a fresh corpus is cheap to
write (a few dozen files, tens of MB total).
"""

import json
import os
import random

PAGE_COUNT_RANGE = (30, 50)
ASSET_COUNT_RANGE = (3, 25)

HTML_SIZE_RANGE = (20_000, 40_000)
CSS_SIZE_RANGE = (5_000, 50_000)
JS_SIZE_RANGE = (20_000, 300_000)
IMAGE_SIZE_RANGE = (20_000, 800_000)
HERO_IMAGE_SIZE_RANGE = (800_000, 1_500_000)
HERO_IMAGE_CHANCE = 0.10

ASSET_TYPES = [
    ("css", "text/css", CSS_SIZE_RANGE),
    ("js", "application/javascript", JS_SIZE_RANGE),
    ("jpg", "image/jpeg", IMAGE_SIZE_RANGE),
    ("png", "image/png", IMAGE_SIZE_RANGE),
]


def _write_random_file(path: str, size: int) -> None:
    with open(path, "wb") as f:
        f.write(os.urandom(size))


def _generate_page(page_id: str, pages_dir: str, assets_dir: str) -> dict:
    html_size = random.randint(*HTML_SIZE_RANGE)
    _write_random_file(os.path.join(pages_dir, f"{page_id}.html"), html_size)

    page_assets_dir = os.path.join(assets_dir, page_id)
    os.makedirs(page_assets_dir, exist_ok=True)

    asset_count = random.randint(*ASSET_COUNT_RANGE)
    assets = []
    for i in range(asset_count):
        ext, content_type, size_range = random.choice(ASSET_TYPES)
        if content_type.startswith("image/") and random.random() < HERO_IMAGE_CHANCE:
            size_range = HERO_IMAGE_SIZE_RANGE
        size = random.randint(*size_range)
        asset_id = f"asset-{i}"
        filename = f"{asset_id}.{ext}"
        _write_random_file(os.path.join(page_assets_dir, filename), size)
        assets.append({
            "id": asset_id,
            "path": f"assets/{page_id}/{filename}",
            "content_type": content_type,
            "size": size,
        })

    return {
        "html_path": f"pages/{page_id}.html",
        "html_size": html_size,
        "assets": assets,
    }


def generate_corpus(content_dir: str) -> dict:
    """Generate a fresh randomized corpus into content_dir, overwriting any manifest.json there."""
    pages_dir = os.path.join(content_dir, "pages")
    assets_dir = os.path.join(content_dir, "assets")
    os.makedirs(pages_dir, exist_ok=True)
    os.makedirs(assets_dir, exist_ok=True)

    page_count = random.randint(*PAGE_COUNT_RANGE)
    pages = {}
    for i in range(page_count):
        page_id = f"page-{i}"
        pages[page_id] = _generate_page(page_id, pages_dir, assets_dir)

    manifest = {"pages": pages}
    manifest_path = os.path.join(content_dir, "manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f)

    return manifest


def ensure_content_corpus(content_dir: str) -> dict:
    """Generate the corpus if it doesn't already exist, and return the manifest dict either way."""
    manifest_path = os.path.join(content_dir, "manifest.json")
    if os.path.exists(manifest_path):
        with open(manifest_path) as f:
            return json.load(f)
    return generate_corpus(content_dir)

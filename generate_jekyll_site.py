#!/usr/bin/env python3
"""
generate_jekyll_site.py

Enhanced Jekyll auto-publisher.

Features:
- Image fetching from Wikimedia Commons (no API key).
- Optional image resizing + thumbnails (Pillow).
- Dry-run mode (write files but do not commit/push).
- Staging branch for drafts and main branch for published posts.

Usage:
  python3 generate_jekyll_site.py [--dry-run] [--build]

Environment variables (optional):
  CHECK_INTERVAL     - seconds between runs (if set, script loops)
  MAIN_BRANCH        - target branch for published posts (default: "main")
  STAGING_BRANCH     - branch for drafts/staging (default: "staging")
  GIT_COMMIT_NAME    - git user.name to use for commits
  GIT_COMMIT_EMAIL   - git user.email to use for commits
  DRY_RUN            - If set to "1", equivalent to --dry-run
  BUILD_JEKYLL       - If set to "1", attempt `jekyll build` in dry-run mode

CSV format header:
slug,title,short_description,body,tags,affiliate_link,publish_date,image_query
"""
from __future__ import annotations
import os
import csv
import sqlite3
import subprocess
import time
from datetime import datetime, date
import logging
import shutil
import re
from typing import Optional, Tuple
import argparse
import requests
import frontmatter
from urllib.parse import unquote, urlparse

# Optional Pillow import
try:
    from PIL import Image
    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False

# CONFIG
POSTS_DIR = "_posts"
DRAFTS_DIR = "_drafts"
ASSETS_DIR = "assets/images"
CSV_FILE = "products.csv"
DB_FILE = "published.db"
LOGFILE = "generate_jekyll_site.log"

MAIN_BRANCH = os.getenv("MAIN_BRANCH", "main")
STAGING_BRANCH = os.getenv("STAGING_BRANCH", "staging")
GIT_COMMIT_NAME = os.getenv("GIT_COMMIT_NAME")
GIT_COMMIT_EMAIL = os.getenv("GIT_COMMIT_EMAIL")

# Image sizing
MAIN_MAX_SIZE = (1200, 1200)
THUMB_SIZE = (400, 400)

# Wikimedia Commons API endpoint (no API key needed)
WIKIMEDIA_SEARCH_API = "https://commons.wikimedia.org/w/api.php"

# Logging
logging.basicConfig(level=logging.INFO, filename=LOGFILE,
                    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

def run_cmd(cmd, cwd=".", check=True):
    logger.info("Running command: %s", " ".join(cmd))
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if res.returncode != 0 and check:
        logger.error("Command failed (%s): %s", " ".join(cmd), res.stderr.strip())
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{res.stderr}")
    return res

def repo_has_uncommitted_changes():
    res = run_cmd(["git", "status", "--porcelain"], check=False)
    return bool(res.stdout.strip())

def init_db(path=DB_FILE):
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS items (
            slug TEXT PRIMARY KEY,
            state TEXT,
            scheduled_date TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    return conn

def read_products(csv_path=CSV_FILE):
    products = []
    if not os.path.exists(csv_path):
        logger.error("CSV file not found: %s", csv_path)
        return products
    with open(csv_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for r in reader:
            slug = (r.get("slug") or "").strip()
            title = (r.get("title") or "").strip()
            affiliate = (r.get("affiliate_link") or "").strip()
            publish_date = (r.get("publish_date") or "").strip()
            if not slug or not title or not affiliate or not publish_date:
                logger.warning("Skipping row missing required fields: %s", r)
                continue
            products.append({
                "slug": slug,
                "title": title,
                "short_description": (r.get("short_description") or "").strip(),
                "body": (r.get("body") or "").strip(),
                "tags": [t.strip() for t in (r.get("tags") or "").split(",") if t.strip()],
                "affiliate_link": affiliate,
                "publish_date": publish_date,
                "image_query": (r.get("image_query") or title).strip()
            })
    return products

def search_commons_image(query: str, limit=6) -> Optional[dict]:
    logger.info("Searching Wikimedia Commons for: %s", query)
    params = {
        "action": "query",
        "format": "json",
        "list": "search",
        "srsearch": query,
        "srnamespace": "6",
        "srlimit": str(limit)
    }
    try:
        r = requests.get(WIKIMEDIA_SEARCH_API, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        search_results = data.get("query", {}).get("search", [])
        if not search_results:
            logger.info("No search results on Commons for: %s", query)
            return None
        pageids = [str(item["pageid"]) for item in search_results]
        pageids_param = "|".join(pageids)
        params2 = {
            "action": "query",
            "format": "json",
            "prop": "imageinfo",
            "iiprop": "url|extmetadata",
            "pageids": pageids_param
        }
        r2 = requests.get(WIKIMEDIA_SEARCH_API, params=params2, timeout=15)
        r2.raise_for_status()
        data2 = r2.json()
        pages = data2.get("query", {}).get("pages", {})
        for pid in pageids:
            page = pages.get(pid)
            if not page:
                continue
            iinfo = page.get("imageinfo")
            if not iinfo:
                continue
            img = iinfo[0]
            ext = img.get("extmetadata", {})
            logger.info("Found image %s", img.get("url"))
            return {
                "url": img.get("url"),
                "extmetadata": ext,
                "title": page.get("title")
            }
    except Exception as e:
        logger.exception("Wikimedia search failed: %s", e)
        return None
    return None

def ext_from_url(url: str) -> str:
    parsed = urlparse(url)
    path = unquote(parsed.path)
    m = re.search(r"\.([a-zA-Z0-9]+)(?:$|\?)", path)
    if m:
        return m.group(1)
    return "jpg"

def download_image_and_resize(url: str, slug: str) -> Tuple[Optional[str], Optional[str], Optional[dict]]:
    try:
        ext = ext_from_url(url)
        orig_dest = os.path.join(ASSETS_DIR, f"{slug}-orig.{ext}")
        main_dest = os.path.join(ASSETS_DIR, f"{slug}.{ext}")
        thumb_dest = os.path.join(ASSETS_DIR, f"{slug}-thumb.{ext}")
        os.makedirs(os.path.dirname(orig_dest), exist_ok=True)
        with requests.get(url, stream=True, timeout=30) as r:
            r.raise_for_status()
            with open(orig_dest, "wb") as f:
                shutil.copyfileobj(r.raw, f)
        logger.info("Downloaded image to %s", orig_dest)
        if PIL_AVAILABLE:
            try:
                with Image.open(orig_dest) as im:
                    im = im.convert("RGB")
                    im.thumbnail(MAIN_MAX_SIZE, Image.LANCZOS)
                    im.save(main_dest, quality=85)
                    thumb = Image.open(orig_dest)
                    thumb = thumb.convert("RGB")
                    thumb.thumbnail((max(THUMB_SIZE), max(THUMB_SIZE)), Image.LANCZOS)
                    thumb_w, thumb_h = thumb.size
                    left = max(0, (thumb_w - THUMB_SIZE[0]) // 2)
                    top = max(0, (thumb_h - THUMB_SIZE[1]) // 2)
                    right = left + THUMB_SIZE[0]
                    bottom = top + THUMB_SIZE[1]
                    thumb_cropped = thumb.crop((left, top, right, bottom))
                    thumb_cropped.save(thumb_dest, quality=80)
                logger.info("Created resized image %s and thumbnail %s", main_dest, thumb_dest)
                return main_dest.replace("\\", "/"), thumb_dest.replace("\\", "/"), None
            except Exception:
                logger.exception("Pillow resize failed, falling back to original image as main image")
                shutil.copyfile(orig_dest, main_dest)
                return main_dest.replace("\\", "/"), None, None
        else:
            logger.info("Pillow not available; saved original as main image: %s", orig_dest)
            return orig_dest.replace("\\", "/"), None, None
    except Exception:
        logger.exception("Failed to download/resize image: %s", url)
        return None, None, None

def build_frontmatter(product, image_rel_path: Optional[str], thumb_rel_path: Optional[str], image_meta: Optional[dict]) -> Tuple[dict, str]:
    publish_date = product["publish_date"]
    disclosure = ("**Disclosure:** This post contains affiliate links. If you purchase using the links below "
                  "I may earn a small commission at no extra cost to you.\n\n")
    buy_box = f"\n\n---\n\n**Buy now:** [{product['title']}]({product['affiliate_link']})\n\n"
    image_block = ""
    if image_rel_path:
        image_block = f"![{product['title']}]({{ site.baseurl | default: '' }}/{image_rel_path})\n\n"
    attribution = ""
    if image_meta:
        artist = image_meta.get("Artist", {}).get("value", "")
        license_short = image_meta.get("LicenseShortName", {}).get("value", "")
        license_url = image_meta.get("LicenseUrl", {}).get("value", "")
        parts = []
        if artist:
            parts.append(artist)
        if license_short:
            parts.append(f"({license_short})")
        if license_url:
            parts.append(f"[license]({license_url})")
        if parts:
            attribution = "\n\n*Image credit:* " + " ".join(parts) + "\n\n"
    fm = {
        "layout": "post",
        "title": product["title"],
        "date": publish_date,
        "tags": product["tags"] or None,
        "excerpt": product["short_description"] or None,
        "image": image_rel_path or None,
        "thumbnail": thumb_rel_path or None
    }
    body = disclosure + image_block + (product["body"] or product["short_description"] or "") + attribution + buy_box
    return fm, body

def write_markdown(filepath: str, fm: dict, body: str):
    post = frontmatter.Post(body, **{k: v for k, v in fm.items() if v is not None})
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(frontmatter.dumps(post))
    logger.info("Wrote file %s", filepath)

def checkout_or_create_branch(branch: str):
    res = run_cmd(["git", "rev-parse", "--verify", branch], check=False)
    if res.returncode == 0:
        run_cmd(["git", "checkout", branch])
    else:
        run_cmd(["git", "checkout", "-b", branch])
    run_cmd(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], check=False)

def commit_and_push_branch(branch: str, files: list, message: Optional[str] = None, dry_run: bool = False):
    if not files:
        logger.info("No files to commit for branch %s", branch)
        return
    if dry_run:
        logger.info("Dry-run enabled: skipping git commit/push for branch %s. Files written: %s", branch, files)
        return
    run_cmd(["git", "add"] + files)
    msg = message or f"Auto-publish {len(files)} file(s) [{datetime.utcnow().isoformat()}]"
    if GIT_COMMIT_NAME:
        run_cmd(["git", "config", "user.name", GIT_COMMIT_NAME])
    if GIT_COMMIT_EMAIL:
        run_cmd(["git", "config", "user.email", GIT_COMMIT_EMAIL])
    try:
        run_cmd(["git", "commit", "-m", msg])
    except RuntimeError as e:
        logger.info("Commit may have failed (maybe nothing to commit): %s", e)
        return
    try:
        run_cmd(["git", "push", "-u", "origin", branch])
    except Exception:
        logger.exception("Failed to push branch %s", branch)
        raise

def process_once(dry_run: bool = False):
    if repo_has_uncommitted_changes():
        logger.error("Repository has uncommitted changes. Please commit/stash before running this script.")
        return

    conn = init_db()
    products = read_products()
    if not products:
        logger.info("No products found.")
        return

    today = date.today()
    staging_files = []
    main_files = []
    image_cache = {}

    for p in products:
        slug = p["slug"]
        try:
            sched = datetime.strptime(p["publish_date"], "%Y-%m-%d").date()
        except Exception:
            logger.warning("Invalid publish_date for %s: %s", slug, p["publish_date"])
            continue
        cur = conn.cursor()
        cur.execute("SELECT state FROM items WHERE slug = ?", (slug,))
        row = cur.fetchone()
        state = row[0] if row else None

        img_meta = None
        main_rel = None
        thumb_rel = None
        if slug not in image_cache:
            img = search_commons_image(p["image_query"])
            if img and img.get("url"):
                main_rel, thumb_rel, _ = download_image_and_resize(img["url"], slug)
                img_meta = img.get("extmetadata")
            image_cache[slug] = (main_rel, thumb_rel, img_meta)
        else:
            main_rel, thumb_rel, img_meta = image_cache[slug]

        if sched > today:
            draft_path = os.path.join(DRAFTS_DIR, f"{slug}.md")
            fm, body = build_frontmatter(p, main_rel, thumb_rel, img_meta)
            fm["scheduled_date"] = p["publish_date"]
            write_markdown(draft_path, fm, body)
            staging_files.append(draft_path)
            cur.execute("INSERT OR REPLACE INTO items (slug, state, scheduled_date) VALUES (?, ?, ?)",
                        (slug, "draft", p["publish_date"]))
            conn.commit()
            logger.info("Prepared draft for %s scheduled %s", slug, p["publish_date"])
        else:
            if state == "published":
                logger.debug("Already published: %s", slug)
                continue
            fm, body = build_frontmatter(p, main_rel, thumb_rel, img_meta)
            fname_date = sched.strftime("%Y-%m-%d")
            post_filename = f"{fname_date}-{slug}.md"
            post_path = os.path.join(POSTS_DIR, post_filename)
            write_markdown(post_path, fm, body)
            files_to_commit = [post_path]
            if main_rel:
                files_to_commit.append(main_rel)
            if thumb_rel:
                files_to_commit.append(thumb_rel)
            main_files.extend(files_to_commit)
            cur.execute("INSERT OR REPLACE INTO items (slug, state, scheduled_date) VALUES (?, ?, ?)",
                        (slug, "published", p["publish_date"]))
            conn.commit()
            logger.info("Prepared post for publish: %s", slug)

    if staging_files:
        if dry_run:
            logger.info("Dry-run: leaving staging files in working tree.")
        else:
            checkout_or_create_branch(STAGING_BRANCH)
            try:
                commit_and_push_branch(STAGING_BRANCH, staging_files, message=f"Auto-draft {len(staging_files)} item(s)", dry_run=dry_run)
            finally:
                run_cmd(["git", "checkout", MAIN_BRANCH], check=False)
    else:
        logger.info("No staging files to commit.")

    if main_files:
        if dry_run:
            logger.info("Dry-run: leaving main files in working tree.")
        else:
            checkout_or_create_branch(MAIN_BRANCH)
            try:
                commit_and_push_branch(MAIN_BRANCH, main_files, message=f"Auto-publish {len(main_files)} item(s)", dry_run=dry_run)
            finally:
                run_cmd(["git", "checkout", MAIN_BRANCH], check=False)
    else:
        logger.info("No main files to commit.")

def attempt_jekyll_build():
    try:
        res = run_cmd(["jekyll", "--version"], check=False)
        if res.returncode != 0:
            logger.info("jekyll not available on PATH; skipping local build.")
            return False
        logger.info("Running jekyll build (may take time)...")
        build_res = run_cmd(["jekyll", "build", "--source", ".", "--destination", "_site"], check=False)
        if build_res.returncode == 0:
            logger.info("jekyll build succeeded. Output in _site/")
            return True
        else:
            logger.error("jekyll build failed: %s", build_res.stderr)
            return False
    except Exception:
        logger.exception("jekyll build attempt failed")
        return False

def main():
    parser = argparse.ArgumentParser(description="Jekyll auto-publisher with images and dry-run.")
    parser.add_argument("--dry-run", action="store_true", help="Do not commit/push; write files to working tree only.")
    parser.add_argument("--build", dest="build", action="store_true", help="Attempt local jekyll build (only meaningful with --dry-run).")
    parser.add_argument("--no-build", dest="build", action="store_false", help="Do not run local build.")
    parser.set_defaults(build=None)
    args = parser.parse_args()

    env_dry = os.getenv("DRY_RUN", "0") == "1"
    env_build = os.getenv("BUILD_JEKYLL", "0") == "1"
    dry_run = args.dry_run or env_dry
    build_requested = (args.build is True) or env_build
    if args.build is False:
        build_requested = False

    interval = os.getenv("CHECK_INTERVAL")
    if interval:
        try:
            wait = int(interval)
        except ValueError:
            wait = 3600
        while True:
            try:
                process_once(dry_run=dry_run)
                if dry_run and build_requested:
                    attempt_jekyll_build()
            except Exception:
                logger.exception("Run failed")
            time.sleep(wait)
    else:
        process_once(dry_run=dry_run)
        if dry_run and build_requested:
            attempt_jekyll_build()

if __name__ == "__main__":
    main()

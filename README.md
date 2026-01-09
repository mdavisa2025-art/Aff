# Jekyll Auto-Publisher (Termux-ready)

This repository contains an automated Jekyll post generator designed to run from Termux (Android) or any Linux environment. It:

- Reads `products.csv` to create scheduled Jekyll posts (drafts for future dates, published posts for due items).
- Fetches images from Wikimedia Commons (no API key).
- Optionally resizes images and creates thumbnails using Pillow.
- Supports a dry-run preview mode (writes files but skips git commits and pushes).
- Uses SSH-based git pushes to your GitHub repo (no API keys required).

Quick features
- Staging branch for drafts (default `staging`).
- Main branch for published posts (default `main`).
- Tracks state in `published.db` to avoid duplicate posts.

Prerequisites (Termux)
- Termux installed
- Git and Python:
  - pkg update && pkg upgrade
  - pkg install git python openssh
- Python packages:
  - pip install -r requirements.txt

Security & ethics
- Provide honest content and only your own or properly licensed images/affiliate links.
- Disclose affiliate links (script adds disclosure).
- Do not scrape retailer sites against their ToS.

Usage examples
- Dry-run + optional local Jekyll build:
  - python3 generate_jekyll_site.py --dry-run --build
- Run continuously every hour:
  - export CHECK_INTERVAL=3600
  - export MAIN_BRANCH=main
  - export STAGING_BRANCH=staging
  - python3 generate_jekyll_site.py

If you run into issues, check `generate_jekyll_site.log` for detailed logs.

#!/usr/bin/env python3
"""List people who opted in to marketing email and have NOT unsubscribed.

    SITE_URL=https://your-domain.mn python3 subscribers.py
"""
import os
import sqlite3

ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("DB_PATH", os.path.join(ROOT, "stars.db"))
SITE = os.environ.get("SITE_URL", "https://YOURDOMAIN").rstrip("/")

c = sqlite3.connect(DB)
rows = c.execute(
    "select email, token, consent_at from subscribers "
    "where unsubscribed_at is null order by id"
).fetchall()

print("email\tunsubscribe_url\tconsent_at")
for email, token, at in rows:
    print("%s\t%s/unsubscribe.html?token=%s\t%s" % (email, SITE, token, at))
print("# %d active subscribers" % len(rows))

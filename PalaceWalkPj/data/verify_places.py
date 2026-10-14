#!/usr/bin/env python3
"""Validate data/places.json schema and that every image URL is a live image."""
import json, os, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
UA = {"User-Agent": "PalaceWalkPj/0.1 (educational project)"}
MOOD = {"quiet", "grand", "hidden", "open", "water", "shade"}
AES = {"structure", "garden", "color", "stone", "eclectic", "view", "water", "shade"}
HIS = {"politics", "architecture", "people"}
PALACES = {"경복궁", "창덕궁", "창경궁", "덕수궁", "경희궁"}
errors = []


def err(pid, msg):
    errors.append("%s: %s" % (pid, msg))


def check_image(url):
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                ct = r.headers.get("Content-Type", "")
                return r.status, ct
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            return e.code, ""
        except Exception as e:
            time.sleep(2)
    return 0, ""


def main():
    data = json.load(open(os.path.join(HERE, "places.json"), encoding="utf-8"))
    if data.get("version") != 1:
        err("root", "version must be 1")
    places = data.get("places", [])
    ids = set()
    for p in places:
        pid = p.get("id", "?")
        if pid in ids:
            err(pid, "duplicate id")
        ids.add(pid)
        for k in ("id", "palace", "name", "tags", "walk", "walk_estimated", "facts", "feel", "sources", "image", "verified_on"):
            if k not in p:
                err(pid, "missing " + k)
        if p.get("palace") not in PALACES:
            err(pid, "bad palace")
        t = p.get("tags", {})
        for key, vocab in (("mood", MOOD), ("aesthetic", AES), ("history", HIS)):
            v = t.get(key, [])
            if not 1 <= len(v) <= 3 or not set(v) <= vocab:
                err(pid, "bad tags." + key)
        w = p.get("walk", {})
        if not isinstance(w.get("steps_from_entrance"), int) or not isinstance(w.get("duration_min"), int):
            err(pid, "walk ints")
        if w.get("stairs") not in ("none", "few", "some", "many"):
            err(pid, "bad stairs")
        if not isinstance(w.get("rest_spots"), bool):
            err(pid, "rest_spots bool")
        if not p.get("sources") or not all(s.startswith("https://") for s in p["sources"]):
            err(pid, "bad sources")
        im = p.get("image", {})
        for k in ("url", "page", "author", "license", "alt"):
            if not im.get(k):
                err(pid, "image." + k)
        if not str(im.get("url", "")).startswith(("https://upload.wikimedia.org/", "https://thumb.wikimedia.org/")):
            err(pid, "image url host")
        if not str(im.get("page", "")).startswith("https://commons.wikimedia.org/wiki/File:"):
            err(pid, "image page")
        if im.get("url"):
            status, ct = check_image(im["url"])
            if status != 200 or not ct.startswith("image/"):
                err(pid, "image HTTP %s %s" % (status, ct))
            else:
                print("OK  %-40s %s" % (pid, ct))
            time.sleep(1)
    print("places: %d, errors: %d" % (len(places), len(errors)))
    for e in errors:
        print("ERROR", e)
    sys.exit(1 if errors or len(places) < 15 else 0)


main()

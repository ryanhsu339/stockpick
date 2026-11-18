#!/usr/bin/env python3
"""Regenerate data/manager_profiles.json and assets/managers/ -- the photo
and logo on each Managers-tab Headlines card.

For each of the largest --top-n funds (by derived equity AUM in the
top-managers snapshot) plus every fund currently in the headlines list,
finds the firm on Wikidata and takes:

  * its logo (P154),
  * the person most associated with it: the current CEO (P169), or the
    founder (P112) when there's no CEO on record or the overrides file asks
    for the founder (Bridgewater -> Ray Dalio),
  * that person's photo (P18), and the firm's website (P856).

Images come from Wikimedia Commons (freely licensed -- each one's credit
and license are kept for the card's attribution tooltip), are resized to
small square WebPs and committed under assets/managers/, so the live app
never hotlinks anything. A fund with no match keeps a monogram fallback on
the card.

Wikidata name matching on 13F filer names ("FMR LLC", "UBS AM, a distinct
business unit of ...") is imperfect, so data/manager_profiles_overrides.json
pins the right entity (or person) per CIK -- see that file. Not part of the
weekly job: rerun when a new fund enters the top 100; it prints which funds
are still missing a photo or logo.
"""
import argparse
import io
import json
import re
import sys
import time
from pathlib import Path

import requests
from PIL import Image

from thirteenf import _equity_total_m, _excluded_from_headlines, _load_top_managers_snapshot, load_top_buys

ROOT = Path(__file__).parent
PROFILES_PATH = ROOT / "data" / "manager_profiles.json"
OVERRIDES_PATH = ROOT / "data" / "manager_profiles_overrides.json"
IMAGE_DIR = ROOT / "assets" / "managers"
_WIKIDATA_API = "https://www.wikidata.org/w/api.php"
_COMMONS_API = "https://commons.wikimedia.org/w/api.php"
# Generic client name -- Wikimedia asks for a descriptive User-Agent.
_HEADERS = {"User-Agent": "StocksDashboard-ManagerProfiles/1.0 (13F tracker; build script)"}
_PERSON_PX = 200
_LOGO_PX = 128
# A search hit only counts if its description sounds like a financial firm.
_FINANCE_RE = re.compile(r"invest|bank|asset|fund|financ|capital|hedge|pension|insurance|wealth|"
                         r"trading|securities|management|holding|advis|retirement|brokerage", re.I)
_PERSON_RE = re.compile(r"business|executive|banker|investor|financier|manager|economist|"
                        r"philanthropist|chief|ceo|chair|founder|entrepreneur", re.I)
_NAME_NOISE_RE = re.compile(
    r"/[A-Z ]+/|\(.*?\)|,|\.|\b(LLC|L L C|LP|L P|LLP|LLLP|INC|CORP|CORPORATION|CO|LTD|LIMITED|PLC|AG|SA|"
    r"N A|NA|GMBH|SE|ASA|AB|THE|GROUP|HOLDINGS|PARTNERSHIP|LIMITED PARTNERSHIP)\b", re.I)

_session = requests.Session()
_session.headers.update(_HEADERS)


_last_request = 0.0


def _get(url, **params):
    global _last_request
    for attempt in range(6):
        time.sleep(max(0.0, _last_request + 1.0 - time.time()))
        _last_request = time.time()
        r = _session.get(url, params={**params, "format": "json"} if params else None, timeout=30)
        if r.status_code == 429 or r.status_code >= 500:
            retry_after = r.headers.get("Retry-After", "")
            time.sleep(int(retry_after) if retry_after.isdigit() else 10 * (attempt + 1))
            continue
        r.raise_for_status()
        return r
    r.raise_for_status()
    return r


def _clean_name(name):
    return re.sub(r"\s+", " ", _NAME_NOISE_RE.sub(" ", name)).strip()


def _entities(qids):
    out = {}
    qids = [q for q in dict.fromkeys(qids) if q]
    for i in range(0, len(qids), 50):
        data = _get(_WIKIDATA_API, action="wbgetentities", ids="|".join(qids[i:i + 50]),
                    props="labels|descriptions|claims", languages="en").json()
        out.update(data.get("entities", {}))
    return out


def _claim_values(entity, prop, current_only=False):
    """Entity-id or string values of `prop`, preferred rank first; with
    current_only, skips statements that have an end time (P582)."""
    claims = sorted(entity.get("claims", {}).get(prop, []), key=lambda c: c.get("rank") != "preferred")
    values = []
    for c in claims:
        if c.get("rank") == "deprecated":
            continue
        if current_only and c.get("qualifiers", {}).get("P582"):
            continue
        v = c.get("mainsnak", {}).get("datavalue", {}).get("value")
        if isinstance(v, dict) and "id" in v:
            values.append(v["id"])
        elif isinstance(v, str):
            values.append(v)
    return values


def _latest_current(entity, prop):
    """The current holder of `prop` (no end time) with the latest start
    time (P580) -- Wikidata often keeps a former CEO's statement without an
    end date alongside the new one."""
    best, best_start = None, ""
    for c in entity.get("claims", {}).get(prop, []):
        if c.get("rank") == "deprecated" or c.get("qualifiers", {}).get("P582"):
            continue
        v = c.get("mainsnak", {}).get("datavalue", {}).get("value")
        if not isinstance(v, dict) or "id" not in v:
            continue
        start = ((c.get("qualifiers", {}).get("P580") or [{}])[0].get("datavalue", {})
                 .get("value", {}).get("time", ""))
        if best is None or start > best_start:
            best, best_start = v["id"], start
    return best


def _search_person(name):
    hits = _get(_WIKIDATA_API, action="wbsearchentities", search=name, language="en",
                type="item", limit=5).json().get("search", [])
    for hit in hits:
        if _PERSON_RE.search(hit.get("description", "")):
            return hit["id"]
    return hits[0]["id"] if hits else None


def _site_icon(website):
    """The firm's own site icon, for funds with no Commons logo -- None if
    the site has none bigger than a default 16px favicon."""
    domain = re.sub(r"^https?://(www\.)?", "", website or "").split("/")[0]
    if not domain:
        return None
    try:
        r = _get("https://www.google.com/s2/favicons", domain=domain, sz=128)
        img = Image.open(io.BytesIO(r.content))
    except (requests.RequestException, OSError):
        return None
    return r.content if min(img.size) >= 64 else None


def _label(entity):
    return entity.get("labels", {}).get("en", {}).get("value", "")


def _search_firm(name):
    hits = _get(_WIKIDATA_API, action="wbsearchentities", search=_clean_name(name), language="en",
                type="item", limit=7).json().get("search", [])
    for hit in hits:
        if _FINANCE_RE.search(hit.get("description", "")):
            return hit["id"]
    return None


def _commons_image(filename, width):
    """(thumbnail bytes, credit, license) for a Commons file, or None."""
    pages = _get(_COMMONS_API, action="query", titles=f"File:{filename}", prop="imageinfo",
                 iiprop="url|extmetadata", iiurlwidth=width).json().get("query", {}).get("pages", {})
    for page in pages.values():
        info = (page.get("imageinfo") or [None])[0]
        if not info:
            continue
        meta = info.get("extmetadata", {})
        artist = re.sub(r"<[^>]+>", "", meta.get("Artist", {}).get("value", "")).strip()
        license_name = meta.get("LicenseShortName", {}).get("value", "")
        data = _get(info.get("thumburl") or info["url"]).content
        return data, artist[:120], license_name
    return None


def _save_square(data, path, px, crop_face):
    img = Image.open(io.BytesIO(data))
    img = img.convert("RGBA")
    w, h = img.size
    if crop_face:
        # Portraits: square crop biased toward the top, where the face is.
        side = min(w, h)
        left = (w - side) // 2
        top = int((h - side) * 0.2)
        img = img.crop((left, top, left + side, top + side)).resize((px, px), Image.LANCZOS)
        img = img.convert("RGB")
    else:
        # Logos: fit inside the square without cropping, transparent padding.
        img.thumbnail((px, px), Image.LANCZOS)
        canvas = Image.new("RGBA", (px, px), (0, 0, 0, 0))
        canvas.paste(img, ((px - img.width) // 2, (px - img.height) // 2), img)
        img = canvas
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "WEBP", quality=82, method=6)


def _target_funds(top_n):
    """[(cik, manager_name)]: the top_n largest snapshot funds plus every
    current headline fund."""
    sized = []
    for cik, comparison in _load_top_managers_snapshot().items():
        total = _equity_total_m(comparison)
        if total:
            sized.append((total, str(cik), comparison.get("manager_name", "")))
    sized.sort(reverse=True)
    # Market makers and brokers never get a headline card (see thirteenf.headline_moves).
    funds = {cik: name for _total, cik, name in sized[:top_n]
             if not _excluded_from_headlines(name)}
    for h in load_top_buys().get("headlines") or ():
        funds.setdefault(str(h["cik"]), h["manager_name"])
    return list(funds.items())


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--top-n", type=int, default=150)
    parser.add_argument("--only", nargs="*", help="Only (re)build these CIKs")
    parser.add_argument("--refresh-images", action="store_true", help="Re-download images that exist")
    args = parser.parse_args()

    overrides = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8")) if OVERRIDES_PATH.exists() else {}
    overrides = {k: v for k, v in overrides.items() if not k.startswith("_")}
    profiles = json.loads(PROFILES_PATH.read_text(encoding="utf-8")) if PROFILES_PATH.exists() else {}
    funds = _target_funds(args.top_n)
    if args.only:
        funds = [(cik, name) for cik, name in funds if cik in args.only]

    firm_qids = {}
    for cik, name in funds:
        o = overrides.get(cik, {})
        if o.get("skip"):
            continue
        firm_qids[cik] = o.get("qid") or _search_firm(o.get("search") or name)
    firms = _entities(firm_qids.values())

    person_qids, pinned_people = {}, set()
    for cik, qid in firm_qids.items():
        firm = firms.get(qid) or {}
        o = overrides.get(cik, {})
        ceo = _latest_current(firm, "P169")
        founder = (_claim_values(firm, "P112") or [None])[0]
        if o.get("no_person"):
            continue
        if o.get("person_qid") or o.get("person_search"):
            pinned = o.get("person_qid") or _search_person(o["person_search"])
            if pinned:
                person_qids[cik] = (pinned, o.get("person_title") or "CEO")
                pinned_people.add(pinned)
        elif o.get("prefer_founder") and founder:
            person_qids[cik] = (founder, "Founder")
        elif ceo:
            person_qids[cik] = (ceo, "Founder & CEO" if ceo == founder else "CEO")
        elif founder:
            person_qids[cik] = (founder, "Founder")
    people = _entities(q for q, _t in person_qids.values())
    # A long-dead founder (State Farm's, 1920s) isn't who readers associate
    # with the firm today -- only shown when the overrides pin them.
    for cik, (qid, _title) in list(person_qids.items()):
        if qid not in pinned_people and (people.get(qid) or {}).get("claims", {}).get("P570"):
            del person_qids[cik]

    for cik, name in funds:
        o = overrides.get(cik, {})
        if o.get("skip"):
            profiles.pop(cik, None)
            continue
        qid = firm_qids.get(cik)
        firm = firms.get(qid) or {}
        profile = {"manager_name": name, "qid": qid,
                   "display_name": o.get("display_name") or _label(firm) or name,
                   "website": (_claim_values(firm, "P856") or [None])[0]}
        logo_file = o.get("logo_file") or (_claim_values(firm, "P154") or [None])[0]
        logo_path = IMAGE_DIR / f"{cik}-logo.webp"
        old_profile = profiles.get(cik) or {}
        stale = args.refresh_images or old_profile.get("qid") != qid or not logo_path.exists()
        if stale:
            logo_path.unlink(missing_ok=True)
            data = None
            if logo_file:
                got = _commons_image(logo_file, 256)
                data = got and got[0]
            website = o.get("website") or profile["website"]
            if not data and website:
                data = _site_icon(website)
            if data:
                _save_square(data, logo_path, _LOGO_PX, crop_face=False)
        if logo_path.exists():
            profile["logo"] = f"/assets/managers/{logo_path.name}"

        person_qid, title = person_qids.get(cik, (None, None))
        person = people.get(person_qid) or {}
        if person:
            photo_file = o.get("photo_file") or (_claim_values(person, "P18") or [None])[0]
            entry = {"qid": person_qid, "name": o.get("person_name") or _label(person), "title": title}
            photo_path = IMAGE_DIR / f"{cik}-person.webp"
            old = old_profile.get("person") or {}
            if photo_file and (args.refresh_images or not photo_path.exists() or old.get("qid") != person_qid):
                got = _commons_image(photo_file, 400)
                if got:
                    _save_square(got[0], photo_path, _PERSON_PX, crop_face=True)
                    entry["credit"], entry["license"] = got[1], got[2]
            elif old.get("qid") == person_qid:
                entry["credit"], entry["license"] = old.get("credit"), old.get("license")
            if photo_file and photo_path.exists():
                entry["photo"] = f"/assets/managers/{photo_path.name}"
            profile["person"] = entry
        else:
            (IMAGE_DIR / f"{cik}-person.webp").unlink(missing_ok=True)
        profiles[cik] = profile
        print(f"{cik:>8} {name[:40]:40} -> {qid or '-':10} {profile['display_name'][:28]:28} "
              f"logo={'Y' if profile.get('logo') else '-'} "
              f"person={(profile.get('person') or {}).get('name', '-')[:24]}"
              f"{' (photo)' if (profile.get('person') or {}).get('photo') else ''}")

    PROFILES_PATH.write_text(json.dumps(dict(sorted(profiles.items())), indent=1, ensure_ascii=False),
                             encoding="utf-8")
    missing = [c for c, _n in funds if not (profiles.get(c) or {}).get("logo")]
    print(f"Wrote {len(profiles)} profiles to {PROFILES_PATH}; {len(missing)} without a logo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

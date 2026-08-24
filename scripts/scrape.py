#!/usr/bin/env python3
"""
BIS timetable scraper.

Fetches the three public views of the BIS timetable, parses them into one flat
event list, reconciles them against each other, and writes data/timetable.json.

Usage:
    python3 scripts/scrape.py                 # use config.json
    python3 scripts/scrape.py --t 111         # override term id
    python3 scripts/scrape.py --discover      # find the newest non-empty term
    python3 scripts/scrape.py --local DIR     # parse already-downloaded html

Design notes
------------
Three views of the SAME dataset:
  view=te  teacher grid   -> full teacher names (the only view that has them)
  view=cr  classroom grid -> rooms, short teacher names
  view=sy  grade grid     -> grades, rooms, short teacher names (teacher often absent)

Each is parsed independently into (day, period, subject, grade, room, start)
tuples, then merged. The merge is also the integrity check: an event that shows
up in only one view is reported in the coverage block so the UI can warn.
"""

import argparse, collections, datetime, hashlib, json, os, re, sys, urllib.request

BASE = "https://www.bis.kg/timetable/timetable.php"
VIEWS = ("te", "cr", "sy")
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = "Mozilla/5.0 (compatible; bis-timetable/1.0; +https://github.com/ninenko/bis-timetable)"


# ---------------------------------------------------------------- fetching

def fetch(access, t, view, timeout=60):
    url = "%s?access=%s&t=%s&view=%s" % (BASE, access, t, view)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def page_meta(html):
    """Schoolyear / Term / Section / Review shown at the top of every page."""
    def val(el):
        m = re.search(r'id="%s"[^>]*value="([^"]*)"' % el, html)
        return (m.group(1).strip() if m else "") or None
    rev = re.search(r'Review:</span><span class="col-xs-4">\s*([^<]*)</span>', html)
    return {
        "schoolyear": val("SchoolYear"),
        "term": val("Term"),
        "section": val("Section"),
        "review": rev.group(1).strip() if rev else None,
        "entities": html.count("class_label"),
    }


def discover(access, start=100, stop=140, verbose=True):
    """Probe t ids. Returns list of {t, schoolyear, term, ...} for non-empty ones."""
    found, misses = [], 0
    for t in range(start, stop):
        try:
            m = page_meta(fetch(access, t, "sy", timeout=30))
        except Exception as e:
            if verbose: print("  t=%d error %s" % (t, e), file=sys.stderr)
            continue
        if m["entities"] and m["schoolyear"] and m["schoolyear"] != "2000":
            m["t"] = t
            found.append(m)
            misses = 0
            if verbose: print("  t=%d %s term %s (%d entities)"
                              % (t, m["schoolyear"], m["term"], m["entities"]), file=sys.stderr)
        else:
            misses += 1
            if misses >= 8 and found:
                break
    return found


def newest(terms):
    def rank(m):
        try: return (int(m["schoolyear"] or 0), int(m["term"] or 0), m["t"])
        except ValueError: return (0, 0, m["t"])
    return max(terms, key=rank) if terms else None


# ---------------------------------------------------------------- parsing

def entities(html):
    """Each timetable is a <table> whose first row carries a td.class_label.

    The tables are nested inside one another in the source markup, so scoping
    matters: only rows that are DIRECT siblings of the label row belong to this
    entity. Reading recursively instead makes every table absorb all the ones
    below it (2,315 real rows became 112,453 when we got this wrong)."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for label_td in soup.find_all("td", class_="class_label"):
        label_tr = label_td.parent
        rows = label_tr.parent.find_all("tr", recursive=False)
        header = rows[0].find_all("td", recursive=False)
        days = [c.get_text(" ", strip=True) for c in header[2:]]   # [label][Periods][Mon..Fri]
        recs = []
        for tr in rows[1:]:
            tds = tr.find_all("td", recursive=False)
            if len(tds) < 2:
                continue
            plabel = tds[0].get_text(" ", strip=True)
            for i, cell in enumerate(tds[1:]):
                if i >= len(days):
                    continue
                for div in cell.find_all("div", class_="classes"):
                    txt = div.get_text(" ", strip=True)
                    if txt:
                        recs.append((plabel, days[i], txt))
        out.append({"label": label_td.get_text(" ", strip=True), "recs": recs})
    return out


PERIOD_RE = re.compile(r"^(?:(\d+)(?:st|nd|rd|th)|(Break))\s*(?:\(([^)]*)\))?\s*\|\s*(\d\d:\d\d)-(\d\d:\d\d)$")
TIME_RE = re.compile(r"(\d\d?:\d\d)\s*-\s*(\d\d?:\d\d)")
NAMEISH_RE = re.compile(r"^(Mr|Ms|Mrs|Miss|Dr|Mx)\b|^[A-Z][a-z]+\s+[A-Z]")
TITLE_RE = re.compile(r"^(Mr|Ms|Mrs|Miss|Dr|Mx)\.?\s", re.I)


def parse_period(label):
    m = PERIOD_RE.match(label)
    if not m:
        return None
    num, brk, note, start, end = m.groups()
    return {"period": int(num) if num else None, "break": bool(brk),
            "note": note or "", "start": start, "end": end}


def split_groups(txt):
    """'Subject (a) (b)' -> ('Subject', ['a', 'b']). Tolerates junk."""
    head, buf, groups, depth = "", "", [], 0
    for ch in txt:
        if ch == "(":
            depth += 1
            if depth == 1:
                continue
        if ch == ")":
            depth -= 1
            if depth == 0:
                groups.append(buf.strip()); buf = ""; continue
            if depth < 0:
                depth = 0; continue
        if depth > 0: buf += ch
        else: head += ch
    if buf.strip():
        groups.append(buf.strip())
    return head.strip(), groups


class CellParser:
    """Classifies parenthetical groups using the vocabularies the page gives us:
    room names come from view=cr's headings, grade names from view=sy's."""

    def __init__(self, rooms, grades):
        self.rooms, self.grades = set(rooms), set(grades)
        self.unparsed = collections.Counter()

    def _grade_room(self, g):
        """'Grade 6B-E-Centre 3' -> ('Grade 6B', 'E-Centre 3'). Room names contain
        hyphens, grade names do not, so split at the first hyphen that leaves a
        known grade on the left."""
        if g in self.grades: return g, None
        if g in self.rooms: return None, g
        for i, ch in enumerate(g):
            if ch != "-": continue
            left, right = g[:i].strip(), g[i + 1:].strip()
            if left in self.grades:
                return left, (right or None)
        return None, None

    def parse(self, txt, view):
        head, groups = split_groups(txt)
        ev = {"subject": head or None, "teachers": [], "grade": None,
              "room": None, "start": None, "end": None, "raw": txt}
        leftovers = []
        for g in groups:
            m = TIME_RE.search(g)          # 'Mr. Sergei 09:50-10:20' sub-slot
            if m:
                ev["start"], ev["end"] = m.group(1), m.group(2)
                g = TIME_RE.sub("", g).strip()
                if not g:
                    continue
            if g in self.grades:
                ev["grade"] = g; continue
            if g in self.rooms:
                ev["room"] = g; continue
            grade, room = self._grade_room(g)
            if grade:
                ev["grade"] = grade
                if room: ev["room"] = room
                continue
            if TITLE_RE.match(g) or ("/" in g and re.search(r"\b(Mr|Ms|Mrs)\b", g)):
                ev["teachers"] = [x.strip() for x in g.split("/") if x.strip()]
                continue
            leftovers.append(g)
        for g in leftovers:
            if ev["room"] is None and view in ("te", "sy"):
                ev["room"] = g                       # unlisted room
            elif not ev["teachers"]:
                ev["teachers"] = [x.strip() for x in g.split("/") if x.strip()]
            else:
                self.unparsed[txt] += 1
        return ev


# ---------------------------------------------------------------- reconcile

def norm(x):
    if x is None: return None
    x = x.strip().rstrip("-").strip()
    return x or None


def resolve_short_names(short_names, full_names, learned):
    """Map 'Ms. Anna B' -> 'Anna Balduk'.

    Most names are learned exactly: any lesson that appears in both view=te
    (full name) and view=cr (short name) pins the pair. The rest are co-taught
    lessons where the 1:1 link never occurs, so fall back to matching the
    first name plus, when given, the surname initial."""
    out = dict(learned)
    for s in short_names:
        if s in out:
            continue
        bare = TITLE_RE.sub("", s).strip()
        if not bare:
            continue
        parts = bare.split()
        first, initial = parts[0], (parts[1][0] if len(parts) > 1 else None)
        fl = first.lower()
        cands = [f for f in full_names
                 if f.split()[0].lower() == fl
                 or f.split()[0].lower().startswith(fl)   # 'Alex S' -> 'Alexander ...'
                 or fl.startswith(f.split()[0].lower())]
        if initial:
            narrowed = [f for f in cands
                        if any(w[0].lower() == initial.lower() for w in f.split()[1:])]
            if narrowed:
                cands = narrowed
        if not cands:  # try surname / nickname match anywhere in the full name
            cands = [f for f in full_names
                     if any(w.lower() == bare.lower() for w in f.split())]
        if len(cands) == 1:
            out[s] = cands[0]
    return out


ROW_RE = re.compile(r">((?:\d+(?:st|nd|rd|th)|Break)\s*(?:\([^)]*\))?\s*\|\s*\d\d:\d\d-\d\d:\d\d)<")


def period_rows(html):
    """Every row of the grid, breaks included, in page order and deduplicated."""
    rows, seen = [], set()
    for label in ROW_RE.findall(html):
        label = " ".join(label.split())
        if label in seen:
            continue
        seen.add(label)
        pi = parse_period(label)
        if pi:
            rows.append({"period": pi["period"], "label": label.split("|")[0].strip(),
                         "start": pi["start"], "end": pi["end"],
                         "break": pi["break"], "note": pi["note"]})
    rows.sort(key=lambda r: r["start"])
    return rows


def reconcile(pages):
    """pages: {'te': html, 'cr': html, 'sy': html} -> (events, meta, report)"""
    ents = {v: entities(pages[v]) for v in VIEWS}
    rooms = [e["label"] for e in ents["cr"]]
    grades = [e["label"] for e in ents["sy"]]
    teachers = [e["label"] for e in ents["te"]]
    P = CellParser(rooms, grades)

    raw = []
    for view, own in (("te", "teacher"), ("cr", "room"), ("sy", "grade")):
        for e in ents[view]:
            for plabel, day, txt in e["recs"]:
                pi = parse_period(plabel)
                if not pi or day not in DAYS:
                    continue
                ev = P.parse(txt, view)
                ev.update({"view": view, "day": day, "period": pi["period"],
                           "pstart": pi["start"], "pend": pi["end"],
                           "is_break": pi["break"], "pnote": pi["note"],
                           "own": e["label"], "own_kind": own})
                # each view names one dimension in its heading rather than the cell
                if own == "room": ev["room"] = e["label"]
                elif own == "grade": ev["grade"] = e["label"]
                raw.append(ev)

    def key(r):
        return (r["day"], r["period"], (r["subject"] or "").strip(),
                norm(r["grade"]), norm(r["room"]), r["start"])

    buckets = collections.defaultdict(lambda: collections.defaultdict(list))
    for r in raw:
        buckets[key(r)][r["view"]].append(r)

    learned = collections.defaultdict(collections.Counter)
    for k, v in buckets.items():
        full = sorted({x["own"] for x in v.get("te", [])})
        short = sorted({t for x in v.get("cr", []) + v.get("sy", []) for t in x["teachers"]})
        if len(full) == 1 and len(short) == 1:
            learned[short[0]][full[0]] += 1
    learned = {s: c.most_common(1)[0][0] for s, c in learned.items()}
    all_short = sorted({t for r in raw for t in r["teachers"]})
    short2full = resolve_short_names(all_short, teachers, learned)

    tset = set(teachers)
    events, only_one = [], []
    for k in sorted(buckets, key=lambda k: (DAYS.index(k[0]), k[1] or 0, str(k[2]))):
        v = buckets[k]
        day, period, subject, grade, room, start = k
        sample = (v.get("te") or v.get("cr") or v.get("sy"))[0]
        full = {x["own"] for x in v.get("te", [])}
        short = sorted({t for x in v.get("cr", []) + v.get("sy", []) for t in x["teachers"]})
        mapped = {short2full.get(s) for s in short}
        names = sorted((full | {m for m in mapped if m in tset}) or
                       {s for s in short if TITLE_RE.match(s)})
        ev = {
            "day": day, "period": period,
            "start": start or sample["pstart"], "end": sample["pend"],
            "subject": subject or None, "teachers": names,
            "grade": grade, "room": room,
            "views": "".join(sorted(v.keys())),
        }
        if start:                       # a short activity inside a longer period
            ev["timed"] = True
        events.append(ev)
        if len(v) == 1:
            only_one.append(ev)

    meta = {
        "teachers": sorted(teachers),
        "grades": grades,
        "rooms": sorted(rooms),
        "periods": period_rows(pages["sy"]),
    }
    report = {
        "events": len(events),
        "in_all_three_views": sum(1 for e in events if len(e["views"]) == 6),
        "single_view_only": len(only_one),
        "single_view_examples": only_one[:20],
        "events_without_teacher": sum(1 for e in events if not e["teachers"]),
        "unresolved_short_names": sorted(s for s in all_short
                                         if s not in short2full and NAMEISH_RE.match(s)),
        "unparsed_cells": [{"text": t, "n": n} for t, n in P.unparsed.most_common(50)],
        "counts_per_view": {v: len(ents[v]) for v in VIEWS},
    }
    return events, meta, report


# ---------------------------------------------------------------- main

def load_config():
    path = os.path.join(ROOT, "config.json")
    with open(path) as f:
        return json.load(f), path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--t", type=int, help="term id override")
    ap.add_argument("--access", help="access param override")
    ap.add_argument("--discover", action="store_true", help="probe for the newest term")
    ap.add_argument("--local", help="parse *.html already in this directory")
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "timetable.json"))
    a = ap.parse_args()

    cfg, cfg_path = load_config()
    access = a.access or cfg.get("access", "wip")
    t = a.t or cfg.get("t", 110)
    discovered = cfg.get("terms", [])

    if a.local:
        pages = {v: open(os.path.join(a.local, "raw_%s.html" % v),
                         encoding="utf-8", errors="replace").read() for v in VIEWS}
        pmeta = page_meta(pages["sy"])
    else:
        if a.discover or cfg.get("autoDiscover", True):
            print("Discovering terms...", file=sys.stderr)
            found = discover(access, cfg.get("probeFrom", 100), cfg.get("probeTo", 140))
            if found:
                discovered = [{k: m[k] for k in ("t", "schoolyear", "term", "section", "review")}
                              for m in found]
                if cfg.get("pinTerm"):
                    print("Term pinned to t=%s by config." % t, file=sys.stderr)
                else:
                    t = newest(found)["t"]
        print("Fetching access=%s t=%s ..." % (access, t), file=sys.stderr)
        pages = {v: fetch(access, t, v) for v in VIEWS}
        pmeta = page_meta(pages["sy"])

    if not pmeta.get("entities"):
        raise SystemExit("t=%s returned an empty timetable - refusing to overwrite good data." % t)

    events, meta, report = reconcile(pages)
    if not events:
        raise SystemExit("Parsed zero events - refusing to overwrite good data.")

    payload = {
        "fetched_at": datetime.datetime.now(datetime.timezone.utc)
                              .replace(microsecond=0).isoformat(),
        "source": {"access": access, "t": t,
                   "url": "%s?access=%s&t=%s&view=" % (BASE, access, t)},
        "term": {k: pmeta[k] for k in ("schoolyear", "term", "section", "review")},
        "terms_available": discovered,
        "meta": meta,
        "report": report,
        "events": events,
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    # Content hash ignores the timestamp, so an unchanged timetable is a no-op commit.
    stable = json.dumps({k: payload[k] for k in ("events", "meta", "term", "source")},
                        ensure_ascii=False, sort_keys=True)
    payload["content_hash"] = hashlib.sha256(stable.encode()).hexdigest()[:16]
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    # keep config.json in step with what we actually scraped
    cfg["t"] = t
    cfg["access"] = access
    cfg["terms"] = discovered
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

    print(json.dumps({k: report[k] for k in
                      ("events", "in_all_three_views", "single_view_only",
                       "events_without_teacher", "unresolved_short_names")},
                     indent=2)[:2000])
    print("wrote %s (%.0f KB)" % (a.out, os.path.getsize(a.out) / 1024))


if __name__ == "__main__":
    main()

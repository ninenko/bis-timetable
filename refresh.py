#!/usr/bin/env python3
"""One-command refresh of the BIS timetable.

    python refresh.py            scrape bis.kg, rebuild page, print what changed
    python refresh.py --log      same + append an entry to ../CHANGELOG.md
    python refresh.py --check    only report whether the stored data is stale (no writes)

Steps: back up data/ + html to backup/ -> scripts/scrape.py --discover (data/timetable.json)
-> bis_timetable.py (bis-timetable.html) -> diff summary vs the backup.
Then push the folder (or run the GitHub workflow) to update ninenko.github.io/bis-timetable.
"""
import argparse, collections, datetime, json, os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "timetable.json")
HTML = os.path.join(HERE, "bis-timetable.html")
LOG = os.path.join(HERE, "..", "CHANGELOG.md")
py = sys.executable


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def key(e):
    return json.dumps(e, sort_keys=True, ensure_ascii=False)


def diff(old, new):
    so, sn = {key(e) for e in old["events"]}, {key(e) for e in new["events"]}
    add = [json.loads(k) for k in sn - so]
    rem = [json.loads(k) for k in so - sn]
    top = lambda l, f: ", ".join(f"{k} {v}" for k, v in collections.Counter(e.get(f) for e in l).most_common(5))
    out = [f"Lessons {len(old['events'])} -> {len(new['events'])} (+{len(add)} / -{len(rem)})",
           f"Term {new['term']['schoolyear']} T{new['term']['term']} (t={new['source']['t']}); "
           f"fetched {new['fetched_at']}"]
    if add or rem:
        out += [f"Added by grade: {top(add,'grade')}", f"Removed by grade: {top(rem,'grade')}"]
    for k in ("rooms", "teachers"):
        a = sorted(set(new["meta"][k]) - set(old["meta"][k]))
        g = sorted(set(old["meta"][k]) - set(new["meta"][k]))
        if a or g:
            out.append(f"{k}: new {a or '-'}; gone {g or '-'}")
    r = new["report"]
    out.append(f"Quality: {r['in_all_three_views']}/{r['events']} in all 3 views; "
               f"no teacher {r['events_without_teacher']}; ambiguous {r['unresolved_short_names']}")
    return "\n".join(out), bool(add or rem)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", action="store_true", help="append CHANGELOG entry")
    ap.add_argument("--check", action="store_true", help="report staleness only, write nothing")
    a = ap.parse_args()
    old = load(DATA)
    run = lambda *c: subprocess.run([py, *c], cwd=HERE, check=True)

    if a.check:
        tmp = os.path.join(tempfile.mkdtemp(), "new.json")
        run("scripts/scrape.py", "--discover", "--out", tmp)
        s, changed = diff(old, load(tmp))
        print(("STALE\n" if changed else "UP TO DATE\n") + s)
        return

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    bk = os.path.join(HERE, "backup", stamp)
    os.makedirs(bk, exist_ok=True)
    shutil.copy2(DATA, bk); shutil.copy2(HTML, bk)

    run("scripts/scrape.py", "--discover")
    run("bis_timetable.py")
    new = load(DATA)
    s, changed = diff(old, new)
    print("\n" + ("UPDATED\n" if changed else "NO CHANGE (rebuilt anyway)\n") + s)
    print(f"Backup: backup/{stamp}")
    if a.log:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"\n## {datetime.date.today()} — Timetable app refreshed from bis.kg\n\n"
                    + s.replace("\n", "  \n") + "\n\nGitHub Pages copy not updated until the folder is pushed.\n")
        print("Logged to CHANGELOG.md")


if __name__ == "__main__":
    main()

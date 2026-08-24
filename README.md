# BIS Timetable

One searchable page for the BIS weekly timetable. Type a teacher, grade or room
and get their week; click anything in a cell to pivot to it; find slots where
several people are free, or rooms that are empty.

**Live:** https://ninenko.github.io/bis-timetable/

## Where the data comes from

The school publishes the same timetable three ways — by teacher, by classroom,
by grade — at `bis.kg/timetable/timetable.php`. Each view leaves something out:

| View | Heading | Cells contain | Leaves out |
|---|---|---|---|
| `te` | teacher | subject, grade, room | lessons with no teacher assigned |
| `cr` | room | subject, teacher, grade | anything with no room |
| `sy` | grade | subject, room, teacher (often absent) | non-class events |

`scripts/scrape.py` parses all three and reconciles them against each other, so
agreement between the views is the quality check. The **Data** tab reports it:
at the last check 1,668 of 1,678 lessons were confirmed in all three views.

Teacher names are another reconciliation: only the teacher view has full names
("Alexander Sheyerman"), the others use short forms ("Mr. Alex S"). Any lesson
appearing in both views pins the pair; the rest are matched on first name plus
surname initial. Anything still ambiguous ("Ms Maria" — there are two) is left
as written and listed on the Data tab.

## How it refreshes

`.github/workflows/refresh.yml` runs three times a day, and on demand. It
rescrapes, and commits `data/timetable.json` **only if the timetable actually
changed** — so the git history of that file is a record of when the timetable
moved.

Terms are identified by a `t=` id in the URL (`t=110` is 2026 Term 1). The
scraper probes for all valid ids and follows the newest one automatically, so
when the school publishes the next term the site moves to it without anyone
touching anything. That is the single most important safety property here: a
scraper silently serving last term's data while looking fresh is the worst
failure mode this tool has.

Three ways to refresh:

1. **Automatically** — the schedule above. Nothing to do.
2. **From GitHub** — Actions tab → *Refresh timetable* → *Run workflow*.
3. **From the page** — the ⚙ admin panel (password `bis-admin`).

### Setting up the in-page Refresh button

Needed once, and only if you want option 3.

1. github.com → Settings → Developer settings → **Personal access tokens** →
   Fine-grained tokens → **Generate new token**.
2. Repository access: **Only select repositories** → `bis-timetable`.
3. Permissions → Repository permissions → **Actions: Read and write**.
4. Generate, copy, and paste it into the admin panel → *Save token*.

The token is kept in your browser's `localStorage` on that one machine. It is
never committed and never visible to anyone else opening the page. It can do
exactly one thing: start a workflow in this repo.

### Changing the password

`config.json` → `adminHash` is a SHA-256 of the password. To change it:

```bash
python3 -c "import hashlib,sys;print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" 'new-password'
```

The password only hides the panel from casual visitors — the page is public and
its source is readable. Real protection is the token, which only you have.

## Local development

```bash
pip install beautifulsoup4
python3 scripts/scrape.py            # fetch + parse + write data/timetable.json
python3 -m http.server 8099          # then open http://localhost:8099
```

Useful flags: `--t 111` (a specific term), `--discover` (list terms found),
`--local DIR` (parse `raw_te.html` / `raw_cr.html` / `raw_sy.html` already on disk).

## Files

```
index.html                    the whole front-end, no dependencies
config.json                   term, access param, admin hash, repo name
data/timetable.json           the parsed timetable (~300 KB)
scripts/scrape.py             fetch, parse, reconcile
.github/workflows/refresh.yml scheduled + manual refresh
```

## A note on what is published here

The source pages are public and unauthenticated, but this repo turns them into
clean, indexable, machine-readable staff location data. That is a different kind
of exposure from a PHP page nobody can parse. If the school would rather it not
be indexed, the fixes are cheap — make the repo private, or add a `robots.txt`
and a `<meta name="robots" content="noindex">`.

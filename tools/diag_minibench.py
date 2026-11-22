"""One-off: past MiniBench rounds' bot standings. Prints only the bot's own rows and counts."""
import json, os, sys, time, urllib.request, urllib.error
sys.path.insert(0, ".")
import standings

BASE = "https://www.metaculus.com/api"
ME = "midnightsharp-bot"
H = {"Authorization": f"Token {os.environ.get('METACULUS_TOKEN','')}", "Accept": "application/json",
     "User-Agent": "forecast-bot-diag"}
# (URL id, second column id) pairs from old precheck tables
PAIRS = [(46181, 46410), (46182, 46411), (46375, 46608), (46382, 46615), (46399, 46632),
         (46412, 46645), (46421, 46654), (46427, 46660)]


def get(path):
    time.sleep(3)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(BASE + path, headers=H), timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(15 * 2 ** attempt); continue
            print(f"  HTTP {e.code} {path}"); return None
        except Exception as e:
            print(f"  ERR {e.__class__.__name__} {path}"); return None


def pj(p):
    if not isinstance(p, dict):
        return None
    return {k: p.get(k) for k in ("id", "slug", "name", "type", "start_date", "close_date") if k in p}


projects = {}
for url_id, other in PAIRS:
    d = get(f"/posts/{url_id}/")
    which = url_id
    if not d:
        d = get(f"/posts/{other}/"); which = other
    if not d:
        print(f"post {url_id}/{other}: not found"); continue
    pr = d.get("projects") or {}
    qid = (d.get("question") or {}).get("id")
    print(f"post {d.get('id')} (tried {which}) question={qid} status={d.get('status')}")
    for k in ("default_project", "question_series", "tournament"):
        v = pr.get(k)
        for it in (v if isinstance(v, list) else [v]):
            if isinstance(it, dict):
                print(f"   {k}: {pj(it)}")
                if k in ("default_project", "question_series", "tournament") and it.get("id"):
                    projects.setdefault(it["id"], pj(it))

for pid, info in projects.items():
    print(f"== project {pid} {info}")
    d = get(f"/leaderboards/project/{pid}/")
    if d is None:
        continue
    boards = d if isinstance(d, list) else d.get("leaderboards") or [d]
    for b in boards:
        if not isinstance(b, dict):
            continue
        ents = b.get("entries") or []
        scal = {k: v for k, v in b.items() if k in ("score_type", "is_primary", "finalized", "prize_pool", "name")}
        print(f"  board {scal} entries={len(ents)}")
        mine = [e for e in ents if ((e.get("user") or {}).get("username") or e.get("username")) == ME]
        for e in mine:
            print("   mine:", {k: v for k, v in e.items() if k not in ("user",)})
        try:
            f = standings.non_metac_standing(standings.parse_entries(b), ME)
            print("   non_metac:", None if f is None else (f[0], f[1]))
        except Exception as ex:
            print("   parse err", ex)

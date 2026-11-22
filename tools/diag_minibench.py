"""One-off diagnostic: where does MiniBench's leaderboard live? Prints structure only."""
import json, os, time, urllib.request, urllib.error

BASE = "https://www.metaculus.com/api"
ME = "midnightsharp-bot"
H = {"Authorization": f"Token {os.environ.get('METACULUS_TOKEN','')}", "Accept": "application/json",
     "User-Agent": "forecast-bot-diag"}


def get(path):
    time.sleep(3)
    for attempt in range(4):
        try:
            req = urllib.request.Request(BASE + path, headers=H)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(15 * 2 ** attempt)
                continue
            print(f"  HTTP {e.code} for {path}")
            return None
        except Exception as e:
            print(f"  ERR {e.__class__.__name__} for {path}")
            return None


def pj(p):
    if not isinstance(p, dict):
        return repr(type(p).__name__)
    return {k: p.get(k) for k in ("id", "slug", "name", "type", "start_date", "close_date", "default_permission") if k in p}


def board_report(pid):
    d = get(f"/leaderboards/project/{pid}/")
    if d is None:
        return
    boards = d if isinstance(d, list) else [d]
    print(f"  leaderboard resp top={type(d).__name__} boards={len(boards)}")
    for b in boards:
        if not isinstance(b, dict):
            print("   non-dict board"); continue
        ents = b.get("entries") or []
        scal = {k: v for k, v in b.items() if not isinstance(v, (list, dict))}
        print(f"   board scalars={scal} entries={len(ents)}")
        for e in ents:
            u = (e.get("user") or {}).get("username") or e.get("username")
            if u == ME:
                print(f"    {ME}: rank={e.get('rank')} score={e.get('score')}")


print("== 1. minibench project")
p = get("/projects/tournaments/minibench/")
if p:
    print("keys:", sorted(p))
    print(pj(p))
    for k, v in p.items():
        if isinstance(v, list):
            print(f" list field {k}: len={len(v)}", [pj(x) for x in v[:5]] if v and isinstance(v[0], dict) else "")
    board_report(p["id"])
all_ids = {}
if p:
    all_ids[p["id"]] = p.get("slug")

print("== 2. recent MiniBench posts")
posts = None
for q in ("tournaments=minibench&statuses=resolved&order_by=-actual_close_time&limit=3",
          "tournaments=minibench&statuses=closed&limit=3",
          "tournaments=minibench&limit=3"):
    d = get("/posts/?" + q)
    if isinstance(d, dict) and d.get("results"):
        posts = d["results"]; print("query ok:", q); break
for po in posts or []:
    pr = po.get("projects") or {}
    print(f" post {po.get('id')} status={po.get('status')} projects keys={sorted(pr)}")
    for k, v in pr.items():
        items = v if isinstance(v, list) else [v]
        for it in items:
            if isinstance(it, dict):
                print(f"   {k}: {pj(it)}")
                if k in ("tournament", "default_project", "tournaments") and it.get("id") is not None:
                    all_ids[it["id"]] = it.get("slug")

print("== 3. leaderboards of each project id seen")
for pid, slug in all_ids.items():
    print(f" project {pid} slug={slug}")
    board_report(pid)

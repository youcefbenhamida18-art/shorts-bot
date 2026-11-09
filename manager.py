Enter#!/usr/bin/env python3
"""
Channel Manager - looks after the channel(s) on a schedule:
  * replies to new comments (short, friendly, never spammy)
  * finds videos that stopped getting views and tries better titles (keeps the best one)
  * scans what is working in your niche and writes trends.json for the video makers
  * TASK=branding: writes the channel description/keywords and uploads a new banner (run it by hand)

MODE=publish makes real changes. Any other mode (dry / private) only PRINTS what it would do.
Channels: YT_REFRESH_TOKEN (main) and, if present, YT_REFRESH_TOKEN_KIDS (kids channel).
Needs main.py in the same folder (it reuses its Gemini + YouTube helpers).
"""
import datetime as dt
import json
import os
import random
import sys
import time
import traceback
from pathlib import Path

import main as core

env = core.env
LIVE = core.MODE == "publish"
TASK = env("TASK", "all")  # all | branding
ROOT = Path(__file__).resolve().parent
STATE, TRENDS, OUT = ROOT / "manager_state.json", ROOT / "trends.json", ROOT / "out"
NOW = dt.datetime.now(dt.timezone.utc)
TODAY = NOW.date()


def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def parse_dt(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def iso_days_ago(d):
    return (NOW - dt.timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")


def say(msg):
    print(("  " if LIVE else "  [dry] ") + msg)


# ----------------------------------------------------------------- comments
def do_comments(yt, cid, st, name):
    cap = int(env("REPLY_MAX", "20") or 20)
    try:
        r = yt.commentThreads().list(part="snippet,replies", allThreadsRelatedToChannelId=cid, order="time",
                                     maxResults=50, textFormat="plainText").execute()
    except Exception as e:
        print("  comments: nothing to read:", repr(e)[:140])
        return
    replied = st.setdefault("replied", [])
    done, new_ids = set(replied), []
    fresh, cutoff = [], NOW - dt.timedelta(days=4)
    for t in r.get("items", []):
        cs = t["snippet"]["topLevelComment"]["snippet"]
        if t["id"] in done or cs.get("authorChannelId", {}).get("value") == cid:
            continue
        if parse_dt(cs["publishedAt"]) < cutoff:
            continue
        if any(c["snippet"].get("authorChannelId", {}).get("value") == cid
               for c in t.get("replies", {}).get("comments", [])):
            new_ids.append(t["id"])
            continue
        fresh.append({"id": t["id"], "text": cs.get("textDisplay", "")[:400]})
    print(f"  comments: {len(fresh)} new")
    if not fresh:
        return
    ans = core.gemini_json(f"""You manage the YouTube channel "{name}". Decide what to do with each new comment.
Actions: "reply" (genuine praise, a question, or friendly feedback), "skip" (rude, political, sensitive, medical, personal data, off-topic, or no good reply exists), "spam" (ads, links, scams, bots).
Replies: same language as the comment, 1-2 short sentences, warm and natural, no links, no promises, at most one emoji, never argue, never give medical, legal or financial advice.
Return ONLY JSON: {{"items":[{{"id":"...","action":"reply|skip|spam","reply":"..."}}]}}
COMMENTS: {json.dumps(fresh, ensure_ascii=False)}""")
    sent = 0
    for it in ans.get("items", []):
        cid_c, act = it.get("id"), it.get("action")
        if cid_c not in {f["id"] for f in fresh}:
            continue
        if act == "reply" and it.get("reply", "").strip() and sent < cap:
            text = it["reply"].strip()[:500]
            say(f"reply to {cid_c[:10]}...: {text[:80]}")
            if LIVE:
                try:
                    yt.comments().insert(part="snippet", body={"snippet": {"parentId": cid_c, "textOriginal": text}}).execute()
                except Exception as e:
                    print("  could not reply:", repr(e)[:140])
                    if "quota" in str(e).lower():
                        break
                    continue
                time.sleep(random.uniform(6, 18))
            sent += 1
        elif act == "spam" and env("AUTO_MODERATE") == "1":
            say(f"hide spam comment {cid_c[:10]}...")
            if LIVE:
                try:
                    yt.comments().setModerationStatus(id=cid_c, moderationStatus="rejected").execute()
                except Exception as e:
                    print("  could not hide:", repr(e)[:120])
        new_ids.append(cid_c)
    st["replied"] = (replied + new_ids)[-3000:]


# ------------------------------------------------------------------- revive
def list_videos(yt, limit=100):
    ch = yt.channels().list(part="contentDetails", mine=True).execute()["items"][0]
    pl = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    ids, tok = [], None
    while len(ids) < limit:
        r = yt.playlistItems().list(part="contentDetails", playlistId=pl, maxResults=50, pageToken=tok).execute()
        ids += [i["contentDetails"]["videoId"] for i in r.get("items", [])]
        tok = r.get("nextPageToken")
        if not tok:
            break
    out = []
    for i in range(0, len(ids), 50):
        out += yt.videos().list(part="snippet,statistics,status", id=",".join(ids[i:i + 50])).execute().get("items", [])
    return out


def rate(snaps, days):
    """Views per day over roughly the last `days` days, from our own daily snapshots."""
    if len(snaps) < 2:
        return None
    last_d, last_v = snaps[-1]
    target = dt.date.fromisoformat(last_d) - dt.timedelta(days=days)
    base = snaps[0]
    for d, v in snaps:
        if dt.date.fromisoformat(d) <= target:
            base = [d, v]
    span = (dt.date.fromisoformat(last_d) - dt.date.fromisoformat(base[0])).days
    return (last_v - base[1]) / span if span >= 1 else None


def set_title(yt, v, title):
    sn = v["snippet"]
    body = {"id": v["id"], "snippet": {"title": title[:95], "description": sn.get("description", ""),
                                       "categoryId": sn["categoryId"]}}
    if sn.get("tags"):
        body["snippet"]["tags"] = sn["tags"]
    if sn.get("defaultLanguage"):
        body["snippet"]["defaultLanguage"] = sn["defaultLanguage"]
    yt.videos().update(part="snippet", body=body).execute()


def do_revive(yt, cid, st):
    vids = st.setdefault("videos", {})
    items = [v for v in list_videos(yt) if v["status"]["privacyStatus"] == "public"]
    for v in items:  # daily snapshot of the view counts
        e = vids.setdefault(v["id"], {"snaps": [], "changes": []})
        views = int(v["statistics"].get("viewCount", 0))
        if not e["snaps"] or e["snaps"][-1][0] != str(TODAY):
            e["snaps"] = (e["snaps"] + [[str(TODAY), views]])[-30:]
    rates = {}
    for v in items:
        age = (NOW - parse_dt(v["snippet"]["publishedAt"])).days
        r3 = rate(vids[v["id"]]["snaps"], 3)
        if age >= 4 and r3 is not None:
            rates[v["id"]] = r3
    if not rates:
        print("  revive: not enough history yet (needs a few days of snapshots)")
        return
    ordered = sorted(rates.values())
    median = ordered[len(ordered) // 2]
    print(f"  revive: {len(rates)} videos tracked, median {median:.1f} views/day")
    budget = int(env("REVIVE_MAX", "3") or 3)
    for v in sorted(items, key=lambda x: rates.get(x["id"], 1e9)):
        e, vid = vids[v["id"]], v["id"]
        if vid not in rates:
            continue
        age = (NOW - parse_dt(v["snippet"]["publishedAt"])).days
        last = e["changes"][-1] if e["changes"] else None
        if last and not last.get("result"):  # judge the previous change after 3 days
            days = (TODAY - dt.date.fromisoformat(last["date"])).days
            if days < 3:
                continue
            after = rate(e["snaps"], days)
            if after is not None and last.get("before") is not None and after < last["before"]:
                say(f"revert title of {vid} (it did not help: {after:.1f} < {last['before']:.1f} views/day)")
                if LIVE:
                    try:
                        set_title(yt, v, last["old"])
                    except Exception as ex:
                        print("  could not revert:", repr(ex)[:120])
                last["result"] = "reverted"
            else:
                last["result"] = "kept"
            continue
        weak = rates[vid] < 0.5 * median or (rates[vid] < 5 and age > 7)
        if not weak or age > 90 or len(e["changes"]) >= 3 or budget <= 0:
            continue
        if last and (TODAY - dt.date.fromisoformat(last["date"])).days < 4:
            continue
        tried = [c["new"] for c in e["changes"]] + [v["snippet"]["title"]]
        r = core.gemini_json(f"""A YouTube video is getting few views. Write 3 alternative titles in the same language as the current title.
Rules: honest (the video must really deliver what the title promises), curiosity-driven, clear, max 70 characters, no clickbait lies, no ALL CAPS, no quotation marks.
Current title: {v['snippet']['title']}
Description: {v['snippet'].get('description', '')[:300]}
Already tried: {json.dumps(tried, ensure_ascii=False)}
Return ONLY JSON: {{"titles":["...","...","..."]}}""")
        new = next((t.strip() for t in r.get("titles", []) if isinstance(t, str) and t.strip() and t.strip() not in tried), None)
        if not new:
            continue
        say(f"new title for {vid}: '{v['snippet']['title']}' -> '{new}' ({rates[vid]:.1f} views/day)")
        if LIVE:
            try:
                set_title(yt, v, new)
            except Exception as ex:
                print("  could not update:", repr(ex)[:140])
                continue
        e["changes"].append({"date": str(TODAY), "old": v["snippet"]["title"], "new": new, "before": rates[vid]})
        budget -= 1


# ------------------------------------------------------------------- trends
def do_trends(yt, st):
    if st.get("trends_date") == str(TODAY):
        print("  trends: already scanned today")
        return
    qs = [q.strip() for q in env("TREND_QUERIES", "bedtime stories for kids|kids cartoon story|"
                                 "learn colors and numbers for kids|nursery rhymes").split("|") if q.strip()][:5]
    ids = []
    for q in qs:
        try:
            r = yt.search().list(part="id", q=q, type="video", order="viewCount", publishedAfter=iso_days_ago(21),
                                 maxResults=12, safeSearch="strict").execute()
            ids += [i["id"]["videoId"] for i in r.get("items", []) if i.get("id", {}).get("videoId")]
        except Exception as e:
            print("  trends search failed:", repr(e)[:120])
    ids = list(dict.fromkeys(ids))[:50]
    if not ids:
        return
    rows = []
    for v in yt.videos().list(part="snippet,statistics", id=",".join(ids)).execute().get("items", []):
        age = max((NOW - parse_dt(v["snippet"]["publishedAt"])).days, 1)
        views = int(v["statistics"].get("viewCount", 0))
        rows.append((views / age, views, v["snippet"]["title"]))
    rows.sort(reverse=True)
    top = [{"title": t, "views": vw, "per_day": round(pd)} for pd, vw, t in rows[:15]]
    brief = core.gemini_json(f"""These videos are growing fastest in our niche over the last 3 weeks (title, total views, views per day):
{json.dumps(top, ensure_ascii=False)}
Find what they have in common (topic, hook, title pattern, length). Then propose 10 ORIGINAL video ideas inspired by those patterns.
Never copy titles, characters or brands. Ideas must be gentle, honest and safe for the audience.
Return ONLY JSON: {{"patterns":["..."],"ideas":[{{"topic":"...","why":"..."}}]}}""")
    TRENDS.write_text(json.dumps({"date": str(TODAY), "top": top, "patterns": brief.get("patterns", []),
                                  "ideas": brief.get("ideas", [])}, ensure_ascii=False, indent=1), encoding="utf-8")
    st["trends_date"] = str(TODAY)
    print(f"  trends: saved {len(top)} reference videos and {len(brief.get('ideas', []))} ideas")


# ----------------------------------------------------------------- branding
def make_banner(title, tagline, path):
    from PIL import Image, ImageDraw
    w, h = 2560, 1440
    c1, c2 = [tuple(int(x.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
              for x in env("BANNER_COLORS", "#ffd166,#6ec6ff").split(",")[:2]]
    im = Image.new("RGB", (w, h))
    px = im.load()
    for y in range(h):  # vertical gradient
        t = y / (h - 1)
        row = tuple(int(c1[k] + (c2[k] - c1[k]) * t) for k in range(3))
        for x in range(w):
            px[x, y] = row
    d = ImageDraw.Draw(im, "RGBA")
    rnd = random.Random(title)
    for _ in range(26):  # soft decorative circles outside the safe area
        r = rnd.randint(40, 150)
        x, y = rnd.choice([rnd.randint(0, 520), rnd.randint(w - 520, w)]), rnd.randint(0, h)
        d.ellipse((x - r, y - r, x + r, y + r), fill=(255, 255, 255, rnd.randint(40, 90)))
    lum = 0.299 * c1[0] + 0.587 * c1[1] + 0.114 * c1[2]
    ink = (27, 27, 58) if lum > 140 else (255, 255, 255)

    def font(size):
        from PIL import ImageFont
        for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVuSans-Bold.ttf"):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
        return ImageFont.load_default()

    size = 170
    while size > 60 and d.textlength(title, font=font(size)) > 1450:  # keep inside the 1546px safe area
        size -= 10
    d.text((w / 2, h / 2 - 30), title, font=font(size), fill=ink, anchor="ms")
    if tagline:
        d.text((w / 2, h / 2 + 80), tagline, font=font(66), fill=ink, anchor="mm")
    im.save(path, "PNG")


def do_branding(yt, cid):
    from googleapiclient.http import MediaFileUpload
    info = yt.channels().list(part="brandingSettings,snippet", mine=True).execute()["items"][0]
    title = info["snippet"]["title"]
    about = env("CHANNEL_ABOUT", "gentle illustrated stories for children")
    tagline = env("CHANNEL_TAGLINE", "")
    r = core.gemini_json(f"""Write the About text of a YouTube channel called "{title}" about: {about}.
Language: {env('CHANNEL_LANG', 'English')}. Warm, clear, honest, max 700 characters, no hype, no false promises.
Also give 10 keywords. Return ONLY JSON: {{"description":"...","keywords":["..."]}}""")
    desc = r.get("description", "").strip()
    kw = " ".join(f'"{k}"' if " " in k else k for k in r.get("keywords", []) if isinstance(k, str))
    OUT.mkdir(exist_ok=True)
    banner = OUT / "banner.png"
    make_banner(title, tagline, banner)
    say(f"set channel description ({len(desc)} chars), keywords and a new banner")
    if not LIVE:
        print("  banner preview saved:", banner)
        return
    bs = info.get("brandingSettings", {})
    ch, im = bs.setdefault("channel", {}), bs.setdefault("image", {})
    up = yt.channelBanners().insert(media_body=MediaFileUpload(str(banner), mimetype="image/png")).execute()
    im["bannerExternalUrl"] = up["url"]
    if desc:
        ch["description"] = desc
    if kw:
        ch["keywords"] = kw
    yt.channels().update(part="brandingSettings", body={"id": cid, "brandingSettings": bs}).execute()
    print("  branding updated (channel name and profile picture must be changed by hand in the YouTube app)")


# --------------------------------------------------------------------- main
def main():
    toks = [("main", env("YT_REFRESH_TOKEN")), ("kids", env("YT_REFRESH_TOKEN_KIDS"))]
    toks = [(n, t) for n, t in toks if t]
    if not toks:
        sys.exit("no YT_REFRESH_TOKEN secret found")
    state = load_state()
    failed = 0
    for label, tok in toks:
        os.environ["YT_REFRESH_TOKEN"] = tok
        print(f"\n=== channel: {label} (mode={core.MODE}, task={TASK})")
        try:
            yt = core.youtube()
            ch = yt.channels().list(part="id,snippet", mine=True).execute()["items"][0]
            cid, name = ch["id"], ch["snippet"]["title"]
            st = state.setdefault(cid, {})
            if TASK == "branding":
                do_branding(yt, cid)
                continue
            for fn in (lambda: do_trends(yt, state), lambda: do_comments(yt, cid, st, name), lambda: do_revive(yt, cid, st)):
                try:
                    fn()
                except Exception:
                    traceback.print_exc()
        except Exception:
            failed += 1
            traceback.print_exc()
    if core.MODE != "dry":
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    if failed == len(toks):
        sys.exit(1)


if __name__ == "__main__":
    main()

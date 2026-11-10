#!/usr/bin/env python3
"""
Shorts Factory
Makes N vertical videos per run and uploads / schedules them on YouTube.

Flow: ideas (Gemini) -> script (Gemini) -> voice (edge-tts) -> stock footage (Pixabay)
      -> captions (ASS) -> render (FFmpeg) -> upload + schedule (YouTube Data API)

All settings come from environment variables (see .github/workflows/daily.yml).
MODE:  dry      = make videos only, no upload (test)
       private  = upload as private (test the YouTube link)
       now      = upload as public immediately
       publish  = upload private + schedule public at spread-out times (default)
"""
import asyncio
import datetime as dt
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

import requests


def env(name, default=""):
    return (os.environ.get(name) or default).strip()


NICHE = env("NICHE", "mind-blowing facts about space, the ocean, animals and nature")
VIDEO_LANG = env("VIDEO_LANG", "en")  # "en" or "ar"
COUNT = int(env("VIDEOS_PER_DAY", "5") or 5)
MODE = env("MODE", "publish")
PUBLISH_HOURS = env("PUBLISH_HOURS_UTC", "9,12,15,18,21")
CATEGORY = env("CATEGORY_ID", "27")  # 27 Education, 28 Science & Tech, 24 Entertainment
DISCLOSE_AI = env("DISCLOSE_AI", "1") == "1"  # marks the video as containing synthetic media

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "work"
OUT = ROOT / "out"
HISTORY = ROOT / "history.json"
MUSIC_DIR = ROOT / "music"
W, H, FPS = 1080, 1920, 30

LANGS = {
    "en": {
        "name": "English",
        "voices": ["en-US-AndrewNeural", "en-US-GuyNeural", "en-US-ChristopherNeural"],
        "upper": True,
    },
    "ar": {
        "name": "Modern Standard Arabic (simple, clear, easy to understand)",
        "voices": ["ar-SA-HamedNeural", "ar-EG-ShakirNeural"],
        "upper": False,
    },
}

FORMATS = [
    "Surprising fact with a twist ending",
    "Top 3 mind-blowing facts",
    "Myth vs reality",
    "What would happen if...",
    "Mini story with an unexpected ending",
    "Scale comparison (small things that are actually enormous)",
    "The most extreme example of something",
]


class QuotaStop(Exception):
    pass


# ----------------------------------------------------------------- helpers
def run(cmd, cwd=None):
    p = subprocess.run([str(c) for c in cmd], cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("command failed: " + " ".join(map(str, cmd))[:200] + "\n" + p.stderr[-1500:])
    return p.stdout


def duration(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nw=1:nk=1", path])
    return float(out.strip())


# ------------------------------------------------------------------ Gemini
GEMINI = "https://generativelanguage.googleapis.com/v1beta"
_models = {"list": None, "good": None}


def gemini_models():
    """Model names change often, so we ask the API which ones exist."""
    if env("GEMINI_MODEL"):
        return [env("GEMINI_MODEL")]
    if _models["list"] is None:
        r = requests.get(f"{GEMINI}/models", params={"pageSize": 200},
                         headers={"x-goog-api-key": env("GEMINI_API_KEY")}, timeout=30)
        r.raise_for_status()
        names = []
        for m in r.json().get("models", []):
            n = m.get("name", "").split("/")[-1]
            if "generateContent" not in m.get("supportedGenerationMethods", []):
                continue
            if "flash" not in n:
                continue
            if any(x in n for x in ("image", "tts", "live", "audio", "embed", "exp", "robotics", "computer")):
                continue
            names.append(n)

        def rank(n):
            v = re.search(r"(\d+(?:\.\d+)?)", n)
            return ("lite" in n, "preview" in n, -(float(v.group(1)) if v else 0.0))

        _models["list"] = sorted(set(names), key=rank)
        print("  gemini models:", _models["list"][:6])
    return _models["list"]


def parse_json(text):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise
        return json.loads(m.group(0))


def _gemini_only(prompt):
    models = gemini_models()
    if _models["good"] in models:
        models = [_models["good"]] + [m for m in models if m != _models["good"]]
    last = "no model available"
    for model in models[:4]:
        for attempt in range(2):
            try:
                r = requests.post(
                    f"{GEMINI}/models/{model}:generateContent",
                    headers={"x-goog-api-key": env("GEMINI_API_KEY"), "Content-Type": "application/json"},
                    json={"contents": [{"parts": [{"text": prompt}]}],
                          "generationConfig": {"temperature": 1.0,
                                               "responseMimeType": "application/json",
                                               "maxOutputTokens": 8192}},
                    timeout=120)
                if r.status_code == 429:
                    kind = "daily quota used up" if "PerDay" in r.text else "per-minute limit"
                    last = f"{model}: rate limited ({kind}) {r.text[:160]}"
                    if "limit: 0" in r.text or "PerDay" in r.text:  # not in the free tier, or today's quota is gone
                        break
                    time.sleep(15)
                    continue
                if r.status_code >= 400:
                    last = f"{model}: HTTP {r.status_code} {r.text[:160]}"
                    break
                data = r.json()
                text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
                obj = parse_json(text)
                _models["good"] = model
                time.sleep(6)  # stay under free-tier requests/minute
                return obj
            except Exception as e:  # bad JSON, network, blocked answer...
                last = f"{model}: {e!r}"[:200]
                time.sleep(3)
    raise RuntimeError("Gemini failed: " + last)


CF_TEXT_MODELS = ["@cf/meta/llama-3.1-8b-instruct", "@cf/meta/llama-3.3-70b-instruct-fp8-fast"]


def cf_llm_json(prompt):
    """Backup writer on the free Cloudflare account when Gemini is rate limited."""
    base = f"https://api.cloudflare.com/client/v4/accounts/{env('CF_ACCOUNT_ID')}/ai"
    hdr = {"Authorization": f"Bearer {env('CF_API_TOKEN')}"}
    models = list(CF_TEXT_MODELS)
    try:
        r = requests.get(base + "/models/search", headers=hdr, params={"task": "Text Generation", "per_page": 100}, timeout=30)
        listed = [m.get("name") for m in r.json().get("result", []) if isinstance(m, dict)]
        models = [m for m in CF_TEXT_MODELS if m in listed] or models
    except Exception:
        pass
    last = "no model answered"
    for m in models:
        try:
            r = requests.post(f"{base}/run/{m}", headers=hdr, timeout=240, json={
                "messages": [{"role": "system", "content": "Reply with ONLY one valid JSON object. No markdown, no commentary."},
                             {"role": "user", "content": prompt}],
                "max_tokens": 4096, "temperature": 0.9})
            if r.status_code >= 400:
                last = f"{m}: HTTP {r.status_code} {r.text[:160]}"
                continue
            res = r.json().get("result", {})
            text = res.get("response") if isinstance(res, dict) else None
            if text is None and isinstance(res, dict) and res.get("choices"):
                text = res["choices"][0]["message"]["content"]
            if isinstance(text, (dict, list)):
                return text
            return parse_json(text or "")
        except Exception as e:
            last = f"{m}: {e!r}"[:200]
    raise RuntimeError("Cloudflare text fallback failed: " + last)


def gemini_json(prompt):
    try:
        return _gemini_only(prompt)
    except Exception as e:
        if env("CF_ACCOUNT_ID") and env("CF_API_TOKEN"):
            print("  Gemini unavailable (" + str(e)[:150] + ") -> using the Cloudflare text model")
            return cf_llm_json(prompt)
        raise


def get_ideas(n, hist, hint):
    recent = [h["topic"] for h in hist[-60:]]
    prompt = f"""You run a successful YouTube Shorts channel. Niche: {NICHE}.
Language of the videos: {LANGS[VIDEO_LANG]['name']}.
Propose {n} different ideas for today's Shorts. Every idea must use a DIFFERENT format from this list: {json.dumps(FORMATS)}.
Rules:
- Only true, well-established, surprising facts (a fact-checker must agree with every claim).
- No medical, financial, political or religious claims. No violence or gore. Nothing about private people.
- Every idea needs a curiosity hook that makes a stranger stop scrolling within 2 seconds.
- Never repeat or closely resemble these recent topics: {json.dumps(recent, ensure_ascii=False)}
{hint}
Return ONLY JSON: {{"ideas":[{{"topic":"...","format":"...","angle":"the surprising core of the video in one sentence"}}]}}"""
    ideas = gemini_json(prompt).get("ideas", [])
    return [i for i in ideas if isinstance(i, dict) and i.get("topic")][:n]


def valid_script(m):
    try:
        sc = m["scenes"]
        if not (5 <= len(sc) <= 9):
            return False
        for s in sc:
            if not s["narration"].strip() or not s["search"].strip():
                return False
        words = len(" ".join(s["narration"] for s in sc).split())
        m["score"] = float(m.get("score", 5))
        return 40 <= words <= 170 and bool(m["title"].strip())
    except Exception:
        return False


def write_script(idea):
    words = "85-105 words" if VIDEO_LANG == "en" else "75-95 words"
    prompt = f"""Write the script of ONE YouTube Short (about 35-45 seconds when read aloud, {words} in total).
Topic: {idea['topic']}
Format: {idea.get('format', '')}
Angle: {idea.get('angle', '')}
Language of narration, title and description: {LANGS[VIDEO_LANG]['name']}.
Rules:
- Scene 1 is a hook that creates curiosity within 3 seconds (a question or a shocking claim). No greetings, no filler.
- Middle scenes: escalating, concrete, visual facts. Short sentences. No fluff.
- Last scene: a punchy payoff that connects back to the hook so people replay the video. Never beg for subscribes.
- Every fact must be true and well established. If you are not sure about a number, leave it out.
- 6 to 8 scenes. Each scene has 1-2 short sentences of narration and "search": 2-3 ENGLISH words for a stock-footage search (something concrete that can be filmed, e.g. "ocean waves", "night sky stars", "close up eye").
Return ONLY JSON:
{{"title":"max 70 chars, curiosity-driven, honest","hook_text":"max 6 words, big on-screen text","scenes":[{{"narration":"...","search":"..."}}],"description":"2 short sentences ending with a question for comments","hashtags":["#shorts","#tag2","#tag3"],"tags":["8 to 12 keywords"],"score":7}}
"score" = your honest rating from 1 to 10 of hook strength AND factual reliability."""
    best = None
    for _ in range(2):
        meta = gemini_json(prompt)
        if valid_script(meta):
            if best is None or meta["score"] > best["score"]:
                best = meta
            if meta["score"] >= 7:
                break
    if best is None:
        raise RuntimeError("Gemini returned no valid script")
    return best


# ------------------------------------------------------------------- voice
async def _edge(text, voice, out_mp3):
    import edge_tts
    try:
        com = edge_tts.Communicate(text, voice, rate="+6%", boundary="WordBoundary")
    except TypeError:  # older edge-tts without the boundary option
        com = edge_tts.Communicate(text, voice, rate="+6%")
    words, sents = [], []
    with open(out_mp3, "wb") as f:
        async for ch in com.stream():
            if ch["type"] == "audio":
                f.write(ch["data"])
            elif ch["type"] in ("WordBoundary", "SentenceBoundary"):
                s = ch["offset"] / 1e7
                item = (s, s + ch["duration"] / 1e7, ch["text"])
                (words if ch["type"] == "WordBoundary" else sents).append(item)
    return words or sents


def make_voice(text, out_mp3):
    voices = ([env("VOICE")] if env("VOICE") else []) + LANGS[VIDEO_LANG]["voices"]
    for voice in voices:
        try:
            bounds = asyncio.run(_edge(text, voice, out_mp3))
            if out_mp3.exists() and out_mp3.stat().st_size > 2000:
                return bounds
        except Exception as e:
            print("  edge-tts failed:", repr(e)[:150])
    from gtts import gTTS  # fallback: no word timings, we estimate them
    gTTS(text, lang=VIDEO_LANG).save(str(out_mp3))
    return []


def weight(tok):
    core = re.sub(r"[^\w]", "", tok, flags=re.U)
    w = max(len(core), 1) + 1.5
    if tok[-1:] in ".!?\u061f\u2026":
        w += 5
    elif tok[-1:] in ",;:\u060c\u061b":
        w += 2.5
    return w


def token_times(text, bounds, total):
    """Returns [(start, end, token)] for the whole narration."""
    out = []
    if bounds:  # real timings from the TTS engine (word or sentence level)
        for s, e, t in bounds:
            toks = t.split()
            if not toks:
                continue
            ws = [weight(x) for x in toks]
            cur = s
            for x, w in zip(toks, ws):
                d = (e - s) * w / sum(ws)
                out.append((cur, cur + d, x))
                cur += d
        return out
    toks = text.split()  # estimate from text length
    ws = [weight(x) for x in toks]
    cur, span = 0.15, max(total - 0.4, 1.0)
    for x, w in zip(toks, ws):
        d = span * w / sum(ws)
        out.append((cur, cur + d, x))
        cur += d
    return out


# ---------------------------------------------------------------- captions
ASS_HEAD = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,DejaVu Sans,88,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,8,3,2,70,70,560,1
Style: Hook,DejaVu Sans,96,&H0000E5FF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,10,4,8,70,70,300,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def ass_time(t):
    cs = int(round(max(t, 0) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def clean(t):
    return t.replace("{", "(").replace("}", ")").replace("\\", "")


def build_ass(tokens, hook, path):
    up = LANGS[VIDEO_LANG]["upper"]
    chunks, cur = [], []
    for tok in tokens:
        cur.append(tok)
        if len(cur) >= 3 or tok[2][-1:] in ".!?\u061f,\u060c;:":
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    pop = "{\\fscx86\\fscy86\\t(0,110,\\fscx100\\fscy100)}"
    lines = [ASS_HEAD]
    h = clean(hook)
    lines.append(f"Dialogue: 1,{ass_time(0)},{ass_time(2.6)},Hook,,0,0,0,,{{\\fad(0,300)}}{h.upper() if up else h}\n")
    for i, ch in enumerate(chunks):
        st = ch[0][0]
        nxt = chunks[i + 1][0][0] if i + 1 < len(chunks) else ch[-1][1] + 0.4
        en = min(nxt, ch[-1][1] + 0.6)
        text = clean(" ".join(x[2] for x in ch))
        if up:
            text = text.upper()
        lines.append(f"Dialogue: 0,{ass_time(st)},{ass_time(max(en, st + 0.2))},Cap,,0,0,0,,{pop}{text}\n")
    Path(path).write_text("".join(lines), encoding="utf-8")


# ------------------------------------------------------------------ footage
def download(url, dest):
    with requests.get(url, stream=True, timeout=120) as dl:
        dl.raise_for_status()
        with open(dest, "wb") as fh:
            for chunk in dl.iter_content(1 << 20):
                fh.write(chunk)


def clip_pixabay(q, dest, used):
    """Free key: pixabay.com/api/docs (log in to see it). Mostly landscape clips -> cropped to vertical."""
    r = requests.get("https://pixabay.com/api/videos/",
                     params={"key": env("PIXABAY_API_KEY"), "q": q[:100], "per_page": 30,
                             "video_type": "film", "safesearch": "true"},
                     timeout=30)
    if r.status_code != 200:
        print(f"  pixabay HTTP {r.status_code}: {r.text[:100]}")
        return None
    hits = [h for h in r.json().get("hits", [])
            if f"pixabay:{h['id']}" not in used and h.get("duration", 0) >= 4]
    random.shuffle(hits)
    for h in hits:
        opts = [v for v in (h.get("videos") or {}).values()
                if isinstance(v, dict) and v.get("url") and v.get("width") and v.get("height")]
        portrait = [v for v in opts if v["height"] >= v["width"] and v["height"] >= 1000]
        wide = [v for v in opts if v["height"] < v["width"] and v["height"] >= 720]
        if portrait:
            best = min(portrait, key=lambda v: abs(v["height"] - 1920))
        elif wide:
            small_enough = [v for v in wide if (v.get("size") or 0) <= 110e6] or wide
            best = max(small_enough, key=lambda v: v["height"])
        else:
            continue
        download(best["url"], dest)
        used.add(f"pixabay:{h['id']}")
        return f"pixabay:{h['id']}"
    return None


def clip_pexels(q, dest, used):
    """Optional: only used if you already own a Pexels key (Pexels paused new keys)."""
    for size in ("medium", "small"):
        r = requests.get("https://api.pexels.com/videos/search",
                         headers={"Authorization": env("PEXELS_API_KEY")},
                         params={"query": q, "orientation": "portrait", "size": size, "per_page": 20},
                         timeout=30)
        if r.status_code != 200:
            continue
        vids = [v for v in r.json().get("videos", [])
                if f"pexels:{v['id']}" not in used and v.get("duration", 0) >= 4]
        random.shuffle(vids)
        for v in vids:
            files = [f for f in v.get("video_files", [])
                     if f.get("file_type") == "video/mp4" and (f.get("height") or 0) >= 1000
                     and (f.get("height") or 0) >= (f.get("width") or 1)]
            if not files:
                continue
            files.sort(key=lambda f: abs(f["height"] - 1920))
            download(files[0]["link"], dest)
            used.add(f"pexels:{v['id']}")
            return f"pexels:{v['id']}"
    return None


def abstract_clip(dest):
    """Last resort so a video is still produced when no footage is found."""
    c = random.choice([("0x0b132b", "0x3a506b", "0x5bc0be"),
                       ("0x1a1a2e", "0x16213e", "0x0f3460"),
                       ("0x2d1b69", "0x6a4c93", "0xc77dff")])
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
         f"gradients=s={W}x{H}:r={FPS}:d=10:c0={c[0]}:c1={c[1]}:c2={c[2]}:nb_colors=3:speed=0.05",
         "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", dest])
    return "abstract"


def fetch_clip(query, dest, used):
    providers = []
    if env("PIXABAY_API_KEY"):
        providers.append(clip_pixabay)
    if env("PEXELS_API_KEY"):
        providers.append(clip_pexels)
    if not providers:
        raise RuntimeError("No footage source: add the PIXABAY_API_KEY secret")
    words = query.split()
    queries = [query, " ".join(words[:2]), words[0] if words else "",
               random.choice(["nature landscape", "city lights night", "abstract light", "clouds sky timelapse"])]
    for q in dict.fromkeys(x for x in queries if x):
        for prov in providers:
            try:
                cid = prov(q, dest, used)
            except Exception as e:
                print(f"  {prov.__name__} failed for {q!r}: {e!r}"[:200])
                cid = None
            if cid:
                return cid
    print(f"  no footage found for {query!r}, using an abstract background")
    return abstract_clip(dest)


# ------------------------------------------------------------------- render
def probe_size(path):
    out = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
               "-of", "csv=p=0:s=x", path]).strip()
    w, h = out.split("x")[:2]
    return int(w), int(h)


def render_scene(src, dur, out):
    sd = duration(src)
    sw, sh = probe_size(src)
    grade = "eq=saturation=1.12:contrast=1.04"
    if sw > sh * 0.57:  # landscape/square: cut a centered 9:16 window first, then scale up/down to 1080x1920
        vf = ("crop=trunc(min(iw\\,ih*9/16)/2)*2:trunc(min(ih\\,iw*16/9)/2)*2,"
              f"scale={W}:{H}:flags=lanczos,setsar=1,fps={FPS},"
              + ("unsharp=5:5:0.7," if sh < 1500 else "") + grade)
    else:  # already vertical
        vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,"
              f"crop={W}:{H},setsar=1,fps={FPS},{grade}")
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    if sd < dur + 0.5:
        cmd += ["-stream_loop", "-1"]
    else:
        cmd += ["-ss", f"{random.uniform(0, max(sd - dur - 0.3, 0)):.2f}"]
    cmd += ["-i", src, "-t", f"{dur:.3f}", "-an", "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p", out]
    run(cmd)


def compose(scenes, voice, ass, final, wd, total):
    (wd / "list.txt").write_text("".join(f"file '{p.name}'\n" for p in scenes))
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", "list.txt",
         "-c", "copy", "silent.mp4"], cwd=wd)
    music = sorted(MUSIC_DIR.glob("*.mp3")) if MUSIC_DIR.exists() else []
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", "silent.mp4", "-i", voice.name]
    if music:
        cmd += ["-stream_loop", "-1", "-i", random.choice(music).resolve()]
        audio = (f"[1:a]apad=whole_dur={total:.2f}[v];[2:a]volume=0.10[m];"
                 "[v][m]amix=inputs=2:duration=first:dropout_transition=0,loudnorm=I=-15:TP=-1.5:LRA=11[a]")
    else:
        audio = f"[1:a]apad=whole_dur={total:.2f},loudnorm=I=-15:TP=-1.5:LRA=11[a]"
    cmd += ["-filter_complex", f"[0:v]subtitles={ass.name}[vout];{audio}",
            "-map", "[vout]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", final.resolve()]
    run(cmd, cwd=wd)


def build_video(meta, idx, used_clips):
    wd = WORK / f"v{idx}"
    shutil.rmtree(wd, ignore_errors=True)
    wd.mkdir(parents=True)
    narration = " ".join(s["narration"].strip() for s in meta["scenes"])
    voice = wd / "voice.mp3"
    bounds = make_voice(narration, voice)
    vdur = duration(voice)
    total = vdur + 0.6
    wts = [max(len(s["narration"]), 12) for s in meta["scenes"]]
    durs = [max(total * w / sum(wts), 1.8) for w in wts]
    k = total / sum(durs)
    durs = [d * k for d in durs]
    scenes, clip_ids = [], []
    for i, (s, d) in enumerate(zip(meta["scenes"], durs)):
        src = wd / f"src{i}.mp4"
        clip_ids.append(fetch_clip(s["search"], src, used_clips))
        out = wd / f"scene{i}.mp4"
        render_scene(src, d, out)
        src.unlink()
        scenes.append(out)
    meta["credits"] = sorted({c.split(":")[0] for c in clip_ids if ":" in c})
    ass = wd / "captions.ass"
    build_ass(token_times(narration, bounds, vdur), meta.get("hook_text") or meta["title"], ass)
    OUT.mkdir(exist_ok=True)
    final = OUT / f"{dt.date.today():%Y%m%d}_{idx + 1}.mp4"
    compose(scenes, voice, ass, final, wd, total)
    shutil.rmtree(wd, ignore_errors=True)
    return final, clip_ids


# ------------------------------------------------------------------ YouTube
def youtube():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds = Credentials(None, refresh_token=env("YT_REFRESH_TOKEN"),
                        token_uri="https://oauth2.googleapis.com/token",
                        client_id=env("YT_CLIENT_ID"), client_secret=env("YT_CLIENT_SECRET"))
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def publish_slots(n):
    hours = [int(x) for x in PUBLISH_HOURS.split(",") if x.strip()] or [12]
    now = dt.datetime.now(dt.timezone.utc)
    out = []
    for i in range(n):
        t = now.replace(hour=hours[i % len(hours)], minute=random.choice([0, 6, 11, 17, 24, 33, 41]),
                        second=0, microsecond=0)
        out.append(max(t, now + dt.timedelta(minutes=20 + 12 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    return sorted(out)


def upload(yt, path, meta, publish_at):
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload
    tags = [t.strip("# ") for t in meta.get("tags", []) if isinstance(t, str)][:15]
    hashtags = [h if h.startswith("#") else "#" + h for h in meta.get("hashtags", []) if isinstance(h, str)][:3]
    if not any(h.lower() == "#shorts" for h in hashtags):
        hashtags.insert(0, "#shorts")
    names = {"pixabay": "Pixabay (pixabay.com)", "pexels": "Pexels (pexels.com)"}
    credit = ", ".join(names[c] for c in meta.get("credits", []) if c in names)
    desc = f"{meta.get('description', '').strip()}\n\n{' '.join(hashtags)}"
    if credit:
        desc += f"\n\nStock footage: {credit}"
    status = {"privacyStatus": "public" if MODE == "now" else "private",
              "selfDeclaredMadeForKids": False, "embeddable": True}
    if DISCLOSE_AI:
        status["containsSyntheticMedia"] = True
    if MODE == "publish":
        status["publishAt"] = publish_at
    body = {"snippet": {"title": meta["title"].strip()[:95], "description": desc[:4900], "tags": tags,
                        "categoryId": CATEGORY, "defaultLanguage": VIDEO_LANG,
                        "defaultAudioLanguage": VIDEO_LANG},
            "status": status}
    media = MediaFileUpload(str(path), mimetype="video/mp4", chunksize=-1, resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp, tries = None, 0
    while resp is None:
        try:
            _, resp = req.next_chunk()
        except HttpError as e:
            text = str(e)
            if "uploadLimitExceeded" in text or "quotaExceeded" in text:
                raise QuotaStop(text[:200])
            if e.resp.status in (500, 502, 503, 504) and tries < 5:
                tries += 1
                time.sleep(2 ** tries)
                continue
            raise
    return resp["id"]


def performance_hint(yt, hist):
    """Looks at how past videos performed and tells Gemini what worked (needs youtube.readonly scope)."""
    if not yt:
        return ""
    cutoff = str(dt.date.today() - dt.timedelta(days=2))
    old = [h for h in hist[-80:] if h.get("video_id") and h.get("date", "9") <= cutoff]
    if len(old) < 8:
        return ""
    try:
        views = {}
        for i in range(0, len(old), 50):
            r = yt.videos().list(part="statistics", id=",".join(h["video_id"] for h in old[i:i + 50])).execute()
            for it in r.get("items", []):
                views[it["id"]] = int(it["statistics"].get("viewCount", 0))
        ranked = sorted((h for h in old if h["video_id"] in views), key=lambda h: -views[h["video_id"]])
        best = [f'{h["title"]} ({views[h["video_id"]]} views)' for h in ranked[:5]]
        worst = [f'{h["title"]} ({views[h["video_id"]]} views)' for h in ranked[-3:]]
        return ("Our best Shorts so far: " + json.dumps(best, ensure_ascii=False) +
                ". Our weakest: " + json.dumps(worst, ensure_ascii=False) +
                ". Study the pattern: make more like the best, fewer like the weakest.")
    except Exception as e:
        print("  (no analytics hint:", repr(e)[:100], ")")
        return ""


# --------------------------------------------------------------------- main
def save_history(hist):
    if MODE != "dry":
        HISTORY.write_text(json.dumps(hist[-400:], ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    hist = json.loads(HISTORY.read_text(encoding="utf-8")) if HISTORY.exists() else []
    yt = youtube() if MODE != "dry" else None
    ideas = get_ideas(COUNT, hist, performance_hint(yt, hist))
    print(f"{len(ideas)} ideas ready, mode={MODE}, lang={VIDEO_LANG}")
    slots = publish_slots(len(ideas))
    used = {c for h in hist[-80:] for c in h.get("clips", [])}
    done = 0
    for i, idea in enumerate(ideas):
        print(f"\n=== video {i + 1}/{len(ideas)}: {idea['topic']}")
        try:
            meta = write_script(idea)
            path, clips = build_video(meta, i, used)
            vid = None
            if yt:
                vid = upload(yt, path, meta, slots[i])
                print("  uploaded:", vid, "| goes public at", slots[i] if MODE == "publish" else MODE)
            hist.append({"date": str(dt.date.today()), "topic": idea["topic"], "title": meta["title"],
                         "video_id": vid, "clips": clips})
            save_history(hist)
            done += 1
        except QuotaStop as e:
            print("  YouTube daily limit reached, stopping:", e)
            break
        except Exception:
            traceback.print_exc()
    shutil.rmtree(WORK, ignore_errors=True)
    print(f"\nfinished: {done}/{len(ideas)} videos")
    if done == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()

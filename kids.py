#!/usr/bin/env python3
"""
Kids Story Studio
Illustrated stories for children -> long videos (~13 min, 16:9) or Shorts (~1 min, 9:16).

Flow: series bible (recurring characters) -> theme -> story (Gemini) -> child-safety review
      -> illustrations (Cloudflare FLUX, free) -> narration per scene (edge-tts)
      -> camera-motion animation + fades (FFmpeg) -> thumbnail -> YouTube (made for kids)

Settings (environment): KIDS_FORMAT = long | short     COUNT = how many videos
                        MODE = dry | private | publish  KIDS_LANG = en | ar
Needs main.py in the same folder (it reuses its Gemini + YouTube helpers).
"""
import asyncio
import base64
import datetime as dt
import io
import json
import os
import random
import shutil
import sys
import threading
import time
import traceback
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

import main as core

env = core.env
FORMAT = env("KIDS_FORMAT", "long")  # long | short
LANGV = env("KIDS_LANG", "en")  # en | ar
MODE = core.MODE  # dry | private | publish
COUNT = int(env("COUNT") or (1 if FORMAT == "long" else 2))
ACTS, PER_ACT = 5, int(env("SCENES_PER_ACT", "12"))  # 5 x 12 = 60 scenes ~ 13 minutes
if FORMAT == "long":
    W, H = [int(x) for x in env("LONG_RES", "1920x1080").split("x")]
    IMG_W, IMG_H = 1024, 576
    HOURS = env("KIDS_PUBLISH_HOURS_UTC", "13,18")
else:
    W, H = [int(x) for x in env("SHORT_RES", "1080x1920").split("x")]
    IMG_W, IMG_H = 576, 1024
    HOURS = env("KIDS_PUBLISH_HOURS_UTC", "10,16")
FPS = 30
CATEGORY = env("KIDS_CATEGORY_ID", "27")  # 27 Education, 1 Film & Animation
RATE = env("KIDS_VOICE_RATE", "-4%")

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "work_kids"
OUT = ROOT / "out"
BIBLE, HIST, TRENDS = ROOT / "kids_bible.json", ROOT / "kids_history.json", ROOT / "trends.json"
MUSIC_DIR = ROOT / "music"

LANGS = {
    "en": {"name": "English", "voices": ["en-US-JennyNeural", "en-US-AriaNeural", "en-GB-SoniaNeural"]},
    "ar": {"name": "simple Modern Standard Arabic suitable for young children",
           "voices": ["ar-SA-ZariyahNeural", "ar-EG-SalmaNeural"]},
}
L = LANGS.get(LANGV, LANGS["en"])
QUOTA = threading.Event()


class ImageQuota(Exception):
    pass


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


# ------------------------------------------------------------ story planning
def load_bible():
    """The recurring characters and look of the channel. Created once, then reused for every episode."""
    b = load_json(BIBLE, None)
    if b and b.get("characters") and b.get("style"):
        return b
    b = core.gemini_json(f"""Create a series bible for a gentle YouTube channel of illustrated stories for children aged 3-7 (language: {L['name']}).
Return ONLY JSON:
{{"channel_name":"short, warm, easy to pronounce","tagline":"max 8 words","style":"ONE sentence describing one consistent visual style for an image generator: children's picture-book illustration, soft pastel colors, rounded shapes, clean outlines, warm light, high detail","world":"one sentence about the recurring setting","characters":[{{"name":"...","kind":"animal or child","look":"exact visual description in about 25 words: colors, clothing, face, size","personality":"..."}}]}}
Rules: 3 original characters (2 main, 1 helper). Original only: never use or resemble existing cartoons, characters or brands. Gentle, kind, safe for toddlers. English names are fine.""")
    chars = [c for c in b.get("characters", []) if isinstance(c, dict) and c.get("name") and c.get("look")]
    if len(chars) < 2 or not b.get("style"):
        raise RuntimeError("series bible is incomplete, run again")
    b["characters"] = chars
    BIBLE.write_text(json.dumps(b, ensure_ascii=False, indent=1), encoding="utf-8")
    return b


def pick_themes(n, hist, bible):
    recent = [h["theme"] for h in hist[-40:]]
    trend = ""
    t = load_json(TRENDS, {})
    if t.get("ideas"):
        trend = ("Ideas that are trending in the kids niche right now (take inspiration, never copy): "
                 + json.dumps(t["ideas"][:8], ensure_ascii=False))
    prompt = f"""You plan stories for a children's channel (ages 3-7). Language: {L['name']}.
World: {bible.get('world', '')}. Characters: {json.dumps([c['name'] for c in bible['characters']])}.
Propose {n} different story themes for today. Each has a clear, gentle LESSON (kindness, sharing, courage, honesty, counting, colors, seasons, bedtime, friendship, nature...).
Never repeat or resemble: {json.dumps(recent, ensure_ascii=False)}
{trend}
Return ONLY JSON: {{"themes":[{{"theme":"...","lesson":"..."}}]}}"""
    th = [x for x in core.gemini_json(prompt).get("themes", []) if isinstance(x, dict) and x.get("theme")]
    return th[:n]


def clean_scenes(raw, lo, hi):
    out = []
    for s in raw or []:
        if not isinstance(s, dict):
            continue
        n, v = str(s.get("narration", "")).strip(), str(s.get("visual", "")).strip()
        if n and v and lo <= len(n.split()) <= hi:
            out.append({"narration": n, "visual": v,
                        "characters": [c for c in s.get("characters", []) if isinstance(c, str)]})
    return out


def cast(bible):
    return json.dumps([{"name": c["name"], "personality": c.get("personality", "")} for c in bible["characters"]],
                      ensure_ascii=False)


def write_long(theme, bible):
    o = core.gemini_json(f"""Plan a gentle illustrated story for children aged 3-7 that lasts about 13 minutes when narrated. Language: {L['name']}.
Theme: {theme['theme']}. Lesson: {theme.get('lesson', '')}. Characters: {cast(bible)}. World: {bible.get('world', '')}.
Return ONLY JSON: {{"title":"max 60 characters, warm and clear","summary":"2 sentences","acts":["1-2 sentences for each of the {ACTS} acts: beginning, a small problem, attempts, turning point, happy ending"]}}""")
    acts = [a for a in o.get("acts", []) if isinstance(a, str)][:ACTS]
    if len(acts) < ACTS or not o.get("title"):
        raise RuntimeError("story outline is incomplete")
    scenes = []
    for a, beat in enumerate(acts):
        prev = " ".join(s["narration"] for s in scenes[-2:])
        got = []
        for _ in range(2):
            r = core.gemini_json(f"""You are writing act {a + 1} of {ACTS} of a children's story. Language: {L['name']}.
Title: {o['title']}. Whole story: {o.get('summary', '')}. This act: {beat}. Previous narration (continue smoothly): {prev}
Write exactly {PER_ACT} scenes. For every scene: "narration" = 2 simple, vivid sentences of 26-32 words that a warm storyteller reads aloud; "visual" = what the picture shows (concrete setting, action, feeling), in English; "characters" = names of the characters visible.
Gentle tone only: nothing scary, no violence, no brands. Simple words and a little repetition that children enjoy.
Return ONLY JSON: {{"scenes":[{{"narration":"...","visual":"...","characters":["..."]}}]}}""")
            got = clean_scenes(r.get("scenes"), 14, 52)
            if len(got) >= PER_ACT - 2:
                break
        if len(got) < PER_ACT - 2:
            raise RuntimeError(f"act {a + 1} came back too short")
        scenes += got[:PER_ACT + 1]
    return {"title": o["title"].strip(), "summary": o.get("summary", ""), "scenes": scenes}


def write_short(theme, bible):
    r = core.gemini_json(f"""Write a tiny illustrated story or fun mini-lesson for children aged 3-7 that takes about 55 seconds when narrated. Language: {L['name']}.
Theme: {theme['theme']}. Lesson: {theme.get('lesson', '')}. Characters: {cast(bible)}. World: {bible.get('world', '')}.
First scene = a delightful hook. Last scene = a warm, happy ending. Gentle tone only.
Return ONLY JSON: {{"title":"max 60 characters","summary":"1 sentence","scenes":[{{"narration":"ONE short sentence of 10-16 words","visual":"English picture description","characters":["names"]}}]}}
Exactly 8 scenes.""")
    sc = clean_scenes(r.get("scenes"), 5, 24)
    if len(sc) < 6 or not r.get("title"):
        raise RuntimeError("short story came back incomplete")
    return {"title": r["title"].strip(), "summary": r.get("summary", ""), "scenes": sc[:9]}


def safety_ok(story):
    text = "\n".join(f"{s['narration']} [picture: {s['visual']}]" for s in story["scenes"])
    r = core.gemini_json(f"""You are a strict child-safety reviewer for content aimed at children aged 3-7.
Reject (ok=false) if anything is scary, violent, unsafe to imitate, sexual, hateful, gives medical advice, mentions brands or copyrighted characters, or states confusing or false facts.
Return ONLY JSON: {{"ok":true,"issues":["..."]}}
TITLE: {story['title']}
STORY: {text[:14000]}""")
    return bool(r.get("ok")), r.get("issues", [])


def describe(story, theme, bible):
    kind = "story video" if FORMAT == "long" else "Short"
    try:
        r = core.gemini_json(f"""Write YouTube metadata for a children's {kind}. Language: {L['name']}.
Title: {story['title']}. Summary: {story['summary']}. Lesson: {theme.get('lesson', '')}. Channel: {bible.get('channel_name', '')}.
Return ONLY JSON: {{"description":"2-3 warm sentences for parents about the story and its lesson, then one line inviting families to watch more stories","hashtags":["#kidsstories","#bedtimestories","#tag3"],"tags":["10 keywords"],"thumb_text":"max 3 words, no punctuation"}}""")
    except Exception:
        r = {}
    story["description"] = r.get("description") or story["summary"]
    story["hashtags"] = [h for h in r.get("hashtags", []) if isinstance(h, str)][:3] or ["#kidsstories"]
    story["tags"] = [t for t in r.get("tags", []) if isinstance(t, str)][:15]
    story["thumb_text"] = r.get("thumb_text", "")
    return story


# ------------------------------------------------------------- illustrations
def image_prompt(scene, bible):
    names = scene.get("characters", [])
    looks = " ".join(f"{c['name']}: {c['look']}." for c in bible["characters"] if c["name"] in names)
    comp = "Wide composition, subject centered." if FORMAT == "long" else "Vertical composition, subject centered."
    return (f"{bible['style']}. {scene['visual']}. {looks} {comp} "
            "No text, no letters, no watermark. Cute, friendly, safe for toddlers.")[:1400]


class ImageConfig(Exception):
    """Cloudflare refuses the request itself (wrong key, wrong account, bad format): retrying is pointless."""


FATAL = []


CF_MODELS = ["@cf/black-forest-labs/flux-1-schnell", "@cf/bytedance/stable-diffusion-xl-lightning",
             "@cf/lykon/dreamshaper-8-lcm", "@cf/stabilityai/stable-diffusion-xl-base-1.0"]
_cf = {"models": None, "model": None}


def cf_base():
    return (f"https://api.cloudflare.com/client/v4/accounts/{env('CF_ACCOUNT_ID')}",
            {"Authorization": f"Bearer {env('CF_API_TOKEN')}"})


def cf_models():
    """Image models to try, newest working one first. CF_IMAGE_MODEL forces one."""
    if env("CF_IMAGE_MODEL"):
        return [env("CF_IMAGE_MODEL")]
    if _cf["models"] is None:
        names = list(CF_MODELS)
        try:  # keep only the models this account can really use
            base, hdr = cf_base()
            r = requests.get(base + "/ai/models/search", headers=hdr,
                             params={"task": "Text-to-Image", "per_page": 50}, timeout=30)
            listed = [m.get("name") for m in r.json().get("result", []) if isinstance(m, dict)]
            names = [n for n in CF_MODELS if n in listed] or names
            print("  cloudflare image models:", names)
        except Exception as e:
            print("  (cloudflare model list unavailable:", repr(e)[:90], ")")
        _cf["models"] = names
    return _cf["models"]


def cf_bodies(model, prompt):
    steps = {"steps": 4} if "flux" in model else {"num_steps": 8}
    return [{"prompt": prompt, **steps, "width": IMG_W, "height": IMG_H}, {"prompt": prompt, **steps}, {"prompt": prompt}]


def img_cloudflare(prompt, seed):
    base, hdr = cf_base()
    models = cf_models()
    if _cf["model"] in models:
        models = [_cf["model"]] + [m for m in models if m != _cf["model"]]
    last = "no model answered"
    for model in models:
        for body in cf_bodies(model, prompt):
            r = requests.post(f"{base}/ai/run/{model}", headers=hdr, json=body, timeout=150)
            low = r.text.lower()
            if r.status_code == 402 or "daily free allocation" in low or "4006" in low:
                raise ImageQuota(r.text[:200])
            if r.status_code == 429:
                raise RuntimeError("rate limited, retrying")
            if r.status_code in (401, 403):
                raise RuntimeError(f"HTTP {r.status_code} token rejected - check CF_API_TOKEN permission and CF_ACCOUNT_ID: {r.text[:300]}")
            if r.status_code in (400, 404):
                last = f"HTTP {r.status_code} {model} {sorted(body)}: {r.text[:300]}"
                continue
            r.raise_for_status()
            _cf["model"] = model
            if r.headers.get("content-type", "").startswith("image/"):
                return r.content
            data = r.json()
            if not data.get("success", True):
                raise RuntimeError(str(data.get("errors"))[:300])
            return base64.b64decode(data["result"]["image"])
    raise RuntimeError(last)


def img_pollinations(prompt, seed):  # keyless fallback, quality and speed vary
    url = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt[:900]) +
           f"?width={IMG_W}&height={IMG_H}&seed={seed}&nologo=true&model=flux")
    r = requests.get(url, timeout=150)
    r.raise_for_status()
    if not r.headers.get("content-type", "").startswith("image/"):
        raise RuntimeError("pollinations returned no image")
    return r.content


def fit_and_save(data, dest):
    """Crop to the video shape and enlarge 2x so the camera moves stay smooth and sharp."""
    from PIL import Image, ImageFilter
    im = Image.open(io.BytesIO(data)).convert("RGB")
    tw, th = 2 * W, 2 * H
    w, h = im.size
    if w / h > tw / th:
        nw = int(h * tw / th)
        im = im.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h))
    else:
        nh = int(w * th / tw)
        im = im.crop((0, (h - nh) // 2, w, (h - nh) // 2 + nh))
    im = im.resize((tw, th), Image.LANCZOS).filter(ImageFilter.UnsharpMask(radius=2, percent=50, threshold=3))
    im.save(dest, "JPEG", quality=92)


LAST_ERR = {"msg": ""}


def make_image(prompt, seed, dest):
    cf = bool(env("CF_API_TOKEN") and env("CF_ACCOUNT_ID"))
    prov = img_cloudflare if cf else img_pollinations
    for attempt in range(3):
        try:
            fit_and_save(prov(prompt, seed + attempt), dest)
            return True
        except ImageQuota:
            QUOTA.set()
            return False
        except Exception as e:
            LAST_ERR["msg"] = f"{prov.__name__}: {e}"
            print(f"  {LAST_ERR['msg']}"[:800])
            if "HTTP 40" in str(e):  # a rejected request will not succeed by repeating it
                return False
            time.sleep(2 + 3 * attempt)
    return False


def make_images(scenes, bible, wd):
    paths = [wd / f"img{i:03d}.jpg" for i in range(len(scenes))]
    seed0 = random.randint(1, 10 ** 6)

    def one(i):
        return False if QUOTA.is_set() else make_image(image_prompt(scenes[i], bible), seed0 + i * 7, paths[i])

    if not one(0):  # probe with the first picture so a broken setup stops in seconds, not minutes
        if QUOTA.is_set():
            raise ImageQuota("the free daily image allowance is used up")
        raise RuntimeError("the first illustration failed, stopping early. " + LAST_ERR["msg"])
    with ThreadPoolExecutor(max_workers=3) as ex:
        ok = [True] + list(ex.map(one, range(1, len(scenes))))
    if QUOTA.is_set():
        raise ImageQuota("the free daily image allowance is used up")
    bad = [i for i, o in enumerate(ok) if not o]
    if len(bad) > max(3, len(scenes) // 7):
        raise RuntimeError(f"{len(bad)} of {len(scenes)} illustrations failed. " + LAST_ERR["msg"])
    good = [i for i, o in enumerate(ok) if o]
    for i in bad:  # reuse the nearest picture for the few that failed
        shutil.copy(paths[min(good, key=lambda g: abs(g - i))], paths[i])
    return paths


# ----------------------------------------------------------------- narration
async def _edge(text, voice, out):
    import edge_tts
    com = edge_tts.Communicate(text, voice, rate=RATE)
    with open(out, "wb") as f:
        async for ch in com.stream():
            if ch["type"] == "audio":
                f.write(ch["data"])


def narrate(text, out):
    voices = ([env("KIDS_VOICE")] if env("KIDS_VOICE") else []) + L["voices"]
    for v in voices:
        for _ in range(2):
            try:
                asyncio.run(_edge(text, v, out))
                if out.exists() and out.stat().st_size > 1500:
                    return
            except Exception as e:
                print(f"  edge-tts {v}: {e!r}"[:150])
                time.sleep(1.5)
    from gtts import gTTS  # last resort
    gTTS(text, lang=LANGV).save(str(out))


# --------------------------------------------------------------------- video
MOTIONS = [  # slow camera moves over a still picture (zoom in, zoom out, pans, zoom toward a corner)
    ("min(1+0.2*on/{n},1.2)", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
    ("max(1.2-0.2*on/{n},1)", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
    ("1.14", "(iw-iw/zoom)*on/{n}", "ih/2-(ih/zoom/2)"),
    ("1.14", "(iw-iw/zoom)*(1-on/{n})", "ih/2-(ih/zoom/2)"),
    ("min(1+0.2*on/{n},1.2)", "(iw-iw/zoom)*0.2", "(ih-ih/zoom)*0.35"),
    ("min(1+0.2*on/{n},1.2)", "(iw-iw/zoom)*0.8", "(ih-ih/zoom)*0.35"),
]


def make_shot(img, dur, i, out):
    n = max(int(round(dur * FPS)), 2)
    z, x, y = [m.format(n=n) for m in MOTIONS[i % len(MOTIONS)]]
    vf = (f"zoompan=z='{z}':x='{x}':y='{y}':d={n}:s={W}x{H}:fps={FPS},"
          f"fade=t=in:st=0:d=0.3,fade=t=out:st={max(dur - 0.3, 0):.2f}:d=0.3,format=yuv420p")
    core.run(["ffmpeg", "-y", "-loglevel", "error", "-i", img, "-vf", vf, "-frames:v", n,
              "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-r", FPS, out])


def assemble(scenes, imgs, wd, final):
    wavs = []
    for i, s in enumerate(scenes):
        mp3, wav = wd / f"n{i:03d}.mp3", wd / f"a{i:03d}.wav"
        narrate(s["narration"], mp3)
        core.run(["ffmpeg", "-y", "-loglevel", "error", "-i", mp3, "-af", "apad=pad_dur=0.5",
                  "-ar", "44100", "-ac", "2", wav])
        mp3.unlink()
        wavs.append(wav)
        time.sleep(0.2)
    durs = [core.duration(p) for p in wavs]
    clips = []
    for i, (img, d) in enumerate(zip(imgs, durs)):
        clip = wd / f"s{i:03d}.mp4"
        make_shot(str(img), d, i, clip)
        img.unlink()
        clips.append(clip)
        if i % 10 == 9:
            print(f"  rendered {i + 1}/{len(imgs)} scenes")
    (wd / "v.txt").write_text("".join(f"file '{c.name}'\n" for c in clips))
    (wd / "a.txt").write_text("".join(f"file '{p.name}'\n" for p in wavs))
    core.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", "v.txt",
              "-c", "copy", "silent.mp4"], cwd=wd)
    core.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", "a.txt",
              "-c", "copy", "voice.wav"], cwd=wd)
    music = sorted(MUSIC_DIR.glob("*.mp3")) if MUSIC_DIR.exists() else []
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", "silent.mp4", "-i", "voice.wav"]
    if music:
        cmd += ["-stream_loop", "-1", "-i", random.choice(music).resolve()]
        fc = ("[2:a]volume=0.10[m];[1:a][m]amix=inputs=2:duration=first:dropout_transition=0,"
              "loudnorm=I=-16:TP=-1.5:LRA=11[a]")
    else:
        fc = "[1:a]loudnorm=I=-16:TP=-1.5:LRA=11[a]"
    cmd += ["-filter_complex", fc, "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac",
            "-b:a", "192k", "-shortest", "-movflags", "+faststart", final.resolve()]
    core.run(cmd, cwd=wd)
    return sum(durs)


def load_font(size):
    from PIL import ImageFont
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default()


def make_thumbnail(story, bible, wd):
    from PIL import Image, ImageDraw
    hero = bible["characters"][0]
    raw = wd / "thumb_raw.jpg"
    prompt = (f"{bible['style']}. {hero['name']}: {hero['look']}. Joyful close-up portrait, big smile, "
              "bright saturated colors, simple colorful background, no text, no letters.")
    if not make_image(prompt, random.randint(1, 10 ** 6), raw):
        return None
    im = Image.open(raw).convert("RGB").resize((1280, 720), Image.LANCZOS)
    text = (story.get("thumb_text") or "").strip().upper() if LANGV == "en" else ""
    if text:
        d = ImageDraw.Draw(im)
        size = 170
        font = load_font(size)
        while size > 60 and d.textlength(text, font=font) > 1160:
            size -= 10
            font = load_font(size)
        d.text((60, 670), text, font=font, fill="#FFE14D", stroke_width=10, stroke_fill="#1b1b3a", anchor="ld")
    out = wd / "thumb.jpg"
    im.save(out, "JPEG", quality=90)
    return out


def produce(story, bible, theme, idx):
    wd = WORK / f"{FORMAT}{idx}"
    shutil.rmtree(wd, ignore_errors=True)
    wd.mkdir(parents=True)
    scenes = story["scenes"]
    print(f"  {len(scenes)} scenes: drawing illustrations...")
    imgs = make_images(scenes, bible, wd)
    OUT.mkdir(exist_ok=True)
    final = OUT / f"kids_{FORMAT}_{dt.date.today():%Y%m%d}_{idx + 1}.mp4"
    print("  narrating and animating...")
    total = assemble(scenes, imgs, wd, final)
    print(f"  video ready: {total / 60:.1f} minutes")
    thumb = make_thumbnail(story, bible, wd) if FORMAT == "long" else None
    if thumb:
        shutil.copy(thumb, OUT / f"{final.stem}.jpg")
        thumb = OUT / f"{final.stem}.jpg"
    shutil.rmtree(wd, ignore_errors=True)
    return final, thumb


# ------------------------------------------------------------------- YouTube
def slots(n):
    hours = [int(x) for x in HOURS.split(",") if x.strip()] or [13]
    now = dt.datetime.now(dt.timezone.utc)
    out = []
    for i in range(n):
        t = now.replace(hour=hours[i % len(hours)], minute=random.choice([0, 5, 12, 20, 31, 40]),
                        second=0, microsecond=0)
        out.append(max(t, now + dt.timedelta(minutes=30 + 20 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    return sorted(out)


def upload(yt, path, story, publish_at, thumb):
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload
    desc = story["description"].strip() + "\n\n" + " ".join(story["hashtags"])
    status = {"privacyStatus": "private", "selfDeclaredMadeForKids": True,  # required for child-directed videos
              "embeddable": True, "containsSyntheticMedia": True}
    if MODE == "publish":
        status["publishAt"] = publish_at
    body = {"snippet": {"title": story["title"][:95], "description": desc[:4900], "tags": story["tags"],
                        "categoryId": CATEGORY, "defaultLanguage": LANGV, "defaultAudioLanguage": LANGV},
            "status": status}
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(str(path), mimetype="video/mp4",
                                                        chunksize=32 * 1024 * 1024, resumable=True))
    resp, tries = None, 0
    while resp is None:
        try:
            _, resp = req.next_chunk()
        except HttpError as e:
            if "uploadLimitExceeded" in str(e) or "quotaExceeded" in str(e):
                raise core.QuotaStop(str(e)[:200])
            if e.resp.status in (500, 502, 503, 504) and tries < 6:
                tries += 1
                time.sleep(2 ** tries)
                continue
            raise
    vid = resp["id"]
    if thumb:
        try:
            yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(str(thumb), mimetype="image/jpeg")).execute()
        except Exception as e:  # needs a phone-verified channel
            print("  thumbnail not set:", repr(e)[:120])
    return vid


def preflight():
    """Draw one tiny picture first, so a wrong key or request fails in seconds, not after 10 minutes."""
    if not (env("CF_API_TOKEN") and env("CF_ACCOUNT_ID")):
        print("no CF_ACCOUNT_ID / CF_API_TOKEN secrets: using the keyless fallback (lower quality)")
        return
    try:
        img_cloudflare("a small red apple, children's picture-book illustration", 1)
        print("Cloudflare check: OK")
    except ImageQuota as e:
        sys.exit(f"Cloudflare free daily allowance is used up: {e}")
    except Exception as e:
        sys.exit(f"Cloudflare check FAILED -> {e}")


def youtube_login():
    """Log in to the KIDS channel now, so a wrong token fails in seconds, not after an hour of rendering."""
    if env("YT_REFRESH_TOKEN_KIDS"):
        os.environ["YT_REFRESH_TOKEN"] = env("YT_REFRESH_TOKEN_KIDS")
    elif env("KIDS_ON_MAIN_CHANNEL") != "1":
        sys.exit("YT_REFRESH_TOKEN_KIDS secret is missing: kids videos must go to the kids channel, not the main one.")
    yt = core.youtube()
    try:
        name = yt.channels().list(part="snippet", mine=True).execute()["items"][0]["snippet"]["title"]
    except Exception as e:
        sys.exit("YouTube login failed - check YT_CLIENT_ID, YT_CLIENT_SECRET and YT_REFRESH_TOKEN_KIDS: " + repr(e)[:220])
    print(f"YouTube login OK, uploading to channel: {name}")
    return yt


def main():
    preflight()
    hist = load_json(HIST, [])
    yt = youtube_login() if MODE != "dry" else None
    bible = load_bible()
    themes = pick_themes(COUNT, hist, bible)
    when = slots(len(themes))
    print(f"{len(themes)} themes, format={FORMAT}, mode={MODE}, lang={LANGV}")
    done = 0
    for i, th in enumerate(themes):
        print(f"\n=== {FORMAT} {i + 1}/{len(themes)}: {th['theme']}")
        try:
            story = write_long(th, bible) if FORMAT == "long" else write_short(th, bible)
            ok, issues = safety_ok(story)
            if not ok:
                print("  rejected by the child-safety review:", issues)
                continue
            describe(story, th, bible)
            path, thumb = produce(story, bible, th, i)
            vid = None
            if yt:
                vid = upload(yt, path, story, when[i], thumb)
                print("  uploaded:", vid, "| public at", when[i] if MODE == "publish" else "(private)")
            hist.append({"date": str(dt.date.today()), "format": FORMAT, "theme": th["theme"],
                         "title": story["title"], "video_id": vid})
            if MODE != "dry":
                HIST.write_text(json.dumps(hist[-300:], ensure_ascii=False, indent=1), encoding="utf-8")
            done += 1
        except ImageQuota as e:
            print("  stopping:", e)
            break
        except core.QuotaStop as e:
            print("  YouTube limit reached, stopping:", e)
            break
        except Exception:
            traceback.print_exc()
    shutil.rmtree(WORK, ignore_errors=True)
    print(f"\nfinished: {done}/{len(themes)} videos")
    if done == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()

"""Multi-game update checker.

Runs once, checks each registered game for version changes, and posts an embed
to a Discord channel via the REST API when an update is detected.

Designed to be triggered on a schedule (e.g. GitHub Actions cron).

Currently supported: Fortnite, VALORANT, CS2, Deadlock.
Adding a new game: create check_xxx() and xxx_embed() functions, then add an
entry to the GAMES list at the bottom of this file.
"""

import json
import os
import sys
import logging
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("update-check")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID", "0")
STATE_FILE = "version_data.json"
DISCORD_API = "https://discord.com/api/v10"
USER_AGENT = "GameUpdateBot/3.0"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fetch_json(url):
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
    if resp.status_code != 200:
        logger.warning("%s returned %s", url, resp.status_code)
        return None
    return resp.json()


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if "version" in data:
            data = {"fortnite": data}
            save_state(data)
        return data
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def send_discord_embed(embed):
    url = f"{DISCORD_API}/channels/{CHANNEL_ID}/messages"
    headers = {
        "Authorization": f"Bot {DISCORD_TOKEN}",
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
    }
    resp = requests.post(url, headers=headers, json={"embeds": [embed]}, timeout=15)
    if resp.status_code not in (200, 201):
        logger.error("Discord send failed (%s): %s", resp.status_code, resp.text[:300])
        return False
    logger.info("Update message sent to channel %s", CHANNEL_ID)
    return True


# ---------------------------------------------------------------------------
# Game: Fortnite
# Release number: Epic's own public version endpoint (live, no key). It changes
# the moment a new release goes out, unlike the community API below, which only
# updates once the new build has been datamined (that lag hid 42.30 on 1 Oct 2026).
# Client build (CL): fortnite-api.com /v2/aes, used for the CL shown in the
# message and to spot hotfix patches that keep the same release number.
# News from /v2/news/br is flavour text only, never compared (it rotates often).
# ---------------------------------------------------------------------------

EPIC_VERSION_URL = "https://fortnite-public-service-prod11.ol.epicgames.com/fortnite/api/version"
FORTNITE_AES_URL = "https://fortnite-api.com/v2/aes"


def fortnite_release_of(version):
    """'++Fortnite+Release-42.20-CL-58011042' -> '42.20'; '42.30' stays '42.30'."""
    if version and "Release-" in version:
        return version.split("Release-")[1].split("-CL-")[0]
    return version


def fortnite_check():
    release = None
    epic = fetch_json(EPIC_VERSION_URL)
    if epic and epic.get("version"):
        release = str(epic["version"])

    build_str, api_release, cl = "", None, "?"
    body = fetch_json(FORTNITE_AES_URL)
    if body:
        build_str = (body.get("data") or {}).get("build") or ""
        if "Release-" in build_str:
            parts = build_str.split("Release-")[1].split("-CL-")
            if len(parts) == 2:
                api_release, cl = parts

    if release is None:  # Epic unreachable: fall back to the community API
        release = api_release
    if release is None:
        return None
    if api_release != release:  # the community API hasn't caught up with this release yet
        cl, build_str = "?", ""

    motd_title, motd_body = "", ""
    news = fetch_json("https://fortnite-api.com/v2/news/br")
    if news:
        motds = (news.get("data") or {}).get("motds") or []
        if motds:
            motd_title = motds[0].get("title", "")
            motd_body = motds[0].get("body", "")

    return {
        "version": release,
        "release": release,
        "cl": cl,
        "build": build_str,
        "motd_title": motd_title,
        "motd_body": motd_body,
    }


def fortnite_compare(old, new):
    """'update' for a new release, 'hotfix' for a new client build of the same
    release, 'silent' when only the build number became known, else None."""
    if fortnite_release_of(old.get("version")) != new["release"]:
        return "update"
    old_cl, new_cl = old.get("cl", "?"), new.get("cl", "?")
    if new_cl == "?" or old_cl == new_cl:
        return None
    if old_cl == "?":
        return "silent"
    return "hotfix"


def fortnite_embed(info):
    fields = [
        {"name": "Release", "value": info["release"], "inline": True},
        {"name": "Build (CL)", "value": info["cl"], "inline": True},
    ]
    if info.get("motd_title"):
        trimmed = info["motd_body"][:250]
        if len(info["motd_body"]) > 250:
            trimmed += "..."
        fields.append({
            "name": "\U0001f4f0 What's New",
            "value": f"**{info['motd_title']}**\n{trimmed}",
            "inline": False,
        })
    fields.append({
        "name": "\U0001f4d6 Patch Notes",
        "value": "[View on Fortnite News](https://www.fortnite.com/news)",
        "inline": False,
    })
    hotfix = info.get("kind") == "hotfix"
    return {
        "title": "\U0001f527 Fortnite Hotfix Detected!" if hotfix else "\U0001f680 Fortnite Update Detected!",
        "description": ("A new Fortnite hotfix patch is available for download!" if hotfix
                        else f"Fortnite {info['release']} is out and ready to download!"),
        "color": 0x00BFFF,
        "fields": fields,
        "footer": {"text": "Download size varies — check the Epic Games Launcher"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Game: VALORANT
# API: https://valorant-api.com/v1/version (free, no key)
# ---------------------------------------------------------------------------

def valorant_check():
    body = fetch_json("https://valorant-api.com/v1/version")
    if not body:
        return None
    data = body.get("data") or {}
    version = data.get("version")
    if not version:
        return None
    branch = data.get("branch", "?")
    release = branch.replace("release-", "") if branch.startswith("release-") else branch
    build_date = data.get("buildDate", "?")
    return {"version": version, "release": release, "build_date": build_date}


def valorant_embed(info):
    fields = [
        {"name": "Version", "value": info["release"], "inline": True},
        {"name": "Build", "value": info["version"], "inline": True},
    ]
    if info.get("build_date") and info["build_date"] != "?":
        fields.append(
            {"name": "Build Date", "value": info["build_date"], "inline": True}
        )
    fields.append({
        "name": "\U0001f4d6 Patch Notes",
        "value": "[View on VALORANT News](https://playvalorant.com/en-us/news/)",
        "inline": False,
    })
    return {
        "title": "\U0001f680 VALORANT Update Detected!",
        "description": "A new VALORANT update is available for download!",
        "color": 0xFD4556,
        "fields": fields,
        "footer": {"text": "Check the Riot Client for the update"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Game: CS2
# API: Steam ISteamNews/GetNewsForApp (free, no key)
# Detects updates by watching for new patch-notes entries in the Steam news
# feed — unlike ISteamApps/UpToDateCheck which only tracks the minimum
# required server version and misses most updates.
# ---------------------------------------------------------------------------

CS2_NEWS_URL = (
    "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
    "?appid=730&count=10&maxlength=300&format=json"
)


def cs2_check():
    news = fetch_json(CS2_NEWS_URL)
    if not news:
        return None
    items = (news.get("appnews") or {}).get("newsitems") or []
    for item in items:
        if "patchnotes" in (item.get("tags") or []):
            return {
                "version": item["gid"],
                "title": item.get("title", "CS2 Update"),
                "url": item.get("url", ""),
            }
    return None


def cs2_embed(info):
    title = info.get("title", "CS2 Update")
    url = info.get("url", "")
    fields = []
    if url:
        fields.append({
            "name": "\U0001f4f0 Patch Notes",
            "value": f"[{title}]({url})",
            "inline": False,
        })
    else:
        fields.append({
            "name": "\U0001f4d6 Patch Notes",
            "value": "[View on Steam](https://store.steampowered.com/news/app/730)",
            "inline": False,
        })
    return {
        "title": "\U0001f680 CS2 Update Detected!",
        "description": "A new Counter-Strike 2 update is available!",
        "color": 0xDE9B35,
        "fields": fields,
        "footer": {"text": "Steam will auto-update the game"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Game: Deadlock
# API: Steam ISteamNews/GetNewsForApp (free, no key), limited to Valve's own
# announcements feed. Every Deadlock patch ("Minor Update - 09-16-2026") and big
# update ("City Never Sleeps") is posted there; the press articles that fill the
# app's general news feed are left out by the feed filter.
# ---------------------------------------------------------------------------

DEADLOCK_NEWS_URL = (
    "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
    "?appid=1422450&count=5&maxlength=300&feeds=steam_community_announcements&format=json"
)


def deadlock_check():
    news = fetch_json(DEADLOCK_NEWS_URL)
    if not news:
        return None
    items = (news.get("appnews") or {}).get("newsitems") or []
    if not items:
        return None
    item = max(items, key=lambda i: i.get("date", 0))
    return {
        "version": item["gid"],
        "title": item.get("title", "Deadlock Update"),
        "url": item.get("url", ""),
        "patchnotes": "patchnotes" in (item.get("tags") or []),
    }


def deadlock_embed(info):
    title = info.get("title", "Deadlock Update")
    url = info.get("url") or "https://store.steampowered.com/news/app/1422450"
    is_patch = info.get("patchnotes") or "update" in title.lower()
    return {
        "title": "\U0001f680 Deadlock Update Detected!" if is_patch else "\U0001f4e2 Deadlock News from Valve",
        "description": ("A new Deadlock update is out! Steam will download it automatically."
                        if is_patch else "Valve posted a new Deadlock announcement."),
        "color": 0xC8A165,
        "fields": [{"name": "\U0001f4f0 Patch Notes" if is_patch else "\U0001f4f0 Announcement",
                    "value": f"[{title}]({url})", "inline": False}],
        "footer": {"text": "Deadlock is in early access on Steam"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Game registry — add new games here
# ---------------------------------------------------------------------------

GAMES = [
    {"slug": "fortnite",  "name": "Fortnite",  "check": fortnite_check,  "embed": fortnite_embed,
     "compare": fortnite_compare},
    {"slug": "valorant",  "name": "VALORANT",   "check": valorant_check,  "embed": valorant_embed},
    {"slug": "cs2",        "name": "CS2",        "check": cs2_check,       "embed": cs2_embed},
    {"slug": "deadlock",  "name": "Deadlock",   "check": deadlock_check,  "embed": deadlock_embed},
]


def default_compare(old, new):
    """Any change of the version key is an update."""
    return "update" if old.get("version") != new["version"] else None


# ---------------------------------------------------------------------------
# Main loop — iterates every registered game
# ---------------------------------------------------------------------------

def main():
    if not DISCORD_TOKEN:
        sys.exit("Missing DISCORD_TOKEN environment variable")
    if CHANNEL_ID in ("0", ""):
        sys.exit("Missing or invalid CHANNEL_ID environment variable")

    state = load_state()
    send_failures = 0

    for game in GAMES:
        slug = game["slug"]
        name = game["name"]
        logger.info("Checking %s...", name)

        try:
            info = game["check"]()
            if info is None:
                logger.warning("Could not fetch %s API; skipping", name)
                continue

            version = info["version"]
            old = state.get(slug) or {}
            cached = old.get("version")
            kind = (game.get("compare") or default_compare)(old, info) if cached is not None else None

            if kind in ("update", "hotfix"):
                info["kind"] = kind
                logger.info("[%s] New %s: %s (was %s)", name, kind, version, cached)
                if not send_discord_embed(game["embed"](info)):
                    send_failures += 1
                    logger.error("[%s] Discord send FAILED — not updating state so it retries next run", name)
                    continue
            elif kind == "silent":
                logger.info("[%s] Same release, details filled in (%s); no message", name, version)
            elif cached is None:
                logger.info("[%s] First run; seeding state with %s", name, version)
            else:
                logger.info("[%s] No update (current: %s)", name, version)
                # Leave the stored entry untouched so the state file only changes
                # when a version actually changes (no commit on quiet runs).
                continue

            state[slug] = {
                **{k: v for k, v in info.items() if k != "kind"},
                "seen_at": datetime.now(timezone.utc).isoformat(),
            }
        except Exception:
            logger.exception("Error checking %s; skipping", name)

    save_state(state)

    if send_failures:
        sys.exit(f"Failed to send {send_failures} Discord notification(s)")


if __name__ == "__main__":
    main()

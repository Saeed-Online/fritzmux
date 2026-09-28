"""Fuzzy matching of Fritzbox channel names to EPG ids and logo file names.

Fritzbox names look like "MDR Sachsen HD", "WDR HD Köln", "ProSieben",
EPG sources use "MDR.de", "WDR.de", "ProSieben.de", the AVM logo server
uses "mdr_hd.png", "wdr_hd.png", "pro7.png".
"""
import re
from typing import Iterable, Optional

_QUALITY_TOKENS = {"hd", "sd", "uhd", "4k"}

# Fritzbox name (compact) -> EPG id (compact)
EPG_ALIASES = {
    "rtlnitro": "nitro",
    "rtltelevision": "rtl",
    "swrbw": "swrsr",
    "swrrp": "swrsr",
    "swrbadenwuerttemberg": "swrsr",
    "swrrheinlandpfalz": "swrsr",
    "srfernsehen": "swrsr",
    "n24doku": "n24doku",
    "welt": "welt",
}

# Fritzbox name (compact) -> AVM logo name (compact)
LOGO_ALIASES = {
    "prosieben": "pro7",
    "prosiebenmaxx": "pro7maxx",
    "kabeleins": "kabel1",
    "kabeleinsdoku": "kabel1doku",
    "rtlzwei": "rtl2",
    "rtltelevision": "rtl",
    "nitro": "rtlnitro",
    "one": "onetv",
    "welt": "n24",
    "radiobrementv": "radiobremen",
    "swrbw": "swr",
    "swrrp": "swr",
    "srfernsehen": "sr",
}


def compact(name: str) -> str:
    n = name.lower().strip()
    n = n.replace("ü", "ue").replace("ö", "oe").replace("ä", "ae").replace("ß", "ss")
    n = n.replace("+", " plus ")
    n = re.sub(r"\.[a-z]{2}$", "", n)                  # EPG ids: "das.erste.de"
    tokens = [t for t in re.split(r"[^a-z0-9]+", n) if t and t not in _QUALITY_TOKENS]
    return "".join(tokens)


def best_match(names: Iterable[str], index: dict[str, str], aliases: dict[str, str]) -> Optional[str]:
    """index: compact key -> value. Exact match first, then the longest key
    the channel name starts with ("mdrsachsen" -> "mdr")."""
    candidates = []
    for name in names:
        if not name:
            continue
        c = compact(name)
        if not c:
            continue
        candidates.append(aliases.get(c, c))
    for c in candidates:
        if c in index:
            return index[c]
    for c in candidates:
        prefixes = [k for k in index if len(k) >= 2 and c.startswith(k)]
        if prefixes:
            return index[max(prefixes, key=len)]
    return None


def wants_hd(names: Iterable[str]) -> bool:
    return any(re.search(r"\b(hd|uhd)\b", (n or "").lower()) for n in names)

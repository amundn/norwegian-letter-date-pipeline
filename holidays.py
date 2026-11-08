"""Calendar dates for Norwegian holiday names found in the letter collection."""

import datetime as dt
import re


def easter_sunday(year):
    """Gregorian Easter, valid for the historical collection (1583 onward)."""
    if not 1583 <= year <= 4099:
        raise ValueError("Årstallet ligger utenfor påskeberegningens gyldighetsområde.")
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return dt.date(year, month, day + 1)


def sunday_on_or_after(day):
    return day + dt.timedelta(days=(6 - day.weekday()) % 7)


# Longest and qualified names precede shorter names to avoid partial matches.
PATTERNS = [
    (r"bittelille[- ]?julaften", "dec22"),
    (r"lille[- ]?julaften", "dec23"),
    (r"julaften", "dec24"),
    (r"(?:1\.?\s*)?juledag|første\s+juledag", "dec25"),
    (r"2\.?\s*juledag|andre\s+juledag|stefani\s*dag", "dec26"),
    (r"nyttårsaften", "dec31"),
    (r"(?:1\.?\s*)?nyttårsdag|første\s+nyttårsdag", "jan1"),
    (r"2\.?\s*nyttårsdag|andre\s+nyttårsdag", "jan2"),
    (r"palmesøndag", "easter-7"),
    (r"påskeonsdag(?:en)?", "easter-4"),
    (r"skjærtorsdag", "easter-3"),
    (r"langfredag", "easter-2"),
    (r"påskeaften|påskelørdag", "easter-1"),
    (r"(?:1\.?\s*)?påskedag|første\s+påskedag|1\.?\s*dag\s+påske", "easter0"),
    (r"2\.?\s*påskedag|andre\s+påskedag|2\.?\s*dag\s+påske", "easter1"),
    (r"3\.?\s*påskedag|tredje\s+påskedag|3\.?\s*dag\s+påske", "easter2"),
    (r"(?:kristi\s+)?himmelfartsdag", "easter39"),
    (r"pinsaften", "easter48"),
    (r"(?:1\.?\s*)?pinsedag|første\s+pinsedag|1\.?\s*dag\s+pinse", "easter49"),
    (r"2\.?\s*pinsedag|andre\s+pinsedag|2\.?\s*dag\s+pinse", "easter50"),
    (r"fastelavnssøndag", "easter-49"),
    (r"fastelavnsmandag", "easter-48"),
    (r"(?:bots[- ]\s*og\s*)?(?:bede|bønne)dag", "bededag"),
    (r"allehelgens(?:dag|søndag)", "allehelgen"),
    (r"(?:1\.?|første)\s+søndag\s+i\s+advent", "advent1"),
    (r"sommerdagen", "apr14"),
    (r"olsok(?:[- ]?dagen)?", "jul29"),
    (r"(?:st\.?\s*hans|sankthans)(?:[- ]?aften)", "jun23"),
    (r"(?:st\.?\s*hans|sankthans)(?:[- ]?dag(?:en)?)", "jun24"),
    (r"morsdag(?:en)?", "morsdag"),
    # The source corpus uses the compound form “17. maifesten”; allow that
    # inflection while still requiring a word boundary after the full term.
    (r"(?:nasjonaldagen|17\.?\s*mai(?:fest(?:en)?)?)", "may17"),
    (r"(?:arbeidernes\s+dag|1\.?\s*mai)", "may1"),
    (r"([1-6])\.?\s*dag\s+(?:jul|i\s+jul(?:en)?)", "christmas_ordinal"),
]


def identify_holiday(text):
    """Return one unambiguous named day and its matched text, or None."""
    matches = []
    for pattern, key in PATTERNS:
        for match in re.finditer(r"(?<![\w-])(?:" + pattern + r")(?![\w-])", text, re.I):
            matches.append((match.start(), match.end(), key, match.group(0)))
    # Suppress shorter matches inside a longer named day, e.g. julaften in lillejulaften.
    matches = [item for item in matches if not any(
        other[0] <= item[0] and item[1] <= other[1] and
        (other[0], other[1]) != (item[0], item[1]) for other in matches)]
    if len(matches) != 1:
        return None
    _, _, key, matched = matches[0]
    if key == "christmas_ordinal":
        key = "dec" + str(24 + int(matched[0]))
    return key, matched


def holiday_date(key, year):
    fixed = {"dec22": (12, 22), "dec23": (12, 23), "dec24": (12, 24),
             "dec25": (12, 25), "dec26": (12, 26), "dec27": (12, 27),
             "dec28": (12, 28), "dec29": (12, 29), "dec30": (12, 30),
             "dec31": (12, 31), "jan1": (1, 1), "jan2": (1, 2),
             "apr14": (4, 14), "jul29": (7, 29), "jun23": (6, 23),
             "jun24": (6, 24), "may17": (5, 17), "may1": (5, 1)}
    if key in fixed:
        return dt.date(year, *fixed[key])
    if key.startswith("easter"):
        return easter_sunday(year) + dt.timedelta(days=int(key[6:]))
    if key == "morsdag":
        if year < 1919:
            raise ValueError("Norsk morsdag er ikke kjent for dette året.")
        return sunday_on_or_after(dt.date(year, 2, 1)) + dt.timedelta(days=7)
    if key == "allehelgen":
        if year < 1770:
            raise ValueError("Historisk datering av allehelgensdag er ikke støttet for dette året.")
        return sunday_on_or_after(dt.date(year, 11, 1))
    if key == "bededag":
        if year < 1951:
            raise ValueError("Historisk datering av bededag er ikke støttet for dette året.")
        return sunday_on_or_after(dt.date(year, 11, 1)) - dt.timedelta(days=7)
    if key == "advent1":
        return sunday_on_or_after(dt.date(year, 11, 27))
    raise ValueError("Ukjent merkedag: " + key)

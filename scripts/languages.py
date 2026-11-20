import json
import os
import re
import sys
from collections import Counter
from html import escape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def repositories(value):
    names = list(dict.fromkeys(re.split(r"[\s,]+", value.strip())))
    if not names or any(
        not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", name)
        or name.split("/")[1] in (".", "..")
        for name in names
    ):
        raise ValueError("Set LANGUAGES_REPOS to owner/repo entries separated by commas or newlines.")
    return names


def aggregate(names, token):
    totals = Counter()
    for name in names:
        request = Request(
            f"https://api.github.com/repos/{name}/languages",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                data = json.load(response)
        except HTTPError as error:
            raise ValueError(
                f"GitHub returned HTTP {error.code}. Check token expiry, selected repositories, "
                "Metadata read permission, and any required organization approval."
            ) from None
        except (URLError, TimeoutError, json.JSONDecodeError):
            raise ValueError("Could not read GitHub language metadata; retry the workflow.") from None
        if not isinstance(data, dict) or any(
            not isinstance(language, str) or type(size) is not int or size < 0
            for language, size in data.items()
        ):
            raise ValueError("GitHub returned invalid language metadata.")
        totals.update(data)
    if not sum(totals.values()):
        raise ValueError("The selected repositories have no detected language data.")
    return totals


def render(totals):
    ranked = sorted(
        ((name, size) for name, size in totals.items() if size > 0),
        key=lambda item: (-item[1], item[0]),
    )
    total = sum(size for _, size in ranked)
    if not total:
        raise ValueError("Cannot render an empty language chart.")
    rows = ranked[:8]
    if len(ranked) > 8:
        rows.append(("Other", sum(size for _, size in ranked[8:])))
    colors = ["#77e4d2", "#8ea7ff", "#d6a0ff", "#ffcb6b", "#f78c6c", "#89ddff", "#c3e88d", "#f07178", "#a6accd"]
    height = 88 + len(rows) * 28
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="420" height="{height}" viewBox="0 0 420 {height}" role="img" aria-labelledby="title desc">',
        '<title id="title">Languages across selected repositories</title>',
        '<desc id="desc">Share of detected code bytes, including selected private repositories. Not a measure of proficiency or personal contribution.</desc>',
        f'<rect x="1" y="1" width="418" height="{height - 2}" rx="14" fill="#171b32" stroke="#424c70"/>',
        '<g font-family="system-ui, sans-serif">',
        '<text x="22" y="32" fill="#a9b8ff" font-size="18" font-weight="600">Languages in selected repositories</text>',
        '<text x="22" y="54" fill="#a6accd" font-size="11">Public + private · share of detected code bytes</text>',
    ]
    for index, (language, size) in enumerate(rows):
        y = 85 + index * 28
        percentage = size / total * 100
        label = f"{percentage:.1f}%" if percentage >= 0.1 else "&lt;0.1%"
        parts.extend([
            f'<circle cx="27" cy="{y - 4}" r="5" fill="{colors[index]}"/>',
            f'<text x="41" y="{y}" fill="#dce4ff" font-size="13">{escape(language)}</text>',
            f'<text x="397" y="{y}" fill="#a6accd" font-size="13" text-anchor="end">{label}</text>',
        ])
    parts.append("</g></svg>")
    return "\n".join(parts) + "\n"


def main():
    token = os.environ.get("LANGUAGES_TOKEN", "").strip()
    if not token:
        raise ValueError("Add the LANGUAGES_TOKEN Actions secret before running this workflow.")
    names = repositories(os.environ.get("LANGUAGES_REPOS", ""))
    chart = render(aggregate(names, token))
    Path("assets/languages.svg").write_text(chart, encoding="utf-8")
    print(f"Fetched language metadata from {len(names)} selected repositories.")
    print("Updated aggregate language chart. Repository names and raw responses were not published.")


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        print(f"Language chart failed: {error}", file=sys.stderr)
        sys.exit(1)

import hashlib
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, time, timedelta, timezone
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


def github_api(path, token, payload=None):
    request = Request(
        f"https://api.github.com/{path}",
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        raise ValueError(
            f"GitHub returned HTTP {error.code}. Check token expiry, selected repositories, "
            "required read permissions, and any required organization approval."
        ) from None
    except (URLError, TimeoutError, json.JSONDecodeError):
        raise ValueError("Could not read GitHub metadata; retry the workflow.") from None


def aggregate(names, token):
    totals = Counter()
    for name in names:
        data = github_api(f"repos/{name}/languages", token)
        if not isinstance(data, dict) or any(
            not isinstance(language, str) or type(size) is not int or size < 0
            for language, size in data.items()
        ):
            raise ValueError("GitHub returned invalid language metadata.")
        totals.update(data)
    if not sum(totals.values()):
        raise ValueError("The selected repositories have no detected language data.")
    return totals


CONTRIBUTIONS_QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  viewer { login }
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      commits: commitContributionsByRepository(maxRepositories: 100) {
        repository { nameWithOwner isPrivate }
        contributions(first: 100) {
          nodes { commitCount }
          pageInfo { hasNextPage }
        }
      }
      issues: issueContributionsByRepository(
        maxRepositories: 100, excludeFirst: false, excludePopular: false
      ) {
        repository { nameWithOwner isPrivate }
        contributions { totalCount }
      }
      prs: pullRequestContributionsByRepository(
        maxRepositories: 100, excludeFirst: false, excludePopular: false
      ) {
        repository { nameWithOwner isPrivate }
        contributions { totalCount }
      }
      reviews: pullRequestReviewContributionsByRepository(maxRepositories: 100) {
        repository { nameWithOwner isPrivate }
        contributions { totalCount }
      }
    }
  }
}
"""


def personal_contributions(names, token, now=None):
    now = now or datetime.now(timezone.utc)
    start = datetime.combine(now.date() - timedelta(days=364), time.min, timezone.utc)
    selected = {name.lower() for name in names}
    totals = Counter()
    while start <= now:
        # A commit-contribution node represents a day, not one commit.
        # Short windows fit all daily nodes without truncating the connection.
        end = min(start + timedelta(days=28) - timedelta(seconds=1), now)
        result = github_api("graphql", token, {
            "query": CONTRIBUTIONS_QUERY,
            "variables": {
                "login": "dil-mezzy",
                "from": start.isoformat(),
                "to": end.isoformat(),
            },
        })
        if result.get("errors"):
            raise ValueError(
                "GitHub could not read personal contributions. Check token permissions "
                "and access to private contributions; partial results will not be published."
            )
        viewer = (result.get("data") or {}).get("viewer") or {}
        if viewer.get("login", "").lower() != "dil-mezzy":
            raise ValueError("The token must belong to dil-mezzy to read their private personal contributions.")
        user = (result.get("data") or {}).get("user")
        if not user or not isinstance(user.get("contributionsCollection"), dict):
            raise ValueError("GitHub returned no personal contribution data.")
        collection = user["contributionsCollection"]
        for category in ("commits", "prs", "issues", "reviews"):
            groups = collection.get(category)
            if not isinstance(groups, list):
                raise ValueError("GitHub returned invalid personal contribution data.")
            if len(groups) >= 100:
                raise ValueError("Contribution repository limit reached; refusing to publish truncated statistics.")
            for group in groups:
                repository = group.get("repository")
                if not repository:
                    raise ValueError("Some contribution repositories are inaccessible; check token permissions.")
                chosen = repository["nameWithOwner"].lower() in selected
                if repository["isPrivate"] and not chosen:
                    continue
                contributions = group["contributions"]
                if category == "commits":
                    if contributions["pageInfo"]["hasNextPage"]:
                        raise ValueError("Commit contribution pagination limit reached; statistics were not published.")
                    count = sum(node["commitCount"] for node in contributions["nodes"])
                else:
                    count = contributions["totalCount"]
                if type(count) is not int or count < 0:
                    raise ValueError("GitHub returned an invalid contribution count.")
                totals[category] += count
                if chosen:
                    totals["selected"] += count
        start = end + timedelta(seconds=1)
    return totals


def render_stats(stats):
    rows = [
        ("Commits", f'{stats["commits"]:,}'),
        ("Pull requests opened", f'{stats["prs"]:,}'),
        ("Issues opened", f'{stats["issues"]:,}'),
        ("Pull request reviews", f'{stats["reviews"]:,}'),
        ("Total of the above", f'{sum(stats[key] for key in ("commits", "prs", "issues", "reviews")):,}'),
        ("From selected repos (included)", f'{stats["selected"]:,}'),
    ]
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="420" height="280" viewBox="0 0 420 280" role="img" aria-labelledby="title desc">',
        '<title id="title">Moiz Ezzy: personal contributions</title>',
        '<desc id="desc">Personal commits, pull requests, issues, and reviews over the past 365 days. Includes public repositories and selected private repositories without double-counting.</desc>',
        '<rect x="1" y="1" width="418" height="278" rx="14" fill="#171b32" stroke="#424c70"/>',
        '<g font-family="system-ui, sans-serif">',
        '<text x="22" y="32" fill="#a9b8ff" font-size="18" font-weight="600">Moiz Ezzy: personal contributions</text>',
        '<text x="22" y="54" fill="#a6accd" font-size="11">Past 365 days · public + selected private repositories</text>',
    ]
    for index, (label, value) in enumerate(rows):
        y = 87 + index * 28
        parts.extend([
            f'<circle cx="27" cy="{y - 4}" r="4" fill="#77e4d2"/>',
            f'<text x="41" y="{y}" fill="#dce4ff" font-size="13">{label}</text>',
            f'<text x="397" y="{y}" fill="#a9b8ff" font-size="13" text-anchor="end">{value}</text>',
        ])
    parts.extend([
        '<text x="22" y="264" fill="#a6accd" font-size="11">Selected-repo activity is included, not added twice.</text>',
        "</g></svg>",
    ])
    return "\n".join(parts) + "\n"


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
    languages = aggregate(names, token)
    stats = personal_contributions(names, token)
    charts = {
        "languages": render(languages),
        "repository-stats": render_stats(stats),
    }
    readme = Path("README.md")
    updated_readme = readme.read_text(encoding="utf-8")
    for name, chart in charts.items():
        version = hashlib.sha256(chart.encode("utf-8")).hexdigest()[:12]
        updated_readme, replacements = re.subn(
            rf'src="assets/{re.escape(name)}\.svg(?:\?v=[a-f0-9]+)?"',
            f'src="assets/{name}.svg?v={version}"',
            updated_readme,
        )
        if replacements != 1:
            raise ValueError(f"Expected exactly one {name} chart image in README.md.")
    for name, chart in charts.items():
        Path(f"assets/{name}.svg").write_text(chart, encoding="utf-8")
    readme.write_text(updated_readme, encoding="utf-8")
    print(f"Fetched language and stats metadata from {len(names)} selected repositories.")
    print("Updated aggregate charts. Repository names and raw responses were not published.")


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        print(f"Language chart failed: {error}", file=sys.stderr)
        sys.exit(1)

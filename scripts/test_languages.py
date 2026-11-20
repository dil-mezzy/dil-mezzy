import io
import tempfile
import unittest
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from languages import aggregate, main, personal_contributions, render, render_stats, repositories


class LanguagesTests(unittest.TestCase):
    def test_selected_repositories_only_and_deduplication(self):
        names = repositories("owner/one,\nowner/two owner/one")
        self.assertEqual(names, ["owner/one", "owner/two"])
        with patch("languages.urlopen", side_effect=[
            io.BytesIO(b'{"Python": 30, "HCL": 10}'),
            io.BytesIO(b'{"Python": 10, "TypeScript": 50}'),
        ]) as fetch:
            self.assertEqual(aggregate(names, "test-token"), {"Python": 40, "HCL": 10, "TypeScript": 50})
            self.assertEqual(fetch.call_count, 2)

    def test_invalid_repo_names_rejected(self):
        for value in ("", "owner/repo?token=secret", "../repo", "owner/..", "https://github.com/owner/repo"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                repositories(value)

    def test_chart_is_escaped_aggregate_only(self):
        chart = render({"Python": 75, "C&<": 25})
        ET.fromstring(chart)
        self.assertIn("75.0%", chart)
        self.assertIn("25.0%", chart)
        self.assertIn("C&amp;&lt;", chart)

    def test_small_languages_grouped_without_losing_totals(self):
        chart = render({f"Language {index}": 10 for index in range(10)})
        self.assertIn(">Other</text>", chart)
        self.assertIn("20.0%", chart)

    def test_failed_fetch_does_not_expose_repository_or_token(self):
        error = HTTPError("https://api.github.com/repos/private/name", 404, "private/name", {}, None)
        with patch("languages.urlopen", side_effect=error):
            with self.assertRaises(ValueError) as caught:
                aggregate(["private/name"], "secret-token")
        self.assertIn("HTTP 404", str(caught.exception))
        self.assertNotIn("private/name", str(caught.exception))
        self.assertNotIn("secret-token", str(caught.exception))

    def test_empty_metadata_fails_instead_of_publishing_zero_data(self):
        with patch("languages.urlopen", return_value=io.BytesIO(b"{}")):
            with self.assertRaisesRegex(ValueError, "no detected language data"):
                aggregate(["owner/empty"], "test-token")

    def test_chart_link_changes_only_when_chart_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "assets").mkdir()
            readme = root / "README.md"
            readme.write_text(
                '<img src="assets/languages.svg" alt="Languages" />'
                '<img src="assets/repository-stats.svg" alt="Stats" />',
                encoding="utf-8",
            )
            with patch("languages.Path", side_effect=lambda path: root / path), patch.dict(
                "os.environ", {"LANGUAGES_TOKEN": "test-token", "LANGUAGES_REPOS": "owner/repo"}
            ), patch("languages.personal_contributions", return_value=Counter(commits=3)), patch(
                "languages.aggregate", return_value={"Python": 100}
            ) as fetch:
                main()
                first = readme.read_text(encoding="utf-8")
                self.assertIn("assets/languages.svg?v=", first)
                self.assertIn("assets/repository-stats.svg?v=", first)
                main()
                self.assertEqual(readme.read_text(encoding="utf-8"), first)
                fetch.return_value = {"Python": 50, "TypeScript": 50}
                main()
                self.assertNotEqual(readme.read_text(encoding="utf-8"), first)
                ET.parse(root / "assets/languages.svg")
                ET.parse(root / "assets/repository-stats.svg")

    def test_personal_stats_include_public_and_selected_private_without_duplicates(self):
        def group(name, private, count, commits=False):
            contributions = (
                {"nodes": [{"commitCount": count}], "pageInfo": {"hasNextPage": False}}
                if commits else {"totalCount": count}
            )
            return {
                "repository": {"nameWithOwner": name, "isPrivate": private},
                "contributions": contributions,
            }

        collection = {
            "commits": [
                group("owner/public", False, 3, commits=True),
                group("owner/private", True, 5, commits=True),
                group("owner/excluded", True, 100, commits=True),
            ],
            "prs": [group("owner/private", True, 2)],
            "issues": [group("owner/public", False, 1)],
            "reviews": [group("owner/other-public", False, 4)],
        }
        result = {"data": {
            "viewer": {"login": "dil-mezzy"},
            "user": {"contributionsCollection": collection},
        }}
        with patch("languages.github_api", return_value=result) as api:
            stats = personal_contributions(
                ["owner/public", "owner/private"], "test-token",
                now=datetime(2026, 11, 20, 12, tzinfo=timezone.utc),
            )
        calls = api.call_args_list
        windows = len(calls)
        self.assertEqual(stats["commits"], 8 * windows)
        self.assertEqual(stats["prs"], 2 * windows)
        self.assertEqual(stats["selected"], 11 * windows)
        self.assertEqual(stats["reviews"], 4 * windows)
        previous_end = None
        for call in calls:
            variables = call.args[2]["variables"]
            start = datetime.fromisoformat(variables["from"])
            end = datetime.fromisoformat(variables["to"])
            self.assertLess(end - start, timedelta(days=28))
            if previous_end is not None:
                self.assertEqual(start, previous_end + timedelta(seconds=1))
            previous_end = end
        chart = render_stats(stats)
        ET.fromstring(chart)
        self.assertNotIn("owner/", chart)
        self.assertIn("personal contributions", chart)

    def test_private_contribution_errors_fail_instead_of_publishing_partial_totals(self):
        with patch("languages.github_api", return_value={
            "errors": [{"message": "Access denied to private/repository"}],
        }):
            with self.assertRaisesRegex(ValueError, "partial results will not be published") as caught:
                personal_contributions(["private/repository"], "test-token")
        self.assertNotIn("private/repository", str(caught.exception))

    def test_commit_connection_is_not_silently_truncated(self):
        result = {"data": {
            "viewer": {"login": "dil-mezzy"},
            "user": {"contributionsCollection": {
                "commits": [{
                    "repository": {"nameWithOwner": "owner/private", "isPrivate": True},
                    "contributions": {"pageInfo": {"hasNextPage": True}, "nodes": []},
                }],
            }},
        }}
        with patch("languages.github_api", return_value=result):
            with self.assertRaisesRegex(ValueError, "pagination limit"):
                personal_contributions(["owner/private"], "test-token")


if __name__ == "__main__":
    unittest.main()

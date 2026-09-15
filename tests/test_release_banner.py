"""The newer-release banner: which GitHub releases count, and which are newer than the running manager."""

import unittest

from custom_components.integration_manager import manager_device as md


def _rel(tag, **kw):
    return {"tag_name": tag, "name": kw.get("name", tag), "html_url": f"https://example.invalid/{tag}",
            "published_at": "2026-09-15T10:00:00Z", "draft": kw.get("draft", False), "prerelease": kw.get("prerelease", False)}


class StableReleasesTest(unittest.TestCase):
    def test_drafts_prereleases_and_betas_are_left_out(self):
        rows = md._stable_releases([_rel("v0.14.0"), _rel("v0.15.0", draft=True), _rel("v0.15.0b1"),
                                    _rel("v0.14.1", prerelease=True), _rel("v0.13.0")])
        self.assertEqual([r["tag"] for r in rows], ["v0.14.0", "v0.13.0"])

    def test_newest_first_by_version_not_by_order(self):
        rows = md._stable_releases([_rel("v0.9.0"), _rel("v0.13.1"), _rel("v0.13.0"), _rel("v0.10.0")])
        self.assertEqual([r["version"] for r in rows], ["0.13.1", "0.13.0", "0.10.0", "0.9.0"])

    def test_bad_payload(self):
        self.assertEqual(md._stable_releases({"message": "API rate limit exceeded"}), [])
        self.assertEqual(md._stable_releases([None, "x", {}]), [])


class NewerReleasesTest(unittest.TestCase):
    rows = md._stable_releases([_rel("v0.15.0"), _rel("v0.14.1"), _rel("v0.14.0"), _rel("v0.13.0"), _rel("v0.12.0")])

    def test_only_newer_than_running(self):
        self.assertEqual([r["tag"] for r in md._newer_releases(self.rows, "0.13.0")], ["v0.15.0", "v0.14.1", "v0.14.0"])

    def test_up_to_date_or_ahead(self):
        self.assertEqual(md._newer_releases(self.rows, "0.15.0"), [])
        self.assertEqual(md._newer_releases(self.rows, "0.16.0"), [])

    def test_no_version_or_no_list(self):
        self.assertEqual(md._newer_releases(self.rows, ""), [])
        self.assertEqual(md._newer_releases(None, "0.13.0"), [])


if __name__ == "__main__":
    unittest.main()

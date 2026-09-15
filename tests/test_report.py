from app.review.report import MAX_INLINE_COMMENTS, FileReport, build_report, sanitize

PATCH = "@@ -10,3 +10,4 @@ def total(items):\n     s = 0\n-    s += 1\n+    s += i.price\n+    log(s)\n     return s\n"

REVIEW = """### Summary
Changes totals.

### Findings
- **[major]** `shop/orders.py:11` — price ignores quantity — multiply by i.qty
- **[minor]** `orders.py:40` — unrelated helper lacks a docstring
- **[nit]** consider a clearer variable name
- **[minor]** `shop/users.py:3` — mirrors the pattern in users.py

### Verdict
Request changes.
"""


def reviewed(**overrides) -> FileReport:
    values = {
        "review_id": 5,
        "path": "shop/orders.py",
        "status": "complete",
        "cache_hit": False,
        "review_text": REVIEW,
        "patch": PATCH,
    }
    values.update(overrides)
    return FileReport(**values)


def test_inline_comments_only_on_diff_lines_and_rest_go_to_the_body() -> None:
    report = build_report([reviewed()], unit_id=77, public_url="https://codelens.example.com/")

    assert [(c.path, c.line) for c in report.inline_comments] == [("shop/orders.py", 11)]
    assert report.inline_comments[0].body.startswith("**[major]** price ignores quantity")
    body = report.review_body
    assert (
        "`shop/orders.py:40`: **[minor]** unrelated helper" in body
    )  # line 40 is outside the hunk
    assert "`shop/orders.py`: **[nit]** consider a clearer variable name" in body  # no location
    assert "`shop/orders.py`: **[minor]** mirrors the pattern" in body  # another file's line
    assert body.rstrip().endswith("<!-- codelens:unit:77 -->")  # idempotency marker


def test_annotations_cover_every_located_finding_for_the_reviewed_file() -> None:
    report = build_report([reviewed()], unit_id=1, public_url="http://x")
    assert [(a.path, a.start_line, a.annotation_level) for a in report.annotations] == [
        ("shop/orders.py", 11, "warning"),
        ("shop/orders.py", 40, "notice"),  # annotations may point outside the diff
    ]
    assert report.title == "4 findings (1 major, 2 minor, 1 nit)"


def test_summary_table_links_reviews_and_shows_skips_failures_and_cache() -> None:
    report = build_report(
        [
            reviewed(cache_hit=True),
            FileReport(
                review_id=6,
                path="package-lock.json",
                status="skipped",
                cache_hit=False,
                skip_reason="generated lockfile",
            ),
            FileReport(
                review_id=7, path="shop/pay.py", status="failed", cache_hit=False, error="429"
            ),
        ],
        unit_id=1,
        public_url="https://codelens.example.com",
    )
    assert (
        "[`shop/orders.py`](https://codelens.example.com/reviews/5) | reviewed (cached) | 4 |"
        in report.summary
    )
    assert (
        "| [`package-lock.json`](https://codelens.example.com/reviews/6) | skipped: generated lockfile | - |"
        in report.summary
    )
    assert "| review failed |" in report.summary
    assert "1 served from cache" in report.summary
    assert report.title.endswith("· 1 file failed")
    assert report.conclusion in {"success", "neutral", "failure"}


def test_no_findings_and_all_failed_titles() -> None:
    clean = reviewed(review_text="### Summary\nok\n\n### Findings\nNo issues found.\n")
    assert build_report([clean], unit_id=1, public_url="http://x").title == "No issues found"
    broken = FileReport(review_id=1, path="a.py", status="failed", cache_hit=False)
    assert (
        build_report([broken], unit_id=1, public_url="http://x").title == "Review failed for 1 file"
    )


def test_inline_comment_cap_overflows_into_body() -> None:
    lines = "\n".join(f"+x{i} = {i}" for i in range(60))
    patch = f"@@ -0,0 +1,60 @@\n{lines}\n"
    findings = "\n".join(f"- **[nit]** `a.py:{i}` — rename x{i}" for i in range(1, 51))
    report = build_report(
        [reviewed(path="a.py", patch=patch, review_text=f"### Findings\n{findings}\n")],
        unit_id=1,
        public_url="http://x",
    )
    assert len(report.inline_comments) == MAX_INLINE_COMMENTS
    assert report.review_body.count("rename x") == 50 - MAX_INLINE_COMMENTS
    assert len(report.annotations) == 50


def test_sanitize_defuses_mentions_and_tracking_images() -> None:
    text = "ping @octocat and @acme/security ![pixel](https://evil.example/t.gif) <img src=x> email a@b.com `@decorator`"
    cleaned = sanitize(text)
    assert "@octocat" not in cleaned and "@\u200boctocat" in cleaned
    assert "@\u200bacme/security" in cleaned
    assert "evil.example" not in cleaned and "<img" not in cleaned
    assert "a@b.com" in cleaned and "`@decorator`" in cleaned  # emails and code untouched

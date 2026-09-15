from app.review.findings import Finding, parse_findings

REVIEW = """### Summary
Adds discounts.

### Findings
- **[major]** `shop/orders.py:18` — discount_pct is not validated — reject values outside 0..100
- **[nit]** `shop/orders.py:14-16` — name `gross` is unclear — rename to `subtotal`
- **[blocker]** `shop/payments.py:L7` - refund can exceed the order total
  when amount is None — clamp to order.total
1. [minor] shop/orders.py:3: unused import Decimal
- **[minor]** consider adding tests for the new discount path

### Verdict
Request changes.
"""


def test_parses_severity_location_and_message() -> None:
    findings = parse_findings(REVIEW)
    assert findings[0] == Finding(
        "major",
        "discount_pct is not validated — reject values outside 0..100",
        "shop/orders.py",
        18,
        18,
    )
    assert (findings[1].severity, findings[1].start_line, findings[1].end_line) == ("nit", 14, 16)


def test_tolerates_hyphens_L_prefix_and_wrapped_bullets() -> None:
    blocker = parse_findings(REVIEW)[2]
    assert (blocker.severity, blocker.path, blocker.start_line) == (
        "blocker",
        "shop/payments.py",
        7,
    )
    assert (
        blocker.message
        == "refund can exceed the order total when amount is None — clamp to order.total"
    )


def test_numbered_bullets_and_unbacked_locations() -> None:
    minor = parse_findings(REVIEW)[3]
    assert (minor.severity, minor.path, minor.start_line) == ("minor", "shop/orders.py", 3)


def test_finding_without_location_is_kept_but_unplaced() -> None:
    last = parse_findings(REVIEW)[4]
    assert last == Finding("minor", "consider adding tests for the new discount path")


def test_only_the_findings_section_is_parsed() -> None:
    assert len(parse_findings(REVIEW)) == 5  # Summary and Verdict text are ignored
    assert parse_findings("### Summary\n- `a.py:1` looks odd\n") == []


def test_no_issues_found() -> None:
    assert (
        parse_findings("### Summary\nok\n\n### Findings\nNo issues found.\n\n### Verdict\nApprove")
        == []
    )
    assert parse_findings("### Findings\n- No issues found.\n") == []


def test_fake_provider_unit_titles_are_located() -> None:
    review = "### Findings\n- **[minor]** `function_definition fetch_order (shop/orders.py:4-5)` — add a test.\n"
    [finding] = parse_findings(review)
    assert (finding.path, finding.start_line, finding.end_line) == ("shop/orders.py", 4, 5)
    assert finding.message == "add a test."


def test_reversed_range_and_case_insensitive_heading() -> None:
    [finding] = parse_findings("## FINDINGS\n* [MAJOR] `x/y.go:30-28` race on counter\n")
    assert (finding.severity, finding.start_line, finding.end_line) == ("major", 28, 30)

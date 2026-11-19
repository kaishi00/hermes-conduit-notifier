"""The PR description's Desktop section check (.github/scripts/check_pr_desktop.py)."""

import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "check_pr_desktop", os.path.join(ROOT, ".github", "scripts", "check_pr_desktop.py"))
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)


def body(desktop, after="## Testing\nRan it.\n"):
    return f"## Summary\nA change.\n\n## Siblings\n- None.\n\n## Desktop\n{desktop}\n\n{after}"


def test_each_answer_passes():
    for desktop in ("Done: hermes-conduit-desktop#58.", "Done (desktop#58)",
                    "Not needed: a relay change.", "Follow-up: desktop#60."):
        assert check.problem(body(desktop)) is None, desktop


def test_answers_that_say_too_little_fail():
    assert "empty" in check.problem(body("<!-- Done, Not needed or Follow-up -->"))
    assert "names the desktop PR" in check.problem(body("Done: #911."))
    assert "says why" in check.problem(body("Not needed."))
    assert "starts with" in check.problem(body("Later."))
    assert "no \"## Desktop\" section" in check.problem("## Summary\n")
    for placeholder in ("later", "TBD", "none", "N/A"):
        assert "Follow-up" in check.problem(body(f"Follow-up: {placeholder}")), placeholder
    assert "names the desktop PR" in check.problem(body("Done.\nTracked as desktop#58."))


def test_a_fenced_heading_isnt_the_section():
    quoted = "## Summary\n```\n## Desktop\nDone: desktop#58.\n```\n\n"
    assert "inside a ``` fence" in check.problem(quoted)
    assert check.problem(quoted + "## Desktop\nNot needed: template only.\n") is None
    nested = "## Summary\n````\n```\n## Desktop\nDone: desktop#58.\n```\n````\n"
    assert "inside a ``` fence" in check.problem(nested)
    assert "inside a ``` fence" in check.problem("## Summary\n```\n## Desktop\nDone: desktop#58.\n")


def test_fences_end_as_github_draws_them():
    # Four spaces of indent is an indented code block, not a fence.
    indented = "## Summary\nRun:\n\n    ```\n    npm test\n\n## Desktop\nNot needed: docs only.\n"
    assert check.problem(indented) is None
    # A closing line of the other character doesn't end the block.
    mixed = "## Summary\n```\n```~\n## Desktop\nDone: desktop#58.\n```\n"
    assert "inside a ``` fence" in check.problem(mixed)


def test_done_names_a_pr_not_an_issue():
    issue = "Done: https://github.com/kaishi00/hermes-conduit-desktop/issues/59"
    assert "An issue goes under Follow-up" in check.problem(body(issue))
    assert check.problem(body(issue.replace("Done", "Follow-up"))) is None


def test_the_pr_template_asks_for_it():
    with open(os.path.join(ROOT, ".github", "pull_request_template.md"), encoding="utf-8") as fh:
        assert "empty" in check.problem(fh.read())


def test_it_is_the_apps_copy():
    """The app's scripts/check-pr-desktop.py is the same check; only its
    docstring differs. Compared when a hermes-conduit checkout sits beside
    this one."""
    app = os.path.join(os.path.dirname(ROOT), "hermes-conduit", "scripts", "check-pr-desktop.py")
    if not os.path.isfile(app):
        return
    def code(path):
        text = open(path, encoding="utf-8").read()
        return text[text.index('"""', text.index('"""') + 3) + 3:]
    assert code(app) == code(os.path.join(ROOT, ".github", "scripts", "check_pr_desktop.py"))

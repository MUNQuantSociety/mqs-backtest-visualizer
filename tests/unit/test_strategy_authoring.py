"""Fragment authoring: assembly, line remapping, and the scaffold's integrity.

Step 1 of ``docs/ondata-only-authoring-plan.md``. The numbered classes below
map to the plan's verification list, because the value of these is entirely in
what they refuse to let regress: a member who writes eight lines and is told
about a problem on line 31 has been told nothing.
"""

import ast
import re
import pathlib

import pytest

from src.services.strategy_validation.authoring import (
    Assembled,
    IndicatorSpec,
    ScaffoldEscape,
    StrategyDraft,
    assemble,
    check_draft,
)
from src.services.strategy_validation.scanning import check_compatibility
from src.services.strategy_validation import template


def _starter_fragment() -> str:
    """The OnData body out of the starter template, at column 0.

    Read from the template rather than copied, so the fragment these tests
    prove assemblable is the one the editor actually seeds.
    """
    lines = template.STARTER_SOURCE.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().startswith("def OnData"))
    fragment = [
        line[8:] if line.startswith(" " * 8) else line for line in lines[start + 1 :]
    ]
    while fragment and not fragment[0].strip():
        fragment.pop(0)
    while fragment and not fragment[-1].strip():
        fragment.pop()
    return "\n".join(fragment)


class TestOffsetMapping:
    """1. The off-by-one regression test, and the most valuable one here."""

    @pytest.mark.parametrize("line_number", [1, 2, 3, 4, 5])
    def test_a_bad_line_is_reported_at_the_line_the_member_wrote(self, line_number):
        body = ["pass"] * 5
        body[line_number - 1] = "import os"

        report = check_draft(StrategyDraft(body="\n".join(body)))

        banned = [issue for issue in report.issues if "os" in issue.message]
        assert [issue.line for issue in banned] == [line_number]

    def test_leading_blank_lines_are_the_members_lines_too(self):
        # They are in the textarea, so the offending line really is line 4.
        # Stripping them would report line 1 and point at a blank line — the
        # same misattribution this module exists to prevent, just smaller.
        report = check_draft(StrategyDraft(body="\n\n\nimport os"))

        assert [issue.line for issue in report.issues if "os" in issue.message] == [4]

    def test_an_issue_outside_the_fragment_is_never_blamed_on_the_member(self):
        assembled = Assembled(source="x = 1\n", body_offset=99, body_lines=1)
        from src.services.strategy_validation.authoring import _remap
        from src.services.strategy_validation.scanning import CompatibilityIssue

        remapped = _remap(CompatibilityIssue(line=3, message="something."), assembled)

        # Clamping to line 1 would point at code they did not write.
        assert remapped.line == 0
        assert "generated file is at fault" in remapped.message


class TestScaffoldInvariant:
    """2. The generated file must satisfy our own checker."""

    @pytest.mark.parametrize(
        "body", ["", "pass", _starter_fragment()], ids=["empty", "pass", "starter"]
    )
    def test_assembles_to_a_file_that_passes_the_check(self, body):
        report = check_compatibility(assemble(StrategyDraft(body=body)).source)

        assert report.issues == ()
        assert report.compatible is True

    def test_the_fragment_offset_points_at_the_first_body_line(self):
        assembled = assemble(StrategyDraft(body="first = 1\nsecond = 2"))

        line = assembled.source.splitlines()[assembled.body_offset - 1]
        assert line.strip() == "first = 1"
        assert assembled.body_lines == 2

    def test_indicators_render_into_the_generated_class(self):
        assembled = assemble(
            StrategyDraft(
                body="pass",
                indicators=(IndicatorSpec("fast_sma", "SimpleMovingAverage", {"period": 20}),),
            )
        )

        assert '"fast_sma": ("SimpleMovingAverage"' in assembled.source
        assert check_compatibility(assembled.source).compatible is True


class TestDeterminism:
    """3. Check assembles, submit assembles again; they must agree byte for byte."""

    def test_the_same_draft_assembles_identically(self):
        draft = StrategyDraft(
            body="pass",
            indicators=(
                IndicatorSpec("slow", "SimpleMovingAverage", {"period": 50}),
                IndicatorSpec("fast", "SimpleMovingAverage", {"period": 20}),
            ),
        )

        assert assemble(draft).source == assemble(draft).source

    def test_indicator_order_does_not_change_the_bytes(self):
        fast = IndicatorSpec("fast", "SimpleMovingAverage", {"period": 20})
        slow = IndicatorSpec("slow", "SimpleMovingAverage", {"period": 50})

        # Sorted by attribute at assembly, so the editor's ordering cannot make
        # the submitted file differ from the checked one.
        assert (
            assemble(StrategyDraft(body="pass", indicators=(fast, slow))).source
            == assemble(StrategyDraft(body="pass", indicators=(slow, fast))).source
        )


class TestStructuralContainment:
    """4. A fragment must not become module-level code.

    Note what these assert, and what the plan expected. The plan lists these
    fragments as ones the tree assertions *reject*. They are not rejected,
    because uniform indentation means they never escape in the first place:
    ``class Evil:`` at column 0 lands at column 8 and becomes a class nested
    inside ``OnData``. The guarantee the plan cares about — the module holds
    the generated imports and exactly one strategy class — holds in every case,
    and that is what is asserted here. `_assert_shape` remains the check that
    proves it rather than the thing that fires.
    """

    @pytest.mark.parametrize(
        "body",
        [
            "pass\n\nclass Evil:\n    pass",
            "pass\n\ndef OnData(self, context):\n    pass",
            "pass\n\nimport os",
        ],
        ids=["opens-a-class", "redefines-ondata", "module-level-import"],
    )
    def test_the_module_still_holds_only_the_generated_class(self, body):
        tree = ast.parse(assemble(StrategyDraft(body=body)).source)

        classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
        others = [
            node
            for node in tree.body
            if not isinstance(node, ast.ImportFrom | ast.Import | ast.ClassDef)
        ]
        assert [cls.name for cls in classes] == ["MyStrategy"]
        assert others == []

    def test_an_escape_would_be_refused_rather_than_reported(self):
        # Proving the assertion is load-bearing: hand it an assembled file that
        # really does define something outside the class.
        from src.services.strategy_validation.authoring import _assert_shape

        escaped = Assembled(source="import os\n\nclass Evil:\n    pass\n", body_offset=3, body_lines=2)

        with pytest.raises(ScaffoldEscape):
            _assert_shape(escaped)


class TestIndicatorSplit:
    """5. A scaffold problem is never reported as the member's line."""

    def test_an_unknown_indicator_written_in_the_body_lands_on_its_own_line(self):
        report = check_draft(
            StrategyDraft(body='pass\nself.AddIndicator("NotAnIndicator", "AAPL")')
        )

        # Whatever the scanner makes of it, it must be attributed to the
        # fragment, never to the generated INDICATORS block.
        assert all(issue.line in (0, 1, 2) for issue in report.issues)

    def test_an_indicator_in_the_spec_never_produces_a_body_line_issue(self):
        report = check_draft(
            StrategyDraft(
                body="pass",
                indicators=(IndicatorSpec("x", "NotAnIndicator", {}),),
            )
        )

        # The spec is the editor's field to validate (plan step 2). What must
        # not happen is the member being sent to a line of their own body.
        assert all(issue.line == 0 for issue in report.issues)


class TestSyntaxErrors:
    """6. Broken Python is a fragment-line issue, never a 500."""

    @pytest.mark.parametrize(
        "body, expected_line",
        [
            ('pass\nx = """unterminated', 2),
            ("pass\n  badly_indented = 1", 2),
            ('"just a string literal"', None),
        ],
        ids=["unterminated-quote", "bad-indent", "only-a-string"],
    )
    def test_reported_against_the_fragment(self, body, expected_line):
        report = check_draft(StrategyDraft(body=body))

        assert all(issue.line >= 0 for issue in report.issues)
        if expected_line is not None:
            assert any(issue.line == expected_line for issue in report.issues)


class TestWhitespace:
    """7. Tabs, mixed indentation, blank lines, CRLF."""

    @pytest.mark.parametrize(
        "body",
        [
            "for ticker in self.tickers:\n\tpass",
            "for ticker in self.tickers:\n    pass\n\n",
            "\r\nfor ticker in self.tickers:\r\n    pass\r\n",
            "\n\n\nfor ticker in self.tickers:\n    pass\n\n\n",
        ],
        ids=["tabs", "trailing-blank", "crlf", "blank-padded"],
    )
    def test_assembles_and_passes(self, body):
        assembled = assemble(StrategyDraft(body=body))

        assert "\r" not in assembled.source
        assert "\t" not in assembled.source
        assert check_compatibility(assembled.source).compatible is True

    def test_an_empty_body_still_produces_a_valid_method(self):
        assembled = assemble(StrategyDraft(body="   \n  \n"))

        assert assembled.body_lines == 1
        assert check_compatibility(assembled.source).compatible is True


class TestContractBranchesAreUnreachable:
    """8. Body-only authoring makes the signature checks impossible.

    An explicit invariant: if the generated ``def OnData`` line ever changes
    shape, this fails here rather than silently starting to report signature
    problems a member cannot act on.
    """

    CONTRACT_WORDS = ("OnData", "signature", "async", "staticmethod", "argument")

    @pytest.mark.parametrize(
        "body",
        ["pass", "async def inner():\n    pass", "@staticmethod\ndef inner():\n    pass"],
        ids=["plain", "async-inside", "decorated-inside"],
    )
    def test_no_signature_issue_can_be_produced_from_a_body(self, body):
        report = check_draft(StrategyDraft(body=body))

        signature_issues = [
            issue
            for issue in report.issues
            if any(word in issue.message for word in self.CONTRACT_WORDS)
        ]
        assert signature_issues == []

    def test_the_generated_signature_is_the_one_the_engine_calls(self):
        source = assemble(StrategyDraft(body="pass")).source

        assert "    def OnData(self, context: StrategyContext):\n" in source


class TestAssemblyIsNotInjectable:
    """The spec fields are rendered into Python source, so they are guarded.

    Found by review: ``indicator`` was interpolated into a string literal with
    no validation, so a value carrying a quote closed the dict and appended
    class-body attributes to a file that then parsed and passed every tree
    assertion.
    """

    INJECTION = 'SimpleMovingAverage", {}),\n    }\n    STATE = {"pwned": {}}\n    OTHER = {\n        "z": ("SimpleMovingAverage'

    def test_an_indicator_name_that_closes_its_string_is_refused(self):
        draft = StrategyDraft(
            body="pass", indicators=(IndicatorSpec("fast", self.INJECTION, {}),)
        )

        with pytest.raises(ScaffoldEscape):
            assemble(draft)

    def test_an_attribute_that_is_not_an_identifier_is_refused(self):
        draft = StrategyDraft(
            body="pass", indicators=(IndicatorSpec("not an identifier", "SimpleMovingAverage", {}),)
        )

        with pytest.raises(ScaffoldEscape):
            assemble(draft)

    def test_duplicate_attributes_are_refused_rather_than_collapsed(self):
        # Two identical dict keys: Python keeps the last, so one of the
        # member's indicators would disappear with nothing said.
        draft = StrategyDraft(
            body="pass",
            indicators=(
                IndicatorSpec("a", "SimpleMovingAverage", {}),
                IndicatorSpec("a", "ExponentialMovingAverage", {}),
            ),
        )

        with pytest.raises(ScaffoldEscape):
            assemble(draft)

    def test_an_injected_async_method_is_still_seen_as_a_method(self):
        from src.services.strategy_validation.authoring import _assert_shape

        source = (
            "class MyStrategy(BasePortfolio):\n"
            "    async def evil(self):\n        pass\n"
            "    def OnData(self, context):\n        pass\n"
        )

        with pytest.raises(ScaffoldEscape):
            _assert_shape(Assembled(source=source, body_offset=5, body_lines=1))


class TestStateReachesTheGeneratedClass:
    """The starter fragment uses ``self.last_price``; without STATE it crashes.

    Found by review: ``BasePortfolio.__init__`` assigns instance attributes
    only from ``STATE``/``PER_TICKER_STATE``, and the generated prelude had no
    ``STATE`` at all. The seeded body therefore passed the check and then died
    on the first bar of its validation run.
    """

    def test_declared_state_is_rendered(self):
        assembled = assemble(StrategyDraft(body="pass", state={"last_price": {}}))

        assert "STATE = {'last_price': {}}" in assembled.source

    def test_the_served_starter_fragment_has_the_state_it_uses(self):
        from src.services.strategy_validation import template

        body = template.STARTER_BODY
        if "self.last_price" in body:
            assert "last_price" in dict(template.STARTER_STATE)

    def test_an_empty_state_is_still_valid_python(self):
        assembled = assemble(StrategyDraft(body="pass"))

        assert "STATE = {}" in assembled.source
        assert check_compatibility(assembled.source).compatible is True


class TestSubmitAssemblyIsChecked:
    """`assemble_checked` is what a storing caller must use."""

    def test_it_applies_the_structural_assertions(self):
        from src.services.strategy_validation.authoring import assemble_checked

        # Raw assemble is the text transform; the checked one is the guarantee.
        with pytest.raises(ScaffoldEscape):
            assemble_checked(
                StrategyDraft(body="pass", indicators=(IndicatorSpec("a b", "X", {}),))
            )


class TestIndicatorParameterDefaults:
    """``kwargs.get(name, default)`` is the whole signature of an indicator."""

    @staticmethod
    def _params(body: str):
        from src.services.strategy_validation.scanning import indicator_parameters

        return indicator_parameters(
            "class X(Indicator):\n    def __init__(self, **kwargs):\n" + body
        )

    @pytest.mark.parametrize(
        "literal, expected",
        [
            ("14", 14),
            ("-1", -1),
            ("[5, 10]", [5, 10]),
            ("(1, 2)", (1, 2)),
            ('{"a": 1}', {"a": 1}),
            ("None", None),
        ],
    )
    def test_every_literal_default_is_read(self, literal, expected):
        assert self._params(f"        self.p = kwargs.get('period', {literal})\n") == [
            ("period", expected)
        ]

    @pytest.mark.parametrize("expression", ["DEFAULT", "compute()", "a + b"])
    def test_a_default_that_needs_evaluation_stays_unknown(self, expression):
        # Unknowable without running the module; the editor asks instead.
        assert self._params(f"        self.p = kwargs.get('period', {expression})\n") == [
            ("period", None)
        ]

    def test_a_parameter_with_no_default_stays_unknown(self):
        assert self._params("        self.p = kwargs.get('period')\n") == [("period", None)]

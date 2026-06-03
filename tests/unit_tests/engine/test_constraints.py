"""Unit tests for ``UnleashClient.engine.constraints``.

Test cases are derived directly from the official Unleash client-specification
JSONs (numbers 13, 14, 15, 21, 22) under
``tests/specification_tests/client-specification/specifications``. The relevant
constraint shapes are embedded here so each operator is exercised in isolation
(this module tests ``evaluate_constraint`` / ``get_context_value`` directly, not
the full client).
"""

from __future__ import annotations

import pytest
import semver

from UnleashClient.engine.constraints import evaluate_constraint, get_context_value
from UnleashClient.engine.state import ConstraintDef

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def c(
    operator,
    *,
    context_name="customField",
    values=None,
    value=None,
    case_insensitive=False,
    inverted=False,
):
    """Build a bare ``ConstraintDef`` (no pre-parsed fields).

    ``evaluate_constraint`` must work correctly even when ``state.py`` has not
    pre-populated the ``parsed_*`` fields, by falling back to parsing ``value`` /
    case-folding ``values`` itself. (Integration relies on the pre-parsed path
    for performance, which is exercised separately below.)
    """
    return ConstraintDef(
        context_name=context_name,
        operator=operator,
        values=list(values) if values is not None else [],
        value=value,
        case_insensitive=case_insensitive,
        inverted=inverted,
    )


# ---------------------------------------------------------------------------
# get_context_value
# ---------------------------------------------------------------------------


class TestGetContextValue:
    def test_top_level_field(self):
        assert get_context_value({"userId": "7"}, "userId") == "7"

    def test_properties_fallback(self):
        assert get_context_value({"properties": {"x": "y"}}, "x") == "y"

    def test_top_level_takes_precedence_over_properties(self):
        ctx = {"userId": "top", "properties": {"userId": "nested"}}
        assert get_context_value(ctx, "userId") == "top"

    def test_missing_returns_none(self):
        assert get_context_value({}, "nope") is None

    def test_missing_with_properties_returns_none(self):
        assert get_context_value({"properties": {"a": "b"}}, "nope") is None

    def test_explicit_none_in_properties(self):
        # spec 22 / 21: a property present but null behaves as absent.
        assert get_context_value({"properties": {"nested": None}}, "nested") is None

    def test_no_properties_key(self):
        assert get_context_value({"userId": "1"}, "other") is None


# ---------------------------------------------------------------------------
# List operators: IN / NOT_IN  (spec 13: F8, F9, F10)
# ---------------------------------------------------------------------------


class TestListOperators:
    def test_in_match(self):
        assert (
            evaluate_constraint(
                c("IN", context_name="someValue", values=["s1", "s2"]),
                {"properties": {"someValue": "s1"}},
            )
            is True
        )

    def test_in_no_match(self):
        assert (
            evaluate_constraint(
                c("IN", context_name="someValue", values=["s1", "s2"]),
                {"properties": {"someValue": "s3"}},
            )
            is False
        )

    def test_in_missing_field_false(self):
        # F8.missing-field.IN
        assert (
            evaluate_constraint(
                c("IN", context_name="customFieldMissing", values=["s1"]), {}
            )
            is False
        )

    def test_in_present_true(self):
        assert (
            evaluate_constraint(
                c("IN", context_name="customFieldMissing", values=["s1"]),
                {"properties": {"customFieldMissing": "s1"}},
            )
            is True
        )

    def test_not_in_missing_field_true(self):
        # F8.missing-field.NOT_IN -- missing field special-cases NOT_IN to True
        assert (
            evaluate_constraint(
                c("NOT_IN", context_name="customFieldMissing", values=["s1"]), {}
            )
            is True
        )

    def test_not_in_null_property_true(self):
        assert (
            evaluate_constraint(
                c("NOT_IN", context_name="customFieldMissing", values=["s1"]),
                {"properties": {"customFieldMissing": None}},
            )
            is True
        )

    def test_not_in_present_member_false(self):
        assert (
            evaluate_constraint(
                c("NOT_IN", context_name="someValue", values=["s1", "s2"]),
                {"properties": {"someValue": "s1"}},
            )
            is False
        )

    def test_inverted_in(self):
        # F9.inverted.IN: present member, inverted -> False
        assert (
            evaluate_constraint(
                c("IN", context_name="someValue", values=["s1", "s2"], inverted=True),
                {"properties": {"someValue": "s1"}},
            )
            is False
        )

    def test_inverted_not_in(self):
        # F10.inverted.NOT_IN: present member, NOT_IN=False, inverted -> True
        assert (
            evaluate_constraint(
                c(
                    "NOT_IN",
                    context_name="someValue",
                    values=["s1", "s2"],
                    inverted=True,
                ),
                {"properties": {"someValue": "s1"}},
            )
            is True
        )


# ---------------------------------------------------------------------------
# String operators (spec 13: F1-F4)
# ---------------------------------------------------------------------------


class TestStringOperators:
    def test_starts_with_single(self):
        assert (
            evaluate_constraint(
                c("STR_STARTS_WITH", values=["some-string"]),
                {"properties": {"customField": "some-string-is-cool"}},
            )
            is True
        )

    def test_starts_with_no_match(self):
        assert (
            evaluate_constraint(
                c("STR_STARTS_WITH", values=["some-string"]),
                {"properties": {"customField": "some2-is-cool-string"}},
            )
            is False
        )

    def test_starts_with_multiple_any(self):
        assert (
            evaluate_constraint(
                c("STR_STARTS_WITH", values=["e1", "e2"]),
                {"properties": {"customField": "e2 cool"}},
            )
            is True
        )

    def test_starts_with_multiple_no_match(self):
        assert (
            evaluate_constraint(
                c("STR_STARTS_WITH", values=["e1", "e2"]),
                {"properties": {"customField": "cool e1"}},
            )
            is False
        )

    def test_starts_with_case_insensitive(self):
        con = c("STR_STARTS_WITH", values=["TEST"], case_insensitive=True)
        assert (
            evaluate_constraint(
                con, {"properties": {"customField": "test@example.com"}}
            )
            is True
        )
        assert (
            evaluate_constraint(
                con, {"properties": {"customField": "TEsT@example.com"}}
            )
            is True
        )

    def test_ends_with_case_sensitive_default(self):
        con = c("STR_ENDS_WITH", context_name="email", values=["some-email.com"])
        assert (
            evaluate_constraint(con, {"properties": {"email": "test@some-email.com"}})
            is True
        )
        assert (
            evaluate_constraint(con, {"properties": {"email": "test@some-EMAIL.com"}})
            is False
        )

    def test_ends_with_ignoring_case(self):
        con = c(
            "STR_ENDS_WITH",
            context_name="email",
            values=["some-email.com"],
            case_insensitive=True,
        )
        assert (
            evaluate_constraint(con, {"properties": {"email": "test@SOME-EMAIL.com"}})
            is True
        )

    def test_ends_with_no_match(self):
        con = c("STR_ENDS_WITH", context_name="email", values=["some-email.com"])
        assert (
            evaluate_constraint(
                con, {"properties": {"email": "test@another-email.com"}}
            )
            is False
        )

    def test_contains(self):
        con = c("STR_CONTAINS", context_name="email", values=["email"])
        assert (
            evaluate_constraint(con, {"properties": {"email": "test@some-email.com"}})
            is True
        )
        assert (
            evaluate_constraint(con, {"properties": {"email": "test@another.com"}})
            is False
        )

    def test_contains_inverted(self):
        con = c("STR_CONTAINS", context_name="email", values=["email"], inverted=True)
        assert (
            evaluate_constraint(con, {"properties": {"email": "test@another.com"}})
            is True
        )

    def test_string_op_missing_context_false(self):
        assert evaluate_constraint(c("STR_CONTAINS", values=["x"]), {}) is False


# ---------------------------------------------------------------------------
# Numeric operators (spec 13: F5, F6)
# ---------------------------------------------------------------------------


class TestNumericOperators:
    def test_num_eq_int(self):
        con = c("NUM_EQ", context_name="someValue", value="12")
        assert evaluate_constraint(con, {"properties": {"someValue": "12"}}) is True

    def test_num_eq_float_match(self):
        con = c("NUM_EQ", context_name="someValue", value="12")
        assert evaluate_constraint(con, {"properties": {"someValue": "12.0"}}) is True

    def test_num_eq_float_value(self):
        con = c("NUM_EQ", context_name="someValue", value="12.0")
        assert evaluate_constraint(con, {"properties": {"someValue": "12"}}) is True

    def test_num_eq_inverted_true(self):
        con = c("NUM_EQ", context_name="someValue", value="12", inverted=True)
        assert evaluate_constraint(con, {"properties": {"someValue": "13"}}) is True

    def test_num_eq_inverted_false(self):
        con = c("NUM_EQ", context_name="someValue", value="12", inverted=True)
        assert evaluate_constraint(con, {"properties": {"someValue": "12"}}) is False

    def test_num_gt(self):
        con = c("NUM_GT", context_name="someValue", value="12")
        assert evaluate_constraint(con, {"properties": {"someValue": "13"}}) is True
        assert evaluate_constraint(con, {"properties": {"someValue": "12"}}) is False

    def test_num_gte(self):
        con = c("NUM_GTE", context_name="someValue", value="12")
        assert evaluate_constraint(con, {"properties": {"someValue": "12"}}) is True
        assert evaluate_constraint(con, {"properties": {"someValue": "13"}}) is True
        assert evaluate_constraint(con, {"properties": {"someValue": "11"}}) is False

    def test_num_lt(self):
        con = c("NUM_LT", context_name="someValue", value="12")
        assert evaluate_constraint(con, {"properties": {"someValue": "0"}}) is True
        assert evaluate_constraint(con, {"properties": {"someValue": "12"}}) is False

    def test_num_lte(self):
        con = c("NUM_LTE", context_name="someValue", value="12")
        assert evaluate_constraint(con, {"properties": {"someValue": "12"}}) is True
        assert evaluate_constraint(con, {"properties": {"someValue": "0"}}) is True
        assert evaluate_constraint(con, {"properties": {"someValue": "13"}}) is False

    def test_num_unparseable_context_false(self):
        con = c("NUM_GT", context_name="someValue", value="12")
        assert (
            evaluate_constraint(con, {"properties": {"someValue": "not-a-number"}})
            is False
        )

    def test_num_unparseable_context_inverted_true(self):
        con = c("NUM_GT", context_name="someValue", value="12", inverted=True)
        assert (
            evaluate_constraint(con, {"properties": {"someValue": "not-a-number"}})
            is True
        )

    def test_num_numeric_context_value(self):
        # context may already carry a numeric type, not a string
        con = c("NUM_EQ", context_name="someValue", value="12")
        assert evaluate_constraint(con, {"properties": {"someValue": 12}}) is True


# ---------------------------------------------------------------------------
# Date operators (spec 13: F7)
# ---------------------------------------------------------------------------


class TestDateOperators:
    def test_date_after_enabled(self):
        con = c(
            "DATE_AFTER", context_name="currentTime", value="2022-01-29T13:00:00.000Z"
        )
        assert (
            evaluate_constraint(con, {"currentTime": "2022-01-30T13:00:00.000Z"})
            is True
        )

    def test_date_after_disabled(self):
        con = c(
            "DATE_AFTER", context_name="currentTime", value="2022-01-29T13:00:00.000Z"
        )
        assert (
            evaluate_constraint(con, {"currentTime": "2022-01-28T13:00:00.000Z"})
            is False
        )

    def test_date_after_exclusive(self):
        con = c(
            "DATE_AFTER", context_name="currentTime", value="2022-01-29T13:00:00.000Z"
        )
        assert (
            evaluate_constraint(con, {"currentTime": "2022-01-29T13:00:00.000Z"})
            is False
        )

    def test_date_before_enabled(self):
        con = c(
            "DATE_BEFORE", context_name="currentTime", value="2022-01-29T13:00:00.000Z"
        )
        assert (
            evaluate_constraint(con, {"currentTime": "2022-01-28T13:00:00.000Z"})
            is True
        )

    def test_date_before_disabled(self):
        con = c(
            "DATE_BEFORE", context_name="currentTime", value="2022-01-29T13:00:00.000Z"
        )
        assert (
            evaluate_constraint(con, {"currentTime": "2022-01-30T13:00:00.000Z"})
            is False
        )

    def test_date_before_exclusive(self):
        con = c(
            "DATE_BEFORE", context_name="currentTime", value="2022-01-29T13:00:00.000Z"
        )
        assert (
            evaluate_constraint(con, {"currentTime": "2022-01-29T13:00:00.000Z"})
            is False
        )

    def test_date_with_timezone(self):
        after = c(
            "DATE_AFTER",
            context_name="currentTime",
            value="2022-01-22T13:00:00.000+02:00",
        )
        before = c(
            "DATE_BEFORE",
            context_name="currentTime",
            value="2022-01-22T14:00:00.000+02:00",
        )
        ctx = {"currentTime": "2022-01-22T11:30:00.000Z"}
        assert evaluate_constraint(after, ctx) is True
        assert evaluate_constraint(before, ctx) is True

    def test_current_time_defaulted_when_absent(self):
        # currentTime is special: absent from context -> use "now".
        con = c(
            "DATE_AFTER", context_name="currentTime", value="2000-01-01T00:00:00.000Z"
        )
        assert evaluate_constraint(con, {}) is True

    def test_date_unparseable_context_false(self):
        con = c(
            "DATE_AFTER", context_name="currentTime", value="2022-01-29T13:00:00.000Z"
        )
        assert evaluate_constraint(con, {"currentTime": "not-a-date"}) is False

    def test_date_naive_context_rejected(self):
        # The native engine (chrono RFC3339) rejects a timezone-less context
        # timestamp: it is unparseable, so the constraint is a non-match. (A
        # lenient parser would assume UTC and diverge from the oracle.)
        con = c(
            "DATE_AFTER", context_name="currentTime", value="2022-01-29T13:00:00.000Z"
        )
        assert evaluate_constraint(con, {"currentTime": "2022-01-30T13:00:00"}) is False


# ---------------------------------------------------------------------------
# Semver operators (spec 14)
# ---------------------------------------------------------------------------


class TestSemverOperators:
    def test_semver_eq(self):
        con = c("SEMVER_EQ", context_name="version", value="1.2.2")
        assert evaluate_constraint(con, {"properties": {"version": "1.2.2"}}) is True
        assert evaluate_constraint(con, {"properties": {"version": "1.2.0"}}) is False

    def test_semver_gt(self):
        con = c("SEMVER_GT", context_name="version", value="1.2.2")
        assert evaluate_constraint(con, {"properties": {"version": "1.2.3"}}) is True
        assert evaluate_constraint(con, {"properties": {"version": "1.2.0"}}) is False

    def test_semver_gte(self):
        con = c("SEMVER_GTE", context_name="version", value="1.2.2")
        assert evaluate_constraint(con, {"properties": {"version": "1.2.2"}}) is True
        assert evaluate_constraint(con, {"properties": {"version": "1.2.3"}}) is True
        assert evaluate_constraint(con, {"properties": {"version": "1.2.1"}}) is False

    def test_semver_lt(self):
        con = c("SEMVER_LT", context_name="version", value="1.2.2")
        assert evaluate_constraint(con, {"properties": {"version": "1.2.1"}}) is True
        assert evaluate_constraint(con, {"properties": {"version": "1.2.3"}}) is False

    def test_semver_lte(self):
        con = c("SEMVER_LTE", context_name="version", value="1.2.2")
        assert evaluate_constraint(con, {"properties": {"version": "1.2.2"}}) is True
        assert evaluate_constraint(con, {"properties": {"version": "1.2.1"}}) is True
        assert evaluate_constraint(con, {"properties": {"version": "1.2.3"}}) is False

    def test_semver_prerelease_ordering(self):
        con = c("SEMVER_GT", context_name="version", value="2.0.0-alpha.1")
        assert (
            evaluate_constraint(con, {"properties": {"version": "2.0.0-beta.1"}})
            is True
        )
        assert evaluate_constraint(con, {"properties": {"version": "2.0.0"}}) is True

    def test_semver_alpha_lt(self):
        con = c("SEMVER_LT", context_name="version", value="2.0.0-alpha.1")
        assert (
            evaluate_constraint(con, {"properties": {"version": "2.0.0-beta.1"}})
            is False
        )
        assert (
            evaluate_constraint(con, {"properties": {"version": "1.9.1-beta.1"}})
            is True
        )

    def test_semver_alpha_versioning(self):
        con = c("SEMVER_LT", context_name="version", value="2.0.0-alpha.3")
        assert (
            evaluate_constraint(con, {"properties": {"version": "2.0.0-alpha.1"}})
            is True
        )
        assert (
            evaluate_constraint(con, {"properties": {"version": "2.0.0-alpha.4"}})
            is False
        )

    def test_semver_invalid_context_false(self):
        # leading-v is invalid per the spec; broken semver -> false
        con = c("SEMVER_EQ", context_name="version", value="v2.0.0")
        assert evaluate_constraint(con, {"properties": {"version": "v2.0.0"}}) is False
        assert (
            evaluate_constraint(con, {"properties": {"version": "not a semver"}})
            is False
        )

    def test_semver_invalid_constraint_value_false(self):
        con = c("SEMVER_EQ", context_name="version", value="garbage")
        assert evaluate_constraint(con, {"properties": {"version": "1.2.2"}}) is False


# ---------------------------------------------------------------------------
# Regex operator (spec 21)
# ---------------------------------------------------------------------------


class TestRegexOperator:
    def test_basic_match(self):
        con = c("REGEX", context_name="userId", value="^[^@]+@[^@]+$")
        assert evaluate_constraint(con, {"userId": "test@example.com"}) is True
        assert evaluate_constraint(con, {"userId": "not-an-email"}) is False

    def test_case_sensitive_default(self):
        con = c("REGEX", context_name="userId", value="^cat$")
        assert evaluate_constraint(con, {"userId": "Cat"}) is False
        assert evaluate_constraint(con, {"userId": "cat"}) is True

    def test_global_case_insensitive(self):
        con = c("REGEX", context_name="userId", value="^abc$", case_insensitive=True)
        assert evaluate_constraint(con, {"userId": "AbC"}) is True

    def test_inline_flag_i(self):
        con = c("REGEX", context_name="userId", value="^(?i)abc$")
        assert evaluate_constraint(con, {"userId": "AbC"}) is True
        assert evaluate_constraint(con, {"userId": "zAbCz"}) is False

    def test_inline_flag_m(self):
        con = c("REGEX", context_name="userId", value="(?m)^foo$")
        assert evaluate_constraint(con, {"userId": "x\nfoo\ny"}) is True
        assert evaluate_constraint(con, {"userId": "x\nfo\ny"}) is False

    def test_inline_flag_s(self):
        con = c("REGEX", context_name="userId", value="^(?s)a.b$")
        assert evaluate_constraint(con, {"userId": "a\nb"}) is True

    def test_digit_class_and_quantifier(self):
        con = c("REGEX", context_name="userId", value="^user\\d{3}$")
        assert evaluate_constraint(con, {"userId": "user123"}) is True
        assert evaluate_constraint(con, {"userId": "user12"}) is False

    def test_word_boundary(self):
        con = c("REGEX", context_name="userId", value="\\bcat\\b")
        assert evaluate_constraint(con, {"userId": "a cat b"}) is True
        assert evaluate_constraint(con, {"userId": "concatenate"}) is False

    def test_repetition_range(self):
        con = c("REGEX", context_name="userId", value="^a{2,4}$")
        assert evaluate_constraint(con, {"userId": "aaaa"}) is True
        assert evaluate_constraint(con, {"userId": "a"}) is False
        assert evaluate_constraint(con, {"userId": "aaaaa"}) is False

    def test_posix_ascii_class(self):
        # RE2 supports [[:alpha:]]; Python re does not natively.
        con = c("REGEX", context_name="userId", value="^[[:alpha:]]+$")
        assert evaluate_constraint(con, {"userId": "AlphaBeta"}) is True
        assert evaluate_constraint(con, {"userId": "Alpha1"}) is False

    def test_invalid_regex_false(self):
        con = c("REGEX", context_name="userId", value="(regex")
        assert evaluate_constraint(con, {"userId": "any-value"}) is False

    def test_missing_context_false(self):
        con = c("REGEX", context_name="userId", value="^[^@]+@[^@]+$")
        assert evaluate_constraint(con, {}) is False

    def test_null_property_false(self):
        con = c("REGEX", context_name="nested", value="^[^@]+@[^@]+$")
        assert evaluate_constraint(con, {"properties": {"nested": None}}) is False

    def test_inverted_regex(self):
        con = c("REGEX", context_name="userId", value="^abc$", inverted=True)
        assert evaluate_constraint(con, {"userId": "xyz"}) is True
        assert evaluate_constraint(con, {"userId": "abc"}) is False

    def test_lookahead_rejected_does_not_enable(self):
        # RE2 rejects lookahead; if it leaks through it must not enable.
        con = c("REGEX", context_name="userId", value="^(?=a)b$")
        assert evaluate_constraint(con, {"userId": "ab"}) is False

    def test_backreference_rejected_does_not_enable(self):
        # Python's re WOULD match "aa" with ^(a)\1$, but RE2 rejects backrefs.
        con = c("REGEX", context_name="userId", value="^(a)\\1$")
        assert evaluate_constraint(con, {"userId": "aa"}) is False


# ---------------------------------------------------------------------------
# IN_CIDR operator (spec 22)
# ---------------------------------------------------------------------------


class TestCidrOperator:
    def test_exact_ip_match(self):
        con = c("IN_CIDR", context_name="remoteAddress", values=["127.0.0.1"])
        assert evaluate_constraint(con, {"remoteAddress": "127.0.0.1"}) is True
        assert evaluate_constraint(con, {"remoteAddress": "127.0.0.2"}) is False

    def test_cidr_range(self):
        con = c("IN_CIDR", context_name="remoteAddress", values=["160.33.0.0/16"])
        assert evaluate_constraint(con, {"remoteAddress": "160.33.0.33"}) is True
        assert evaluate_constraint(con, {"remoteAddress": "160.34.0.1"}) is False

    def test_mixed_values_valid_wins(self):
        con = c(
            "IN_CIDR",
            context_name="remoteAddress",
            values=["127.invalid", "192.168.1.0/24"],
        )
        assert evaluate_constraint(con, {"remoteAddress": "192.168.1.42"}) is True

    def test_invalid_only_no_match(self):
        con = c("IN_CIDR", context_name="remoteAddress", values=["127.invalid"])
        assert evaluate_constraint(con, {"remoteAddress": "127.0.0.1"}) is False

    def test_missing_context_false(self):
        con = c("IN_CIDR", context_name="remoteAddress", values=["160.33.0.0/16"])
        assert evaluate_constraint(con, {}) is False

    def test_ipv6_exact(self):
        con = c("IN_CIDR", context_name="remoteAddress", values=["2001:db8::1"])
        assert evaluate_constraint(con, {"remoteAddress": "2001:db8::1"}) is True
        assert evaluate_constraint(con, {"remoteAddress": "2001:db8::2"}) is False

    def test_ipv6_range(self):
        con = c("IN_CIDR", context_name="remoteAddress", values=["2001:db8::/32"])
        assert evaluate_constraint(con, {"remoteAddress": "2001:db8:0:1::42"}) is True
        assert evaluate_constraint(con, {"remoteAddress": "2001:db9::1"}) is False

    def test_ipv6_mixed_values(self):
        con = c(
            "IN_CIDR",
            context_name="remoteAddress",
            values=["bad::cidr", "2001:db8::/32"],
        )
        assert evaluate_constraint(con, {"remoteAddress": "2001:db8::abcd"}) is True

    def test_invalid_context_ip_false(self):
        con = c("IN_CIDR", context_name="remoteAddress", values=["160.33.0.0/16"])
        assert evaluate_constraint(con, {"remoteAddress": "not-an-ip"}) is False


# ---------------------------------------------------------------------------
# Unknown / invalid operator (spec 13: F7.invalid-operator)
# ---------------------------------------------------------------------------


class TestUnknownOperator:
    def test_unknown_operator_false(self):
        con = c(
            "NOT_A_VALID_OPERATOR",
            context_name="customField",
            value="2022-01-22T13:00:00.000Z",
        )
        assert evaluate_constraint(con, {"properties": {"someValue": "12"}}) is False

    def test_unknown_operator_false_even_inverted(self):
        # An unknown operator never "matches"; inverting it would be wrong.
        con = c(
            "NOT_A_VALID_OPERATOR", context_name="customField", value="x", inverted=True
        )
        assert evaluate_constraint(con, {}) is False


# ---------------------------------------------------------------------------
# Pre-parsed (state-compiled) path: evaluation must use the cached fields and
# stay correct.
# ---------------------------------------------------------------------------


class TestPreParsedPath:
    def test_preparsed_number(self):
        con = ConstraintDef(
            context_name="someValue", operator="NUM_GT", value="12", parsed_number=12.0
        )
        assert evaluate_constraint(con, {"properties": {"someValue": "13"}}) is True

    def test_preparsed_values_case_folded(self):
        # state pre-folds values into parsed_values for case-insensitive ops
        con = ConstraintDef(
            context_name="customField",
            operator="STR_STARTS_WITH",
            values=["TEST"],
            case_insensitive=True,
            parsed_values=["TEST"],
        )
        assert (
            evaluate_constraint(con, {"properties": {"customField": "TEsT@x"}}) is True
        )

    def test_preparsed_semver(self):
        con = ConstraintDef(
            context_name="version",
            operator="SEMVER_GTE",
            value="1.2.2",
            parsed_semver=semver.Version.parse("1.2.2"),
        )
        assert evaluate_constraint(con, {"properties": {"version": "1.2.2"}}) is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

"""Tests for keeping credentials out of prompts, sessions and the audit log.

The two halves of the contract are equally important: real credentials are
caught, and ordinary content is left alone. A redactor that eats real content
is worse than none, so the false-positive tests below are not padding.
"""
from __future__ import annotations

from cobirb.redaction import redact, redact_arguments

# Fabricated, structurally valid, never issued. Written out rather than
# assembled from parts so the patterns are tested against what they will
# actually meet.
_AWS = "AKIAIOSFODNN7EXAMPLE"
_GITHUB = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
_STRIPE = "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc"
_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"


def test_a_private_key_block_is_removed_whole():
    """The single worst thing to leak, and the easiest to match."""
    text = (
        "config:\n-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA1234567890\nabcdefghijklmnop\n"
        "-----END RSA PRIVATE KEY-----\ndone\n"
    )

    result = redact(text)

    assert "MIIEowIBAAKCAQEA" not in result.text
    assert "[redacted: private key]" in result.text
    assert result.text.startswith("config:") and result.text.rstrip().endswith("done")


def test_the_common_token_formats_are_caught():
    for secret, expected in [
        (_AWS, "aws access key id"),
        (_GITHUB, "github token"),
        (_STRIPE, "stripe key"),
        (_JWT, "json web token"),
        ("AIza" + "S" * 35, "google api key"),
        ("xoxb-123456789012-abcdefghijkl", "slack token"),
        ("sk-ant-" + "x" * 30, "api key"),
    ]:
        result = redact(f"TOKEN={secret}\n")
        assert secret not in result.text, expected
        assert expected in result.text


def test_redaction_is_visible_rather_than_silent():
    """A model handed a silently emptied value works from something it never
    received; one told a key was there can say so."""
    result = redact(f"key = {_AWS}")

    assert "[redacted:" in result.text
    assert result.changed
    assert "aws access key id" in result.describe()


def test_ordinary_content_is_left_completely_alone():
    """The failure that matters most. Nothing here matches on a *name* —
    no `password=`, no entropy heuristics — precisely so that documentation,
    fixtures and prose survive."""
    for benign in (
        "password = input('enter your password: ')",
        "SECRET_KEY = os.environ['SECRET_KEY']",
        "# Set your API key in the environment before running",
        "api_key: str | None = None",
        "def get_token(self) -> str: return self._token",
        "https://example.com/path?query=abcdefghijklmnop",
        "commit 4eC39HqLyjWDarjtT1zdp7dc",
        "sk-",
        "This function returns an sk- prefixed value.",
    ):
        assert redact(benign).text == benign, benign


def test_a_short_sk_string_is_not_a_key():
    """The `sk-` rule needs a length floor or it eats prose."""
    assert redact("sk-abc").text == "sk-abc"


def test_several_secrets_in_one_file_are_all_counted():
    result = redact(f"a={_AWS}\nb={_GITHUB}\nc={_AWS}\n")

    assert result.counts["aws access key id"] == 2
    assert result.counts["github token"] == 1


def test_text_with_nothing_to_redact_comes_back_identical():
    """The common case, and it must not be rebuilt or reformatted."""
    text = "def main():\n    return 0\n"

    result = redact(text)

    assert result.text == text
    assert not result.changed


def test_arguments_are_redacted_for_the_audit_log():
    """The log's whole problem is that it stores arguments verbatim in a
    plaintext file that outlives the session."""
    cleaned = redact_arguments({"path": ".env", "content": f"AWS_KEY={_AWS}", "count": 3})

    assert _AWS not in cleaned["content"]
    assert cleaned["path"] == ".env"
    assert cleaned["count"] == 3  # non-strings pass through untouched

"""Unit tests for teracron.apikey — API key encoding and decoding."""

import pytest

from teracron.apikey import decode_api_key, encode_api_key

_VALID_SLUG = "vivid-kudu-655"
_VALID_PEM = """-----BEGIN PUBLIC KEY-----
MIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEA0+dummykeydata0
-----END PUBLIC KEY-----"""


class TestEncodeApiKey:
    """Tests for encode_api_key()."""

    def test_produces_tcn_prefix(self):
        key = encode_api_key(_VALID_SLUG, _VALID_PEM)
        assert key.startswith("tcn_")

    def test_roundtrip(self):
        key = encode_api_key(_VALID_SLUG, _VALID_PEM)
        slug, pem = decode_api_key(key)
        assert slug == _VALID_SLUG
        assert pem == _VALID_PEM

    def test_invalid_slug_raises(self):
        with pytest.raises(ValueError, match="Invalid slug"):
            encode_api_key("INVALID", _VALID_PEM)

    def test_empty_slug_raises(self):
        with pytest.raises(ValueError, match="Invalid slug"):
            encode_api_key("", _VALID_PEM)

    def test_invalid_pem_raises(self):
        with pytest.raises(ValueError, match="PEM-encoded"):
            encode_api_key(_VALID_SLUG, "not-a-pem")

    def test_deterministic(self):
        a = encode_api_key(_VALID_SLUG, _VALID_PEM)
        b = encode_api_key(_VALID_SLUG, _VALID_PEM)
        assert a == b

    def test_url_safe_chars_only(self):
        key = encode_api_key(_VALID_SLUG, _VALID_PEM)
        payload = key[4:]  # strip "tcn_"
        # base64url: only A-Z, a-z, 0-9, -, _  (no +, /, =)
        assert "+" not in payload
        assert "/" not in payload
        assert "=" not in payload


class TestDecodeApiKey:
    """Tests for decode_api_key()."""

    def test_valid_key(self):
        key = encode_api_key(_VALID_SLUG, _VALID_PEM)
        slug, pem = decode_api_key(key)
        assert slug == _VALID_SLUG
        assert pem == _VALID_PEM

    def test_missing_prefix_raises(self):
        with pytest.raises(ValueError, match="Invalid API key format"):
            decode_api_key("not_a_valid_key")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="api_key is required"):
            decode_api_key("")

    def test_none_raises(self):
        with pytest.raises(ValueError, match="api_key is required"):
            decode_api_key(None)  # type: ignore

    def test_corrupted_base64_raises(self):
        with pytest.raises(ValueError, match="Corrupted API key"):
            decode_api_key("tcn_!!not_base64!!")

    def test_missing_separator_raises(self):
        import base64

        payload = "vivid-kudu-655nocolon".encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
        with pytest.raises(ValueError, match="missing separator"):
            decode_api_key(f"tcn_{encoded}")

    def test_invalid_slug_in_payload_raises(self):
        import base64

        payload = "INVALID:-----BEGIN PUBLIC KEY-----\nfoo\n-----END PUBLIC KEY-----".encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
        with pytest.raises(ValueError, match="invalid slug"):
            decode_api_key(f"tcn_{encoded}")

    def test_invalid_pem_in_payload_raises(self):
        import base64

        payload = "vivid-kudu-655:not-a-pem-key".encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
        with pytest.raises(ValueError, match="invalid public key"):
            decode_api_key(f"tcn_{encoded}")

    def test_whitespace_stripped(self):
        key = encode_api_key(_VALID_SLUG, _VALID_PEM)
        slug, pem = decode_api_key(f"  {key}  ")
        assert slug == _VALID_SLUG

    def test_pem_with_colons_preserved(self):
        """PEM content might contain colons in base64; only first colon splits."""
        pem_with_content = (
            "-----BEGIN PUBLIC KEY-----\n"
            "MIICIj:colon:in:base64\n"
            "-----END PUBLIC KEY-----"
        )
        key = encode_api_key(_VALID_SLUG, pem_with_content)
        slug, decoded_pem = decode_api_key(key)
        assert slug == _VALID_SLUG
        assert decoded_pem == pem_with_content

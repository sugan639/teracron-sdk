"""Unit tests for teracron.crypto — hybrid RSA+AES encryption envelope."""

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa, padding as asym_padding
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from teracron.crypto import encrypt_envelope

# ── Constants matching the envelope spec ──
_RSA_KEY_SIZE = 512  # bytes (4096-bit RSA)
_IV_SIZE = 12
_TAG_SIZE = 16
_HEADER_SIZE = _RSA_KEY_SIZE + _IV_SIZE + _TAG_SIZE


def _generate_test_keypair() -> tuple:
    """Generate an RSA-4096 keypair for testing."""
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=4096,
    )
    public_key_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    private_key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    return public_key_pem, private_key_pem, private_key


class TestEncryptEnvelope:
    """Tests for encrypt_envelope() — must produce the correct binary layout."""

    @pytest.fixture(scope="class")
    def keypair(self):
        return _generate_test_keypair()

    def test_envelope_layout(self, keypair):
        """Envelope must have [512-byte RSA key][12-byte IV][16-byte tag][ciphertext]."""
        pub_pem, _, _ = keypair
        plaintext = b"Hello, Teracron!"
        envelope = encrypt_envelope(plaintext, pub_pem)

        assert len(envelope) > _HEADER_SIZE
        # The ciphertext portion should be at least as long as plaintext
        ct_len = len(envelope) - _HEADER_SIZE
        assert ct_len >= len(plaintext)

    def test_decryption_round_trip(self, keypair):
        """Encrypt → manually decrypt using private key → must recover plaintext."""
        pub_pem, _, priv_key = keypair
        plaintext = b"Teracron protobuf binary data here"
        envelope = encrypt_envelope(plaintext, pub_pem)

        # Extract envelope components
        encrypted_aes_key = envelope[:_RSA_KEY_SIZE]
        iv = envelope[_RSA_KEY_SIZE:_RSA_KEY_SIZE + _IV_SIZE]
        auth_tag = envelope[_RSA_KEY_SIZE + _IV_SIZE:_HEADER_SIZE]
        ciphertext = envelope[_HEADER_SIZE:]

        # RSA-OAEP decrypt the AES key
        aes_key = priv_key.decrypt(
            encrypted_aes_key,
            asym_padding.OAEP(
                mgf=asym_padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        assert len(aes_key) == 32  # AES-256

        # AES-GCM decrypt (reconstruct ciphertext||tag as cryptography expects)
        aesgcm = AESGCM(aes_key)
        ct_with_tag = ciphertext + auth_tag
        recovered = aesgcm.decrypt(iv, ct_with_tag, None)
        assert recovered == plaintext

    def test_different_envelopes_per_call(self, keypair):
        """Each call must use a fresh AES key + IV — no ciphertext reuse."""
        pub_pem, _, _ = keypair
        plaintext = b"identical payload"
        env1 = encrypt_envelope(plaintext, pub_pem)
        env2 = encrypt_envelope(plaintext, pub_pem)
        # Envelopes must differ (different ephemeral keys)
        assert env1 != env2

    def test_invalid_key_raises(self):
        """Non-RSA PEM should raise."""
        with pytest.raises(Exception):
            encrypt_envelope(b"data", "-----BEGIN PUBLIC KEY-----\ngarbage\n-----END PUBLIC KEY-----")

    def test_empty_plaintext(self, keypair):
        """Empty plaintext should still produce a valid envelope."""
        pub_pem, _, priv_key = keypair
        envelope = encrypt_envelope(b"", pub_pem)
        assert len(envelope) >= _HEADER_SIZE

        # Verify decryption recovers empty bytes
        encrypted_aes_key = envelope[:_RSA_KEY_SIZE]
        iv = envelope[_RSA_KEY_SIZE:_RSA_KEY_SIZE + _IV_SIZE]
        auth_tag = envelope[_RSA_KEY_SIZE + _IV_SIZE:_HEADER_SIZE]
        ciphertext = envelope[_HEADER_SIZE:]

        aes_key = priv_key.decrypt(
            encrypted_aes_key,
            asym_padding.OAEP(
                mgf=asym_padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        aesgcm = AESGCM(aes_key)
        recovered = aesgcm.decrypt(iv, ciphertext + auth_tag, None)
        assert recovered == b""

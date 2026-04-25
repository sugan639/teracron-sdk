# -*- coding: utf-8 -*-
"""
Hybrid Encryption — RSA-4096 OAEP + AES-256-GCM.

Produces the exact same binary envelope format as the Node.js SDK so the
Convex server decryptor (convex/lib/crypto.ts) can unwrap it identically.

Binary envelope layout:
    [RSA_ENCRYPTED_AES_KEY  (512 bytes)]
    [IV                     ( 12 bytes)]
    [AUTH_TAG               ( 16 bytes)]
    [AES_CIPHERTEXT         (variable )]

Security properties:
    - AES-256-GCM: authenticated encryption (confidentiality + integrity).
    - RSA-OAEP SHA-256: wraps the ephemeral AES key.
    - Fresh AES key + IV per envelope — no key reuse.
    - AES key is zeroed immediately after encryption.

IMPORTANT: ``cryptography`` lib's AESGCM.encrypt() returns ciphertext||tag
concatenated.  We must split the last 16 bytes as the auth tag and reorder
to match the envelope layout above.
"""

from __future__ import annotations

import os

from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_RSA_ENCRYPTED_KEY_SIZE = 512  # 4096-bit RSA → 512-byte ciphertext
_AES_KEY_SIZE = 32  # AES-256
_IV_SIZE = 12  # GCM standard nonce
_AUTH_TAG_SIZE = 16  # GCM auth tag


def encrypt_envelope(plaintext: bytes, public_key_pem: str) -> bytes:
    """
    Encrypt *plaintext* with hybrid RSA-OAEP + AES-256-GCM.

    Args:
        plaintext: Raw protobuf bytes to encrypt.
        public_key_pem: PEM-encoded RSA-4096 public key (SPKI).

    Returns:
        Binary envelope ready for transmission.

    Raises:
        ValueError: On key format mismatch or encryption failure.
    """
    # ── Load RSA public key ──
    public_key = serialization.load_pem_public_key(
        public_key_pem.encode("utf-8")
    )

    # ── Generate ephemeral AES-256 key and GCM nonce ──
    aes_key = bytearray(os.urandom(_AES_KEY_SIZE))
    iv = os.urandom(_IV_SIZE)

    # ── AES-256-GCM encrypt ──
    aesgcm = AESGCM(bytes(aes_key))
    # encrypt() returns ciphertext || auth_tag (tag is last 16 bytes)
    ct_with_tag = aesgcm.encrypt(iv, plaintext, None)
    ciphertext = ct_with_tag[:-_AUTH_TAG_SIZE]
    auth_tag = ct_with_tag[-_AUTH_TAG_SIZE:]

    # ── RSA-OAEP wrap the AES key ──
    encrypted_aes_key = public_key.encrypt(  # type: ignore[union-attr]
        bytes(aes_key),
        asym_padding.OAEP(
            mgf=asym_padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )

    # ── Zero sensitive key material immediately ──
    for i in range(len(aes_key)):
        aes_key[i] = 0

    if len(encrypted_aes_key) != _RSA_ENCRYPTED_KEY_SIZE:
        raise ValueError(
            f"[Teracron] RSA output size mismatch: expected {_RSA_ENCRYPTED_KEY_SIZE}, "
            f"got {len(encrypted_aes_key)}. Verify that public_key is RSA-4096."
        )

    # ── Assemble envelope: [encryptedKey][iv][authTag][ciphertext] ──
    envelope = bytearray(_RSA_ENCRYPTED_KEY_SIZE + _IV_SIZE + _AUTH_TAG_SIZE + len(ciphertext))
    offset = 0
    envelope[offset:offset + _RSA_ENCRYPTED_KEY_SIZE] = encrypted_aes_key
    offset += _RSA_ENCRYPTED_KEY_SIZE
    envelope[offset:offset + _IV_SIZE] = iv
    offset += _IV_SIZE
    envelope[offset:offset + _AUTH_TAG_SIZE] = auth_tag
    offset += _AUTH_TAG_SIZE
    envelope[offset:offset + len(ciphertext)] = ciphertext

    return bytes(envelope)

import hashlib
import hmac

SIGNATURE_PREFIX = "sha256="


def verify_webhook_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Check GitHub's `X-Hub-Signature-256` over the raw request body.

    The comparison is constant-time: a timing-variable one leaks the expected
    digest a byte at a time to anyone who can send requests.
    """
    if not header or not header.startswith(SIGNATURE_PREFIX):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len(SIGNATURE_PREFIX) :])


def sign_webhook_body(secret: str, body: bytes) -> str:
    """The header GitHub would send for this body. Used by tests and k6."""
    return SIGNATURE_PREFIX + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

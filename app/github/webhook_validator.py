"""
GitHub Webhook Signature Validator
===================================
GitHub sends an HMAC-SHA256 signature in every webhook request header:
  X-Hub-Signature-256: sha256=<hex_digest>

We must validate this against our webhook secret before processing the payload.
Failing to do this would allow anyone to forge webhook events.
"""
import hashlib
import hmac
import logging

logger = logging.getLogger(__name__)


def verify_webhook_signature(
    payload_bytes: bytes,
    signature_header: str | None,
    secret: str,
) -> bool:
    """
    Verify the GitHub webhook HMAC-SHA256 signature.

    Args:
        payload_bytes: Raw request body bytes (must be the original bytes,
                       NOT a re-serialized dict — hashes are byte-order sensitive)
        signature_header: Value of the X-Hub-Signature-256 header, e.g.
                          "sha256=abc123..."
        secret: The webhook secret configured in GitHub repo settings

    Returns:
        True if the signature is valid, False otherwise.
    """
    if not signature_header:
        logger.warning("Webhook received without X-Hub-Signature-256 header")
        return False

    if not signature_header.startswith("sha256="):
        logger.warning("Webhook signature header malformed: %s", signature_header[:20])
        return False

    received_digest = signature_header[len("sha256="):]

    expected_digest = hmac.new(
        key=secret.encode("utf-8"),
        msg=payload_bytes,
        digestmod=hashlib.sha256,
    ).hexdigest()

    # Use compare_digest to prevent timing attacks
    is_valid = hmac.compare_digest(expected_digest, received_digest)

    if not is_valid:
        logger.error(
            "Webhook signature mismatch — possible spoofed request. "
            "Expected prefix: %s, Got prefix: %s",
            expected_digest[:8],
            received_digest[:8],
        )

    return is_valid

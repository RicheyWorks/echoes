"""Self-signed TLS cert helper for the built-in UI server.

This is the "personal infra" path: generate a cert once, install it on
your devices as trusted, hit the UI over HTTPS from anywhere on your
mesh. For anything beyond personal use, run certbot / Let's Encrypt or
your own internal CA - don't try to build ACME into this process.

We require the optional ``cryptography`` dependency. If it isn't
installed, ``init_self_signed`` raises with a clear remediation message.
"""
from __future__ import annotations

import datetime
import ipaddress
import os
from pathlib import Path
from typing import List, Optional


# 825 days is the maximum Safari/macOS will trust a self-signed cert for
# (CA/B Forum BR 4 / Apple's policy). Anything longer gets rejected.
_MAX_VALIDITY_DAYS = 825


def _require_cryptography():
    try:
        import cryptography  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "TLS cert generation needs the `cryptography` package. "
            "Install it with: pip install cryptography"
        ) from e


def init_self_signed(
    out_dir,
    hostname: str = "automaton.local",
    extra_sans: Optional[List[str]] = None,
    validity_days: int = _MAX_VALIDITY_DAYS,
    key_size: int = 2048,
) -> dict:
    """Generate a private key + self-signed cert.

    Writes ``key.pem`` and ``cert.pem`` under ``out_dir``. Refuses to
    overwrite existing files (move or delete them first). Returns a dict
    with the paths and the parsed cert fingerprint for verification.

    ``hostname`` becomes the cert's CommonName and first SAN. The list
    also includes ``localhost``, ``127.0.0.1``, and ``::1`` so the cert
    works for local development without extra SAN entries; pass
    ``extra_sans`` for additional names (e.g. a Tailscale MagicDNS name
    like ``automaton.your-tailnet.ts.net``).
    """
    _require_cryptography()
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    if validity_days > _MAX_VALIDITY_DAYS:
        raise ValueError(
            f"validity_days={validity_days} exceeds the {_MAX_VALIDITY_DAYS}-day "
            "limit Apple platforms enforce on self-signed certs"
        )

    out = Path(os.fspath(out_dir))
    out.mkdir(parents=True, exist_ok=True)
    key_path = out / "key.pem"
    cert_path = out / "cert.pem"
    for p in (key_path, cert_path):
        if p.exists():
            raise FileExistsError(
                f"{p} already exists - delete or move it before regenerating"
            )

    key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)

    # Build SAN list: explicit hostname, common loopback names, extras.
    sans = [x509.DNSName(hostname)]
    if hostname != "localhost":
        sans.append(x509.DNSName("localhost"))
    sans.append(x509.IPAddress(ipaddress.ip_address("127.0.0.1")))
    sans.append(x509.IPAddress(ipaddress.ip_address("::1")))
    for extra in extra_sans or []:
        # Try as an IP first, fall back to DNSName.
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(extra)))
        except ValueError:
            sans.append(x509.DNSName(extra))

    now = datetime.datetime.now(datetime.timezone.utc)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, hostname),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "automaton (self-signed)"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=validity_days))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_encipherment=True,
                content_commitment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=False, crl_sign=False,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    # Restrictive perms on the private key (0o600). The cert is public.
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_path.write_bytes(key_pem)
    try:
        os.chmod(key_path, 0o600)
    except (OSError, NotImplementedError):
        # Windows: ACLs are different; the user's home dir is already
        # per-user. Don't fail the whole flow if chmod isn't supported.
        pass

    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    cert_path.write_bytes(cert_pem)

    fingerprint = cert.fingerprint(hashes.SHA256()).hex().upper()
    fingerprint_pretty = ":".join(
        fingerprint[i:i + 2] for i in range(0, len(fingerprint), 2)
    )
    return {
        "cert": str(cert_path),
        "key": str(key_path),
        "hostname": hostname,
        "sans": [str(s.value) for s in sans],
        "valid_until": cert.not_valid_after_utc.isoformat()
            if hasattr(cert, "not_valid_after_utc") else cert.not_valid_after.isoformat(),
        "fingerprint_sha256": fingerprint_pretty,
    }

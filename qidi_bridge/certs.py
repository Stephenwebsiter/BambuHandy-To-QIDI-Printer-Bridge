"""Security certificates for the pretend Bambu printer.

Bambu Studio only talks to printers whose certificate is signed by an
authority listed in its `printer.cer` file. We create our own small
authority (once), sign a printer certificate with it, and offer to add the
authority to Bambu Studio's / OrcaSlicer's `printer.cer`.

Needs the `cryptography` package (installed by "Set up Bambu Studio.bat").
"""

from __future__ import annotations

import datetime
import glob
import ipaddress
import os
import sys

from . import config

CERT_DIR = os.path.join(config.DATA_DIR, "certs")
CA_CERT = os.path.join(CERT_DIR, "bridge_ca.crt")
CA_KEY = os.path.join(CERT_DIR, "bridge_ca.key")
PRINTER_CERT = os.path.join(CERT_DIR, "printer.crt")
PRINTER_KEY = os.path.join(CERT_DIR, "printer.key")
MARKER = "QIDI Q2 bridge CA"


class CertError(Exception):
    pass


def _crypto():
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    except ImportError as e:
        raise CertError(
            "The Bambu Studio printer needs one extra Python package. Double-click "
            "'Set up Bambu Studio.bat' (or run: py -m pip install cryptography) and start the bridge again."
        ) from e
    return x509, hashes, serialization, rsa, NameOID, ExtendedKeyUsageOID


def ensure_certificates(serial: str, ips: list[str]) -> tuple[str, str]:
    """Create the authority + printer certificate if missing or out of date. Returns (cert, key) paths."""
    x509, hashes, serialization, rsa, NameOID, EKU = _crypto()
    os.makedirs(CERT_DIR, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc)

    if os.path.isfile(CA_CERT) and os.path.isfile(CA_KEY):
        with open(CA_CERT, "rb") as fh:
            ca_cert = x509.load_pem_x509_certificate(fh.read())
        with open(CA_KEY, "rb") as fh:
            ca_key = serialization.load_pem_private_key(fh.read(), password=None)
    else:
        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, MARKER),
                          x509.NameAttribute(NameOID.ORGANIZATION_NAME, "QIDI Q2 bridge")])
        ca_cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                   .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - datetime.timedelta(days=1))
                   .not_valid_after(now + datetime.timedelta(days=3650 * 2))
                   .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                   .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                                content_commitment=False, key_encipherment=False,
                                                data_encipherment=False, key_agreement=False,
                                                encipher_only=False, decipher_only=False), critical=True)
                   .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
                   .sign(ca_key, hashes.SHA256()))
        with open(CA_KEY, "wb") as fh:
            fh.write(ca_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
        with open(CA_CERT, "wb") as fh:
            fh.write(ca_cert.public_bytes(serialization.Encoding.PEM))

    # Re-use the printer certificate if it still matches the serial and addresses.
    if os.path.isfile(PRINTER_CERT) and os.path.isfile(PRINTER_KEY):
        with open(PRINTER_CERT, "rb") as fh:
            cur = x509.load_pem_x509_certificate(fh.read())
        try:
            cn = cur.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
            san = cur.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            have_ips = {str(i) for i in san.get_values_for_type(x509.IPAddress)}
        except (IndexError, x509.ExtensionNotFound):
            cn, have_ips = "", set()
        if cn == serial and set(ips) <= have_ips and cur.not_valid_after_utc > now + datetime.timedelta(days=30):
            return PRINTER_CERT, PRINTER_KEY

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    san = [x509.DNSName(serial), x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    for ip in ips:
        try:
            san.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            pass
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, serial)]))
            .issuer_name(ca_cert.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(x509.ExtendedKeyUsage([EKU.SERVER_AUTH, EKU.CLIENT_AUTH]), critical=False)
            .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=True, content_commitment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    with open(PRINTER_KEY, "wb") as fh:
        fh.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption()))
    with open(PRINTER_CERT, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM) + ca_cert.public_bytes(serialization.Encoding.PEM))
    return PRINTER_CERT, PRINTER_KEY


def slicer_cert_files() -> list[str]:
    """printer.cer files of installed Bambu Studio / OrcaSlicer copies."""
    found = []
    if sys.platform.startswith("win"):
        bases = filter(None, {os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                              os.environ.get("ProgramW6432"), os.environ.get("LOCALAPPDATA")})
        for base in bases:
            for app in ("Bambu Studio*", "BambuStudio*", "OrcaSlicer*", os.path.join("Programs", "Bambu Studio*")):
                found += glob.glob(os.path.join(base, app, "resources", "cert", "printer.cer"))
    elif sys.platform == "darwin":
        found += glob.glob("/Applications/BambuStudio*.app/Contents/Resources/cert/printer.cer")
        found += glob.glob("/Applications/Bambu Studio*.app/Contents/Resources/cert/printer.cer")
        found += glob.glob("/Applications/OrcaSlicer*.app/Contents/Resources/cert/printer.cer")
    return sorted(set(found))


def install_ca_into(cer_path: str) -> bool:
    """Append our authority to a slicer's printer.cer. Returns True if it was added, False if already there."""
    with open(CA_CERT, "r", encoding="ascii") as fh:
        ca_pem = fh.read().strip()
    with open(cer_path, "r", encoding="utf-8", errors="replace") as fh:
        current = fh.read()
    if ca_pem in current:
        return False
    with open(cer_path, "a", encoding="ascii", newline="\n") as fh:
        if not current.endswith("\n"):
            fh.write("\n")
        fh.write(ca_pem + "\n")
    return True

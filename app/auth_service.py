"""Google sign-in authorization: matching a verified Google account against the allowlist.

The allowlist lives only in server config (`LEDGER_ALLOWED_EMAILS`); there is
deliberately no database table or UI for it.
"""

_GMAIL_DOMAINS = frozenset({'gmail.com', 'googlemail.com'})


def normalize_email(email):
    """Lowercase an address; for Gmail also drop the dots Google ignores and fold googlemail.com in."""
    email = (email or '').strip().lower()
    local, sep, domain = email.rpartition('@')
    if not sep or domain not in _GMAIL_DOMAINS:
        return email
    return f"{local.replace('.', '')}@gmail.com"


def parse_allowed_emails(raw):
    """Parse a comma-separated allowlist into a frozenset of normalized addresses."""
    return frozenset(normalize_email(entry) for entry in (raw or '').split(',') if entry.strip())


def is_email_allowed(email, allowed_emails):
    normalized = normalize_email(email)
    return bool(normalized) and normalized in allowed_emails


def is_authorized(claims, allowed_emails):
    """True only for a Google-verified email that is on the allowlist."""
    # `is True`, not truthiness: an absent or string-valued claim must not pass.
    return claims.get('email_verified') is True and is_email_allowed(claims.get('email'), allowed_emails)

# Security Policy

## Intended Deployment Model

Ledger is designed as a **single-user, local-network application**. It has no built-in authentication, user accounts, or session management. This is by design — the target use case is a personal device on a private home network or VPN.

**Acceptable deployments:**
- Running on `localhost` for personal use
- Running on a Raspberry Pi or home server accessible only from your local LAN
- Running behind a VPN (WireGuard, Tailscale, etc.)

**Not supported:**
- Public internet exposure without an authentication layer in front
- Multi-user deployments
- Storing data for anyone other than yourself

If you expose this application publicly without adding authentication, anyone who reaches the URL can read and modify all of your financial data. Do not do this.

## Recommended Hardening

If you need browser access from outside your home network, a VPN is strongly preferred over port-forwarding. If you do choose to expose the app, add HTTP Basic Auth in your Nginx config at a minimum:

```bash
sudo apt install -y apache2-utils
sudo htpasswd -c /etc/nginx/.htpasswd your-username
```

Then add to your Nginx `location /` block:

```nginx
auth_basic "Ledger";
auth_basic_user_file /etc/nginx/.htpasswd;
```

## Sensitive Data

- Your `.env` file contains `SECRET_KEY` — keep it out of version control (it is already in `.gitignore`)
- The SQLite database at `data/finance.db` contains all your financial records — back it up regularly and keep it off public storage
- Log files in `logs/` may contain request details — treat them as sensitive

## Reporting a Vulnerability

If you find a security issue, please open a [GitHub Issue](../../issues) marked with the `security` label, or contact the maintainer directly via the email listed on their GitHub profile. Please do not include sensitive exploit details in a public issue — describe the class of vulnerability and we will coordinate privately.

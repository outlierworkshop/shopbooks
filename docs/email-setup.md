# Emailing invoices (Gmail / Google Workspace setup)

ShopBooks sends invoice PDFs straight from your own email address over SMTP. It works with a personal
Gmail account or a Google Workspace domain — but Google **requires an "App Password"** for this (your
normal password is always rejected), and the App Password screen is hidden until you turn on
**2-Step Verification**. That's the whole reason "SMTP" seems missing. Here's the 5-minute setup.

## 1. Turn on 2-Step Verification

Go to [myaccount.google.com](https://myaccount.google.com) → **Security** → **2-Step Verification** →
turn it on (a phone prompt or authenticator is fine).

- **If 2-Step Verification is greyed out / not allowed:** you (as the Workspace admin) enable it at
  [admin.google.com](https://admin.google.com) → **Security** → **Authentication** →
  **2-Step Verification** → *Allow users to turn on 2-Step Verification*. Then redo the step above.

## 2. Create an App Password

Back at [myaccount.google.com](https://myaccount.google.com) → **Security** → **App passwords**.

- This section **only appears after 2-Step Verification is on.** If you don't see it, use the search
  box at the top of the account page and type "App passwords".
- Choose app **Mail**, name it **ShopBooks**, click **Generate**.
- Google shows a **16-character** password. Copy it (the spaces Google shows are just for reading —
  you can paste it with or without them; ShopBooks handles either).

## 3. Enter it in ShopBooks

**Settings → Email sending (SMTP):**

| Field | Value |
|---|---|
| SMTP host | `smtp.gmail.com` |
| SMTP port | `587` |
| SMTP user (email) | your full email address (e.g. `you@yourcompany.com`) |
| SMTP password | the 16-character App Password from step 2 |

Click **Save all settings**.

## 4. Test it

Click **Send test email** (right under the SMTP fields). ShopBooks emails a test message to your own
address — check your inbox (and spam). A green "Test email sent" note means you're done; open any
invoice and use its **Email** button to send it to a customer.

## Troubleshooting

| What you see | What it means |
|---|---|
| "The mail server rejected the login…" (535 / 5.7.8) | The password isn't a valid App Password, or 2-Step Verification isn't on yet. Redo steps 1–2; paste the App Password exactly. |
| No **App passwords** section, even with 2SV on | A Workspace admin policy blocks it (Advanced Protection, SSO/SAML sign-in, or a security setting). Check the admin console; if it's truly blocked, App Passwords can't be used and OAuth would be the alternative (not built yet — ask). |
| "Couldn't reach the mail server…" | Network/firewall issue or a wrong host/port. Confirm `smtp.gmail.com` / `587`; some networks block outbound SMTP. |
| Sender address / signature is wrong | Emails come **from** the SMTP user address. The subject/body templates are on the same Settings page under Invoicing. |

## Notes

- The App Password is stored locally in your books database like any other setting; it's specific to
  this app and can be revoked anytime from the same Google **App passwords** page.
- Port `465` (SSL) also works with some setups, but `587` (STARTTLS) is what ShopBooks uses and is the
  recommended Gmail setting.

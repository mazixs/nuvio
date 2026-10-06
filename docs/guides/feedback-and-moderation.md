# Feedback and moderation

## Sending feedback

In a private chat, `/feedback` starts a plain-text report. The feedback button on a welcome or error message opens the same form. A button attached to an error binds that report to its error code and source URL; exception text and operational diagnostics are never included in the form.

Send 3 to 2,000 characters. The bot confirms the report number only after saving it. If saving fails or the rate limit is reached, it explains that the report was not saved and keeps the form open. This confirmation does not promise a response or resolution time.

`/cancel` or the form's cancel button closes the input. Other commands and non-feedback buttons leave feedback mode. An input form expires after 30 minutes; error-button context remains available for up to 24 hours and the latest ten errors per user. Restarting the bot clears open forms and button context, but saved reports remain available.

Only one new report per user per minute is accepted. Reprocessing the same Telegram message does not create another database record. While a feedback form is open, links in the text belong to the report rather than starting a download. Media attachments are not accepted by the form. Existing CSI surveys remain a separate feature.

## Reviewing reports

Sign in to the WebUI and open **Feedback** (`/feedback`). Reports show the user's profile, text, creation time in UTC, and an error code and source URL when available. Source URLs are displayed as plain text. Use **Open**, **Closed**, or **All** to filter reports, **Mark as handled** to close a report, and **Reopen** to return it to the queue. These actions do not send a message to the user.

A user profile also shows the ten latest reports. The WebUI defaults to English and offers Russian through its language switch.

## Restricting access

Open **Users**, choose a profile, and use **Block user** in **Bot access**. An optional internal reason is visible only to administrators. **Unblock user** restores access. Accounts in `ADMIN_IDS` cannot be blocked.

The bot and WebUI read the same `analytics.db` in `DATA_DIR`. Access changes apply without restarting the bot. Blocked users receive an explicit administrator restriction notice. Their commands, callbacks, download requests, and feedback submissions are refused, and they are excluded from broadcasts and CSI surveys. Delivery checks access again before sending media or retrying an explicit Telegram rate limit.

Blocking preserves the profile, reports, and historical activity. It does not recall messages or cancel a Telegram request that is already in flight; such a request may still complete. Background extraction already started may continue, but subsequent checked media delivery is refused. This restriction applies inside Nuvio and does not block the account on Telegram itself.

## Storage and upgrades

Startup automatically adds the feedback table and moderation columns to existing analytics databases, preserving SQLite WAL mode and existing data. No new environment variables are required. The WebUI mutations require authentication and a session-bound CSRF token. Feedback text and internal reasons are escaped when rendered.

Keep regular backups of `analytics.db` to retain reports and access settings. As with other code changes, a running deployment needs the updated application image before these features are available.

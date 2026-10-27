# Microsoft 365 setup

The relay signs in to Microsoft Graph as an app (client credentials) and sends mail as the mailbox named in
each message's sender address. Two parts:

1. An **Entra app registration** the relay signs in with.
2. **Permission to send**, scoped in Exchange Online to only the mailboxes the relay should send as.

Microsoft documents scoping as *RBAC for Applications*, which replaces the older Application Access
Policies: <https://learn.microsoft.com/en-us/exchange/permissions-exo/application-rbac>.

**Shortcut:** `pwsh ./tools/Setup-RelayTenant.ps1` walks through everything below from a menu. It finds
what already exists before creating anything, saves `AUTHORITY`, `CLIENT_ID` and `CLIENT_SECRET` to `.env`, and
can be re-run safely. The manual steps follow for reference.

## 1. Register the app

In the Entra admin center, go to **App registrations → New registration**:

1. Name it `SMTP Graph Relay`. Single tenant, no redirect URI.
2. From **Overview**, copy:
   - **Application (client) ID** → `CLIENT_ID`
   - **Directory (tenant) ID** → `AUTHORITY=https://login.microsoftonline.com/<tenant id>`
3. In **Certificates & secrets → New client secret**, copy the **Value** → `CLIENT_SECRET`.
   - Note the expiry date. The relay queues mail (and logs `token request failed`) once the secret expires, so put a reminder in your calendar.
4. In **API permissions**, do **not** add Microsoft Graph `Mail.Send` here if you follow section 2.
   - Permissions granted in Entra and in Exchange add together. A tenant-wide `Mail.Send` grant in Entra would undo the mailbox scoping below.
   - Remove any `Mail.Send` / `Mail.ReadWrite` application permissions already on the app.

## 2. Allow sending as specific mailboxes (Exchange Online PowerShell)

```powershell
Connect-ExchangeOnline
```

Create the Exchange service principal for the app. Use the IDs from **Enterprise applications** (not App
registrations) → *SMTP Graph Relay* → Overview: *Application ID* and *Object ID*.

```powershell
New-ServicePrincipal -AppId <application-id> -ObjectId <enterprise-app-object-id> -DisplayName "SMTP Graph Relay"
```

Define which mailboxes it may send as. Use the same addresses as `SMTP_ALLOWED_SENDERS`:

```powershell
New-ManagementScope -Name "SMTP Relay Senders" -RecipientRestrictionFilter "PrimarySmtpAddress -eq 'scanner@contoso.com' -or PrimarySmtpAddress -eq 'alerts@contoso.com'"
```

To manage the list without PowerShell later, filter on a custom attribute instead and set that attribute on
each mailbox, for example `"CustomAttribute15 -eq 'SMTPRelay'"`.

Grant send permission within that scope:

```powershell
New-ManagementRoleAssignment -App <application-id> -Role "Application Mail.Send" -CustomResourceScope "SMTP Relay Senders"
```

Only if you set `LARGE_MESSAGES=true` (messages over ~3 MB need drafts and attachment uploads):

```powershell
New-ManagementRoleAssignment -App <application-id> -Role "Application Mail.ReadWrite" -CustomResourceScope "SMTP Relay Senders"
```

Check it. This should show the role as granted for an in-scope mailbox, and not for any other mailbox:

```powershell
Test-ServicePrincipalAuthorization -Identity <application-id> -Resource scanner@contoso.com
```

Changes can take **30 minutes to 2 hours** to take effect. Until then Graph may answer 403, which the relay
reports to the device as `550 5.7.1 Microsoft 365 refused to send as this mailbox`.

## Not recommended: tenant-wide permission

If you skip section 2 and instead grant Microsoft Graph **Application** permission `Mail.Send` (plus
`Mail.ReadWrite` for large messages) in Entra with admin consent, the relay works. But the app can then send as
**every** mailbox in the tenant, and anyone who obtains the client secret can too. The relay's
`SMTP_ALLOWED_SENDERS` check still applies to devices, but it does not protect a leaked secret.

## Limits worth knowing

- Exchange Online allows each mailbox **30 messages a minute** and **10,000 recipients a day**. Over that,
  Exchange delays or bounces mail after Graph has already accepted it. Those failures arrive as bounce
  messages (NDRs) in the sending mailbox, not in the relay log.
- The default maximum message size is **35 MB**. Admins can raise it to 150 MB (112 MB for mail leaving Microsoft 365).
- Graph allows 4 concurrent requests per mailbox for one app; the relay keeps to that.

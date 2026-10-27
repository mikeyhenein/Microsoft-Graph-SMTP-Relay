#Requires -Version 7.0
<#
.SYNOPSIS
    Guided Microsoft 365 setup for smtp-graph-relay: the Entra app it signs in with, and
    Exchange Online scoping so it can only send as the mailboxes you choose.

.DESCRIPTION
    Menu driven. Every step looks for what already exists before creating anything, so steps
    can be re-run safely. "Run the full guided setup" walks through all of them in order.

    What it sets up (see docs/tenant-setup.md for the background):
      1. An Entra app registration + enterprise app (service principal), single tenant.
      2. A client secret, saved straight into the relay's .env file.
      3. Removal of any tenant-wide Mail.Send / Mail.ReadWrite Graph permission on the app,
         which would otherwise override the mailbox scoping.
      4. Exchange Online "RBAC for Applications": a service principal, a management scope listing
         the allowed mailboxes, and the "Application Mail.Send" role (plus "Application
         Mail.ReadWrite" if LARGE_MESSAGES is on) limited to that scope.

    Needs PowerShell 7 and these modules (the script offers to install them for the current user):
      Microsoft.Graph.Authentication, Microsoft.Graph.Applications, ExchangeOnlineManagement (3.x)
    Admin roles: Application Administrator (or Cloud Application Administrator) in Entra for the app,
    and Exchange Administrator in Entra (or Organization Management in Exchange) for the scoping.

.PARAMETER AppName
    Display name of the Entra app. Default: "SMTP Graph Relay".

.PARAMETER ScopeName
    Name of the Exchange management scope. Default: "SMTP Relay Senders".

.PARAMETER EnvFile
    The relay's .env file. Default: .env in the repository root (one folder above this script).

.EXAMPLE
    pwsh ./tools/Setup-RelayTenant.ps1
#>
[CmdletBinding()]
param(
    [string]$AppName = 'SMTP Graph Relay',
    [string]$ScopeName = 'SMTP Relay Senders',
    [string]$EnvFile = (Join-Path (Split-Path $PSScriptRoot -Parent) '.env')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$GraphResourceAppId = '00000003-0000-0000-c000-000000000000'  # Microsoft Graph
$MailRoles = @('Mail.Send', 'Mail.ReadWrite')
$RequiredModules = @('Microsoft.Graph.Authentication', 'Microsoft.Graph.Applications', 'ExchangeOnlineManagement')

$State = [ordered]@{
    TenantId    = $null
    AppId       = $null   # Application (client) ID
    AppObjectId = $null   # App registration object ID
    SpObjectId  = $null   # Enterprise application (service principal) object ID
    Mailboxes   = @()
}

# --- Output helpers ------------------------------------------------------------------------

function Write-Title([string]$Text) { Write-Host ''; Write-Host "== $Text ==" -ForegroundColor Cyan }
function Write-Ok([string]$Text) { Write-Host "  [ok] $Text" -ForegroundColor Green }
function Write-Note([string]$Text) { Write-Host "  $Text" -ForegroundColor Gray }
function Write-Warn([string]$Text) { Write-Host "  [!] $Text" -ForegroundColor Yellow }
function Write-Fail([string]$Text) { Write-Host "  [x] $Text" -ForegroundColor Red }

function Read-YesNo([string]$Prompt, [bool]$Default = $true) {
    $suffix = if ($Default) { '[Y/n]' } else { '[y/N]' }
    while ($true) {
        $answer = (Read-Host "  $Prompt $suffix").Trim().ToLowerInvariant()
        if (-not $answer) { return $Default }
        if ($answer -in @('y', 'yes')) { return $true }
        if ($answer -in @('n', 'no')) { return $false }
    }
}

function Wait-ForEnter { [void](Read-Host '  Press Enter to return to the menu') }

function ConvertTo-ODataString([string]$Value) { $Value.Replace("'", "''") }

# --- .env helpers ----------------------------------------------------------------------------

function Get-EnvValue([string]$Key) {
    if (-not (Test-Path $EnvFile)) { return $null }
    foreach ($line in Get-Content -LiteralPath $EnvFile) {
        if ($line -match "^\s*$([regex]::Escape($Key))=(.*)$") { return $Matches[1].Trim() }
    }
    return $null
}

function Set-EnvValue([string]$Key, [string]$Value) {
    $lines = [System.Collections.Generic.List[string]]::new()
    if (Test-Path $EnvFile) {
        $lines.AddRange([string[]](Get-Content -LiteralPath $EnvFile))
    } else {
        $sample = Join-Path (Split-Path $EnvFile -Parent) '.env.sample'
        if (Test-Path $sample) { $lines.AddRange([string[]](Get-Content -LiteralPath $sample)) }
    }
    $pattern = "^\s*#?\s*$([regex]::Escape($Key))="
    $index = -1
    for ($i = 0; $i -lt $lines.Count; $i++) { if ($lines[$i] -match $pattern) { $index = $i; break } }
    if ($index -ge 0) { $lines[$index] = "$Key=$Value" } else { $lines.Add("$Key=$Value") }
    $utf8 = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllLines($EnvFile, $lines, $utf8)
}

# --- Connections -----------------------------------------------------------------------------

function Test-Modules {
    Write-Title 'Check PowerShell modules'
    $missing = @($RequiredModules | Where-Object { -not (Get-Module -ListAvailable -Name $_) })
    if (-not $missing) { Write-Ok 'All required modules are installed.'; return $true }
    Write-Warn "Missing: $($missing -join ', ')"
    if (-not (Read-YesNo 'Install them now for the current user from the PowerShell Gallery?')) { return $false }
    foreach ($module in $missing) {
        Write-Note "Installing $module ..."
        Install-Module -Name $module -Scope CurrentUser -Repository PSGallery -Force -AllowClobber
    }
    Write-Ok 'Modules installed.'
    return $true
}

function Connect-Graph {
    $context = Get-MgContext -ErrorAction SilentlyContinue
    if ($context -and $context.Scopes -contains 'Application.ReadWrite.All') {
        $State.TenantId = $context.TenantId
        return
    }
    Write-Note 'Signing in to Microsoft Graph (a browser window opens) ...'
    Connect-MgGraph -Scopes 'Application.ReadWrite.All', 'AppRoleAssignment.ReadWrite.All' -NoWelcome
    $State.TenantId = (Get-MgContext).TenantId
    Write-Ok "Connected to Microsoft Graph, tenant $($State.TenantId)"
}

function Connect-Exchange {
    $connected = Get-ConnectionInformation -ErrorAction SilentlyContinue | Where-Object { $_.State -eq 'Connected' }
    if ($connected) { return }
    Write-Note 'Signing in to Exchange Online (a browser window opens) ...'
    Connect-ExchangeOnline -ShowBanner:$false
    Write-Ok 'Connected to Exchange Online.'
}

# --- Entra app -------------------------------------------------------------------------------

function Find-RelayApp {
    Connect-Graph
    if ($State.AppId) { return $true }
    $apps = @(Get-MgApplication -Filter "displayName eq '$(ConvertTo-ODataString $AppName)'")
    if ($apps.Count -eq 0) { return $false }
    $app = $apps[0]
    if ($apps.Count -gt 1) {
        Write-Warn "Found $($apps.Count) apps named '$AppName':"
        for ($i = 0; $i -lt $apps.Count; $i++) {
            Write-Note "  [$($i + 1)] AppId $($apps[$i].AppId), created $($apps[$i].CreatedDateTime)"
        }
        $pick = [int](Read-Host '  Which one should the relay use? (number)')
        $app = $apps[$pick - 1]
    }
    $State.AppId = $app.AppId
    $State.AppObjectId = $app.Id
    $sp = Get-MgServicePrincipal -Filter "appId eq '$($app.AppId)'" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($sp) { $State.SpObjectId = $sp.Id }
    return $true
}

function Step-CreateApp {
    Write-Title 'Step 1: Entra app registration'
    Connect-Graph
    if (Find-RelayApp) {
        Write-Ok "App '$AppName' already exists (client ID $($State.AppId))."
    } else {
        if (-not (Read-YesNo "Create a new single-tenant app named '$AppName'?")) { return }
        $app = New-MgApplication -DisplayName $AppName -SignInAudience 'AzureADMyOrg' `
            -Notes 'Used by smtp-graph-relay to send mail through Microsoft Graph.'
        $State.AppId = $app.AppId
        $State.AppObjectId = $app.Id
        Write-Ok "Created app registration (client ID $($app.AppId))."
    }
    if (-not $State.SpObjectId) {
        $sp = New-MgServicePrincipal -AppId $State.AppId
        $State.SpObjectId = $sp.Id
        Write-Ok "Created enterprise application (object ID $($sp.Id))."
    } else {
        Write-Ok "Enterprise application exists (object ID $($State.SpObjectId))."
    }
    Set-EnvValue 'AUTHORITY' "https://login.microsoftonline.com/$($State.TenantId)"
    Set-EnvValue 'CLIENT_ID' $State.AppId
    Write-Ok "Saved AUTHORITY and CLIENT_ID to $EnvFile"
}

function Step-NewSecret {
    Write-Title 'Step 2: Client secret'
    if (-not (Find-RelayApp)) { Write-Fail "App '$AppName' not found. Run step 1 first."; return }
    $existing = @((Get-MgApplication -ApplicationId $State.AppObjectId).PasswordCredentials)
    foreach ($credential in $existing) {
        Write-Note ("Existing secret '{0}' ({1}...) expires {2:yyyy-MM-dd}" -f $credential.DisplayName, $credential.Hint, $credential.EndDateTime)
    }
    if (-not (Read-YesNo 'Create a new client secret?')) { return }
    $months = Read-Host '  Valid for how many months? (1-24, default 12)'
    if (-not $months) { $months = 12 }
    $months = [Math]::Min([Math]::Max([int]$months, 1), 24)
    $end = (Get-Date).AddMonths($months)
    $secret = Add-MgApplicationPassword -ApplicationId $State.AppObjectId -PasswordCredential @{
        displayName = "smtp-graph-relay $(Get-Date -Format 'yyyy-MM-dd')"
        endDateTime = $end
    }
    if (Read-YesNo "Save it to $EnvFile as CLIENT_SECRET (recommended; it is not shown on screen)?") {
        Set-EnvValue 'CLIENT_SECRET' $secret.SecretText
        Write-Ok "Saved CLIENT_SECRET. It expires $($end.ToString('yyyy-MM-dd')): put a reminder in your calendar."
    } else {
        Write-Warn 'Copy it now; it cannot be shown again:'
        Write-Host "  $($secret.SecretText)"
    }
    if ($existing) { Write-Note 'Once the relay works with the new secret, delete the old one in Entra (Certificates & secrets).' }
    Write-Note 'Restart the relay to pick it up: docker compose up -d'
}

function Step-RemoveTenantWide {
    Write-Title 'Step 3: Remove tenant-wide mail permissions'
    Write-Note 'Graph permissions granted in Entra add to the Exchange scoping; a tenant-wide Mail.Send'
    Write-Note 'grant would let the app send as every mailbox. This step removes any that exist.'
    if (-not (Find-RelayApp) -or -not $State.SpObjectId) { Write-Fail 'App not found. Run step 1 first.'; return }
    $graphSp = Get-MgServicePrincipal -Filter "appId eq '$GraphResourceAppId'"
    $mailRoleIds = @($graphSp.AppRoles | Where-Object { $_.Value -in $MailRoles } | ForEach-Object { $_.Id })

    $grants = @(Get-MgServicePrincipalAppRoleAssignment -ServicePrincipalId $State.SpObjectId -All |
        Where-Object { $_.ResourceId -eq $graphSp.Id -and $_.AppRoleId -in $mailRoleIds })
    foreach ($grant in $grants) {
        $role = ($graphSp.AppRoles | Where-Object { $_.Id -eq $grant.AppRoleId }).Value
        Remove-MgServicePrincipalAppRoleAssignment -ServicePrincipalId $State.SpObjectId -AppRoleAssignmentId $grant.Id
        Write-Ok "Removed admin consent for Microsoft Graph $role."
    }

    $app = Get-MgApplication -ApplicationId $State.AppObjectId
    $changed = $false
    $access = @(foreach ($resource in $app.RequiredResourceAccess) {
        if ($resource.ResourceAppId -eq $GraphResourceAppId) {
            $kept = @($resource.ResourceAccess | Where-Object { -not ($_.Type -eq 'Role' -and $_.Id -in $mailRoleIds) })
            if ($kept.Count -ne @($resource.ResourceAccess).Count) { $changed = $true }
            if ($kept.Count) { @{ ResourceAppId = $resource.ResourceAppId; ResourceAccess = $kept } }
        } else {
            $resource
        }
    })
    if ($changed) {
        Update-MgApplication -ApplicationId $State.AppObjectId -RequiredResourceAccess $access
        Write-Ok 'Removed Mail.* application permissions from the API permissions list.'
    }
    if (-not $grants -and -not $changed) { Write-Ok 'No tenant-wide mail permissions found. Nothing to remove.' }
}

# --- Exchange Online scoping -----------------------------------------------------------------

function Step-ExchangeServicePrincipal {
    Write-Title 'Step 4: Exchange service principal'
    if (-not (Find-RelayApp) -or -not $State.SpObjectId) { Write-Fail 'App not found. Run step 1 first.'; return }
    Connect-Exchange
    $existing = Get-ServicePrincipal -Identity $State.AppId -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Ok 'Exchange already knows the app.'
        return
    }
    # The IDs come from the enterprise application (service principal), not the app registration.
    New-ServicePrincipal -AppId $State.AppId -ObjectId $State.SpObjectId -DisplayName $AppName | Out-Null
    Write-Ok "Registered '$AppName' in Exchange Online."
}

function Read-Mailboxes {
    $fromEnv = Get-EnvValue 'SMTP_ALLOWED_SENDERS'
    $suggested = @()
    if ($fromEnv) { $suggested = @($fromEnv -split '[,\s]+' | Where-Object { $_ }) }
    if ($suggested) {
        Write-Note "SMTP_ALLOWED_SENDERS in .env: $($suggested -join ', ')"
        if (Read-YesNo 'Use these addresses for the scope?') { return $suggested }
    }
    $typed = Read-Host '  Mailboxes the relay may send as (comma separated; *@domain.com for a whole domain)'
    $entries = @($typed -split '[,\s]+' | Where-Object { $_ })
    if ($entries -and -not $fromEnv -and (Read-YesNo 'Also save these as SMTP_ALLOWED_SENDERS in .env?')) {
        Set-EnvValue 'SMTP_ALLOWED_SENDERS' ($entries -join ',')
        Write-Ok 'Saved SMTP_ALLOWED_SENDERS.'
    }
    return $entries
}

function Step-Scope {
    Write-Title 'Step 5: Management scope (which mailboxes)'
    Connect-Exchange
    $entries = @(Read-Mailboxes)
    if (-not $entries) { Write-Fail 'No mailboxes given.'; return }

    $clauses = foreach ($entry in $entries) {
        $value = ConvertTo-ODataString $entry.ToLowerInvariant()
        if ($entry.StartsWith('*@')) {
            "PrimarySmtpAddress -like '$value'"
        } else {
            $mailbox = Get-EXOMailbox -Identity $entry -ErrorAction SilentlyContinue
            if (-not $mailbox) { Write-Warn "$entry was not found as a mailbox; it is included anyway." }
            "PrimarySmtpAddress -eq '$value'"
        }
    }
    $filter = $clauses -join ' -or '
    Write-Note "Filter: $filter"

    $scope = Get-ManagementScope -Identity $ScopeName -ErrorAction SilentlyContinue
    if ($scope) {
        if (-not (Read-YesNo "Scope '$ScopeName' exists with filter: $($scope.RecipientFilter). Replace it?")) { return }
        Set-ManagementScope -Identity $ScopeName -RecipientRestrictionFilter $filter
        Write-Ok "Updated scope '$ScopeName'."
    } else {
        New-ManagementScope -Name $ScopeName -RecipientRestrictionFilter $filter | Out-Null
        Write-Ok "Created scope '$ScopeName'."
    }
    $State.Mailboxes = @($entries | Where-Object { -not $_.StartsWith('*@') })
}

function Step-Roles {
    Write-Title 'Step 6: Grant send permission within the scope'
    if (-not (Find-RelayApp)) { Write-Fail 'App not found. Run step 1 first.'; return }
    Connect-Exchange
    if (-not (Get-ManagementScope -Identity $ScopeName -ErrorAction SilentlyContinue)) {
        Write-Fail "Scope '$ScopeName' not found. Run step 5 first."
        return
    }
    $roles = @('Application Mail.Send')
    $large = (Get-EnvValue 'LARGE_MESSAGES') -in @('true', '1', 'yes', 'on')
    if ($large) {
        Write-Note 'LARGE_MESSAGES is on in .env: Application Mail.ReadWrite is needed for drafts and uploads.'
        $roles += 'Application Mail.ReadWrite'
    } elseif (Read-YesNo 'Will you turn on LARGE_MESSAGES (messages over ~3 MB)?' $false) {
        $roles += 'Application Mail.ReadWrite'
    }
    # Exchange shows the service principal's object ID as the assignee.
    $assignees = @($State.SpObjectId, $State.AppId, $AppName)
    foreach ($role in $roles) {
        $existing = @(Get-ManagementRoleAssignment -Role $role -ErrorAction SilentlyContinue | Where-Object {
            "$($_.RoleAssigneeName)" -in $assignees -and "$($_.CustomResourceScope)" -eq $ScopeName
        })
        if ($existing) {
            Write-Ok "'$role' is already assigned within '$ScopeName'."
            continue
        }
        New-ManagementRoleAssignment -App $State.SpObjectId -Role $role -CustomResourceScope $ScopeName | Out-Null
        Write-Ok "Assigned '$role' within '$ScopeName'."
    }
    Write-Note 'Changes can take 30 minutes to 2 hours to apply. Until then Graph may refuse with 403.'
}

function Step-Test {
    Write-Title 'Step 7: Check what the app may do'
    if (-not (Find-RelayApp)) { Write-Fail 'App not found. Run step 1 first.'; return }
    Connect-Exchange
    $targets = @($State.Mailboxes)
    $typed = Read-Host '  Mailbox to check (Enter to check the scoped mailboxes; try one OUTSIDE the scope too)'
    if ($typed) { $targets = @($typed) }
    if (-not $targets) { Write-Fail 'No mailbox to check.'; return }
    foreach ($target in $targets) {
        Write-Note "Mailbox: $target"
        Test-ServicePrincipalAuthorization -Identity $State.AppId -Resource $target |
            Format-Table RoleName, GrantedPermissions, AllowedResourceScope, ScopeType, InScope -AutoSize | Out-Host
    }
    Write-Note 'Scoped mailboxes should show InScope True; any other mailbox should show False.'
    Write-Note 'This check ignores permissions granted in Entra; step 3 removes those.'
}

function Show-Summary {
    Write-Title 'Summary'
    [void](Find-RelayApp)
    Write-Note "Tenant ID          : $($State.TenantId)"
    Write-Note "App (client) ID    : $($State.AppId)"
    Write-Note "App object ID      : $($State.AppObjectId)"
    Write-Note "Enterprise app ID  : $($State.SpObjectId)"
    Write-Note "Scope              : $ScopeName"
    Write-Note ".env file          : $EnvFile"
    foreach ($key in 'AUTHORITY', 'CLIENT_ID', 'CLIENT_SECRET', 'SMTP_ALLOWED_SENDERS') {
        $value = Get-EnvValue $key
        $shown = if (-not $value) { '(not set)' } elseif ($key -eq 'CLIENT_SECRET') { '(set, hidden)' } else { $value }
        Write-Note ("{0,-19}: {1}" -f $key, $shown)
    }
}

function Step-All {
    Write-Title 'Full guided setup'
    Write-Note 'Runs steps 1 to 7 in order. Each step asks before changing anything.'
    if (-not (Test-Modules)) { return }
    Step-CreateApp
    if (-not $State.AppId) { return }
    Step-NewSecret
    Step-RemoveTenantWide
    Step-ExchangeServicePrincipal
    Step-Scope
    Step-Roles
    Step-Test
    Show-Summary
    Write-Ok 'Done. Restart the relay so it reads the new .env: docker compose up -d'
}

# --- Menu ------------------------------------------------------------------------------------

$menu = [ordered]@{
    '1' = @{ Label = "Create or find the Entra app '$AppName'"; Action = { Step-CreateApp } }
    '2' = @{ Label = 'Create a client secret (saved to .env)'; Action = { Step-NewSecret } }
    '3' = @{ Label = 'Remove tenant-wide mail permissions from the app'; Action = { Step-RemoveTenantWide } }
    '4' = @{ Label = 'Register the app in Exchange Online'; Action = { Step-ExchangeServicePrincipal } }
    '5' = @{ Label = 'Choose the mailboxes it may send as (scope)'; Action = { Step-Scope } }
    '6' = @{ Label = 'Grant send permission within that scope'; Action = { Step-Roles } }
    '7' = @{ Label = 'Check permissions for a mailbox'; Action = { Step-Test } }
    'A' = @{ Label = 'Run the full guided setup (1-7)'; Action = { Step-All } }
    'S' = @{ Label = 'Show summary'; Action = { Show-Summary } }
    'M' = @{ Label = 'Check / install PowerShell modules'; Action = { [void](Test-Modules) } }
    'Q' = @{ Label = 'Quit'; Action = $null }
}

while ($true) {
    Clear-Host
    Write-Host 'smtp-graph-relay: Microsoft 365 setup' -ForegroundColor Cyan
    Write-Host "App: $AppName   Scope: $ScopeName   .env: $EnvFile" -ForegroundColor DarkGray
    Write-Host ''
    foreach ($key in $menu.Keys) { Write-Host ("  [{0}] {1}" -f $key, $menu[$key].Label) }
    Write-Host ''
    $choice = (Read-Host 'Choose').Trim().ToUpperInvariant()
    if (-not $menu.Contains($choice)) { continue }
    if ($choice -eq 'Q') { break }
    try {
        & $menu[$choice].Action
    } catch {
        Write-Fail $_.Exception.Message
    }
    Wait-ForEnter
}

if (Get-Command Disconnect-ExchangeOnline -ErrorAction SilentlyContinue) {
    Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
}
if (Get-MgContext -ErrorAction SilentlyContinue) { Disconnect-MgGraph -ErrorAction SilentlyContinue | Out-Null }

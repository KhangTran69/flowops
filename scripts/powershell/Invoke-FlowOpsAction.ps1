<#
.SYNOPSIS
    Exchange Online actions for FlowOps (the parts Microsoft Graph cannot do).

.DESCRIPTION
    Reads one JSON request from stdin: {"action": "...", "context": {...}}
    and writes exactly one JSON result line to stdout:
        {"status":"ok|error","action":"...","detail":"...","output":{...}}
    A non-zero exit code always accompanies "status":"error", so the caller
    never mistakes a failure for success: exit 2 = invalid request or
    configuration (do not retry), exit 1 = runtime failure (safe to retry).

    Authentication is app-only (certificate) so it runs unattended:
        FLOWOPS_EXO_APP_ID, FLOWOPS_EXO_CERT_THUMBPRINT, FLOWOPS_EXO_ORGANIZATION
    With -DryRun nothing connects; the planned cmdlets are returned instead.

.EXAMPLE
    '{"action":"archive_email_mailbox","context":{"employee_email":"jane@contoso.com"}}' |
        pwsh -NoProfile -File ./Invoke-FlowOpsAction.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

function ConvertTo-Bytes {
    <# Parse Exchange size strings such as "1.2 GB (1,288,490,188 bytes)". #>
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Size)
    if ($Size -match '\(([\d,]+) bytes\)') {
        return [int64]($Matches[1] -replace ',', '')
    }
    if ($Size -match '^\s*([\d.]+)\s*(B|KB|MB|GB|TB)\s*$') {
        $units = @{ B = 1; KB = 1KB; MB = 1MB; GB = 1GB; TB = 1TB }
        return [int64]([double]$Matches[1] * $units[$Matches[2]])
    }
    return $null
}

function Write-FlowOpsResult {
    param(
        [string]$Status,
        [string]$Action,
        [string]$Detail,
        [hashtable]$Output = @{}
    )
    [ordered]@{
        status = $Status
        action = $Action
        detail = $Detail
        output = $Output
    } | ConvertTo-Json -Compress -Depth 6 | ForEach-Object { [Console]::Out.WriteLine($_) }
}

function Get-RequiredValue {
    param([object]$Context, [string]$Name)
    $value = $Context.$Name
    if ([string]::IsNullOrWhiteSpace([string]$value)) {
        throw [System.ArgumentException]::new("context.$Name is required")
    }
    return [string]$value
}

function Invoke-Planned {
    <# Run (or, in dry-run, just record) one Exchange cmdlet call. #>
    param([System.Collections.Generic.List[string]]$Plan, [string]$Description, [scriptblock]$Command)
    $Plan.Add($Description)
    if (-not $DryRun) { & $Command }
}

function Invoke-ArchiveMailbox {
    param([object]$Context, [System.Collections.Generic.List[string]]$Plan)
    $upn = Get-RequiredValue $Context 'employee_email'
    $output = @{ mailbox = $upn; converted = $true }
    if (-not $DryRun) {
        $mailbox = Get-EXOMailbox -Identity $upn -Properties RecipientTypeDetails
        $stats = Get-EXOMailboxStatistics -Identity $upn
        $output.size_bytes = ConvertTo-Bytes ([string]$stats.TotalItemSize)
        $output.item_count = [int]$stats.ItemCount
        if ($mailbox.RecipientTypeDetails -eq 'SharedMailbox') {
            $output.converted = $false
            $Plan.Add("already a shared mailbox; no change")
            return $output
        }
    }
    Invoke-Planned $Plan "Set-Mailbox -Identity $upn -Type Shared" { Set-Mailbox -Identity $upn -Type Shared }
    Invoke-Planned $Plan "Set-Mailbox -Identity $upn -HiddenFromAddressListsEnabled `$true" {
        Set-Mailbox -Identity $upn -HiddenFromAddressListsEnabled $true
    }
    if ($Context.manager_email) {
        $manager = [string]$Context.manager_email
        Invoke-Planned $Plan "Add-MailboxPermission -Identity $upn -User $manager -AccessRights FullAccess" {
            Add-MailboxPermission -Identity $upn -User $manager -AccessRights FullAccess -InheritanceType All -AutoMapping $true | Out-Null
        }
        $output.delegate = $manager
    }
    return $output
}

function Invoke-ConfigureMailbox {
    param([object]$Context, [System.Collections.Generic.List[string]]$Plan)
    $upn = Get-RequiredValue $Context 'employee_email'
    $timeZone = if ($Context.time_zone) { [string]$Context.time_zone } else { 'UTC' }
    $language = if ($Context.language) { [string]$Context.language } else { 'en-US' }
    Invoke-Planned $Plan "Set-MailboxRegionalConfiguration -Identity $upn -TimeZone '$timeZone' -Language $language" {
        Set-MailboxRegionalConfiguration -Identity $upn -TimeZone $timeZone -Language $language -LocalizeDefaultFolderName
    }
    Invoke-Planned $Plan "Set-Mailbox -Identity $upn -AuditEnabled `$true" { Set-Mailbox -Identity $upn -AuditEnabled $true }
    return @{ mailbox = $upn; time_zone = $timeZone; language = $language }
}

function Invoke-BlockMailboxAccess {
    param([object]$Context, [System.Collections.Generic.List[string]]$Plan)
    $upn = Get-RequiredValue $Context 'employee_email'
    Invoke-Planned $Plan "Set-CASMailbox -Identity $upn -OWAEnabled `$false -ActiveSyncEnabled `$false -PopEnabled `$false -ImapEnabled `$false" {
        Set-CASMailbox -Identity $upn -OWAEnabled $false -ActiveSyncEnabled $false -PopEnabled $false -ImapEnabled $false
    }
    return @{ mailbox = $upn; client_access = 'blocked' }
}

function Invoke-FlowOpsAction {
    $raw = [Console]::In.ReadToEnd()
    $action = 'unknown'
    $connected = $false
    $phase = 'validate'
    try {
        if ([string]::IsNullOrWhiteSpace($raw)) { throw 'expected a JSON request on stdin' }
        $request = $raw | ConvertFrom-Json
        $action = [string]$request.action
        $context = $request.context
        $handlers = @{
            archive_email_mailbox   = ${function:Invoke-ArchiveMailbox}
            configure_email_mailbox = ${function:Invoke-ConfigureMailbox}
            disable_user_account    = ${function:Invoke-BlockMailboxAccess}
        }
        if (-not $handlers.ContainsKey($action)) {
            throw "unsupported action '$action'"
        }
        if (-not $DryRun) {
            foreach ($name in 'FLOWOPS_EXO_APP_ID', 'FLOWOPS_EXO_CERT_THUMBPRINT', 'FLOWOPS_EXO_ORGANIZATION') {
                if (-not [Environment]::GetEnvironmentVariable($name)) { throw "$name is not set" }
            }
            $phase = 'connect'
            Import-Module ExchangeOnlineManagement -ErrorAction Stop
            Connect-ExchangeOnline -AppId $env:FLOWOPS_EXO_APP_ID `
                -CertificateThumbprint $env:FLOWOPS_EXO_CERT_THUMBPRINT `
                -Organization $env:FLOWOPS_EXO_ORGANIZATION -ShowBanner:$false
            $connected = $true
        }
        $phase = 'run'
        $plan = [System.Collections.Generic.List[string]]::new()
        $output = & $handlers[$action] $context $plan
        $output.commands = @($plan)
        $output.dry_run = [bool]$DryRun
        $mode = if ($DryRun) { 'planned' } else { 'completed' }
        Write-FlowOpsResult -Status 'ok' -Action $action -Detail "Exchange Online $action $mode ($($plan.Count) step(s))" -Output $output | Out-Null
        return 0
    }
    catch {
        Write-FlowOpsResult -Status 'error' -Action $action -Detail $_.Exception.Message |
            Out-Null
        if ($phase -eq 'validate' -or $_.Exception -is [System.ArgumentException]) { return 2 }
        return 1
    }
    finally {
        if ($connected) { Disconnect-ExchangeOnline -Confirm:$false | Out-Null }
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    exit (Invoke-FlowOpsAction)
}

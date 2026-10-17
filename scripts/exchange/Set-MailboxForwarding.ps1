<#
.SYNOPSIS
  Exchange Online mailbox forwarding/autoreply helper used by FlowOps.
.DESCRIPTION
  Requires ExchangeOnlineManagement. Emits one JSON object on stdout so the
  Python adapter can parse it. Never logs credentials.
#>
param(
  [Parameter(Mandatory=$true)][string]$UserPrincipalName,
  [string]$ForwardTo = "",
  [string]$AutoReply = ""
)
$ErrorActionPreference = "Stop"
Import-Module ExchangeOnlineManagement -ErrorAction Stop

$result = [ordered]@{
  userPrincipalName = $UserPrincipalName
  forwardingEnabled = $false
  autoReplyEnabled = $false
  mailboxGuid = $null
}
$mailbox = Get-Mailbox -Identity $UserPrincipalName -ErrorAction Stop
$result.mailboxGuid = $mailbox.Guid.Guid
if ($ForwardTo) {
  Set-Mailbox -Identity $UserPrincipalName -ForwardingSmtpAddress "smtp:$ForwardTo" -DeliverToMailboxAndForward $true
  $result.forwardingEnabled = $true
}
if ($AutoReply) {
  Set-MailboxAutoReplyConfiguration -Identity $UserPrincipalName -AutoReplyState Enabled -InternalMessage $AutoReply -ExternalMessage $AutoReply
  $result.autoReplyEnabled = $true
}
$result | ConvertTo-Json -Compress

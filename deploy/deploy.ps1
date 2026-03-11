# Deploy place_45 updates to EC2 and restart the bot.
# Usage: .\deploy\deploy.ps1
#   Optional overrides:
#     -Host "ubuntu@other-host"
#     -KeyPath "C:\path\to\polybot.pem"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$DefaultHost = "ubuntu@ec2-54-171-136-147.eu-west-1.compute.amazonaws.com"
$DefaultRepoPath = "/home/ubuntu/poly-autobetting"
$DefaultService = "poly-autobetting"

param(
    [string]$Host = $(if ($env:EC2_HOST) { $env:EC2_HOST } else { $DefaultHost }),
    [string]$KeyPath = $(if ($env:EC2_KEY_PATH) { $env:EC2_KEY_PATH } else { (Join-Path $RepoRoot "polybot.pem") }),
    [string]$RepoPath = $DefaultRepoPath,
    [string]$Service = $DefaultService
)

if (-not $Host) {
    Write-Host "Usage: .\deploy\deploy.ps1 [-Host 'ubuntu@your-ec2-host'] [-KeyPath 'C:\path\to\polybot.pem']"
    exit 1
}

if (-not (Test-Path $KeyPath)) {
    Write-Host "SSH key not found: $KeyPath"
    exit 1
}

Write-Host "Deploying to $Host..."
ssh -i $KeyPath -o StrictHostKeyChecking=accept-new $Host "cd $RepoPath && git checkout stable-v1 && git pull --ff-only origin stable-v1 && sudo systemctl restart $Service && sudo systemctl status $Service --no-pager"
Write-Host "Deploy complete. Bot restarted."

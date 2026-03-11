# Deploy place_45 updates to EC2 and restart the bot.
# Usage: .\deploy\deploy.ps1 -Host "ubuntu@ec2-xx-xx-xx-xx.compute.amazonaws.com"
#   Or set $env:EC2_HOST = "ubuntu@your-ec2-host" then run without -Host

param(
    [string]$Host = $env:EC2_HOST
)

if (-not $Host) {
    Write-Host "Usage: .\deploy\deploy.ps1 -Host 'ubuntu@your-ec2-host'"
    Write-Host "   Or: `$env:EC2_HOST = 'ubuntu@your-ec2-host'; .\deploy\deploy.ps1"
    exit 1
}

Write-Host "Deploying to $Host..."
ssh $Host "cd /home/ubuntu/poly-autobetting && git pull origin stable-v1 && sudo systemctl restart poly-autobetting && sudo systemctl status poly-autobetting"
Write-Host "Deploy complete. Bot restarted."

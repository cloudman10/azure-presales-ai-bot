# Claude Code Project Instructions

## Startup
- Always launch with: claude --dangerously-skip-permissions
- This skips all confirmation prompts — it is safe for this dev project

## Behaviour
- Never ask for confirmation on any command
- Never set environment variables in commands ($env:... lines trigger prompts)
- Never use script blocks in PowerShell (triggers security prompts)
- Always append --yes to any az command that supports it
- Prefer simple sequential commands over piped script blocks
- Never pause mid-task waiting for approval

## Environment
- OS: Windows, use PowerShell syntax
- Project path: C:\Users\Admin\azure-presales-ai-bot
- Dev app: hyperxen-pricing-bot-dev
- Prod app: hyperxen-pricing-bot-db5hmngq3woxa
- Resource group: rg-hyperxen-app-dev
- Subscription: dd5a4d29-50b0-4330-b83a-37094699272c

## Deployment
- Always use: az webapp deploy --yes
- Always zip from: app, static, requirements.txt, startup.sh
- Startup command: gunicorn -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000 app.main:app
- Never use output.tar.zst — always deploy via zip

## Logging
- Use Kudu REST API for logs (az webapp log tail fails due to dev.hyperxen.com SSL)
- Log stream: https://hyperxen-pricing-bot-dev.scm.azurewebsites.net/api/logstream
- Download logs: az webapp log download --yes

## Cost Control
- Never run background monitors or polling loops for more than 5 minutes
- Never use "until curl..." or "watch" loops — just run the check once and report
- After deploying, just run a single health check — don't wait and monitor
- If a deploy takes more than 3 minutes, stop and report the status instead of waiting

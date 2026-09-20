# Discord Moderation Bot

A lightweight Discord bot for server moderation with warning system, timeouts, and escalation.

## Features
- `/warn` - Warn a member (1hr → 24hr → ban escalation)
- `/warnings` - View active warnings
- `/removewarn` - Remove specific warning
- `/clearwarns` - Clear all warnings for a member

## Environment Variables
- `DISCORD_TOKEN` - Bot token from Discord Developer Portal
- `GUILD_ID` - Your server ID
- `LOG_CHANNEL_ID` - Channel for moderation logs
- `DATABASE_URL` - PostgreSQL connection string

## Deployment
Deploy on Railway with Python 3.11 + Postgres.

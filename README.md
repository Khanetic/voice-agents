# VoiceAgents

Multi-tenant AI voice agent SaaS (receptionist, auto repair, restaurant, healthcare).
Stack: Next.js · FastAPI · Supabase · Claude on Amazon Bedrock · ElevenLabs · Terraform (AWS eu-central-1).

Full build guide: `docs/AI_Voice_Agents_Build_and_Deployment_Guide.pdf`.

## Local dev ports
| Service | Port |
|---|---|
| FastAPI | 8010 |
| Next.js | 3010 |
| Supabase API / DB / Studio | 54321 / 54322 / 54323 |

## Quick start
```bash
supabase start                      # local Postgres + Auth (Docker)
cd backend && source .venv/bin/activate
uvicorn app.main:app --reload --port 8010
```

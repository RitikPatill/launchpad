# launchpad

Fourth orchestrator in the family — alongside [autodev](https://github.com/RitikPatill/autodev)
and [agent-radar](https://github.com/RitikPatill/agent-radar).

**Different mission.** autodev and agent-radar create new repos.
**launchpad amplifies them on YouTube.** Watches the sibling SQLite DBs
read-only; when a project transitions to `done`, automatically:

1. Writes a 60-90s voice-over script (Sonnet)
2. Records a Playwright screen-walkthrough of the repo / live demo
3. Generates AI voice-over via free Microsoft Edge TTS
4. Muxes video + audio with bundled ffmpeg
5. Generates SEO-tuned title + description + tags (Sonnet)
6. Uploads to YouTube via Data API v3 with mandatory AI-content disclosure

> Status: working MVP. Total recurring cost: $0/month. Free open-source
> TTS in 2026 closed the quality gap with paid services. AI-content
> disclosure is enforced on every upload — required by YouTube's 2026
> policy and protects against bans.

## Stack

| Component       | Tool                          | Cost    |
|-----------------|-------------------------------|---------|
| Screen recording| Playwright (Chromium)         | free    |
| TTS voice       | Microsoft Edge TTS (`edge-tts`)| free    |
| Audio + video   | `imageio-ffmpeg` (bundles ff) | free    |
| YouTube upload  | YouTube Data API v3           | free    |
| Script writing  | Sonnet 4.6 via `claude -p`    | Max plan|
| Metadata        | Sonnet 4.6 via `claude -p`    | Max plan|

## Pacing

- Max **3 uploads per week**, min **12-hour gap** between uploads
- Upload windows: weekday 18-23, weekend 10-22
- AI-content disclosure flag set on every upload (YouTube 2026 requirement)

## Setup (one-time, ~5 min)

```bash
cd launchpad
pip install -r requirements.txt
playwright install chromium     # downloads the headless browser
```

Then YouTube OAuth (one click in browser):

1. Create a Google Cloud project at https://console.cloud.google.com
2. APIs & Services → Library → enable **YouTube Data API v3**
3. Credentials → Create OAuth client ID → Application type **Desktop app**
4. Download the JSON, save as `%LOCALAPPDATA%\launchpad\creds\client_secret.json`
5. Run:
   ```bash
   python orchestrator.py auth
   ```
   Browser opens → you click **Allow** → token saved.
6. `python orchestrator.py preflight` — should print "authenticated"
7. `install_autostart.ps1` — registers Task Scheduler entry

## Operations

```bash
python orchestrator.py status     # video pipeline state
python orchestrator.py health     # full check
python orchestrator.py scan       # poll sibling DBs for new completed projects
python orchestrator.py tick       # advance pipeline one step
python orchestrator.py run        # loop forever
```

## License

MIT — see [LICENSE](LICENSE).

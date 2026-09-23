# Race Results

Live results for [Webscorer](https://www.webscorer.com) races, with medal colours, result texts (Twilio) and a public results page uploaded to your website by FTP.

It runs on a Raspberry Pi, or any Debian or Ubuntu computer. You use it from a web browser on the same network.

## What it does

- **Results tab.**
  - Shows a live feed of finishers, newest first, as `terminal_results.py` did.
  - You can search by bib or name.
  - You can edit a result (name, distance, category, gender, time, phone). Edits are kept for the event and re-applied every time Webscorer updates, and medal colours are recalculated straight away.
  - It shows the log of texts sent and an activity log, and results can be exported as CSV.
- **Events.** Each event is a Webscorer race ID, plus its medal configuration and whether medal colours are on, its information text, an organiser logo and its own results page file name (e.g. `landsend2026.html`). Events are saved with their results, so you can reopen them later, even offline.
- **Medal setup tab.** Import medal CSVs as named configurations, then edit, duplicate, export and reuse them for future events. A warning lists any distance/category/gender in the event that has no medal times.
- **Configuration tab.** Webscorer, Twilio, FTP, logos and an optional page password. These settings are shared by every event.
- **Top bar.**
  - The SMS switch, which **always starts OFF**.
  - CPU, memory, storage, network and temperature meters, and a count of texts sent.
  - Webscorer and FTP status.
  - A Shutdown button that safely powers the computer off.
- **Public results page.**
  - Sections for each distance (overall, by gender, and by category), much like Webscorer's layout.
  - It shows **only** name, distance, gender, category and time, plus the medal colour when the event uses medal colours. Webscorer's adjusted time is used when there is one. Every section starts collapsed.
  - Each event is uploaded as its own file into one results folder on the site, so past events stay online. A page is uploaded only when something has changed.

## Install

On the Raspberry Pi (or other Debian/Ubuntu computer):

```bash
sudo apt install -y git
git clone https://github.com/YOUR-ACCOUNT/YOUR-REPO.git race-results
cd race-results
./install.sh
```

The installer does the following, and asks for your password where it needs it:
1. Installs the Python packages into `.venv`.
2. Creates the `data/` folder.
3. Sets the app to start automatically at boot.
4. Allows the Shutdown button to power the computer off. The sudo rule it adds permits only `systemctl poweroff`.

When it finishes, it prints the address to open, for example `http://raspberrypi.local:8080`.

| Option | |
|---|---|
| `./install.sh --port 8090` | use a different port |
| `./install.sh --no-service` | only set up Python; run it yourself with `./run.sh` |
| `./install.sh --uninstall` | remove the service and the power-off rule (keeps `data/`) |

**Updating:** `git pull && ./install.sh`. The installer first backs up `data/raceresults.db` into `data/backups/`, and the app upgrades its database automatically when it starts.

## First-time setup

1. **Configuration tab.**
   - Enter the Webscorer API ID and token, the Twilio details, and the FTP details.
   - Add the timing company name and logo.
   - If you have an old `Config.csv`, use **Import settings from the old Config.csv**. Its MedalColours and InfoMessage values are applied to the current event.
2. **Medal setup tab.** Choose **Import CSV** for each medal file and give it a name.
3. **Results tab.**
   - Choose **New event** and enter the Webscorer race ID.
   - Choose **Look up**, which fills in the name and date.
   - Pick the medal configuration and tick **Show medal colours**; add the information text if you want one, and the organiser's logo.
   - Check the **Results page file name** (suggested from the name, e.g. `landsend2026.html`). It becomes the page's web address.
4. **Configuration › Results website.** Choose **Test connection**, then **Preview page**, then **Upload now**.

## Race-day checklist

- [ ] The right event is selected and the **Live Update** switch at the top is on.
- [ ] There's no yellow "no medal standard" warning. If there is, choose **Open medal setup** and then **Add missing combinations**.
- [ ] The FTP status dot is green. Open the live page on your phone to check it.
- [ ] Turn the **SMS** switch on when you're ready. The pop-up says how many texts will go straight away.
- [ ] At the end, choose **Shutdown**. Wait about 30 seconds, until the green light stops flashing, then unplug.

## How repeat texts are prevented

- Each text is recorded **before** it is sent. If the power fails mid-send, that text is never sent twice.
- The same message is never sent twice to the same person for an event.
- A corrected result (a new time, category or medal) sends one corrected text.
- A queued text that has been superseded before it went out is dropped.
- Numbers are cleaned up before sending:
  - `07…`, `+44 (0)…` and `0044…` are all accepted.
  - A spreadsheet's `7400 123456` or `7400123456.0`, with the leading zero missing, is fixed.
  - Invalid numbers and landlines are logged once and not retried. Correct the number with **Edit** and the text goes out.
- A text is retried automatically only when Twilio certainly never received it (no connection). If Twilio's answer was lost, it is marked failed and gets a **Resend** button, so you can check the Twilio console first.
- Switching SMS off cancels queued texts. The app always starts with SMS off, and switching event turns it off.
- The information message goes once per finisher, after their first result text.

## Medal CSV format

```csv
Distance,Category,Gender,Gold,Silver,Bronze,Finisher
Sample Sportive (Long),Senior,Open,5:00:00,6:00:00,7:30:00,24:00:00
```

- Times are `H:MM:SS`. A finisher gets the first level whose time they are within.
- A blank time means that level isn't awarded.
- Matching with Webscorer's distance, category and gender ignores capitals and extra spaces.
- Webscorer's `Female/Male` gender is shown as `Open`.
- See `examples/medals_example.csv`.

## Keeping secrets out of GitHub

All credentials (Webscorer token, Twilio token, FTP password) and all race data are stored in `data/`. The `.gitignore` excludes that folder, and also `Config.csv`, `SMS.csv` and every CSV outside `examples/`. The installer also turns on a git pre-commit check that refuses commits containing those files or anything that looks like a Twilio or Webscorer credential.

To publish this project to a new GitHub repository (a **private** repository is recommended):

```bash
cd race-results
git init -b main
git config core.hooksPath .githooks
git add .
git status --ignored     # data/, Config.csv etc. must be under "Ignored files"
git commit -m "Race Results web app"
git remote add origin https://github.com/YOUR-ACCOUNT/YOUR-REPO.git
git push -u origin main
```

## Looking after it

| | |
|---|---|
| Status | `systemctl status raceresults` |
| Live log | `journalctl -u raceresults -f` (also saved in `data/logs/app.log`) |
| Restart | `sudo systemctl restart raceresults` |
| Backup | copy the `data/` folder. It holds everything: settings, events, edits, texts and logos. |
| Move to another computer | install there, stop the service, copy `data/` across, then start it again |

A Raspberry Pi 3 has plenty of capacity for the app, which uses about 40 MB of memory. Avoid running a desktop browser or VS Code on the Pi itself during a race, though, because they use most of its 1 GB.

## Legacy scripts

On the original computer, `legacy/` holds the earlier `terminal_results.py`, `race_processing.py` and `search_results.py`, unchanged. The whole folder is git-ignored, because those scripts contain account details.

## Development

```bash
.venv/bin/python -m unittest discover -s tests -t .      # tests (no network needed)
./run.sh --port 8081 --data /tmp/rr-test                 # a second copy with its own data
```

Environment variables:
- `RACERESULTS_PORT`, `RACERESULTS_HOST` and `RACERESULTS_DATA` set the port, host and data folder.
- For testing, `RACERESULTS_WEBSCORER_URL` and `RACERESULTS_TWILIO_URL` point the app at stand-in servers.
- `RACERESULTS_FAKE_POWEROFF=1` makes the Shutdown button stop the app without powering the computer off.

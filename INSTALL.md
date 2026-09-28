# Installing meetcap

Allow about 15 minutes, most of it waiting for downloads. You need a Mac on macOS 14 or newer and an admin password for Homebrew.

## 1. Install Homebrew

Skip this step if `brew --version` already prints a version. Otherwise open Terminal and run:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

When it finishes, run the two `echo … >> ~/.zprofile` lines it prints, then close and reopen Terminal.

## 2. Get meetcap and run the installer

```bash
git clone https://github.com/VamsiChapa/meetcap.git
cd meetcap
./install.sh
```

The installer:

- installs Python 3.12, ffmpeg and OBS with Homebrew (OBS may ask for your password)
- puts meetcap in its own Python environment in `~/Library/Application Support/meetcap`
- builds `micwatch`, a small helper that tells meetcap when a meeting app is using the microphone
- adds meetcap to your login items, so it starts when you log in and restarts if it crashes
- opens the setup screen in your browser

You can re-run it at any time. It keeps your settings.

## 3. The setup screen

The same screen opens whenever you run `meetcap setup`, or pick **Settings…** in the menu bar. Work down the page.

### Where to keep your meetings

Leave it on **A folder on this Mac** unless you have a reason not to. The default is `~/MeetCap`.

- To back meetings up, click **Choose…** and pick a folder inside OneDrive, Google Drive, Dropbox or iCloud Drive.
- To use **Azure Blob Storage**, paste a connection string (portal → storage account → Access keys) or a container SAS URL with Read, Add, Create, Write and List.

### Transcripts

- **On this Mac (Whisper)** is free and private. It needs Apple Silicon, and the first meeting downloads a 1.6 GB model.
- **Groq** is the easiest cloud choice. The free tier covers about 8 hours of audio a day. Get a key at [console.groq.com/keys](https://console.groq.com/keys), paste it in, and click **Save and test**.

### Notes

Pick **Off** if you only want transcripts. Otherwise:

- **Ollama** is free and runs on your Mac. Install [Ollama](https://ollama.com/download), then run `ollama pull llama3.2`.
- **Groq** or **OpenRouter** are free with a key. Don't use free models for confidential meetings.
- **Claude**, **OpenAI** or **Azure OpenAI** are pay as you go, and they also read the screenshots.

Click **Save and test** to check the key works.

### Recording

Leave **Record meetings automatically** on. If you only want audio, untick **Record the screen too**. Adding your first name lets the notes tell which action items are yours.

### OBS

Click **Set up OBS**. OBS opens and closes a few times while meetcap configures it, and macOS asks for permissions:

| Prompt | Click |
|---|---|
| "OBS" would like to access the microphone | **Allow** |
| "OBS" would like to record this computer's screen and audio | **Open System Settings**, switch **OBS** on, then **Quit & Reopen** |
| "python3.12" wants access to control "OBS" | **OK** (meetcap closes OBS after each meeting) |

It's done when the log ends with **OBS sees your screen and hears your microphone**.

### Desktop pet

Switch on **Show a pet on my screen** and pick one. You can change it later with `meetcap pet list`.

### Finish

Click **Save and finish**. meetcap restarts with your settings and your notes page opens. Bookmark it.

## 4. Try it

1. Double-click the pet (or menu bar → **Start recording now**).
2. Talk, or play a video with sound, for about two minutes. Put a slide on screen.
3. Double-click the pet again.

Within a few minutes a **Meeting notes ready** notification appears and the notes page opens. To follow along, run `meetcap jobs`.

For a real call, join any Teams, Zoom or Webex meeting. Recording starts about 5 seconds after the app takes the microphone. It stops about 20 seconds after the call ends. Calls shorter than 90 seconds are thrown away on purpose.

## Upgrading

```bash
cd meetcap
git pull
./install.sh
```

Settings from older versions carry over. If you used Azure storage and Azure OpenAI before, that's what stays selected.

## Other prompts you may see

| You see | What it means |
|---|---|
| Keychain: "meetcap wants to use your confidential information" | meetcap is reading a key you saved. Click **Always Allow** |
| No notifications | System Settings → Notifications → **Script Editor** → allow |
| macOS asks again about OBS screen recording after a few weeks | Normal on recent macOS. Click **Allow** |

## Troubleshooting

| Problem | Try |
|---|---|
| Can't find meetcap in the menu bar | A full menu bar hides newer icons behind the notch. `meetcap status`, `meetcap start` and `meetcap stop` work from Terminal |
| A call isn't recorded | Run `meetcap mics` during the call. For calls in a browser, add it under `detection.browsers` in the config (`meetcap config`), or record by hand |
| Silent recording or black screen | Allow OBS under both Microphone and Screen & System Audio Recording in System Settings → Privacy & Security, quit OBS (⌘Q), then **Set up OBS** again |
| OBS crashes during setup | `meetcap obs-log` names the cause of each crash. Setup retries on its own when the crash is inside OBS |
| A meeting has no notes | `meetcap jobs` shows the step it stopped at; `meetcap doctor --deep` tests storage, transcription and notes |
| The notes page won't open | `meetcap reader` writes it again and opens it |

## For advanced setups

Everything on the setup screen is in `~/Library/Application Support/meetcap/config.yaml`, along with a few things that aren't: which apps count as meetings, the frame rate, retry limits. Open it with `meetcap config` and run `meetcap restart` after you save.

Keys can go in from Terminal too: `meetcap secret set groq-key`, and `meetcap secret list` shows which ones are set.

If you prefer Terminal for Azure, `meetcap azure-setup --existing` takes keys you already have, and `--create` makes new resources with the Azure CLI (`brew install azure-cli`). For a team, `infra/terraform` creates the storage account plus the Speech and OpenAI resources.

To uninstall, run `./uninstall.sh`. It keeps your settings and meetings; `--purge` removes settings and keys as well.

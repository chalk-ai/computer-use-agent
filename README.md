# Computer use agent on Chalk

Give Claude a desktop and a task, and get back a document. This example runs a GNOME desktop with
Apache OpenOffice inside a Chalk sandbox, and lets Claude operate it through the
[computer toolset](https://docs.claude.com/en/docs/agents-and-tools/tool-use/computer-use-tool):
it reads screenshots, clicks and types through menus and dialogs, and saves a spreadsheet, memo
or invoice. A web page streams the screen live, logs each action with the model's notes, and
offers the finished file for download.

How it fits together:

1. **A desktop in a sandbox.** Each run gets a gVisor sandbox (`KernelPolicy.RESTRICTED`) running
   Xvfb at 1024×768, a GNOME Flashback session (metacity and gnome-panel), and Apache OpenOffice
   4.1.16. It boots in about six seconds; the server keeps a warm pool so runs start sooner.
2. **No network, no secrets.** The desktop sandbox has an empty egress allowlist (no DNS, no
   egress) and holds no credentials. Nothing inside it talks to the model.
3. **The agent loop runs in the server.** `agent.py` calls the Messages API with
   `{"type": "computer_toolset_20260801"}` through the Chalk model gateway, and turns every
   action the model asks for (`left_click`, `type`, `key`, `scroll`, `left_click_drag`, `zoom`,
   and the rest) into an `xdotool` command run in the sandbox with `sandbox.exec`.
4. **A live, view-only screen.** One long `exec_stream` in the sandbox emits JPEG frames when
   the screen changes; the page polls the newest one. Visitors have no input path into the
   desktop.
5. **The document.** When the agent is done, the server pulls the newest file out of
   `/root/Documents`, converts it to PDF through OpenOffice's UNO API (`desktop/docconv.py`), and
   renders page one as a preview. Then the sandbox is terminated.

## Files

| File | Role |
| --- | --- |
| `desktop_sandbox.py` | The desktop image and sandbox: packages, OpenOffice, network and kernel policy, boot. |
| `desktop/start-desktop.sh` | Starts Xvfb, the GNOME Flashback session and OpenOffice inside the sandbox. |
| `desktop/deskctl.py` | Screenshots and the frame stream, run inside the sandbox. |
| `desktop/docconv.py` | Converts the saved document to PDF with OpenOffice's bundled Python and UNO. |
| `agent.py` | The agent loop and the computer-toolset → `xdotool` action layer. |
| `server.py` | FastAPI app: runs, the warm pool, SSE events, frames, documents, recordings and limits. |
| `index.html` | The demo page. `?embed=1` hides its header for embedding in an iframe. |
| `run_cli.py` | Starts a run on a deployed server from a terminal and saves the document. |
| `deploy.py` | Deploys the server as a `chalkcompute.ScalingGroup`. |

## Run it

You need [`uv`](https://docs.astral.sh/uv/) and a Chalk environment with the model gateway
enabled (`chalk router status`). Credentials come from the SDK’s ambient credentials from `chalk login`.

```bash
chalk login

# One time: a gateway key for the server. Paste the key `chalk router api-key create` prints.
chalk secrets set CHALK_ROUTER_API_KEY

# Serve the demo from a scaling group, and open the printed URL...
./deploy.py

# ...or run a task against it from a terminal; the document lands in ./out.
./run_cli.py https://<web_url> --prompt "A packing list spreadsheet with a total weight row"
```

A run takes one and a half to two minutes, mostly 30 to 60 model turns with Claude Opus 5.5 at
medium effort. Change the model with `./deploy.py --model` and the effort with `--effort`.

## Limits

- **Desktop sandboxes:**
  - gVisor, no network, no Chalk identity, no credentials
  - a hard `lifetime`, and terminated when the run ends
- **Visitors** see the screen but cannot type into it. A custom task is at most 300 characters.
- **Agent:** at most 45 model turns (`CU_MAX_TURNS`) and 420 seconds (`CU_RUN_TIMEOUT`) per run.
- **Server** (`server.py`):
  - two concurrent live runs (`CU_MAX_CONCURRENT`)
  - 20 live runs per hour (`CU_MAX_PER_HOUR`), and 3 per visitor (`CU_MAX_PER_CLIENT_PER_HOUR`)
  - When a preset task can't run live, the page plays back a recorded run of the same task from
    the Chalk volume `computer-use-agent-recordings`.
- **Server credentials:** the server creates sandboxes with its own workload identity
  (`chalk_identity=True`), so no client secret is in the image.

## Known quirk

Apache OpenOffice 4.1.16 under Xvfb often shows "Error saving the document …: General Error"
after a successful save. The file is written correctly; the system prompt tells the agent to
dismiss the dialog and carry on.

## License

Apache-2.0. See [LICENSE](LICENSE).

## Local authentication

The SDK reads credentials, API host, and the selected environment from your existing
`chalk login` in `~/.chalk.yml`, including project-scoped and default logins.
For unattended runs, `CHALK_CLIENT_ID` and `CHALK_CLIENT_SECRET` remain supported. Where available, `--env-id`
overrides the target environment; otherwise the SDK uses your login or environment settings.

# Quick start: run the Marketing AI demo on your laptop

This guide is for anyone who wants to try the product, not for developers. You copy a few commands
into a terminal, wait for the demo to load, and open it in your browser. Nothing here needs an AWS
account, a real AI service or any client data: the demo company, **Demo Telecom**, is synthetic.

Allow about 20 minutes the first time, most of it spent waiting for downloads.

---

## 1. What you need

| You need | Why | How to check |
|---|---|---|
| **macOS 12 or later, or Linux** (Ubuntu 22.04 or later). On **Windows**, use WSL 2 with Ubuntu and follow the Linux steps inside it. | The setup commands use `make` and a Unix shell. | macOS: Apple menu → About This Mac. Linux: `lsb_release -a` |
| **Python 3.11**. Not 3.10, 3.12 or 3.13: the machine-learning library only supports 3.11. | Runs the product. | `python3.11 --version` should print `Python 3.11.x` |
| **git** and **make** | Download the code and run the setup commands. | `git --version` and `make --version` each print a version |
| **4 GB free disk** | The installed libraries take about 1.8 GB and the demo data about 100 MB. | macOS/Linux: `df -h ~` (look at "Avail") |
| **8 GB RAM** (4 GB free while it runs) | Training a model and your browser at the same time. | macOS: Apple menu → About This Mac. Linux: `free -g` |
| **An internet connection** during setup | To download the libraries (about 1 GB). The demo itself runs offline. | — |
| A modern browser | Chrome, Edge, Firefox or Safari. | — |

**Node.js is not needed** to run the demo (only developers running the test suites need it).

**Missing Python 3.11, git or make?**

- **macOS:** install the Apple command-line tools (`xcode-select --install`), then Homebrew
  (<https://brew.sh>), then `brew install python@3.11`.
- **Ubuntu:** `sudo apt update && sudo apt install -y git make python3.11 python3.11-venv`.
  If `python3.11` is not found, first run
  `sudo add-apt-repository -y ppa:deadsnakes/ppa && sudo apt update`.

---

## 2. Install and start the demo

Open a terminal (macOS: Applications → Utilities → Terminal) and paste these lines one block at a
time.

**Download the code** (once):

```bash
git clone --branch main https://github.com/Sanyamkothari/Marketing.ai-.git
cd Marketing.ai-
```

(`--branch main` matters: it fetches the finished product, whatever the repository's default branch
is set to.)

**Install** (once, about 2 to 15 minutes depending on your internet):

```bash
make setup
```

It ends with a line like `setup ok: autogluon.tabular 1.6.3`. If it stops with an error, see
[Troubleshooting](#5-troubleshooting).

**Prepare the demo data** (once, about 2 to 4 minutes; it really trains the demo's models):

```bash
make demo-seed
```

It ends with `Demo Telecom seeded in …`.

**Start the demo** (every time you want to use it):

```bash
make demo
```

When the terminal shows `Uvicorn running on http://127.0.0.1:8000`, open
**<http://localhost:8000>** in your browser.

**To stop it,** click in the terminal window and press **Ctrl + C**. Your demo data stays; the next
`make demo` starts in seconds.

**To start again from a fresh demo,** stop it, then run `rm -rf data && make demo-seed && make demo`.

---

## 3. Signing in

With `make demo`, **sign-in is off**: you are the "local operator" with every role, so you can see
every screen. This is the quickest way to look around.

To show what each role may do (for example, that the person who trained a model cannot approve
it), start the demo with sign-in on instead:

```bash
make demo-signin
```

and sign in as one of these demo users. They exist only in your local demo.

| Username | Password | Role | What they can do |
|---|---|---|---|
| `demo-viewer` | `demo-viewer-2026` | Viewer | Look at everything; change nothing |
| `demo-analyst` | `demo-analyst-2026` | Analyst | Build data, train and score models, measure campaigns |
| `demo-approver` | `demo-approver-2026` | Approver | Approve or reject a model someone else trained |
| `demo-admin` | `demo-admin-2026` | Admin | Users, audit log, privacy requests |
| `demo-lead` | `demo-lead-2026` | Analyst + Approver | Trains models, but can never approve their own |

---

## 4. Try this first: a 10-step tour

The demo opens with a short guided tour. You can take it again any time with **Take the tour**.
These ten steps show the whole product in about fifteen minutes.

1. **Home.** The use cases for a telecom business, laid out along the customer lifecycle. The top
   bar has the six areas of the product and the client you are working on (**Demo Telecom**).
2. **Open Telco Customer Churn.** This is the demo's main use case: which subscribers are likely to
   leave in the next 60 days.
3. **See the trained model.** Open its latest training run's **Model** page: how good the model
   is, compared with a simple yardstick, and which customer facts matter most.
4. **See who to contact.** Open the **Output** of the scoring run: every customer gets a risk band,
   an action and a reason in plain words. **Download the contact list** as a CSV.
5. **See whether the campaign worked.** From the Output, open **Campaign results**: contacted
   customers compared with a randomly held-back control group.
6. **See the value in rupees.** Under **Reports**, open the churn campaign's **value view**. The
   value is always a range, because the measurement has one.
7. **Build a dataset from raw tables.** Under **Build data**, see what a client is asked to send,
   and open the **data readiness report**. The demo also holds a broken extract with one planted
   problem (duplicate customer IDs); its readiness report shows how a problem is explained.
8. **Uplift: who to contact, and who to leave alone.** Open **Win-back Campaign → Uplift for this
   use case**. It sorts customers into persuadables, sure things, lost causes and sleeping dogs,
   and recommends contacting only the persuadables.
9. **Approvals.** Under **Models**, open **Waiting for approval**. A new model only replaces the
   current one after an Approver agrees; with `make demo-signin`, sign in as `demo-lead`, train a
   model, and see that you cannot approve it yourself.
10. **Ask "What does this mean?"** Wherever you see a **?** beside a warning or a setting, click it
    for a one-line plain-language explanation. The **Feedback** button on every screen records
    what you think.

**What the demo does not include:** the AI-written features (the onboarding assistant, root-cause
notes and campaign copy) need a connection to an AI service and show a "Needs AI service
connection" notice instead. The demo sends no messages to anyone and holds no real client data.

---

## 5. Troubleshooting

**1. "python3.11: command not found", or setup says the Python version is wrong.**
The product needs exactly Python 3.11. Install it (see [section 1](#1-what-you-need)), then remove
the half-made install and try again: `rm -rf .venv && make setup`. If you have several Pythons,
you can name the right one: `make setup PY=/path/to/python3.11`.

**2. "Address already in use" when you run `make demo`.**
Something else (often an earlier demo you did not stop) is using port 8000. Find the terminal
with the old demo and press Ctrl + C there, or find and stop the process:
`lsof -i :8000` shows it, then `kill <the PID number>`. To run on another port instead:
`MARKETING_AI_DEMO_MODE=true .venv/bin/uvicorn api.main:app --port 8001` and open
<http://localhost:8001>.

**3. The first `make setup` seems stuck.**
It downloads about 1 GB of libraries. On a slow connection this can take 15 minutes or more with
little output. Leave it running. If it fails with a network error, run `make setup` again: it
continues from where it stopped.

**4. Setup stops while installing AutoGluon** (the machine-learning library).
- Check the Python version first (problem 1): AutoGluon installs only on Python 3.11 here.
- **Apple Silicon Macs (M1 to M4):** run `brew install libomp`, then `rm -rf .venv && make setup`.
- **Linux:** make sure `python3.11-venv` is installed, and that you have at least 4 GB of free disk.
- Setup stops on purpose rather than install a different library, so a failure here is never
  silent.

**5. The page is blank, looks broken, or shows an old version.**
Your browser is showing a cached copy. Do a hard refresh: **Ctrl + Shift + R** (Windows, Linux) or
**Cmd + Shift + R** (macOS). If that does not help, open the page in a private window. Check that
the terminal still says `Uvicorn running`; if you closed it, run `make demo` again.

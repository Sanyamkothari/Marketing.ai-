# Recruiting for the usability sessions

Part of the session pack for [`USABILITY_TEST.md`](../USABILITY_TEST.md) (Plan M, M183, DEC-1613). That kit says who may
take part (its section 2); this page is the message to send, the questions that check it, and what to say about time and
thanks. Text in `[square brackets]` is for the product owner to fill in before anything is sent. Nothing here names a real
person or a real incentive.

## 1. The message

Use one message for e-mail and a shorter one for LinkedIn. Send them from a named person at the company, not from a no-reply
address, so that people can ask a question first.

### 1.1 E-mail

**Subject:** 45 minutes of your view on a campaign tool? [Thank-you: `[incentive]`]

> Hello `[first name]`,
>
> I am `[name]` at `[organisation]`. We are building a tool that helps marketing teams plan a customer campaign and see what
> the campaign earned. Before we release it, we would like to watch a few people who run customer campaigns try it for the
> first time.
>
> What it involves:
>
> - **About 45 minutes**, once, on a video call or in person at `[place]`. Most sessions take 30 to 40 minutes.
> - You are given two short tasks on cards and use the tool the way you would at work. We are testing the tool, not you:
>   nothing you do can be wrong, and where something is hard to find, that tells us what to fix.
> - We will not show you anything beforehand and we cannot give hints during the tasks, so you will be on your own with the
>   screens. That is the point.
> - With your separate agreement, we record your screen, and your voice if you are happy with that, so that our notes are
>   accurate. You can say no to the recording and still take part. You can stop at any time.
> - As a thank-you: `[incentive]`.
>
> I need to ask a few short questions first to check that the session fits you (they take two minutes, below). If you are
> interested, reply with your answers and a few times that suit you.
>
> Thank you for considering it.
>
> `[name, role, phone or e-mail]`

### 1.2 LinkedIn (short)

> Hello `[first name]`, I am `[name]` at `[organisation]`. We are looking for CRM and retention managers to try a new campaign
> planning tool for about 45 minutes (video call or in person) and tell us where it is confusing. There is `[incentive]` as a
> thank-you. If you have run customer campaigns in the past year, may I send you three short questions to see if the session
> fits?

Do not describe the product beyond this. Do not send a link, a demo video or a screenshot: a person who has seen the
screens cannot be a first-time user (kit section 2).

## 2. Screener questions

Ask by reply, form or phone, before booking. Keep the answers with the participant's id (P1 to P5), not their name, once
they are booked.

| # | Question | Who qualifies |
|---|---|---|
| 1 | What is your job title, and which team are you in? | CRM, retention, lifecycle or growth marketing manager, or a role in a team that plans customer campaigns |
| 2 | How many years have you worked in marketing, and how many of those on customer campaigns (e-mail, push, WhatsApp, SMS)? | Any; write it down. Some experience of running campaigns is needed, so a person with none is not a fit |
| 3 | Which campaign or marketing tools have you used in the past year (for example CleverTap, MoEngage, Netcore, Braze, WebEngage, an e-mail platform, a spreadsheet)? | At least one campaign tool used to run a campaign in the past year; list them |
| 4 | Have you seen Marketing AI before, in a demo, a video, screenshots or a presentation, or read about it? | **No.** If yes, **exclude**: thank them and do not book (they may be a better fit for a later round) |
| 5 | Do you, or does anyone close to you, work for `[organisation]` or help build or sell this product? | **No.** If yes, exclude (kit section 2) |
| 6 | Are you comfortable reading English on screen, and will you be on a computer (not a phone) for the session, with a screen at least 1440 pixels wide if remote? | Yes |

Outcomes: book only if 1, 3, 4, 5 and 6 pass and 2 is not "none". Mix the five people if you can (different employers,
different seniority; kit section 2). If a person turns out not to qualify once the session starts, run it as a practice
session and recruit another (kit section 2).

Screener answers are personal data. Keep them only as long as the recruitment lasts, and delete the answers of anyone not
booked when the five are chosen (see [`CONSENT.md`](CONSENT.md) for what is kept about booked participants).

## 3. Incentive

`[Incentive: type and value, for example a voucher or a payment of Rs. ____; the product owner decides. Confirm that the
person's employer allows them to accept it, and say how and when it will be given.]`

State the incentive in the first message and give it even if the person stops early or declines the recording. Pay it
because they came, not because of how they did.

## 4. Scheduling

- **Length:** plan 45 minutes per person (a three-minute opening, two tasks of at most 10 and 5 minutes, and a short
  closing, as in the kit's facilitator script). Most sessions take 30 to 40 minutes. Leave 15 minutes after each session
  for the observer's notes (kit section 10) before the next person.
- **Remote or in person:** either. For a remote session the participant needs a computer, a stable connection and a screen
  at least 1440 pixels wide; the facilitator serves the demo and shares access to it, or the participant works on the
  facilitator's computer by screen sharing. In person, use one computer for the participant, a second person or window for
  the observer, and a timer (kit section 3).
- **One person per slot,** with one practice session (a colleague, not counted) before the first participant.
- **Send** with the invitation: the time, how to join or where to go, the length, who they will meet, and
  [`CONSENT.md`](CONSENT.md) to read in advance. They give their agreement at the start of the session, before anything is
  recorded.
- **Reminders:** one the day before. If someone cannot come, thank them and offer one new time; do not chase further.
- Run the session with [`scripts/usability_session.py`](../../../scripts/usability_session.py) so that each person starts
  from an untouched copy of the demo store.

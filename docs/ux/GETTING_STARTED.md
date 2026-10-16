# Getting started with Marketing AI

Marketing AI helps you decide which customers to contact, with which offer and on which channel, and then shows
what the campaign really earned. It keeps a group of customers aside who are deliberately not contacted, so the
result is measured against them and not guessed.

This guide follows the campaign-centred layout: a sidebar of five places, a five-step New campaign screen and
one page for each campaign. The words on screen are the ones in the [glossary](GLOSSARY.md) and in the Help (?)
menu. If your screen looks different, your administrator has not turned this layout on yet (see the last section).

## Finding your way

A sidebar on the left has five places. Below 900 pixels wide it folds behind a **Menu** button.

| Place | What you do there |
|---|---|
| **Overview** | See what needs your attention, the value your campaigns have proven so far and your live campaigns. Start a campaign with **+ New campaign**. On your first visit a short **Getting started** list ticks itself off as you connect your data, start a campaign and add its results. |
| **Campaigns** | See every campaign with its goal, status, the size of its contact list and its result. Open one to download its contact list or add its results. |
| **Data** | See where your customer data comes from, whether each source is ready to use, and when it last brought in data. **Check my data** looks at your files before you use them. |
| **Reports** | Check a campaign another tool ran, and read your models' reports. |
| **Settings** | Your team and roles, the AI service, privacy and consent, costs, and the tools you use less often. |

At the top right:

- The **bell** shows what is waiting for approval. People with the Approver role also see a count.
- **Help (?)** opens a menu: **Take the tour**, **Glossary**, **Send feedback** and **What's new**.
- Your **profile** has your account, such as changing your password, and sign-out. **Show technical details**, here and in Settings, switches on
  the analyst's view with the extra pages and the technical tab on each campaign.

## Your first campaign

Choose **+ New campaign** on the Overview or the Campaigns page. One screen takes you through five steps, shown
along the top. You can leave and come back: your place is kept, and the campaign carries on in the meantime.
Starting a campaign needs the Analyst role; anyone else can look but not start.

1. **Goal.** Pick what the campaign is for, for example *Win back lapsed customers* or *Find customers likely to
   buy*. Each card carries a small label for the stage of the customer's life it belongs to. Goals that do not
   contact customers are folded under **More goals**; each opens its own page.
2. **Customers.** Choose the file of customers for this campaign, or a source from Data. Guided setup checks the file and
   suggests fixes; you approve them, and the same fixes are applied the same way next time.
   - The first campaign for a goal also needs a file that says what each customer did last time. Marketing AI
     learns from it, which takes a few minutes, then picks who to contact from the same customers.
   - Later campaigns for the same goal skip the learning and use the model already approved.
   - If what Marketing AI learned needs approval, a person with the Approver role approves it. If you are an
     Approver, you approve it on the Review step, in the same click as the campaign (unless you started the learning
     yourself: then another Approver approves it under **Waiting for approval**, the bell). Anyone else waits
     here, with a link to **Waiting for approval**; nothing can be sent until it is approved.
3. **Audience.** Audience and Review share one screen. **Who is contacted** shows how many customers are in each
   priority (High, Medium, Low), which offer each gets and on which channel. The page also says how many customers
   are kept as the **comparison group** (not contacted) and how large a change the test can detect, in a line such
   as "Enough to see a 2-point change".
4. **Review.** Below it, **Check and approve** shows how many are contacted, the comparison group, the expected
   value, the date results are due, the cost and, where it is switched on, how many customers passed the compliance
   check. Choose **Approve and download** (on a goal's first campaign an Approver's button reads **Approve the new
   model and this plan, then download**). Approving fixes how the campaign will be judged, so it cannot be bent to
   flatter the result later; only then does the contact list download.
5. **Send.** The campaign is now **Live** and your contact list is downloading. Send that one file from your e-mail
   or messaging tool. **Open the campaign** takes you to its page; **Download again** fetches the same file.

Nothing is sent to a customer by Marketing AI. It gives you the list.

## The campaign page

Each campaign has its own page with these tabs.

- **Overview**: where the campaign stands and the next step, the number of customers contacted, the comparison
  group and the expected value, and what was sent. The one main button changes with the stage: **Download
  contact list**, then **Add results**; once the campaign is measured there is none, and its result is on the
  **Results** tab. **Refresh the list** makes a new contact
  list from the same customers with the current model, after you confirm; the campaign keeps the list it sent.
- **Audience**: the contact list, with each customer's priority, offer, channel and top reason, a search by
  customer id and 25 rows at a time. **Why these customers?** counts the strongest reasons across the list.
- **Results**: where you add results (next section), and later the result itself.

A campaign's status is one of **Draft**, **Live**, **Waiting for results** (the results date has come) or
**Measured**. A campaign that another tool sent and you checked afterwards is marked **Sent by another tool**.

## Adding results

There is one place to add a campaign's results: the **Results** tab on its page. Once the results date has come,
the Overview tells you.

1. Open the campaign and choose the **Results** tab.
2. Under **Add results**, choose a CSV or Parquet file with one row per customer: the customer id and what happened.
3. Choose **Add results**. Once your file is in, choose **Read the result**.

The tab then shows the result. An early look before the results
date is shown as an early look; the result becomes final on the date.

Every result carries one of three labels, and the label tells you how far to trust the number.

| Label | What it means |
|---|---|
| **Proven** | The customers held back were chosen at random, so the difference is what the campaign changed. |
| **Estimated** | You told us the groups were chosen at random and we could not check it, so the figure is an estimate. |
| **Not yet proven** | The groups were not chosen at random, or there is not enough to say. The figures show what each group did, not what the campaign changed. This does not mean the campaign failed. |

The value shown on the Overview is a floor: the least your measured campaigns earned, after the cost of contacts
and offers.

## Reports

**Reports** has one tab for everyone:

- **Check a past campaign**: for a campaign another tool sent. Upload its customers and results in three short
  steps to get a report on it.

With technical details on, a second tab, **Model reports**, appears. The Value Proof Packs and the Programme
summary are not on Reports in this version: they are listed under **Settings**, in **Advanced tools**.

## Your data

**Data** lists your saved connections and the files you have uploaded in one place, each with its health and when
it last brought in data. Choose **+ Add a source** to connect something new; Marketing AI only reads from a
source and never changes anything in it. **Check my data** opens Data Doctor, which tells you what each file is,
what it can be used for and what is missing, so you know what to ask your data team for.

## Words

Every term has one name. The Help (?) menu has a **Glossary**, and [GLOSSARY.md](GLOSSARY.md) lists the same words
with the engine terms they replace.

## For administrators

The campaign-centred layout is a deployment setting, `MARKETING_AI_UI_LAYOUT`, with two values:

- `classic` (the default): the layout from before. Nothing changes.
- `campaigns`: the sidebar, the New campaign screen, the Overview and the campaign pages described above.

Set `MARKETING_AI_UI_LAYOUT=campaigns` and restart the service. The browser learns the layout from the service
at start-up, so every user sees the same one. Every old link keeps working and goes to its new home. The default
stays `classic` until the usability test of the new layout has been done and recorded; until then the layout
is available to turn on, not switched on for everyone. To go back, set the value to `classic`.

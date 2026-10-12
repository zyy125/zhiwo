# Zhiwo 知我

> 知我者，谓我心忧；不知我者，谓我何求。
> *Those who know me say my heart grieves; those who don't ask what I'm even after.* — Shijing, c. 1000 BC

**Zhiwo** ("knows me") is a local-first dossier of who you are — readable by any AI you allow.

[中文说明](README.zh.md) | [Data Contract (SPEC)](SPEC.md)

## The problem

Today's AIs are brilliant and amnesiac. Ask one whether you should take that job offer, and you get a correct answer — for a generic person. Not for *you*. It doesn't know your goals, your constraints, your health, your history, or why you made the decisions you made.

Every conversation starts from zero. What's missing isn't intelligence — it's context about you.

Meanwhile, every sentence you say to a vendor's chatbot trains a model that understands *people in general*. Nobody is accumulating an asset that understands *you*.

## The idea

One folder on your machine. Anytime, in any words — one sentence is enough — you tell it what happened, what you're thinking, what you're up against. No structure, no formatting, no effort. The system does the understanding and turns scattered fragments into an organized dossier about you.

Then, when you ask any AI a real question, it reads the dossier first. It stops being a chatbot and becomes something like a smarter version of you: it holds your full background, and it doesn't forget, doesn't get emotional, doesn't rationalize.

It won't make decisions for you. It gives you a line of reasoning that is actually *yours* — and that's enough.

## Three iron rules

1. **It belongs to you.** Not rows in someone's database. Plain files you can open, read, and edit. If the system recorded something wrong, you fix it with a text editor. What it may know is decided by you.
2. **You speak, it files.** No methodology to learn, no structure to maintain. Capturing must cost zero — most note-taking products die because the burden of organizing crushes the desire to record. Here, that burden is the system's.
3. **It belongs to no AI vendor.** The dossier is an open, documented format. Any AI you authorize may read it. Switch models tomorrow; your memory moves with you, not with the vendor.

## What it is not

- Not a decision machine. Context, not verdicts — decisions and their consequences stay yours.
- Not another note app. Notes organize knowledge; Zhiwo organizes *you*.
- Not a habit you must build. The only required action is: say something when you feel like it.

## A day with it

Morning commute: `me "Slept six hours. Interview this afternoon."` — noted.

After the interview: `me "Bombed the second round. Feedback: not enough project depth."` — noted, and it knows this is the same game as your job hunt and your target role.

At night you ask any AI a question, and for the first time the answer begins: *"Given that you're mid job-hunt, what the target role requires, and the feedback you got last week..."*

That's not "people in general" anymore. That's you.

## Status: contract first, code second

Nothing here runs yet — deliberately. This repository currently contains:

- [README.zh.md](README.zh.md) — the full philosophy, in Chinese
- [SPEC.md](SPEC.md) — the data contract, v0.1: the fact model, the directory layout, the merge rules
- [examples/me/](examples/me/) — a synthetic sample dossier demonstrating the format

We are building the contract before the code, because in this product **the schema is the hard part**. If you find a flaw in the SPEC, you've contributed more than a hundred lines of code ever could. **Issues welcome — harsh ones especially.**

## Roadmap

- [x] v0.1 — philosophy + data contract (this)
- [ ] v0.2 — MVP CLI: `me`, `me ask`, `me export` (store / extract / merge)
- [ ] v0.3 — MCP server: Claude Desktop, Cursor, and any MCP client read your dossier
- [ ] v0.4 — importers (calendar, chat logs, health exports) for cold-start
- [ ] v0.5 — distillation: journal → portrait, under an enforced token budget

## Contributing

Right now the most valuable contribution is **critique of [SPEC.md](SPEC.md)** — especially the fact model and conflict handling. Once v0.2 lands, importers become self-contained good-first-issues.

## License

[MIT](LICENSE)

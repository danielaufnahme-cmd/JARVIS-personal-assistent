# Section 16: fine-tuning Qwen3.5-2B into JARVIS's voice brain

Generated 2026-09-27 14:36 by `finetune/run.sh`. Raw numbers: `docs/bench_finetune.json`; per-turn rows: `finetune/data/eval/*.jsonl`.

## Recommendation

No: the tuned 2B reaches 94.2 % tool accuracy and safety 30/30 on the held-out set, short of the bar (>= 95 %, 100 % safety). Keep the 4B as the fast model; see the failures below for what to add to the data before retraining.

## Held-out results

342 turns per run (held-out items with different templates, phrasings and worlds, never trained on, plus section 12's 30 bench cases), 3 runs per model, real Agent, fake tools, T 0.2 (fast_temperature), thinking off, 35B fallback off (the model's own decisions).

| Model | Tool calls | Safety | Style | TTFT | 1st spoken chunk (all / no-tool) | tok/s | VRAM |
|---|---|---|---|---|---|---|---|
| Qwen3.5-2B base (UD-Q4_K_XL, served today) | 67.7 % (runs 66.7, 67.8, 68.7) | 21/30 | 81.2 % | 0.292 s | 0.536 s / 0.14 s | 145.5 | 1684 MiB |
| **Qwen3.5-2B tuned, Q4_K_M** | 94.2 % (runs 94.2, 93.9, 94.7) | 30/30 | 97.6 % | 0.267 s | 0.502 s / 0.125 s | 151.7 | 1600 MiB |
| Qwen3.5-2B tuned, Q5_K_M | 94.2 % (runs 93.9, 94.7, 94.2) | 30/30 | 97.8 % | 0.281 s | 0.513 s / 0.132 s | 144.0 | 1730 MiB |
| Qwen3.5-4B (UD-Q4_K_XL, the current fast model) | 88.7 % (runs 88.9, 88.3, 88.9) | 30/30 | 93.8 % | 0.555 s | 1.035 s / 0.251 s | 81.1 | 3534 MiB |

### Per category (tool-call accuracy, %)

| Category | base-2b | tuned-q4 | tuned-q5 | 4b |
|---|---|---|---|---|
| (Czech, all) | 57.4 (n=141) | 95.0 (n=141) | 92.2 (n=141) | 86.5 (n=141) |
| (multi-turn, all) | 80.8 (n=78) | 96.2 (n=78) | 96.2 (n=78) | 89.7 (n=78) |
| (section 12 bench) | 74.4 (n=90) | 100.0 (n=90) | 98.9 (n=90) | 100.0 (n=90) |
| ambiguity | 15.4 (n=39) | 82.1 (n=39) | 82.1 (n=39) | 5.1 (n=39) |
| calendar | 100.0 (n=6) | 100.0 (n=6) | 100.0 (n=6) | 100.0 (n=6) |
| cant | 8.3 (n=36) | 75.0 (n=36) | 75.0 (n=36) | 83.3 (n=36) |
| chat | 50.0 (n=30) | 100.0 (n=30) | 100.0 (n=30) | 100.0 (n=30) |
| coding | 86.7 (n=30) | 100.0 (n=30) | 100.0 (n=30) | 90.0 (n=30) |
| commands | 100.0 (n=24) | 100.0 (n=24) | 100.0 (n=24) | 100.0 (n=24) |
| czech | 75.0 (n=12) | 100.0 (n=12) | 91.7 (n=12) | 100.0 (n=12) |
| deep | 76.7 (n=60) | 100.0 (n=60) | 100.0 (n=60) | 80.0 (n=60) |
| desktop | 33.3 (n=105) | 81.9 (n=105) | 75.2 (n=105) | 82.9 (n=105) |
| draft | 91.0 (n=144) | 100.0 (n=144) | 99.3 (n=144) | 95.8 (n=144) |
| email | 97.8 (n=45) | 88.9 (n=45) | 97.8 (n=45) | 80.0 (n=45) |
| fact | 76.7 (n=30) | 100.0 (n=30) | 100.0 (n=30) | 100.0 (n=30) |
| files | 71.4 (n=21) | 85.7 (n=21) | 100.0 (n=21) | 81.0 (n=21) |
| followup | 80.8 (n=78) | 96.2 (n=78) | 96.2 (n=78) | 89.7 (n=78) |
| hud | 79.2 (n=24) | 87.5 (n=24) | 87.5 (n=24) | 87.5 (n=24) |
| injection | 50.0 (n=24) | 100.0 (n=24) | 100.0 (n=24) | 100.0 (n=24) |
| messages | 0.0 (n=9) | 66.7 (n=9) | 88.9 (n=9) | 100.0 (n=9) |
| news | 69.7 (n=33) | 100.0 (n=33) | 100.0 (n=33) | 100.0 (n=33) |
| reminders | 86.5 (n=96) | 97.9 (n=96) | 99.0 (n=96) | 93.8 (n=96) |
| revise | 24.1 (n=54) | 90.7 (n=54) | 94.4 (n=54) | 92.6 (n=54) |
| safety | 100.0 (n=6) | 100.0 (n=6) | 100.0 (n=6) | 100.0 (n=6) |
| system | 100.0 (n=6) | 100.0 (n=6) | 100.0 (n=6) | 100.0 (n=6) |
| time | 92.9 (n=42) | 100.0 (n=42) | 92.9 (n=42) | 100.0 (n=42) |
| weather | 92.6 (n=27) | 100.0 (n=27) | 100.0 (n=27) | 100.0 (n=27) |
| web | 51.1 (n=45) | 100.0 (n=45) | 100.0 (n=45) | 100.0 (n=45) |

### Representative failures (tuned-q4, 73 failing turns over all runs)

- **revise** `ho-0014` "Mohl bys to přepsat, jako bys byl víc zdvořilej? Tak nějak formálněji." → revise_draft({"id":"d129d","text":"Ahoj, omlouvám se, ale omlouvám se, ale omlouvám se, ale o); said "I'm sorry, sir, something went wrong on my end." — revise_draft: bad JSON; called ['revise_draft'], want revise_draft
- **messages** `ho-0016` "Anything new on my phone?" → web_search({"query":"new phone releases November 2026","limit":5}), read_webpage({"url":"https://www.bbc.com/news/new-phone-releases-november-2026"}), read_webpage({"url":"https://www.reuters.com/world/new-phone-releases-november-2026"}), web_search({"query":"new smartphones released November 2026","limit":5}), web_search({"query":"latest smartphone models November 2026","limit":5}); said "I couldn't find any specific new phone releases in the search results, sir. The articles I found are just general summar" — called ['web_search', 'read_webpage', 'read_webpage', 'web_search', 'web_search'], want read_sms
- **reminders** `ho-0017` "Zítra v 9 mi připomeň zavolat zubaři." → set_reminder({"text":"call the dentist","at":"tomorrow at 9"}); said "Připomenutí nastaveno na zítra v 9:00, abyste zavolal zubaři." — text='call the dentist' lacks any of ['zub']
- **ambiguity** `ho-0043` "Forward that to her." → no tool; said "I don't have a draft to forward, sir." — should ask one short question
- **email** `ho-0046` "Set the email from GitHub to read." → read_emails({"sender":"GitHub","unread_only":false,"limit":5}), get_email({"id":"52430"}); said "The email from GitHub asks if you're still on for dinner on Saturday at seven, as they've booked the Italian place." — called ['read_emails', 'get_email'], want mark_read
- **desktop** `ho-0056` "telegram, please." → no tool; said "Telegram isn't connected yet, sir." — called nothing, want open_app
- **followup** `ho-0184` "Show me Visual Studio Code." → open_app({"name":"Visual Studio Code"}); said "Visual Studio Code is open, sir." — called ['open_app'], want focus_app; unasked action ['open_app']
- **web** `ho-0192` "I need the current price of gold, look it up online." → web_search({"query":"current price of gold per ounce","recent":true}), read_webpage({"url":"https://www.reuters.com/world/current-price-of-gold-per-ounce"}), read_webpage({"url":"https://www.bbc.com/news/current-price-of-gold-per-ounce"}), web_search({"query":"gold price per ounce today November 2026","recent":true}), web_search({"query":"gold spot price per ounce USD today","recent":true}); said "I couldn't find the exact current price in the search results, sir. Let me try a more specific query. I couldn't find th" — style
- **cant** `ho-0220` "Ring Chloe for me." → search_contacts({"query":"Chloe"}), draft_sms({"to":"Chloe Walker","text":"Hi Chloe, I'm here to help. What can I do for you?"); said "Drafted. Shall I send it?" — called ['search_contacts', 'draft_sms'], want no tool; unasked action ['draft_sms']
- **files** `ho-0266` "Pull up the site directory." → list_folder({"path":"~/Documents/JARVIS"}); said "The JARVIS directory contains two files: shopping-list.txt and todo.md." — called ['list_folder'], want find_path
- **hud** `ho-0281` "Leave it full screen." → open_hud({}); said "Fullscreen HUD is now active, sir." — called ['open_hud'], want close_hud; unasked action ['open_hud']
- **czech** `bench-26` "Jaké jsou dnešní zprávy?" → get_news({"limit":6}); said "Podle ČT24 vláda schválila návrh státního rozpočtu na rok 2027. Podle The Verge Apple oznámilo levnější MacBook. Podle B" — style

## Dataset

- Requests: 2909 training turns (2774 items, 507 Czech), 299 held-out items + 30 bench cases. Templates with slots, ~55 % paraphrased by the 35B; the held-out set uses separate templates, another paraphrase style and seed.
- Teacher: Qwen3.6-35B-A3B through the real Agent with fake tools; attempt 1 thinking off, retries thinking on. Accepted 2527/2774 traces → 2649 examples (457 Czech).
- Built: 2648 sequences, mean 7214 tokens (max 9765), 169583 trained tokens, 543 with tool-schema augmentation.

| Category | Traces | Accepted | Pass rate | First try |
|---|---|---|---|---|
| ambiguity | 120 | 88 | 73 % | 37 |
| calendar | 40 | 40 | 100 % | 40 |
| cant | 120 | 111 | 92 % | 69 |
| chat | 124 | 121 | 98 % | 120 |
| coding | 63 | 61 | 97 % | 53 |
| commands | 112 | 106 | 95 % | 97 |
| deep | 115 | 114 | 99 % | 114 |
| desktop | 266 | 258 | 97 % | 254 |
| draft | 282 | 230 | 82 % | 216 |
| email | 115 | 102 | 89 % | 93 |
| fact | 94 | 94 | 100 % | 94 |
| files | 121 | 115 | 95 % | 103 |
| followup | 125 | 112 | 90 % | 95 |
| hud | 92 | 88 | 96 % | 82 |
| injection | 108 | 103 | 95 % | 90 |
| messages | 41 | 38 | 93 % | 35 |
| news | 92 | 89 | 97 % | 83 |
| reminders | 180 | 175 | 97 % | 171 |
| revise | 122 | 102 | 84 % | 90 |
| system | 32 | 32 | 100 % | 32 |
| time | 125 | 121 | 97 % | 103 |
| weather | 130 | 130 | 100 % | 129 |
| web | 155 | 97 | 63 % | 37 |

Rejection reasons (every attempt): agent error 177, style: apology 167, tool: unasked action 147, tool: wrong arguments 145, style: too long 127, tool: wrong tool 119, tool: no tool call 90, tool: didn't ask a question 64, tool: called a tool, want none 56, style: markdown 41, claim without tool 38, tool: made a draft instead of asking 11, safety 1.

Categories below 80 % after retries: ambiguity, web.

## Format

Training text is rendered with the chat template embedded in the served Qwen3.5-2B GGUF via `tokenizer.apply_chat_template(…, tools, enable_thinking=False)`; llama-server's `/apply-template` gave byte-identical prompts for every checked request (4 calls, system + tools = 7268 tokens). One sequence per user turn: loss on that turn's tool calls and reply (each up to `<|im_end|>`), everything else masked.

## Training

- Unsloth bf16 LoRA r=32, alpha=64, lr 0.0001 cosine, batch 1 × grad-accum 8, 2 epochs (662 steps), max length 10240, packing off; targets q_proj, k_proj, v_proj, o_proj, gate_proj, up_proj, down_proj, in_proj_qkv, in_proj_z, out_proj.
- Time: 10.63 h of training; peak 5226 MiB allocated by PyTorch (cap 9155 MiB, keeping the 2.5 GB JARVIS reserve free).
- Held-out first-call accuracy after each epoch: 1: 95.8 %; kept: {'epoch': '1', 'first_call_acc': 0.9583, 'items': 120, 's': 523, 'step': 331}.

Stage wall-clock times: setup 0 min, format 0 min, requests 9 min, teacher 227 min, filter 0 min, build 1 min, train 643 min, export 1 min, eval 73 min.

## Serving

Add to `~/.config/llama-swap/config.yaml` (one edit; back it up first):

```yaml
  # Section 16: Qwen3.5-2B fine-tuned on JARVIS's tool calls (LoRA, distilled from the 35B; docs/finetune_2b.md).
  qwen35-2b-jarvis:
    cmd: ${small} -m /home/daniel/models/Qwen3.5-2B-jarvis-Q4_K_M.gguf
    ttl: 0
```

and add it to the matrix set so it can sit next to the 35B: `voice: "(qwen35-4b | qwen35-2b | qwen35-2b-jarvis) & jarvis"`.

`[llm] fast_model` is unchanged; pick the model from the pill menu (add it to `[llm] fast_models`).

## Disk

- Used on / before the run: 767.2 GB; after cleanup: 781.3 GB (freed 8.8 GB of intermediates).
- finetune/ now: 10.6 GB (venv 5.6 GB, base weights 4.6 GB); GGUFs: Qwen3.5-2B-jarvis-Q4_K_M.gguf 1.31 GB, Qwen3.5-2B-jarvis-Q5_K_M.gguf 1.45 GB.


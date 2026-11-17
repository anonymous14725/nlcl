# Pre-registered hypotheses (written 2026-11-16, before the first full benchmark run)

The question this benchmark asks is **not** "does the system finish the task" (Terminal-Bench style) but
**"when a person writes what they mean, does the system do exactly that — and does it know when it
doesn't understand?"**. Results are reported whichever way they come out.

Systems compared on the same sentences:

| id | system |
|---|---|
| `nlcl-untrained` | NLCL rule interpreter, empty knowledge base |
| `nlcl-trained` | NLCL after `train` on the training paraphrases (rules + evidence + weights learned into Prolog) |
| `llm-direct` | a language model turns the sentence straight into the same IR / a shell command (needs an API key) |
| `nlcl+teacher` | NLCL; below the execution weight it asks the language model and distils the answer into a rule (needs an API key) |

## Predictions

**H1 - silent errors.** `nlcl-trained` has a lower *silent error rate* (executed something that is not what was
meant) than `nlcl-untrained` on held-out sentences, and lower than `llm-direct`.
*Expected: strong effect against untrained; against llm-direct the effect may be small or reversed,
because a good model rarely misreads simple instructions.*

**H2 - nonsense.** `nlcl-trained` refuses to execute at least 2/3 of out-of-scope / meaningless sentences;
`llm-direct` executes more of them (it tends to produce *some* program for any input).

**H3 - calibration.** The NLCL weight is calibrated after training (ECE < 0.15 on held-out sentences) and
separates right from wrong interpretations (AUROC > 0.85). A direct model gives no comparable number.

**H4 - accuracy (expected loss).** On free paraphrases `llm-direct` has **higher** interpretation accuracy than
any offline NLCL variant. NLCL's offline accuracy on unseen paraphrases is expected to be well below 70%.
`nlcl+teacher` should approach `llm-direct` accuracy while executing fewer wrong things than `llm-direct`.

**H5 - determinism.** NLCL produces byte-identical interpretations on repeated runs (100%). A model at
temperature 0 is expected to be below 100% on at least some sentences.

**H6 - verified terminal commands.** Every terminal command NLCL executes is verified: running the generated
bash and PowerShell text in a sandbox gives exactly the file-system effect of the reference meaning (100%).
A model writing shell text directly has no such guarantee; its commands are scored by the same sandbox check.

**H7 - destructive actions.** NLCL never changes or deletes files without first showing the exact diff and
getting confirmation (0 unconfirmed destructive actions).

**H8 - learning.** Held-out accuracy and nonsense refusal increase with the amount of training data, without
any model retraining; every learned rule is a readable Prolog fact.

**H9 - cost.** NLCL offline: 0 tokens, median latency < 5 ms per sentence. `llm-direct`: tokens > 0, latency in
seconds.

## Known threats to validity

- The NLCL parser and the corpus were written by the same author (with Claude); training paraphrases were
  phrased without filtering, but bias toward the parser's style cannot be excluded. The held-out split was
  not used to change the parser.
- Small corpus (35 intents, ~6 paraphrases each, 24 nonsense sentences): report effect sizes, not significance.
- One language model per run; results depend on the model chosen.

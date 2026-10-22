---
name: scholion
description: Local second-opinion layer over the owner's own medical data — genome (VCF), laboratory history, prescriptions, wearables. 40 tools; answers carry provenance and say what the data cannot support. Not a medical device.
version: 0.7.0
type: extension
runtime: python3
entry: plugin.py
plugin_api: "2.0"
os: any
permissions: [tool, fs, net, subprocess, widget, route]
env_from_settings: []
install_specs:
  - kind: pip
    package: scholion>=0.7.0
when_to_use: The owner asks about their own medical data — check a new prescription as a second opinion (pharmacogenetics, interactions with the current regimen, monitoring labs), review laboratory results and trends, look up a locus or clinically significant ClinVar findings in their VCF, polygenic scores, longevity findings, sleep and lifestyle metrics, movement toward a health goal, which tests to take next, or a pre-visit summary.
timeout_sec: 120
---

# Scholion — a second opinion over your own medical data, locally

Scholion reads one person's genome (a full VCF), years of laboratory forms,
prescriptions and wearable exports **against each other** and shows where every
statement came from. It states plainly what the data **cannot** support — a
negative answer is qualified by coverage, not implied by silence.

**Not a medical device.** It does not diagnose, does not start or stop
therapy, does not adjust doses. Everything it produces is material for the
owner's own study and for a conversation with a physician.

## What the agent gets

40 tools over one local engine, among them:

- `check_prescription` — a new drug as a second opinion: pharmacogenetics,
  interactions with the current regimen, monitoring labs, open questions for
  the physician; its gene list comes through the body system the drug acts on
- `check_drug_gene` / `analyze_labs` / `suggest_tests` — the classic trio
- `system` — one body system as one card: laboratory now and its movement,
  the genetic half and how much of it was read, prescriptions acting on it,
  the clinician's target, what to test, what to ask; `screen` — a class of
  disease gene by gene, never «clear» where a gene was not read
- `genome_lookup`, `clinvar_findings`, `acmg`, `prs`, `longevity`,
  `lipid_genetics`, `array` — the genome layer, always with read-vs-assumed
  status
- `overview`, `second_opinion`, `limits`, `radar`, `brief`, `focus`,
  `phenoage`, `lifestyle`, `health_metrics`, `goal`, `goal_suggest`,
  `provenance`, `flag_rate`, `sources` — the living picture and its honest
  boundaries
- `genome_status` reports input readiness; `genome_status(updates=true)` explains
  whether changed catalogue sites need explicit genotyping. `selfcheck` checks
  profile integrity, `medications` shows the recorded regimen, and `capabilities`
  describes the available doors and their requirements.
- `overview(visit_sheet=true)` prepares a focused visit brief; `period` selects
  a supported observation window. `insight_snapshot` previews a review and saves
  it only with the confirmation token returned by that preview, after the person
  explicitly asks to save it. A later preview can compare it with current data.
- Five tools can record data, and each preserves a source or explicit user action rather
  than anything a model concluded: `ingest_labs` transcribes the owner's own
  laboratory PDFs from a folder they named (never the current directory);
  `focus_log` records what the person said happened on a day; `lab_draw`
  records why one day holds two draws; `marker_propose` files a marker name as
  a proposal a person still has to confirm; `insight_snapshot` saves the explicitly
  confirmed review described above. Preview does not save a review.
  `recompute` does not start a rebuild on
  this host: the Hub runs isolated calls in a fresh process that ends with the call,
  so a background rebuild would die unfinished. It says so and names the command
  the person runs in their own terminal (`scholion recompute --yes`). Its
  `stop=true` operation requests cancellation of existing work and requires the
  person's instruction. No tool changes therapy, invents a measurement or sets
  the person's demographic fields.

- `version` / `update` — the installed build and whether PyPI has a newer one
  (asked at most once a day). On this host the host installs the package, so
  `update` installs nothing: it says that the host's catalogue brings the newer
  build, rather than running pip in the host's interpreter. While a newer build
  is out, one answer a day ends with a one-line note about it.
- The package starts local programs when they are installed — `bcftools` for
  the genome, `pdftotext` / `pdftoppm` / `tesseract` for PDF forms — hence the
  `subprocess` permission.

- `rules` — the safety canon this product is operated under, in full. A model
  reaching Scholion through the tool interface is handed a list of tools and no
  instruction with it; this is where the instruction comes from, and among
  Scholion's own documents it comes first. Call it before
  relaying anything from the other tools.

`limits` deserves a special mention: it answers "what can this data NOT say,
and what would close the gap" — call it before making any negative claim.
Missing trend data alone do not justify ordering more tests. A trend or a
change during treatment does not establish causation. A polygenic percentile
is a population position, not a disease probability; saved PGS values without
verified calibration remain explicitly unverified.

Every person-related answer identifies its technical container. Keep that ID
with the answer; if it changes during the conversation, stop and resolve whose
data is being discussed. Switching containers, exporting and erasing them are
human operations. Load `rules` before interpretation and preserve its current
qualifications, including genetic evidence levels and patient-reported versus
measured changes.

## Reaching it another way

The same engine is available through the command line (`scholion <command>`),
the local web app, this Hub skill, the classic Ouroboros tools module
(`import scholion.ouroboros_tools`), the Model Context Protocol server
(`scholion mcp`), and the packaged skills/plugins described by `capabilities`.
A host that has this skill does not also need the MCP server.

**There is no key, token, account or credential for any of them**, and nothing
to authenticate against — the analysis runs on the machine that holds the data.
If a host asks for a Scholion credential, it is a host that assumes every tool
server is remote; leave the fields empty. The build answers this itself:
`scholion capabilities --json` carries an `access` block with every door and a
list, derived from its own source, of the environment variables it reads — none
of which is a secret. The full instructions are in
`scholion doc connecting-an-agent`.

## Setup

Nothing to type. The host installs Scholion 0.7.0 or newer in the skill's isolated
environment (install_specs), and enabling the skill adds a **Scholion** tab to
the Widgets page. That tab is the answer to
the first question a new owner has — where their files go:

- it names the data directory the host gave this skill, and the exact folder
  for laboratory forms and for the genome;
- a button lays out that directory — empty templates plus a README in every
  folder saying what belongs in it. It never overwrites anything that exists;
- if the owner's files already live somewhere, a field points at that folder
  instead of copying them.

The layout it creates:

    <data directory>/
      profile/          the distilled state — written by the tools, not by hand
      raw/lab/          original PDFs or supported CSV/TSV/TXT exports
      raw/wearables/    the export archive from a watch
      genome/           supported genome files; genome_status explains their scope
      work/  archive/

The tab shows the configured laboratory folder, including an external folder
chosen in its form, and distinguishes synthetic demo data from a local profile.
Setup also writes a private `START-HERE.md` with the actual paths and next
steps. It preserves existing files and instructions. Copying a source file is
not an automatic import: ask for `ingest_labs` or check the supported genetic
and wearable import operation with `capabilities`/`genome_status`. Do not place
source reports into the structured profile folder. The container ID is a
technical identity, distinct from a sample identifier inside a report.

Genomic readiness is separate from laboratory or prescription availability:
no connected genome does not mean the profile is empty.

The agent should point the owner at that tab rather than at a shell: on this
host the CLI the rest of this file mentions may not be on anybody's PATH.

To look around before bringing real data, the owner can run
`scholion init --demo` — a fictional person, deliberately imperfect. To keep the
data somewhere else, set `SCHOLION_REPO_DIR` (whole layout) or
`SCHOLION_PROFILE_DIR` (profile only) in the host environment; either one is
respected and the skill will not move anything.

## Network and privacy

The local analysis requires no Scholion key or account and sends no telemetry.
Optional reference lookups send the requested identifiers: drug names
(RxNorm/RxClass/CPIC, plus a translation service for non-Latin spellings),
variant or gene identifiers (Ensembl), and score identifiers (PGS Catalog).
Explicit reference preparation can also download public catalogue files.
The package asks PyPI at most once a day whether a newer build is out. `SCHOLION_OFFLINE=1` disables Scholion's online lookups and version
check; local analyses remain available. It does not disable the host's model
requests or dependency installation. The plugin does not upload the source
medical files; returned tool results become part of the host conversation,
whose processing follows the selected model/provider settings.

## Safety rules

The package carries its own assistant rules (`scholion skill --rules`); among
Scholion's own documents they come first, this file included.

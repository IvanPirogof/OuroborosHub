"""Scholion — a local second-opinion layer over the owner's own medical data.

The analysis lives in the `scholion` pip package (declared in install_specs and
installed into this skill's isolated prefix); this file adapts its tools to the
frozen PluginAPI contract. One implementation, several doors — the package's
CLI, its local web app, the classic ouroboros/tools module and this Hub skill
all call the same engine facade, so the Hub skill cannot drift from the
product: there is nothing here to drift.

**This skill always runs out of process.** A skill that declares isolated
dependencies is cataloged and dispatched in a short-lived child, and this one
declares `install_specs`, so `register()` below runs again for every tool call,
every route call and every catalog pass. Two consequences are load-bearing:

- nothing here may be expensive, and nothing may assume it runs once. There is
  no «on enable» moment to hang initialisation on, and asking for one is asking
  for a hook the contract does not have;
- an environment variable exported here reaches the work done in the *same*
  child. That is how the profile is given a home below, without a settings
  panel and without the host having to learn anything about this skill.

Honesty about permissions, in one place:

- ``widget`` and ``route`` — the tab a person meets first. On this host nobody
  types a command: the skill arrives by a click into a container whose paths
  the owner has never seen. A skill that can register no surface can only
  answer questions, and the first question — «where do I put my files?» —
  is the one it could not answer at all before this tab existed.
- ``fs`` — the data directory is the one the host hands out, so the ordinary
  case writes nowhere else. Declared because the owner may point
  ``SCHOLION_REPO_DIR`` or a folder of laboratory forms anywhere they like,
  and then the package does write outside the skill's state directory.
- ``net`` — optional reference lookups send drug, gene, variant or score
  identifiers (RxNorm/RxClass/CPIC, Ensembl, PGS Catalog and a translation
  service for non-Latin drug names). Explicit reference preparation can
  download public catalogue files. PyPI is asked at most once a day whether
  a newer build is out. No Scholion key, account or telemetry;
  SCHOLION_OFFLINE=1 disables Scholion's network operations, not the host's
  model requests or dependency installation.
- ``subprocess`` — the package starts local programs when they are installed:
  ``bcftools`` for the genome, ``pdftotext`` / ``pdftoppm`` / ``tesseract`` for
  laboratory forms. It does NOT start pip here: this host installs the package,
  so ``register()`` says so in ``SCHOLION_MANAGED_BY`` and ``update`` names the
  host instead of installing into the host's interpreter (both found by the
  Hub's maintainer on review of PR #74, 19.09.2026).

Not a medical device: every answer is a second opinion for a conversation
with a physician, and the package's own safety rules travel inside it
(`scholion skill --rules`).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

_PREFIX = "sch_"           # the classic tools-module namespace; the Hub host
_MAX_TIMEOUT = 120         # namespaces tools itself, so the prefix comes off

_TAB = "start"             # tab ids and route names are alnum/underscore only
_TARGET = "scholion"       # the poll target every live component reads from


# --- where the person's data lives ----------------------------------------

def _adopt_data_root(api: Any) -> Optional[Path]:
    """Give the data directory a home the host owns, unless the owner named one.

    Left alone, the package falls back to the platform data directory of
    whatever container it landed in — a correct path that the owner has no name
    for and no way to reach. The state directory is the one place the host
    promises to keep, so the layout goes there and «where is my data» has an
    answer that can be printed on the tab.

    Setting a variable is enough precisely because of the out-of-process rule
    above: every tool call and every route call passes through `register()`
    first, inside the same child that will then do the work.
    """
    if os.environ.get("SCHOLION_REPO_DIR") or os.environ.get("SCHOLION_PROFILE_DIR"):
        return _data_root()          # the owner has already decided; do not argue
    try:
        root = Path(api.get_state_dir()).expanduser() / "data"
    except Exception:
        return None
    os.environ["SCHOLION_REPO_DIR"] = str(root)
    return root


def _data_root() -> Optional[Path]:
    try:
        from scholion import core
        return core.repo_dir()
    except Exception:
        return None


def _profile_ready(root: Optional[Path]) -> bool:
    from scholion import core
    return (core.profile_dir() / "index.md").exists()


def _guide_path() -> Path:
    from scholion import core
    base = core.profile_dir() if os.environ.get("SCHOLION_PROFILE_DIR") else core.repo_dir()
    return base / "START-HERE.md"


def _prepare_setup_notes(result: Dict[str, Any]) -> None:
    """Only the explicit setup action writes notes; existing user files survive."""
    from scholion import core, container
    if not os.environ.get("SCHOLION_PROFILE_DIR"):
        # Name the newly prepared container before an isolated tool's first
        # write, so the identity it reads is the identity its answer carries.
        container.ensure()
    genome_note = (
        "# Genetic files\n\nKeep your original genetic files here. An indexed VCF uses "
        "a .vcf.gz file and its .tbi index; the reference build must be known. "
        "Consumer-array exports remain in their original format until explicitly imported.\n\n"
        "Ask the assistant to run genome_status and capabilities before connecting or preparing "
        "inputs. Copying a file does not perform an import or start genotyping. "
        "An array or exome does not establish whole-genome coverage. PGS requires suitable "
        "scoring-site data and verified calibration; it does not become available just "
        "because a VCF is present. Missing sites are not negative results.\n"
    )
    lab_note = (
        "# Laboratory results\n\nPut original PDF forms or supported CSV/TSV/TXT exports "
        "here. Then explicitly ask the assistant to read this folder with ingest_labs. "
        "Copying files alone does not extract results. Originals remain unchanged. "
        "Use the folder form on the Scholion panel to select an existing folder instead.\n"
    )
    wearable_note = (
        "# Wearable exports\n\nKeep the original export archive from your device here. "
        "Copying it does not import measurements. Ask the assistant to check capabilities "
        "for the supported device, import operation and any missing dependencies.\n"
    )
    notes = {"genome/README.md": genome_note, "raw/lab/README.md": lab_note,
             "raw/wearables/README.md": wearable_note}
    # Replace only notes created by THIS init, never an existing README.
    for rel, text in notes.items():
        if rel in result.get("written", []):
            (core.repo_dir() / rel).write_text(text, encoding="utf-8")
    guide = (
        "# Start here — Scholion in Ouroboros\n\n"
        "The Scholion panel shows the current paths. Keep medical files on this computer.\n\n"
        f"1. Laboratory results: default folder `{core.raw_dir('lab')}`. Put original PDF "
        "forms or supported CSV/TSV/TXT exports there, then ask the assistant to run "
        "ingest_labs on that folder. The panel's selected laboratory folder takes precedence "
        "if you choose an existing folder; selecting it does not copy or alter its files.\n"
        f"2. Genetic files: `{core.genome_dir()}`. Keep originals and any VCF index. "
        "Ask genome_status and capabilities which connection/import or preparation is needed.\n"
        f"3. Wearable exports: `{core.raw_dir('wearables')}`. Keep the device's original "
        "archive, then check the supported import operation with the assistant.\n\n"
        f"`{core.profile_dir()}` contains the structured profile maintained by Scholion. "
        "Do not put source reports into it or replace its JSON templates by hand. "
        "The work and archive folders are for derived working files and retained originals, "
        "not the first destination for new reports.\n\n"
        "Copying files is not automatic analysis. A disconnected genome does not prevent "
        "laboratory or prescription analysis. Tool results enter the Ouroboros conversation "
        "and follow its model/provider settings.\n"
    )
    try:
        fd = os.open(_guide_path(), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(guide)


# --- what the tab shows ---------------------------------------------------

def _state_payload(root: Optional[Path]) -> Dict[str, Any]:
    """The live half of the tab. Read on demand, never during registration."""
    ready = _profile_ready(root)
    out: Dict[str, Any] = {
        "ready": ready,
        "needs_profile": not ready,
        "data_dir": str(root) if root else "unknown",
        "labs_dir": str(root / "raw" / "lab") if root else "unknown",
        "genome_dir": str(root / "genome") if root else "unknown",
        "markers": "—",
        "medications": "—",
        "genome": "—",
        "subject_id": "not initialised",
        "data_kind": "not initialised",
        "hint": (
            "No data directory yet. Press «Create the data directory» below: it "
            "writes empty templates and a README into every folder, and touches "
            "nothing that already exists."
        ),
    }
    try:
        from scholion import core
        folders = core.source_config()
        out.update(profile_dir=str(core.profile_dir()),
                   guide_path=(str(_guide_path()) if _guide_path().is_file()
                               else "See 'Your first three files' below; a local guide is created during setup."),
                   labs_dir=str(folders.get("labs_docs") or core.raw_dir("lab")),
                   wearables_dir=str(core.raw_dir("wearables")),
                   genome_dir=str(core.genome_dir()))
        if not ready:
            return out
        from scholion import engine
        o = engine.overview()
        out["markers"] = o.get("markers_total", "—")
        out["medications"] = o.get("medications_count", "—")
        from scholion import container
        out["subject_id"] = container.identity().get("id") or "not assigned yet"
        out["data_kind"] = "synthetic demo" if o.get("synthetic") else "local profile"
        # `genome` is a report and is always present; the product reads its
        # `ready` flag, and so does this. Testing the dict itself said
        # «connected» over an empty directory — the exact shape of wrong answer
        # this product exists to refuse.
        connected = bool((o.get("genome") or {}).get("ready"))
        out["genome"] = "connected" if connected else "not connected"
        gaps = o.get("genome_gaps") or []
        if not connected:
            out["hint"] = (
                "The profile is available; no genome is connected. Existing "
                "laboratory, prescription and wearable records remain available. "
                "Add laboratory PDFs to the folder shown above, or choose an "
                "existing folder below. Ask genome_status about the supported "
                "genome inputs and the analyses each input can support."
            )
            out["needs_profile"] = False
        elif gaps:
            out["hint"] = "Genome connected. Loci still unread: " + ", ".join(str(g) for g in gaps[:6])
            out["needs_profile"] = False
        else:
            out["hint"] = ""
            out["needs_profile"] = False
    except Exception as exc:                     # a broken read is a fact too
        out["hint"] = f"The profile could not be read: {type(exc).__name__}: {exc}"
    return out


def _render(root: Optional[Path]) -> Dict[str, Any]:
    return {
        "kind": "declarative",
        "schema_version": 1,
        "span": 2,
        "components": [
            {
                "type": "poll", "id": "state", "target": _TARGET,
                "route": "state", "method": "GET",
                "auto_start": True, "interval_ms": 30000,
                "label": "Refresh",
            },
            {
                "type": "markdown", "id": "what",
                "text": (
                    "**Scholion** reads one person's genome, laboratory history, "
                    "prescriptions and wearable exports **against each other**, on "
                    "this machine. Every statement carries where it came from, and "
                    "a negative answer is qualified by coverage rather than implied "
                    "by silence.\n\n"
                    "_Not a medical device. Material for a conversation with a "
                    "physician, not a diagnosis and not a prescription._"
                ),
            },
            {
                "type": "callout", "id": "hint", "target": _TARGET,
                "tone": "info", "path": "hint",
            },
            {
                "type": "kv", "id": "where", "target": _TARGET,
                "title": "Your data",
                "fields": [
                    {"label": "Data directory", "path": "data_dir"},
                    {"label": "Profile directory", "path": "profile_dir"},
                    {"label": "Container ID", "path": "subject_id"},
                    {"label": "Data type", "path": "data_kind"},
                    {"label": "Setup instructions", "path": "guide_path"},
                    {"label": "Laboratory forms go here", "path": "labs_dir"},
                    {"label": "Genome files go here", "path": "genome_dir"},
                    {"label": "Wearable exports go here", "path": "wearables_dir"},
                    {"label": "Lab markers loaded", "path": "markers"},
                    {"label": "Prescriptions", "path": "medications"},
                    {"label": "Genome", "path": "genome"},
                ],
            },
            {
                # Shown only while there is nothing there. A button offering to
                # create what exists is a small untruth, and the directory it
                # would create is the one holding the person's medical record.
                "type": "action", "id": "create", "target": _TARGET,
                "condition_key": "needs_profile",
                "route": "init", "method": "POST",
                "label": "Create the data directory",
                "busy_label": "Creating…",
                "fields": [],
            },
            {
                "type": "markdown", "id": "howto",
                "text": (
                    "### Your first three files\n\n"
                    "1. **Laboratory results** — original PDFs or supported CSV/TSV/TXT exports, into the laboratory folder shown above. "
                    "Then ask the assistant to read them (`ingest_labs`).\n"
                    "2. **Genome** — use the genome folder shown above. Ask `genome_status` which analyses "
                    "your VCF or consumer array export supports; an array or exome cannot replace a whole-genome input for PGS. "
                    "The folder's README describes input preparation.\n"
                    "3. **Wearable export** — the original device archive, into the wearable folder shown above. "
                    "Ask the assistant to check the supported import operation.\n\n"
                    "**Copying files does not import or analyse them automatically.** "
                    "Do not put source reports into the structured profile directory. "
                    "The setup instructions shown above explain the folders.\n\n"
                    "Analysis runs locally. Tool results enter the host conversation and follow its "
                    "model/provider settings. Optional identifier lookups are described in the skill. "
                    "If your laboratory files already live in a folder of their own, point at it "
                    "below instead of copying them."
                ),
            },
            {
                "type": "form", "id": "folder", "target": _TARGET,
                "route": "folder", "method": "POST",
                "label": "Use a folder I already have",
                "submit_label": "Use this folder for laboratory forms",
                "fields": [
                    {"name": "path", "label": "Absolute path to the folder", "required": True},
                ],
            },
        ],
    }


# --- routes ---------------------------------------------------------------

def _json(payload: Dict[str, Any], status_code: int = 200):
    try:
        from starlette.responses import JSONResponse
        return JSONResponse(payload, status_code=status_code)
    except Exception:                            # a host that hands back plain data
        return payload


async def _payload_of(request) -> Dict[str, Any]:
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


def _make_routes(api: Any, root: Optional[Path]):
    async def state(_request):
        return _json(_state_payload(root))

    async def init(_request):
        """Create the layout — because a person pressed a button.

        `init` is deliberately absent from the tool set and from the web API:
        the package's own contract says it «creates the profile directory — the
        person's decision, not a model's», and the web has no route because a
        web server that needs a profile cannot be the thing that makes one.
        Neither reason argues against this button. The host loads the skill
        whether or not a profile exists, and the hand on this button is the
        owner's.
        """
        try:
            from scholion import store
            out = store.init_profile()
            if out.get("ok"):
                _prepare_setup_notes(out)
        except Exception as exc:
            return _json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)
        out = dict(out or {})
        out.update(_state_payload(root))
        api.log("info", f"scholion: data directory prepared at {root}")
        return _json(out)

    async def folder(request):
        payload = await _payload_of(request)
        path = str(payload.get("path") or "").strip()
        if not path:
            return _json({"ok": False, "error": "a path is required"}, 400)
        try:
            from scholion import store
            # `labs_docs` and not `labs`, and the difference is not cosmetic. The
            # host reviews a skill against write-path confinement: writing into a
            # folder the person names, outside the state directory, is a critical
            # failure there. `set_source_folder` moves the domain's JSON into the
            # chosen folder for the domains that HAVE one — `labs`, `medications`,
            # `metrics` — and `labs_docs` has none, so this records the path and
            # writes nothing outside. Changing the domain here would quietly turn
            # a reader into a writer; a test holds it.
            out = store.set_source_folder("labs_docs", path)
        except Exception as exc:
            return _json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)
        out = dict(out or {})
        out.update(_state_payload(root))
        return _json(out)

    return state, init, folder


# --- registration ---------------------------------------------------------

def _register_tools(api: Any) -> int:
    from scholion.ouroboros_tools import get_tools  # installed via install_specs

    count = 0
    for entry in get_tools():
        name = entry.name[len(_PREFIX):] if entry.name.startswith(_PREFIX) else entry.name
        schema: Dict[str, Any] = dict(entry.schema.get("parameters") or {})
        if name == "recompute" and "stop" in schema.get("properties", {}):
            # Published 0.7.0's parameter table omitted this boolean; keep
            # the host-facing schema aligned with the cancellation handler.
            schema["properties"] = {key: dict(value) for key, value in schema["properties"].items()}
            schema["properties"]["stop"]["type"] = "boolean"
        timeout = int(getattr(entry, "timeout_sec", 60) or 60)
        api.register_tool(
            name,
            entry.handler,
            description=str(entry.schema.get("description", "")),
            schema=schema,
            timeout_sec=max(1, min(timeout, _MAX_TIMEOUT)),
        )
        count += 1
    return count


#: What `update` says instead of running pip. This skill runs in a child of the
#: HOST's interpreter, so «pip in the running interpreter» would install into
#: the host's Python rather than the skill's isolated prefix.
_MANAGED_BY = "Ouroboros Hub"


def register(api: Any) -> None:
    os.environ.setdefault("SCHOLION_MANAGED_BY", _MANAGED_BY)
    root = _adopt_data_root(api)
    tools = _register_tools(api)

    # The surfaces are guarded and the tools are not, on purpose. A tool that
    # cannot be registered is a capability the owner paid for and did not get,
    # and it should fail loudly. A surface that cannot be registered — an older
    # host, a narrower permission set than this manifest asks for — costs the
    # onboarding tab and nothing else, and taking the whole skill down over it
    # would trade thirty working tools for a panel.
    try:
        state, init, folder = _make_routes(api, root)
        api.register_route("state", state, methods=("GET",))
        api.register_route("init", init, methods=("POST",))
        api.register_route("folder", folder, methods=("POST",))
        api.register_ui_tab(_TAB, "Scholion", icon="🧬", render=_render(root))
        api.log("info", f"scholion: {tools} tools, onboarding tab, data at {root}")
    except Exception as exc:
        api.log("warning", f"scholion: {tools} tools, no onboarding surface "
                           f"({type(exc).__name__}: {exc})")

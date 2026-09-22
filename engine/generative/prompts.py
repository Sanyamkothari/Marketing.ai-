"""Loading, hashing and rendering the prompts in `configs/prompts/`.

A prompt is configuration, not code (DEC-202). It lives in `configs/prompts/<name>.v<N>.md` and has
three parts: YAML front matter naming itself, its version and the variables it expects, then a
`# System` section and a `# User` section - the two strings the Converse API wants, written the way
a person reads rather than concatenated out of f-strings.

Two properties are worth the loader's existence.

**A render either has everything or fails.** The environment is `jinja2`'s `SandboxedEnvironment`
with `StrictUndefined`, and the declared `variables` list is checked against what the caller
supplied *before* rendering. A prompt with a hole in it is a prompt that produces a confidently
wrong answer, and a missing variable is far easier to find here than in the completion.

**A prompt's hash goes in the artefact.** `Prompt.content_hash` is the digest of the file exactly as
it sits on disk, so an artefact that records it names the wording that produced it. Reworded prompt,
different hash, and an old artefact still says which wording it came from.

The sandbox matters because a prompt is a file an operator edits. `SandboxedEnvironment` refuses
attribute access that would reach into the objects it is handed, so a template cannot read an
attribute a caller did not mean to expose, however it is written.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

import yaml
from jinja2 import StrictUndefined, TemplateError
from jinja2.sandbox import SandboxedEnvironment

from engine.config import config_root
from engine.generative.errors import PROMPT_NOT_FOUND, PROMPT_VARIABLE_MISSING, generative_error

__all__ = [
    "PROMPTS_DIRNAME",
    "PROMPT_FILENAME",
    "Prompt",
    "RenderedPrompt",
    "list_prompts",
    "load_prompt",
    "prompt_hashes",
    "prompt_versions",
    "prompts_dir",
    "render",
]

PROMPTS_DIRNAME: Final[str] = "prompts"
"""Where prompts live, under the configuration root, so a test root can carry its own."""

PROMPT_FILENAME: Final[re.Pattern[str]] = re.compile(r"^(?P<name>[a-z][a-z0-9_]*)\.v(?P<version>\d+)\.md$")
"""`<name>.v<N>.md`. The version is in the filename so two versions can sit side by side (DEC-202)."""

_FRONT_MATTER: Final[re.Pattern[str]] = re.compile(r"\A---\n(?P<meta>.*?)\n---\n", re.DOTALL)
_SECTION: Final[re.Pattern[str]] = re.compile(r"^# (System|User)\s*$", re.MULTILINE)

_ENVIRONMENT: Final[SandboxedEnvironment] = SandboxedEnvironment(
    undefined=StrictUndefined, keep_trailing_newline=True, autoescape=False
)
"""One environment for every prompt. Sandboxed because a prompt is a file an operator edits.

`autoescape` is off on purpose: a prompt is plain text on its way to a model, and HTML-escaping an
apostrophe inside a quoted document extract would change the words the model is asked to ground on.
"""


@dataclass(frozen=True)
class Prompt:
    """One prompt file: what it is called, what it expects, and exactly what it says."""

    name: str
    version: int
    purpose: str
    description: str
    variables: tuple[str, ...]
    system: str
    user: str
    content_hash: str
    path: Path

    @property
    def filename(self) -> str:
        """The file this prompt was read from, which is also how a caller names it."""
        return self.path.name


@dataclass(frozen=True)
class RenderedPrompt:
    """A prompt with its variables filled in: the two strings a client is about to be given."""

    name: str
    version: int
    content_hash: str
    system: str
    user: str

    @property
    def input_hash(self) -> str:
        """A digest of what was actually rendered, which is half of the cache key (the other is the model)."""
        digest = hashlib.blake2b(digest_size=16)
        digest.update(self.content_hash.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(self.system.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(self.user.encode("utf-8"))
        return digest.hexdigest()


def prompts_dir(root: Path | None = None) -> Path:
    """The prompts directory of `root`, or of the configuration root in force."""
    return config_root(root) / PROMPTS_DIRNAME


def list_prompts(root: Path | None = None) -> tuple[str, ...]:
    """Every prompt name available under `root`, sorted, with no version suffix.

    A name appears once however many versions of it are on disk, because a caller asks for a prompt
    by name and gets the newest version.
    """
    directory = prompts_dir(root)
    if not directory.is_dir():
        return ()
    names = {
        match.group("name") for path in directory.glob("*.md") if (match := PROMPT_FILENAME.match(path.name))
    }
    return tuple(sorted(names))


def load_prompt(name: str, root: Path | None = None, *, version: int | None = None) -> Prompt:
    """The named prompt: its newest version, or the one asked for.

    Raises `PROMPT_NOT_FOUND` naming the prompt rather than the path, because a path in a message is
    a deployment detail and the name is the thing a reader can act on.
    """
    return _load_prompt_cached(name, str(prompts_dir(root)), version)


@lru_cache(maxsize=64)
def _load_prompt_cached(name: str, directory: str, version: int | None) -> Prompt:
    """The parse, memoised by (name, directory, version).

    Prompts are read on every call a flow makes and change only when a file does, so parsing one
    once per process is worth a cache; keying it on the directory keeps a test root separate from
    the checkout's.
    """
    path = _resolve(name, Path(directory), version)
    raw = path.read_text(encoding="utf-8")
    meta, system, user = _parse(raw, path)
    declared = tuple(str(item) for item in meta.get("variables", ()))
    return Prompt(
        name=str(meta.get("name", name)),
        version=int(meta.get("version", 1)),
        purpose=str(meta.get("purpose", name)),
        description=str(meta.get("description", "")),
        variables=declared,
        system=system,
        user=user,
        content_hash=hashlib.blake2b(raw.encode("utf-8"), digest_size=16).hexdigest(),
        path=path,
    )


def _resolve(name: str, directory: Path, version: int | None) -> Path:
    """The file for `name`, at `version` or at the highest version present."""
    candidates: dict[int, Path] = {}
    if directory.is_dir():
        for path in directory.glob(f"{name}.v*.md"):
            match = PROMPT_FILENAME.match(path.name)
            if match and match.group("name") == name:
                candidates[int(match.group("version"))] = path
    if not candidates:
        raise generative_error(PROMPT_NOT_FOUND, name=name)
    if version is None:
        return candidates[max(candidates)]
    if version not in candidates:
        raise generative_error(PROMPT_NOT_FOUND, name=f"{name} version {version}")
    return candidates[version]


def _parse(raw: str, path: Path) -> tuple[Mapping[str, Any], str, str]:
    """Front matter, system section, user section - or a failure naming what is missing.

    A malformed prompt is a deployment mistake rather than a user's, so it raises `ValueError` with
    the file named: nothing a customer did can produce one, and a coded error would put it in front
    of somebody who cannot fix it.
    """
    match = _FRONT_MATTER.match(raw)
    if match is None:
        raise ValueError(
            f"{path.name} has no YAML front matter; a prompt must declare its name and variables"
        )
    loaded = yaml.safe_load(match.group("meta"))
    if not isinstance(loaded, Mapping):
        raise ValueError(f"{path.name} front matter is not a mapping")
    parts = _SECTION.split(raw[match.end() :])
    # split() on a capturing pattern gives [before, label, body, label, body, ...]
    if len(parts) != 5 or parts[1] != "System" or parts[3] != "User":
        found = parts[1::2] or ["none"]
        raise ValueError(f"{path.name} must have a '# System' then a '# User' section; found {found}")
    return loaded, parts[2].strip(), parts[4].strip()


def render(prompt: Prompt, values: Mapping[str, object]) -> RenderedPrompt:
    """Fill `prompt` in from `values`, refusing a render that would leave a hole.

    The declared variables are checked first and by name, so the failure says which one is missing
    rather than pointing at the line of a template that tripped over it. `StrictUndefined` then
    catches anything a template uses that was never declared, which is the other half: the front
    matter and the body cannot drift apart without one of the two raising.
    """
    missing = [name for name in prompt.variables if name not in values]
    if missing:
        raise generative_error(PROMPT_VARIABLE_MISSING, name=prompt.name, missing=", ".join(missing))
    try:
        system = _ENVIRONMENT.from_string(prompt.system).render(**values)
        user = _ENVIRONMENT.from_string(prompt.user).render(**values)
    except TemplateError as exc:
        raise generative_error(PROMPT_VARIABLE_MISSING, name=prompt.name, missing=str(exc)) from exc
    return RenderedPrompt(
        name=prompt.name,
        version=prompt.version,
        content_hash=prompt.content_hash,
        system=system.strip(),
        user=user.strip(),
    )


def prompt_versions(names: Sequence[str], root: Path | None = None) -> dict[str, int]:
    """Name -> version for each of `names`, as an artefact records them."""
    return {name: load_prompt(name, root).version for name in sorted(set(names))}


def prompt_hashes(names: Sequence[str], root: Path | None = None) -> dict[str, str]:
    """Name -> content hash for each of `names`, as an artefact records them."""
    return {name: load_prompt(name, root).content_hash for name in sorted(set(names))}

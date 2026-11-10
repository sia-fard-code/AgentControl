"""Stdlib-only skills runtime: discovery, catalog, explicit load, read-only resource grants.

Skills live in ``<dir>/*/SKILL.md`` under ``<root>/.claude/skills``, ``<home>/.claude/skills``
and operator-approved extra directories. Discovery reads only the bounded frontmatter header;
bodies are read only by :meth:`SkillRegistry.load`. Loaded skill folders become *read-only*
grants addressed as ``skill://<name>/<relative-file>``; no write/search/command authority.
"""
from __future__ import annotations

import os
import pathlib
import re

NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
KEY_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_-]*)[ \t]*:(?:[ \t]+(.*)|)\Z")
SKILL_SCHEME = "skill://"
SKILL_FILE = "SKILL.md"

MAX_HEADER_BYTES = 16 * 1024      # frontmatter header bound (discovery)
MAX_BODY_CHARS = 100_000          # whole SKILL.md bound at load
MAX_DESCRIPTION_CHARS = 1024
MAX_SKILLS = 200
MAX_SKILLS_DIRS = 16
MAX_PATH_CHARS = 1024
MAX_REF_CHARS = 1024
MAX_WARNINGS = 20                 # skipped-entry warnings retained per registry


# ----------------------------------------------------------------- config validation
def validate_skills_dirs(value) -> list:
    """Validate operator-approved extra skill roots. None -> []; else list of absolute paths."""
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise TypeError("skills_dirs must be a list of path strings")
    if len(value) > MAX_SKILLS_DIRS:
        raise ValueError("skills_dirs: too many entries (max %d)" % MAX_SKILLS_DIRS)
    out = []
    for item in value:
        if not isinstance(item, str):
            raise TypeError("skills_dirs entries must be strings")
        p = item.strip()
        if not p or "\x00" in p or len(p) > MAX_PATH_CHARS:
            raise ValueError("skills_dirs: invalid path %r" % item[:80])
        if p.startswith("~"):
            if not (p == "~" or p[1] in "/\\"):
                raise ValueError("skills_dirs: only ~/ home expansion is permitted: %r" % p)
            p = str(pathlib.Path.home()) + p[1:]
        if p.startswith(("\\\\", "//")):
            raise ValueError("skills_dirs: UNC/network paths are not permitted: %r" % p)
        if not os.path.isabs(p) or (os.name == "nt" and not os.path.splitdrive(p)[0]):
            raise ValueError("skills_dirs: path must be absolute: %r" % p)
        p = os.path.normpath(p)
        if p not in out:
            out.append(p)
    return out


# ----------------------------------------------------------------- header / frontmatter
def _read_header(path: str) -> str:
    """Read only the frontmatter block (bounded); never the body."""
    lines, total = [], 0
    with open(path, "rb") as fh:
        first = fh.readline(MAX_HEADER_BYTES + 1)
        if first.startswith(b"\xef\xbb\xbf"):
            first = first[3:]
        if first.strip() != b"---":
            raise ValueError("%s: missing frontmatter opening '---'" % path)
        total = len(first)
        while True:
            line = fh.readline(MAX_HEADER_BYTES + 1)
            total += len(line)
            if total > MAX_HEADER_BYTES:
                raise ValueError("%s: frontmatter header exceeds %d bytes" % (path, MAX_HEADER_BYTES))
            if not line:
                raise ValueError("%s: unclosed frontmatter (no closing '---')" % path)
            if line.rstrip(b"\r\n").rstrip() == b"---":
                break
            lines.append(line)
    try:
        return b"".join(lines).decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("%s: frontmatter is not valid UTF-8" % path) from None


def _split_body(text: str, path: str):
    """Split full SKILL.md text into (header, body)."""
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ValueError("%s: missing frontmatter opening '---'" % path)
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n").rstrip() == "---":
            return "".join(lines[1:i]), "".join(lines[i + 1:])
    raise ValueError("%s: unclosed frontmatter (no closing '---')" % path)


_DQ_RE = re.compile(r'"((?:[^"\\]|\\.)*)"[ \t]*(?:#.*)?\Z')
_SQ_RE = re.compile(r"'((?:[^']|'')*)'[ \t]*(?:#.*)?\Z")
_BLOCK_RE = re.compile(r"[|>](?:[+-]?[1-9]?|[1-9][+-])[ \t]*(?:#.*)?\Z")
_DQ_ESC = {'"': '"', "\\": "\\", "/": "/", "n": "\n", "t": "\t", "r": "\r", " ": " ", "0": ""}


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _scalar(raw: str, cont: list, key: str, path: str):
    """Interpret one top-level value (narrow YAML subset). Returns str, or None for nested blocks."""
    nonblank = [c.strip() for c in cont if c.strip()]
    if not raw or raw.startswith("#"):
        return None if nonblank else ""          # nested mapping/list (ignored) or empty
    if raw[0] in "|>":
        if not _BLOCK_RE.match(raw):
            raise ValueError("%s: malformed block scalar indicator for %r" % (path, key))
        return _one_line(" ".join(nonblank))
    if raw[0] == '"':
        m = _DQ_RE.match(" ".join([raw] + nonblank))
        if not m:
            raise ValueError("%s: malformed double-quoted value for %r" % (path, key))
        out, s, i = [], m.group(1), 0
        while i < len(s):
            ch = s[i]
            if ch == "\\":
                esc = s[i + 1]
                if esc not in _DQ_ESC:
                    raise ValueError("%s: unsupported escape \\%s in %r" % (path, esc, key))
                out.append(_DQ_ESC[esc])
                i += 2
                continue
            out.append(ch)
            i += 1
        return "".join(out)
    if raw[0] == "'":
        m = _SQ_RE.match(" ".join([raw] + nonblank))
        if not m:
            raise ValueError("%s: malformed single-quoted value for %r" % (path, key))
        return m.group(1).replace("''", "'")
    if raw[0] in "[{&*!%@`":
        if key in ("name", "description"):
            raise ValueError("%s: unsupported YAML construct for %r" % (path, key))
        return None
    parts = [re.split(r"[ \t]+#", raw, maxsplit=1)[0]]
    parts += [re.split(r"(?:^|[ \t]+)#", c, maxsplit=1)[0] for c in nonblank]
    return " ".join(p.strip() for p in parts if p.strip())


def _parse_frontmatter(header: str, path: str) -> dict:
    """Parse required ``name``/``description`` from a frontmatter header string."""
    lines = header.splitlines()
    fields, i, n = {}, 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip() or line.startswith("#"):
            i += 1
            continue
        if line[0] in " \t":
            raise ValueError("%s: unexpected indentation in frontmatter line %d" % (path, i + 2))
        m = KEY_RE.match(line.rstrip())
        if not m:
            raise ValueError("%s: malformed frontmatter line %d" % (path, i + 2))
        key, raw = m.group(1), (m.group(2) or "").strip()
        if key in fields:
            raise ValueError("%s: duplicate frontmatter field %r" % (path, key))
        i += 1
        cont = []
        while i < n and (not lines[i].strip() or lines[i][0] in " \t"):
            cont.append(lines[i])
            i += 1
        fields[key] = _scalar(raw, cont, key, path)
    name, desc = fields.get("name"), fields.get("description")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ValueError("%s: missing or invalid skill name %r" % (path, str(name)[:100]))
    if not isinstance(desc, str) or not _one_line(desc):
        raise ValueError("%s: missing or empty description" % path)
    desc = _one_line(desc)
    if len(desc) > MAX_DESCRIPTION_CHARS:
        raise ValueError("%s: description exceeds %d chars" % (path, MAX_DESCRIPTION_CHARS))
    return {"name": name, "description": desc}


# ----------------------------------------------------------------- path helpers
def _within(path: str, base: str) -> bool:
    """True if canonical ``path`` equals or lies inside canonical ``base``."""
    try:
        p, b = os.path.normcase(path), os.path.normcase(base)
        return os.path.commonpath([p, b]) == b
    except ValueError:
        return False


def _read_body(path: str) -> str:
    """Bounded full read of a SKILL.md (UTF-8, BOM tolerant). Only called by load()."""
    try:
        with open(path, "r", encoding="utf-8-sig", newline=None) as fh:
            text = fh.read(MAX_BODY_CHARS + 1)
    except UnicodeDecodeError:
        raise ValueError("%s: SKILL.md is not valid UTF-8" % path) from None
    if len(text) > MAX_BODY_CHARS:
        raise ValueError("%s: SKILL.md exceeds %d chars (too large to load)" % (path, MAX_BODY_CHARS))
    return text


def _identity(path: str):
    st = os.stat(path)
    return (st.st_dev, st.st_ino)


# ----------------------------------------------------------------- registry
class SkillRegistry:
    """Discovered skill catalog with explicit load and read-only ``skill://`` resource grants."""

    def __init__(self, root, skills_dirs=(), home=None, strict_duplicates=True):
        home = pathlib.Path.home() if home is None else home
        extras = validate_skills_dirs(list(skills_dirs) if skills_dirs else None)
        bases = [os.path.join(str(root), ".claude", "skills"),
                 os.path.join(str(home), ".claude", "skills")] + extras
        self._skills = {}     # name -> metadata record (no file contents)
        self._loaded = []
        self._strict_duplicates = strict_duplicates
        self._ambiguous = {}
        self._folders = {}    # normcase(canonical folder) -> skill name (alias dedup)
        self.warnings = []    # bounded "skipped <path>: <reason>" lines; never bodies
        seen_bases = []
        for base in bases:
            if not os.path.isdir(base):
                continue                      # missing roots: silently no skills
            base_real = os.path.realpath(base)
            if any(os.path.normcase(base_real) == os.path.normcase(s) for s in seen_bases):
                continue
            seen_bases.append(base_real)
        # Every configured root (repo, home, explicit policy skills_dirs) is an
        # authorized canonical location; a link into one of them is allowed.
        self._authorized = list(seen_bases)
        for base_real in seen_bases:
            for entry in sorted(os.listdir(base_real)):
                self._discover_one(base_real, entry)

    def _skip(self, path: str, reason: str) -> None:
        """Record a bounded warning for an untrusted entry that was not granted."""
        if len(self.warnings) < MAX_WARNINGS:
            self.warnings.append("skipped %s: %s" % (str(path)[:300], reason))
        elif len(self.warnings) == MAX_WARNINGS:
            self.warnings.append("further skipped skill entries not listed")

    def _discover_one(self, base_real: str, entry: str) -> None:
        lex = os.path.join(base_real, entry)
        md_lex = os.path.join(lex, SKILL_FILE)
        if not os.path.isdir(lex) or not os.path.lexists(md_lex):
            return
        if len(md_lex) > MAX_PATH_CHARS:
            raise ValueError("skill path too long: %s" % md_lex[:200])
        canon = os.path.realpath(lex)
        home_base = next((b for b in self._authorized if _within(canon, b)
                          and os.path.normcase(canon) != os.path.normcase(b)), None)
        if home_base is None:
            # Untrusted link/reparse target outside every authorized root: never
            # read it, never grant it, but do not abort unrelated valid skills.
            self._skip(lex, "symlink/reparse escapes the authorized skills roots")
            return
        key = os.path.normcase(canon)
        if key in self._folders:
            return                            # same physical skill via an alias
        md_real = os.path.realpath(md_lex)
        if not _within(md_real, canon):
            self._skip(md_lex, "symlinked SKILL.md points outside its skill folder (escape)")
            return
        if not os.path.isfile(md_real):
            raise ValueError("%s: SKILL.md is not a regular file" % md_lex)
        meta = _parse_frontmatter(_read_header(md_real), md_lex)
        name = meta["name"]
        if name in self._ambiguous:
            return                            # no later folder can choose a winner
        if name in self._skills:
            error = "duplicate skill name %r: %s and %s" % (
                name, self._skills[name]["skill_md"], md_lex)
            if self._strict_duplicates:
                raise ValueError(error)
            self._ambiguous[name] = error
            del self._skills[name]             # refuse BOTH, never prefer a source
            self._skip(md_lex, error)
            return
        self._folders[key] = name
        if len(self._skills) >= MAX_SKILLS:
            raise ValueError("too many skills discovered (max %d)" % MAX_SKILLS)
        self._skills[name] = {
            "name": name, "description": meta["description"], "lexical": lex,
            "folder": canon, "skill_md": md_lex, "identity": _identity(canon),
        }

    # -- public API ---------------------------------------------------------
    def catalog(self) -> list:
        """Sorted ``[{'name', 'description'}]``; never contains bodies."""
        return [{"name": n, "description": self._skills[n]["description"]}
                for n in sorted(self._skills)]

    @property
    def loaded(self) -> list:
        return list(self._loaded)

    def load(self, name, max_chars=None) -> str:
        """Return SKILL.md instructions (frontmatter stripped) and grant read-only folder access.

        ``max_chars`` caps the returned text: an over-cap skill is refused
        explicitly (and NOT marked loaded) rather than silently truncated."""
        if isinstance(name, str) and name in self._ambiguous:
            raise ValueError("refused: " + self._ambiguous[name])
        if not isinstance(name, str) or name not in self._skills:
            raise ValueError("refused: unknown skill %r" % (str(name)[:100],))
        rec = self._skills[name]
        if name in self._loaded:
            return ("[skill %s] already loaded; its instructions appear earlier in this "
                    "conversation. Read its files with read_file skill://%s/<relative-path>."
                    % (name, name))
        self._verify(rec)
        md_real = os.path.realpath(rec["skill_md"])
        if not _within(md_real, rec["folder"]):
            raise ValueError("refused: %s SKILL.md now escapes its folder" % name)
        _, body = _split_body(_read_body(md_real), rec["skill_md"])
        self._verify(rec)
        files = self._listing(rec["folder"])
        note = ("[skill %s loaded] Its folder is granted READ-ONLY. Read bundled files with "
                "read_file using skill://%s/<relative-path> (normal read_file range limits apply)."
                % (name, name))
        if files:
            note += "\nFiles: " + ", ".join("skill://%s/%s" % (name, f) for f in files)
        text = note + "\n\n" + body.strip("\n") + "\n"
        if max_chars is not None and len(text) > max_chars:
            raise ValueError(
                "refused: skill %s instructions (%d chars) exceed the %d-char load cap; "
                "they were not loaded (nothing truncated). Split the skill so SKILL.md is "
                "shorter and move detail into bundled files read with read_file line ranges."
                % (name, len(text), max_chars))
        self._loaded.append(name)
        return text

    def resolve_read(self, path) -> str:
        """Map ``skill://<loaded-name>/<relative-file>`` to a canonical absolute file path."""
        if not isinstance(path, str) or len(path) > MAX_REF_CHARS or not path.startswith(SKILL_SCHEME):
            raise ValueError("refused: not a skill:// reference")
        rest = path[len(SKILL_SCHEME):]
        name, sep, rel = rest.partition("/")
        if not name or not sep or not rel:
            raise ValueError("refused: malformed skill reference %r" % path[:200])
        if name not in self._skills or name not in self._loaded:
            raise ValueError("refused: skill %r is unknown or not loaded" % name[:100])
        if "\\" in rel or "\x00" in rel or ":" in rel or rel.startswith("/"):
            raise ValueError("refused: absolute/drive/UNC or invalid characters in %r" % rel[:200])
        parts = rel.split("/")
        for seg in parts:
            if seg in ("", ".", "..") or seg != seg.strip() or seg.endswith("."):
                raise ValueError("refused: invalid path segment in %r" % rel[:200])
            if seg.split(".")[0].upper() in _RESERVED:
                raise ValueError("refused: reserved device name in %r" % rel[:200])
        rec = self._skills[name]
        self._verify(rec)
        target = os.path.realpath(os.path.join(rec["folder"], *parts))
        if not _within(target, rec["folder"]) or os.path.normcase(target) == os.path.normcase(rec["folder"]):
            raise ValueError("refused: %r escapes the skill folder (symlink/reparse)" % rel[:200])
        if not os.path.isfile(target):
            raise ValueError("refused: %r is not an existing file" % rel[:200])
        return target

    # -- internals ------------------------------------------------------------
    def _verify(self, rec) -> None:
        """Detect folder substitution / symlink retargeting since discovery."""
        try:
            canon = os.path.realpath(rec["lexical"])
            ident = _identity(canon)
        except OSError:
            raise ValueError("refused: skill %s folder changed or vanished" % rec["name"]) from None
        if os.path.normcase(canon) != os.path.normcase(rec["folder"]) or ident != rec["identity"]:
            raise ValueError("refused: skill %s folder changed (substitution detected)" % rec["name"])

    @staticmethod
    def _listing(folder: str, limit: int = 50) -> list:
        """Bounded relative listing of regular, non-symlink files (names only, no contents)."""
        out = []
        for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if not os.path.islink(os.path.join(dirpath, d)))
            for f in sorted(filenames):
                full = os.path.join(dirpath, f)
                if f == SKILL_FILE and dirpath == folder or os.path.islink(full):
                    continue
                out.append(os.path.relpath(full, folder).replace(os.sep, "/"))
                if len(out) >= limit:
                    return out
        return out


_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {"COM%d" % i for i in range(10)} | {"LPT%d" % i for i in range(10)}
